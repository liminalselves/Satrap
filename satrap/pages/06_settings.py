from __future__ import annotations

import streamlit as st
from satrap.pages._state import ensure_state
from satrap.cli.client import DaemonClient

st.set_page_config(page_title="系统设置", page_icon="", layout="wide")


def render():
    ensure_state()
    st.subheader("系统设置")

    with st.container(border=True):
        st.write("**后端控制**")
        client = DaemonClient()
        alive = client.is_alive()
        if alive:
            st.success("后端运行中")
            if st.button("重载配置"):
                try:
                    result = client.reload_config()
                    if "error" in result:
                        st.error(f"重载失败: {result['error']}")
                    else:
                        st.success("配置已重载")
                except Exception as e:
                    st.error(f"重载失败: {e}")
        else:
            st.warning("后端未运行")
            st.caption("运行 `python -m satrap.main run` 启动后端")

    with st.container(border=True):
        st.write("**配置文件**")
        config = st.session_state.config
        st.code(f"Model Config Path:      {config.model_config_path or '(默认)'}")
        st.code(f"Session Class Config:   {config.session_class_config_path or '(默认)'}")
        st.code(f"Session DB Path:        {config.session_db_path or '(默认)'}")
        st.code(f"User DB Path:           {config.user_db_path or '(默认)'}")
        st.code(f"API Host:               {config.api_host}:{config.api_port}")

        if st.button("重新加载配置"):
            from satrap.core.config_loader import ConfigLoader
            new_config = ConfigLoader.autodetect()
            st.session_state.config = new_config
            st.success("配置已重新加载")
            st.rerun()

    with st.container(border=True):
        st.write("**Streamlit 信息**")
        st.code(f"Streamlit Version:      {st.__version__}")
        import sys
        st.code(f"Python Version:         {sys.version}")
        import os
        st.code(f"Working Directory:      {os.getcwd()}")

    with st.container(border=True):
        st.write("**提醒**")
        st.caption("修改模型配置或会话类配置后, 后端的配置管理器会自动感知文件变更")
        st.caption("如果后端运行中, 可以通过上方[重载配置]按钮热加载配置")
        st.caption("Streamlit 管理面板和后端是独立的两个进程, 各自独立运行")


render()
