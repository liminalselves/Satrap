"""编程工具的固定 Shell 参数, 授权说明与子进程执行"""
from __future__ import annotations
import subprocess
from pathlib import Path
import shutil
import os
import signal
import sys
import threading
import time
from typing import IO

from satrap.expend.plugins.satrap_coding.core.command_gate import classify_command
from satrap.core.approval.policy import RiskLevel
from satrap.core.utils.proc_env import sanitized_child_env


def _resolve_shell_executable(shell: str) -> str:
    """
    按平台解析允许的 Shell 绝对路径, 解释器不存在时明确拒绝

    参数:
    - shell: Shell 类型

    System32 不在 PATH 的环境 (部分 Git Bash / 服务进程) 下裸文件名会 WinError 2

    返回:
    - 可执行文件绝对路径; 系统组合不支持或解释器缺失时抛出 ValueError
    """
    if shell in {"bash", "sh"}:
        if os.name == "nt":
            raise ValueError("Windows 请使用 powershell 或 cmd; bash/sh 需要 POSIX 运行环境")
        found = shutil.which(shell)
        if found is None:
            raise ValueError(f"未找到 Shell 解释器: {shell}")
        return str(Path(found).absolute())
    if shell not in {"powershell", "cmd"} or os.name != "nt":
        raise ValueError("powershell/cmd 仅支持 Windows, POSIX 请使用 bash 或 sh")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    name = "powershell.exe" if shell == "powershell" else "cmd.exe"
    fallback = system_root / "System32"
    if shell == "powershell":
        fallback = fallback / "WindowsPowerShell" / "v1.0"
    executable = Path(shutil.which(name) or fallback / name).absolute()
    if not executable.is_file():
        raise ValueError(f"未找到 Shell 解释器: {shell}")
    return str(executable)


def _prepare_shell(
    command: str, shell: str, root: Path, workdir: Path
) -> tuple[list[str], RiskLevel, str]:
    """
    固定命令参数与审查材料, 由工具按三档模式决定是否审批

    参数:
    - command: 用户要求执行的完整命令
    - shell: auto, powershell, cmd, bash 或 sh, auto 按当前系统选择
    - root: 当前工具工作区
    - workdir: 已解析的实际工作目录

    返回:
    - 固定 argv, 风险提示和完整说明; 无效参数或命中硬黑名单时抛出 ValueError
    """
    if shell == "auto":
        shell = "powershell" if os.name == "nt" else ("bash" if shutil.which("bash") else "sh")
    if shell not in {"powershell", "cmd", "bash", "sh"}:
        raise ValueError("shell 必须为 auto、powershell、cmd、bash 或 sh")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command 必须为非空字符串")
    risk, _ = classify_command(command, posix=shell in {"bash", "sh"})
    if risk == RiskLevel.FORBIDDEN:
        raise ValueError(f"命令被拒绝 (黑名单): {command}")
    executable = _resolve_shell_executable(shell)
    if shell == "powershell":
        prefix = (
            "[Console]::InputEncoding=[System.Text.UTF8Encoding]::new($false); "
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
            "$OutputEncoding=[Console]::OutputEncoding; "
            "$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; chcp 65001 > $null;\n"
        )
        args = [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            prefix + command + "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1 }",
        ]
    elif shell == "cmd":
        args = [executable, "/D", "/S", "/C", f'chcp 65001 > nul & "{executable}" /D /S /C "{command}"']
        # cmd 在解析复合行时缓存代码页, 先设置 UTF-8 再启动实际命令解释器
    elif shell == "bash":
        args = [executable, "--noprofile", "--norc", "-c", command]
    else:
        args = [executable, "-c", command]
    description = (
        f"本机执行 (可访问当前进程资源): {command}\n"
        f"Shell: {shell} ({executable})\n工作区: {root}\n工作目录: {workdir}"
    )
    return args, max(risk, RiskLevel.HIGH), description


_OUTPUT_BYTES = 80_000
_OUTPUT_CHARS = 20_000


