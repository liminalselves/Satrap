from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from satrap.cli.client import DaemonClient, DaemonInfo
from satrap.core.backend.BackendManager import BackendConfig


PROC_KEY = "backend_process"
"""Streamlit session_state 中保存的后端子进程 key"""

NOTIFY_KEY = "backend_notification"
"""Streamlit session_state 中保存的通知 key"""


@dataclass
class BackendControlResult:
    """后端控制操作结果"""

    ok: bool
    message: str
    level: str = "info"


def daemon_info(config: BackendConfig) -> DaemonInfo:
    """从当前配置构建后端连接信息"""
    return DaemonInfo.from_config(config)


def daemon_client(config: BackendConfig, timeout: float = 5) -> DaemonClient:
    """创建当前配置对应的后端客户端"""
    return DaemonClient(daemon=daemon_info(config), timeout=timeout)


def check_backend(config: BackendConfig) -> tuple[dict | None, str | None]:
    """检测后端是否运行"""
    client = daemon_client(config, timeout=2)
    health = client.health()
    if health.get("running", False):
        return health, None
    return None, health.get("error")


def managed_process() -> subprocess.Popen | None:
    """返回当前面板启动的后端进程"""
    return st.session_state.get(PROC_KEY)


def managed_process_alive() -> bool:
    """检查当前面板启动的后端进程是否还在运行"""
    proc = managed_process()
    return proc is not None and proc.poll() is None


def show_notification(message: str, level: str = "info"):
    """通过 toast 和 session_state 展示通知"""
    st.toast(message)
    st.session_state[NOTIFY_KEY] = (level, message)


def render_notification():
    """渲染滞留通知"""
    val = st.session_state.get(NOTIFY_KEY)
    if val is None:
        return
    level, msg = val
    if level == "success":
        st.success(msg)
    elif level == "error":
        st.error(msg)
    elif level == "warning":
        st.warning(msg)
    else:
        st.info(msg)


def err_log_path() -> Path:
    """stderr 捕获文件路径"""
    root = project_root()
    return root / ".satrap" / "_backend_stderr.log"


def project_root() -> Path:
    """返回项目根目录"""
    return Path(__file__).resolve().parent.parent.parent


def clean_err_log():
    """删除 stderr 捕获文件"""
    try:
        err_log_path().unlink(missing_ok=True)
    except Exception:
        pass


def read_err_log() -> str:
    """读取 stderr 捕获文件并删除"""
    path = err_log_path()
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    finally:
        clean_err_log()


def wait_backend_ready(config: BackendConfig, proc: subprocess.Popen, seconds: float = 8) -> bool:
    """等待后端 health 接口就绪"""
    client = daemon_client(config, timeout=1)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.5)
        health = client.health()
        if health.get("running", False):
            return True
        if proc.poll() is not None:
            return False
    return False


def wait_backend_down(config: BackendConfig, seconds: float = 8) -> bool:
    """等待后端停止响应"""
    client = daemon_client(config, timeout=1)
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not client.is_alive():
            return True
        time.sleep(0.5)
    return False


def start_backend(config: BackendConfig) -> BackendControlResult:
    """由管理面板启动后端"""
    client = daemon_client(config, timeout=1)
    info = client.daemon
    if client.is_alive():
        return BackendControlResult(True, f"后端已在运行中 ({info.base_url})", "warning")

    proc = managed_process()
    if proc is not None and proc.poll() is None:
        return BackendControlResult(True, "后端已在运行中", "warning")

    cmd = [
        sys.executable,
        "-m",
        "satrap.main",
        "--api-host",
        info.host,
        "--api-port",
        str(info.port),
        "run",
    ]

    startupinfo = None
    creationflags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW

    clean_err_log()
    err_path = err_log_path()
    err_path.parent.mkdir(parents=True, exist_ok=True)
    err_fh = open(err_path, "wb")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root()),
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        st.session_state[PROC_KEY] = proc
        if wait_backend_ready(config, proc):
            clean_err_log()
            return BackendControlResult(True, "后端已就绪", "success")
        if proc.poll() is not None:
            err_fh.close()
            err_text = read_err_log()
            short_err = err_text.strip().split("\n")[-5:] if err_text else ["(无错误输出)"]
            return BackendControlResult(False, "后端启动失败\n" + "\n".join(short_err), "error")
        return BackendControlResult(True, "后端启动中, 仍在等待就绪...", "info")
    except Exception as e:
        return BackendControlResult(False, f"启动后端失败: {e}", "error")
    finally:
        try:
            err_fh.close()
        except Exception:
            pass


def stop_backend(config: BackendConfig) -> BackendControlResult:
    """停止当前配置对应的后端"""
    client = daemon_client(config, timeout=2)
    proc = managed_process()

    if client.is_alive():
        result = client.shutdown()
        if "error" in result:
            return BackendControlResult(False, f"后端不支持前端关闭: {result['error']}", "error")
        if wait_backend_down(config):
            st.session_state[PROC_KEY] = None
            clean_err_log()
            return BackendControlResult(True, "后端已停止", "success")
        return BackendControlResult(False, "已发送停止请求, 但后端仍在响应", "warning")

    if proc is None or proc.poll() is not None:
        return BackendControlResult(False, "后端未运行", "warning")

    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        st.session_state[PROC_KEY] = None
        clean_err_log()
        return BackendControlResult(True, "后端已停止", "success")
    except Exception as e:
        return BackendControlResult(False, f"停止后端失败: {e}", "error")


def restart_backend(config: BackendConfig) -> BackendControlResult:
    """重启当前配置对应的后端"""
    stopped = stop_backend(config)
    if not stopped.ok and stopped.level != "warning":
        return stopped
    time.sleep(1)
    return start_backend(config)
