from __future__ import annotations
import subprocess
from pathlib import Path
import shutil
import os
from satrap.expend.plugins.satrap_coding.core.command_gate import classify_command
from satrap.expend.plugins.satrap_coding.core.permission import RiskLevel


def _resolve_shell_executable(shell: str) -> str:
    """
    解析 shell 可执行文件: PATH 优先, 回落系统目录绝对路径

    参数:
    - shell: Shell 类型

    System32 不在 PATH 的环境 (部分 Git Bash / 服务进程) 下裸文件名会 WinError 2

    返回:
    - str: 解析 shell 可执行文件: PATH 优先, 回落系统目录绝对路径
    """
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if shell.lower() == "powershell":
        found = shutil.which("powershell.exe")
        return found or str(
            system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        )
    found = shutil.which("cmd.exe")
    return found or str(system_root / "System32" / "cmd.exe")


def _prepare_shell(
    command: str, shell: str, root: Path, workdir: Path
) -> tuple[list[str], RiskLevel, str]:
    """固定执行参数和审批说明; 所有解释器输入均需逐次授权"""
    if shell not in ("powershell", "cmd"):
        raise ValueError("shell 必须为 powershell 或 cmd")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command 必须为非空字符串")
    risk, _ = classify_command(command)
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
            prefix + command,
        ]
    else:
        args = [executable, "/D", "/S", "/C", "chcp 65001 > nul & " + command]
    description = (
        f"本机执行 (可访问当前进程资源): {command}\n"
        f"Shell: {shell} ({executable})\n工作区: {root}\n工作目录: {workdir}"
    )
    return args, max(risk, RiskLevel.HIGH), description


def _run_shell(args: list[str], workdir: Path, timeout: int) -> str:
    """以 UTF-8 执行已经批准的固定参数, 同步和异步共用"""
    try:
        result = subprocess.run(
            args,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        output = (result.stdout or "")[-20000:]
        error = (result.stderr or "")[-20000:]
        if result.returncode == 0:
            return output or "(无输出)"
        return f"退出码 {result.returncode}:\n{error or output}"
    except subprocess.TimeoutExpired:
        return f"执行超时 ({timeout}s)"
    except OSError as error:
        return f"错误: 执行失败: {error}"