class _OutputTail:
    """持续排空输出管道, 每路只保留固定大小的尾部"""

    def __init__(self, stream: IO[bytes]) -> None:
        """
        初始化单路输出读取器

        参数:
        - stream: 子进程 stdout 或 stderr 的二进制管道
        """
        self.stream = stream
        self.buffer = bytearray()
        self.truncated = False
        self.thread = threading.Thread(target=self._drain, name="coding-shell-output", daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        """持续读取有界块, 避免进程管道堵塞或完整输出占满内存"""
        while block := self.stream.read(8192):
            self.buffer.extend(block)
            if len(self.buffer) > _OUTPUT_BYTES:
                del self.buffer[:-_OUTPUT_BYTES]
                self.truncated = True

    def finish(self) -> str:
        """
        等待管道排空并解码固定大小的输出尾部

        返回:
        - UTF-8 输出及必要的截断提示
        """
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            return "(输出管道未关闭, 后代进程可能保留了管道)"
        self.stream.close()
        text = self.buffer.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        truncated = self.truncated or len(text) > _OUTPUT_CHARS
        return ("(输出已截断, 仅保留末尾)\n" if truncated else "") + text[-_OUTPUT_CHARS:]


def _stop_shell(process: subprocess.Popen[bytes]) -> None:
    """
    回收执行进程, POSIX 同时回收本次执行的整个进程组

    参数:
    - process: 本次执行持有的子进程
    """
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


def _run_shell(args: list[str], workdir: Path, timeout: int, allow: frozenset[str] = frozenset(),
               *, cancelled: threading.Event | None = None) -> str:
    """
    以 UTF-8 执行已经批准的固定参数, 同步和异步工具共用

    参数:
    - args: 已批准的命令参数
    - workdir: 执行目录
    - timeout: 超时秒数
    - allow: 当前工具实例显式放行的环境变量名
    - cancelled: 异步取消通知, 默认 None 表示只按超时回收

    返回:
    - str: 有界输出或执行错误说明
    """
    try:
        if cancelled is not None and cancelled.is_set():
            return "执行已取消"
        env = sanitized_child_env(allow=allow, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        env = {key: value for key, value in env.items()
               if key not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"} and not key.startswith("BASH_FUNC_")}
        if os.name != "nt":
            locale = "C.UTF-8" if sys.platform.startswith("linux") else "en_US.UTF-8"
            env.update(LANG=locale, LC_ALL=locale)
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        launch: list[str] | str = args
        if os.name == "nt" and Path(args[0]).name.lower() == "cmd.exe":
            launch = subprocess.list2cmdline(args[:-1]) + " " + args[-1]
            # /C 后是 cmd 原生语法, 不能再用 CRT 规则把引号转成反斜杠引号
        process: subprocess.Popen[bytes] = subprocess.Popen(
            launch,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False,
            env=env, close_fds=True, start_new_session=os.name != "nt",
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
            # cmd 的 chcp 需要真实控制台, 创建隐藏控制台以隔离代码页并保持 UTF-8
        )
        assert process.stdout is not None and process.stderr is not None
        output_reader: _OutputTail | None = None
        error_reader: _OutputTail | None = None
        reason = ""
        finished = False
        try:
            output_reader = _OutputTail(process.stdout)
            error_reader = _OutputTail(process.stderr)
            deadline = time.monotonic() + timeout
            while process.poll() is None or output_reader.thread.is_alive() or error_reader.thread.is_alive():
                if cancelled is not None and cancelled.is_set():
                    reason = "执行已取消"
                    break
                if time.monotonic() >= deadline:
                    reason = f"执行超时 ({timeout}s)"
                    break
                time.sleep(0.02)
            finished = True
        finally:
            if not finished or reason:
                _stop_shell(process)
            else:
                process.wait()
            output = output_reader.finish() if output_reader is not None else ""
            error = error_reader.finish() if error_reader is not None else ""
            if output_reader is None:
                process.stdout.close()
            if error_reader is None:
                process.stderr.close()
        if reason:
            return reason
        if process.returncode == 0:
            return output or "(无输出)"
        return f"退出码 {process.returncode}:\n{error or output}"
    except OSError as error:
        return f"错误: 执行失败: {error}"
