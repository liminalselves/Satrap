"""
一次性 Python worker 的宿主进程管理

为每次调用建立私有临时环境, 传递单次 JSON 请求并校验完整结果
同步与异步共用超时和真实进程回收逻辑, 不在宿主执行业务源码
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import cast

from .models import MAX_MESSAGE_BYTES, Request, SandboxResult, failure_result
from ..proc_env import sanitized_child_env


_SENSITIVE = re.compile(r"(?:^|_)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)S?$", re.IGNORECASE)
_STARTUP = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT", "PYTHONBREAKPOINT",
            "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH"}


def validate_environment(allowed: Iterable[str]) -> None:
    """
    拒绝免审批执行器显式放行敏感或启动控制变量

    参数:
    - allowed: 管理员声明的兼容环境变量名集合

    发现禁止项时抛出 ValueError, 正常非敏感环境仍由公共清理函数继承
    """
    for name in allowed:
        if _SENSITIVE.search(name) or name.upper() in _STARTUP or name.upper().startswith("DYLD_"):
            raise ValueError(f"沙箱不允许放行环境变量: {name}")


def run(request: Request, executable: str, timeout: float, allowed_env: Iterable[str],
        cancelled: threading.Event | None = None) -> SandboxResult:
    """
    同步启动 worker 并在返回前终止和等待实际进程

    参数:
    - request: 冻结的单次业务请求
    - executable: 可信 Python 解释器的绝对路径
    - timeout: 包含启动和请求发送的总秒数预算
    - allowed_env: 待验证的兼容环境变量名集合
    - cancelled: 异步调用传入的取消事件, 默认 None 表示仅使用超时

    返回:
    - 通过协议校验的结果或配置, 超时, 取消, 输出和启动失败结果
    """
    started = time.monotonic()
    try:
        # Step.1 校验配置并限制序列化请求大小
        validate_environment(allowed_env)
        payload = json.dumps(asdict(request), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            return failure_result("output_limit_exceeded", "沙箱请求超过 8 MiB")
        worker = Path(__file__).with_name("worker.py")
        # Step.2 为环境, 缓存和输出建立本次调用独占的资源
        with tempfile.TemporaryDirectory(prefix="satrap-worker-") as private, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostics:
            env = sanitized_child_env()
            env = {key: value for key, value in env.items() if key.upper() not in _STARTUP and not key.upper().startswith("DYLD_")}
            env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "HOME": private, "USERPROFILE": private,
                        "TEMP": private, "TMP": private, "TMPDIR": private, "MPLCONFIGDIR": private,
                        "MPLBACKEND": "Agg", "XDG_CACHE_HOME": private})
            # Step.3 使用既定解释器和可信入口, 不经过 Shell 或工作区启动模块
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen([executable, "-I", "-S", "-B", "-X", "utf8", str(worker)],
                                       stdin=subprocess.PIPE, stdout=output, stderr=diagnostics, env=env,
                                       close_fds=True, creationflags=creationflags)
            sender: threading.Thread | None = None
            try:
                stdin = process.stdin
                assert stdin is not None

                def send() -> None:
                    """
                    发送一个请求后关闭 stdin, 让 worker 的有界读取遇到 EOF

                    worker 提前退出造成的管道错误由外层进程状态和结果协议处理
                    """
                    try:
                        stdin.write(payload)
                        stdin.close()
                    except OSError:
                        pass
                    # 进程提前退出导致的管道失败由外层状态和完整结果校验统一判定

                # Step.4 单独发送请求, 同时监视超时, 取消和协议输出大小
                sender = threading.Thread(target=send, name="sandbox-request", daemon=True)
                sender.start()
                while process.poll() is None:
                    if cancelled is not None and cancelled.is_set():
                        return failure_result("cancelled", "沙箱执行已取消", returncode=-5)
                    if time.monotonic() - started >= timeout:
                        return failure_result("timeout", f"沙箱执行超时: 超过 {timeout} 秒", returncode=-4)
                    if os.fstat(output.fileno()).st_size > MAX_MESSAGE_BYTES or os.fstat(diagnostics.fileno()).st_size > MAX_MESSAGE_BYTES:
                        return failure_result("output_limit_exceeded", "沙箱协议输出超过 8 MiB")
                    time.sleep(0.01)
            finally:
                # Step.5 所有退出分支均等待实际进程结束, 再释放请求管道
                if process.poll() is None:
                    process.kill()
                process.wait()
                if sender is not None:
                    sender.join(timeout=1)
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
            # Step.6 严格解析完整结果, 不从混杂输出中截取成功片段
            output.seek(0)
            raw = output.read(MAX_MESSAGE_BYTES + 1)
            if len(raw) > MAX_MESSAGE_BYTES:
                return failure_result("output_limit_exceeded", "沙箱结果超过 8 MiB")
            try:
                result = _parse_result(json.loads(raw.decode("utf-8")))
            except (ValueError, UnicodeError, TypeError):
                diagnostics.seek(0)
                detail = diagnostics.read(20_000).decode("utf-8", errors="replace")
                return failure_result("worker_failed", "沙箱进程或结果协议异常" + (": " + detail if detail else ""))
            if process.returncode != 0:
                return failure_result("worker_failed", "沙箱进程异常退出")
            result["elapsed_seconds"] = round(time.monotonic() - started, 4)
            return result
    except FileNotFoundError:
        return failure_result("sandbox_unavailable", "Python 解释器不存在", returncode=-1)
    except ValueError as error:
        return failure_result("configuration_error", str(error))
    except OSError as error:
        return failure_result("worker_failed", f"无法运行沙箱 worker: {error}")


def _parse_result(value: object) -> SandboxResult:
    """
    验证完整 JSON 结果的字段集合和类型

    参数:
    - value: worker stdout 解码后的对象, 必须包含整数 returncode

    返回:
    - 验证后的 SandboxResult; 未知字段或类型错误抛出 ValueError
    """
    if not isinstance(value, dict) or type(value.get("returncode")) is not int:
        raise ValueError("沙箱结果无效")
    result: dict[str, object] = {}
    text_fields = {"operation", "path", "stdout", "stderr", "content", "status", "error", "error_code", "action", "module"}
    integer_fields = {"returncode", "line", "worker_pid"}
    boolean_fields = {"stdout_truncated", "stderr_truncated"}
    for key, item in value.items():
        valid = ((key in text_fields and isinstance(item, str)) or (key in integer_fields and type(item) is int)
                 or (key in boolean_fields and type(item) is bool)
                 or (key in {"setup_seconds", "execution_seconds"} and type(item) is float and item >= 0)
                 or (key == "files" and isinstance(item, list) and all(isinstance(entry, str) for entry in item)))
        if not valid:
            raise ValueError("沙箱结果字段无效")
        result[key] = item
    return cast(SandboxResult, result)


async def run_async(request: Request, executable: str, timeout: float, allowed_env: Iterable[str]) -> SandboxResult:
    """
    异步等待同步执行器, 取消时通知并等待真实进程回收

    参数:
    - request: 冻结的单次业务请求
    - executable: 可信 Python 解释器的绝对路径
    - timeout: 包含启动和请求发送的总秒数预算
    - allowed_env: 待验证的兼容环境变量名集合

    返回:
    - 通过协议校验的结果或结构化失败; 回收完成后重新抛出 CancelledError
    """
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(run, request, executable, timeout, allowed_env, cancelled))
    # shield 保留后台执行器的生命周期, 协程取消后仍能等待其回收真实进程
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        await asyncio.shield(task)
        raise
