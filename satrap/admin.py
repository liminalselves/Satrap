from __future__ import annotations

import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

import streamlit as st
from satrap.admin_utils.state import ensure_state
from satrap.cli.client import DaemonClient, DaemonInfo

st.set_page_config(
    page_title="Satrap 管理面板",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

ensure_state()


def _backend_status() -> tuple[bool, str]:
    """返回后端运行状态和当前 API 地址"""
    daemon = DaemonInfo.from_config(st.session_state.config)
    client = DaemonClient(daemon=daemon, timeout=2)
    health = client.health()
    return health.get("running", False), daemon.base_url


def render():
    """渲染管理面板欢迎页"""
    st.title("Satrap 管理面板")
    st.caption("欢迎回来. 从左侧页面进入具体管理功能.")

    backend_ok, api_url = _backend_status()

    cols = st.columns(4)
    with cols[0]:
        st.metric("后端状态", "运行中" if backend_ok else "未运行")
    with cols[1]:
        st.metric("会话类", len(st.session_state.scm.list_configs()))
    with cols[2]:
        st.metric("LLM 配置", len(st.session_state.mcm.list_llm_configs(mask_api_key=True)))
    with cols[3]:
        st.metric("API 地址", api_url)

    st.divider()

    if backend_ok:
        st.success("后端 API 当前可用.")
    else:
        st.warning("后端未运行. 可进入仪表盘启动后端或查看启动状态.")

    st.subheader("快速入口")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.page_link("pages/01_dashboard.py", label="仪表盘")
        st.page_link("pages/02_model_config.py", label="模型配置")
    with col2:
        st.page_link("pages/03_session_management.py", label="会话管理")
        st.page_link("pages/04_platform_status.py", label="平台状态")
    with col3:
        st.page_link("pages/05_log_monitor.py", label="日志监控")
        st.page_link("pages/06_settings.py", label="系统设置")

    st.divider()
    st.caption("如页面未自动刷新, 请进入仪表盘使用刷新按钮查看最新后端状态.")


render()

# streamlit run satrap/admin.py
