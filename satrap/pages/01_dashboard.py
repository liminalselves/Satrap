from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import streamlit as st
from satrap.pages._state import ensure_state
from satrap.cli.client import DaemonClient

st.set_page_config(page_title="仪表盘", page_icon="", layout="wide")

_PROC_KEY = "backend_process"
_NOTIFY_KEY = "backend_notification"


def _check_backend():
    """检测后端是否在线并返回健康信息"""
    client = DaemonClient()
    alive = client.is_alive()
    if alive:
        try:
            health = client.health()
            return health, None
        except Exception as e:
            return None, str(e)
    return None, None


def _proc() -> subprocess.Popen | None:
    """返回 session_state 中保存的后端进程对象"""
    return st.session_state.get(_PROC_KEY)


def _proc_alive() -> bool:
    """检查本面板启动的子进程是否还在运行"""
    p = _proc()
    return p is not None and p.poll() is None


def _show(toast: str, level: str = "info"):
    """通过 st.toast 和 session_state 双重通知, 确保用户看到"""
    st.toast(toast)
    st.session_state[_NOTIFY_KEY] = (level, toast)


def _show_notification_from_state():
    """在页面顶部渲染 session_state 中滞留的通知"""
    val = st.session_state.get(_NOTIFY_KEY)
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


def _err_log_path() -> Path:
    """stderr 捕获文件的路径"""
    root = str(Path(__file__).resolve().parent.parent.parent)
    return Path(root) / ".satrap" / "_backend_stderr.log"


def _clean_err_log():
    """删除 stderr 捕获文件"""
    try:
        _err_log_path().unlink(missing_ok=True)
    except Exception:
        pass


def _read_err_log() -> str:
    """读取 stderr 捕获文件并删除"""
    p = _err_log_path()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        return text
    except Exception:
        return ""
    finally:
        _clean_err_log()


def _start_backend():
    """启动后端子进程"""
    # 先检查端口是否已被占用 (外部启动或之前启动的进程还在)
    import urllib.request
    health_url = "http://127.0.0.1:19870/api/health"
    try:
        with urllib.request.urlopen(health_url, timeout=1) as resp:
            if resp.status == 200:
                _show("后端已在运行中 (端口 19870 已被占用)", "warning")
                return
    except Exception:
        pass

    p = _proc()
    if p is not None and p.poll() is None:
        _show("后端已在运行中", "warning")
        return

    root = str(Path(__file__).resolve().parent.parent.parent)
    python = sys.executable
    cmd = [python, "-m", "satrap.main", "run"]

    startupinfo = None
    creationflags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW

    # 启动前清理上次残留的 stderr 文件
    _clean_err_log()
    err_path = _err_log_path()
    err_path.parent.mkdir(parents=True, exist_ok=True)
    err_fh = open(err_path, "wb")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        st.session_state[_PROC_KEY] = proc

        # 等待后端就绪: 轮询 health 最多 8 秒
        ready = False
        for _ in range(16):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(health_url, timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                pass
            if proc.poll() is not None:
                break

        if ready:
            _clean_err_log()
            _show("后端已就绪", "success")
        elif proc.poll() is not None:
            err_fh.close()
            err_text = _read_err_log()
            short_err = err_text.strip().split("\n")[-5:] if err_text else ["(无错误输出)"]
            detail = "\n".join(short_err)
            _show(f"后端启动失败\n{detail}", "error")
        else:
            _show("后端启动中, 仍在等待就绪...", "info")
    except Exception as e:
        _show(f"启动后端失败: {e}", "error")
    finally:
        try:
            err_fh.close()
        except Exception:
            pass


def _stop_backend():
    """停止后端子进程"""
    proc = _proc()
    if proc is None or proc.poll() is not None:
        _show("未找到由本面板启动的后端进程", "warning")
        return

    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        st.session_state[_PROC_KEY] = None
        _clean_err_log()
        _show("后端已停止", "success")
    except Exception as e:
        _show(f"停止后端失败: {e}", "error")


def _render_start_stop_buttons(backend_ok: bool):
    """渲染启动/停止/重启按钮"""
    managed = _proc_alive()

    col1, col2, col3 = st.columns([1, 1, 3])

    with col1:
        if managed or backend_ok:
            if st.button("重启后端", type="primary"):
                _stop_backend()
                # 等进程完全退出再启动
                time.sleep(1)
                _start_backend()
        else:
            if st.button("启动后端", type="primary"):
                _start_backend()

    with col2:
        if managed or backend_ok:
            if st.button("停止后端"):
                _stop_backend()

    with col3:
        if managed:
            st.caption("后端由此面板管理")
        elif backend_ok:
            st.caption("后端由外部启动")
        else:
            st.caption("后端未运行")


def render():
    ensure_state()
    st.subheader("仪表盘")

    # 渲染之前滞留的通知
    _show_notification_from_state()

    health_data, err = _check_backend()
    backend_ok = health_data is not None

    cols = st.columns(6)
    with cols[0]:
        if backend_ok:
            st.metric("后端状态", "运行中", delta=None)
        else:
            st.metric("后端状态", "未运行", delta=None)
    with cols[1]:
        mcm = st.session_state.mcm
        llm_count = len(mcm.list_llm_configs(mask_api_key=True))
        emb_count = len(mcm.list_embedding_configs(mask_api_key=True))
        rerank_count = len(mcm.list_rerank_configs(mask_api_key=True))
        st.metric("LLM 配置", llm_count)
    with cols[2]:
        st.metric("Embedding", emb_count)
    with cols[3]:
        st.metric("Rerank", rerank_count)
    with cols[4]:
        scm = st.session_state.scm
        session_count = len(scm.list_configs())
        st.metric("会话类", session_count)
    with cols[5]:
        adapter_count = 0
        if backend_ok:
            adapters = health_data.get("adapters", {})
            adapter_count = len(adapters)
        st.metric("适配器", adapter_count)

    st.divider()

    _render_start_stop_buttons(backend_ok)

    st.divider()

    if backend_ok:
        st.info(f"后端 API 地址可用, 运行中适配器: {adapter_count} 个")
        adapters = health_data.get("adapters", {})
        if adapters:
            ad_data = []
            for aid, info in adapters.items():
                ad_data.append({
                    "id": aid,
                    "status": info.get("status", "?"),
                    "started": "" if info.get("started") else "否",
                })
            st.dataframe(ad_data, use_container_width=True)
    else:
        detail = f"({err})" if err else ""
        st.warning(f"后端未运行, 部分实时状态不可用 {detail}")
        st.caption("运行 `python -m satrap.main run` 启动后端")

    st.divider()
    st.caption(f"页面刷新时间: {time.strftime('%H:%M:%S')}")
    if st.button("刷新"):
        st.rerun()


render()
