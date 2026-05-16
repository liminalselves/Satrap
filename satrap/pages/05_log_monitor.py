from __future__ import annotations

import time
from pathlib import Path

import streamlit as st
from satrap.admin_utils.log_state import read_log_increment

st.set_page_config(page_title="日志监控", page_icon="", layout="wide")

DEFAULT_LOG_PATHS = [
    Path.cwd() / ".satrap" / "logs",
]


def _find_log_file() -> Path | None:
    for p in DEFAULT_LOG_PATHS:
        if p.is_dir():
            for f in sorted(p.iterdir(), reverse=True):
                if f.suffix == ".log" and f.is_file():
                    return f
        elif p.is_file():
            return p
    alt = Path.cwd()
    for f in alt.iterdir():
        if f.suffix == ".log" and f.is_file():
            return f
    return None


LEVEL_COLORS = {
    "DEBUG": "gray",
    "INFO": "green",
    "WARNING": "orange",
    "ERROR": "red",
    "CRITICAL": "red",
}


def _parse_level(line: str) -> str:
    for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        if level in line:
            return level
    return "INFO"


def _colorize(line: str) -> str:
    level = _parse_level(line)
    color = LEVEL_COLORS.get(level, "white")
    return f":{color}[{line}]"


def render():
    st.subheader("日志监控")

    log_file = _find_log_file()
    if not log_file:
        st.warning("未找到日志文件, 请确认后端已运行并生成了日志文件")
        st.caption("查找路径: " + ", ".join(str(p) for p in DEFAULT_LOG_PATHS))
        return

    st.caption(f"日志文件: `{log_file}`")

    if "log_position" not in st.session_state:
        st.session_state.log_position = 0
    if "log_paused" not in st.session_state:
        st.session_state.log_paused = False
    if "log_lines" not in st.session_state:
        st.session_state.log_lines = []

    col1, col2, col3, col4 = st.columns([1, 1, 1, 3])
    with col1:
        level_filter = st.multiselect(
            "级别过滤",
            ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            default=["INFO", "WARNING", "ERROR", "CRITICAL"],
        )
    with col2:
        search_query = st.text_input("关键词搜索", placeholder="输入关键词...")
    with col3:
        if st.button("暂停" if not st.session_state.log_paused else "继续"):
            st.session_state.log_paused = not st.session_state.log_paused
        if st.button("清空显示"):
            st.session_state.log_lines = []
            st.session_state.log_position = log_file.stat().st_size
            st.rerun()
        if st.button("刷新"):
            st.rerun()

    max_lines = st.slider("显示行数", 50, 500, 200)

    try:
        position, cached_lines, display_lines = read_log_increment(
            log_file=log_file,
            position=st.session_state.log_position,
            cached_lines=list(st.session_state.log_lines),
            max_lines=max_lines,
            paused=st.session_state.log_paused,
        )
        st.session_state.log_position = position
        st.session_state.log_lines = cached_lines
    except Exception as e:
        st.error(f"读取日志文件失败: {e}")
        return

    if level_filter:
        display_lines = [l for l in display_lines if _parse_level(l) in level_filter]

    if search_query:
        display_lines = [l for l in display_lines if search_query.lower() in l.lower()]

    if not display_lines:
        st.info("暂无新日志")
    else:
        container = st.container(height=600)
        with container:
            for line in display_lines:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                st.markdown(_colorize(stripped))

    if not st.session_state.log_paused:
        time.sleep(2)
        st.rerun()


render()
