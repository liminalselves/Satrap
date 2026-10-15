"""
隔离解释器直接启动的单次请求入口

从可信源码目录加载策略, 安装网络与进程守卫后执行业务操作
捕获有界输出并汇总首次违规, stdout 最终只写一个 UTF-8 JSON 结果
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import sysconfig
import tempfile
import time
import traceback


sys.path.insert(0, str(Path(__file__).resolve().parent))
from models import MAX_MESSAGE_BYTES, MAX_OUTPUT_CHARS, PolicyViolation, Request, SandboxResult, Violations, failure_result
from guards import install
from policy import FilePolicy, PythonPolicy
# -I/-S 下仅加入可信执行器目录, 不导入 Satrap 应用或工作区启动代码


class OutputBuffer(io.TextIOBase):
    """保留有界输出尾部, 不因长 print 耗尽内存"""

    def __init__(self) -> None:
        """初始化输出尾部缓冲和截断标记"""
        self.value = ""
        self.truncated = False

    def write(self, value: str) -> int:
        """
        接收文本并只保留固定长度的输出尾部

        参数:
        - value: 待捕获的本次输出文本

        返回:
        - 接收的原文本字符数; 丢弃旧内容时置截断标记, 不缩短写入计数
        """
        self.truncated |= len(self.value) + len(value) > MAX_OUTPUT_CHARS
        self.value = (self.value + value[-MAX_OUTPUT_CHARS:])[-MAX_OUTPUT_CHARS:]
        return len(value)

    def flush(self) -> None:
        """兼容文本流刷新接口, 内存缓冲无需额外刷新"""
        pass


def operate(request: Request, files: FilePolicy, policy: PythonPolicy, stdout: OutputBuffer) -> SandboxResult:
    """
    在当前 worker 内分派源码执行与业务文件操作

    参数:
    - request: 已经解析的单次请求
    - files: 当前根目录与临时目录的路径策略
    - policy: 当前公开模块与受限执行策略
    - stdout: 接收脚本 print 的有界文本缓冲

    返回:
    - 包含操作及对应业务字段的成功结果; 参数, 文件和策略异常由 main 统一转换
    """
    # Step.1 建立所有操作共用的成功结果字段
    operation = request.operation
    result: SandboxResult = {"operation": operation, "returncode": 0, "status": "success"}
    if request.path is not None:
        result["path"] = request.path
    # Step.2 内联源码直接复用受限执行策略, 不需要业务文件路径
    if operation == "run":
        if request.code is None:
            raise ValueError("run 需要 code")
        policy.execute(request.code, "<sandbox>", stdout)
        return result
    # Step.3 文件操作先解析受控路径, run_file 复用同一源码执行流程
    target = files.path(request.path or "", write=operation in {"save", "delete", "delete_dir"})
    if operation == "run_file":
        with files.original_open(target, "r", encoding="utf-8") as stream:
            source = stream.read(MAX_MESSAGE_BYTES + 1)
        if len(source.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise PolicyViolation("output_limit_exceeded", "源码文件超过 8 MiB")
        policy.execute(source, str(target), stdout)
    elif operation == "save":
        if request.code is None:
            raise ValueError("save 需要 code")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(request.code, encoding="utf-8")
    elif operation == "read":
        with files.original_open(target, "rb") as stream:
            payload = stream.read(MAX_MESSAGE_BYTES + 1)
        if len(payload) > MAX_MESSAGE_BYTES:
            raise PolicyViolation("output_limit_exceeded", "文件内容超过 8 MiB")
        result["content"] = io.StringIO(payload.decode(request.encoding), newline=None).getvalue()
        # 保持旧文本读取的通用换行语义, 二进制产物不转换为附件通道
    elif operation == "list":
        if not target.is_dir():
            raise NotADirectoryError("列出文件子目录不是目录")
        entries: list[str] = []
        total = 0
        for directory, folders, names in os.walk(target, followlinks=False):
            for name in folders:
                files.path(Path(directory) / name)
            folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()
                          and not getattr(Path(directory) / name, "is_junction", lambda: False)()]
            # 目录先做越界校验再排除链接, 不跟随 symlink 或 junction 访问其他业务根
            for name in names:
                path = files.path(Path(directory) / name)
                relative = str(path.relative_to(files.root))
                total += len(relative.encode("utf-8")) + 8
                if total > MAX_MESSAGE_BYTES:
                    raise PolicyViolation("output_limit_exceeded", "文件列表超过 8 MiB")
                entries.append(relative)
        result["files"] = sorted(entries)
    elif operation in {"delete", "delete_dir"}:
        if target == files.root:
            raise PermissionError("禁止删除沙箱根目录")
        if operation == "delete":
            if target.is_dir():
                raise IsADirectoryError("路径是目录, 请使用 delete_dir")
            target.unlink()
        else:
            if not target.is_dir():
                raise NotADirectoryError("删除路径不是目录")
            if request.recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
    return result


def main() -> None:
    """
    初始化单次运行时并把业务输出汇总为一个 JSON 结果

    策略失败优先于业务成功, 普通异常转换为错误结果, 不回退宿主执行
    """
    stdout, stderr = OutputBuffer(), OutputBuffer()
    started = time.monotonic()
    setup_finished: float | None = None
    result: SandboxResult = {}
    violations = Violations()
    request: Request | None = None
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            # Step.1 有界读取单个请求, 等待父进程关闭 stdin 后解析
            payload = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
            if len(payload) > MAX_MESSAGE_BYTES:
                raise PolicyViolation("output_limit_exceeded", "请求超过 8 MiB")
            request = Request.parse(json.loads(payload.decode("utf-8")))
            # Step.2 固定工作目录并从解释器安装位置恢复依赖目录
            root = Path(request.root).resolve()
            root.mkdir(parents=True, exist_ok=True)
            os.chdir(root)
            prefix = Path(sys.prefix)
            executable_dir = Path(sys.executable).parent
            for candidate in (executable_dir, executable_dir.parent):
                if (candidate / "pyvenv.cfg").is_file():
                    prefix = candidate
                    break
            # Python 3.13 及更早版本的 -S 会跳过 venv 前缀初始化, 需显式读取可信 pyvenv.cfg
            variables = {"base": str(prefix), "platbase": str(prefix)}
            paths = {str(Path(sysconfig.get_path(key, vars=variables)).resolve()) for key in ("purelib", "platlib")}
            sys.path.extend(sorted(paths))
            if os.name == "nt" and hasattr(os, "add_dll_directory"):
                library = Path(sys.base_prefix) / "Library" / "bin"
                if library.is_dir():
                    dll_directory = os.add_dll_directory(str(library))
                    # 保留句柄直到进程退出, 避免 Conda 数值库的 DLL 搜索目录提前失效
            # Step.3 在 RestrictedPython 和业务依赖加载之前安装固定守卫
            install(violations)
            try:
                import RestrictedPython
            except ImportError:
                raise PolicyViolation("sandbox_unavailable", "缺少 RestrictedPython, 请安装项目执行依赖") from None
            files = FilePolicy(root, Path(tempfile.gettempdir()), violations)
            policy = PythonPolicy(request.modules, files, violations)
            setup_finished = time.monotonic()
            # Step.4 执行业务, 由本次请求边界统一转换运行失败
            result = operate(request, files, policy, stdout)
        except PolicyViolation as error:
            result = failure_result(error.code, str(error))
            result.update(action=error.action, module=error.module, line=error.line)
        except FileNotFoundError:
            result = failure_result("file_not_found", "文件不存在", returncode=-3)
        except PermissionError as error:
            result = failure_result("permission_denied", str(error))
        except (IsADirectoryError, NotADirectoryError) as error:
            result = failure_result("invalid_path", str(error))
        except SyntaxError as error:
            result = failure_result("code_not_supported", str(error))
        except (ValueError, UnicodeError) as error:
            result = failure_result("configuration_error" if request is None else "execution_failed", str(error))
        except BaseException as error:
            result = failure_result("execution_failed", f"{type(error).__name__}: {error}")
            stderr.write(traceback.format_exc())
        # Step.5 即使模型捕获策略异常, 最终结果仍优先报告首次违规
        if violations.first is not None:
            first = violations.first
            result = failure_result(first.code, str(first))
            result.update(action=first.action, module=first.module, line=first.line)
    # Step.6 合并有界输出与计时, 最后只写一个完整 JSON 结果
    result["worker_pid"] = os.getpid()
    finished = time.monotonic()
    result["setup_seconds"] = round((setup_finished or finished) - started, 4)
    result["execution_seconds"] = round(finished - setup_finished, 4) if setup_finished is not None else 0.0
    result["stdout"] = stdout.value
    result["stderr"] = stderr.value or result.get("stderr", "")
    result["stdout_truncated"] = stdout.truncated
    result["stderr_truncated"] = stderr.truncated
    if request is not None:
        result["operation"] = request.operation
        if request.path is not None:
            result["path"] = request.path
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        encoded = json.dumps(failure_result("output_limit_exceeded", "沙箱结果超过 8 MiB"), ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
