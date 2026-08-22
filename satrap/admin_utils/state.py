"""各页面共享的 session_state 初始化, 不含 st.set_page_config"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

import streamlit as st
from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config_loader import ConfigLoader
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.type import safe_getattr_str, safe_getattr_int


def trigger_backend_reload():
    """触发后端热加载配置(fire-and-forget)"""
    config = st.session_state.config
    host = safe_getattr_str(config, 'api_host', '127.0.0.1')
    port = safe_getattr_int(config, 'api_port', 19870)
    try:
        urllib.request.urlopen(f"http://{host}:{port}/api/config/reload", data=b"", timeout=5)
    except Exception:
        pass


def reset_state_managers(config: BackendConfig):
    """
    用新配置重建前端共享管理器

    参数:
    - config: 配置信息
    """
    st.session_state.config = config
    st.session_state.scm = SessionClassConfigManager(
        storage_path=st.session_state.config.session_class_config_path,
        session_scan_paths=st.session_state.config.session_scan_paths,
    )
    st.session_state.mcm = ModelConfigManager(
        storage_path=st.session_state.config.model_config_path,
    )


def ensure_state():
    """确保 st.session_state 中已初始化共享管理器, 各页面在 render() 开头调用"""
    if "config" not in st.session_state:
        config = ConfigLoader.autodetect()
        st.session_state.config = config

    if "scm" not in st.session_state:
        st.session_state.scm = SessionClassConfigManager(
            storage_path=st.session_state.config.session_class_config_path,
            session_scan_paths=st.session_state.config.session_scan_paths,
        )

    if "mcm" not in st.session_state:
        st.session_state.mcm = ModelConfigManager(
            storage_path=st.session_state.config.model_config_path,
        )
