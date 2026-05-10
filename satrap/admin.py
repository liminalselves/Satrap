from __future__ import annotations

import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

import streamlit as st
from satrap.pages._state import ensure_state

st.set_page_config(
    page_title="Satrap 管理面板",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

ensure_state()

# streamlit run satrap/admin.py
