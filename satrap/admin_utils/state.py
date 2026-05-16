from __future__ import annotations

"""各页面共享的 session_state 初始化, 不含 st.set_page_config"""

from pathlib import Path
import sys

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

import streamlit as st
from satrap.core.config_loader import ConfigLoader
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.BackGroundManager import ModelConfigManager


def reset_state_managers(config):
    """用新配置重建前端共享管理器"""
    st.session_state.config = config
    st.session_state.scm = SessionClassConfigManager(
        storage_path=st.session_state.config.session_class_config_path,
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
        )

    if "mcm" not in st.session_state:
        st.session_state.mcm = ModelConfigManager(
            storage_path=st.session_state.config.model_config_path,
        )
