from __future__ import annotations

import time

import streamlit as st
from satrap.admin_utils.backend_control import (
    check_backend,
    daemon_info,
    managed_process_alive,
    render_notification,
    restart_backend,
    show_notification,
    start_backend,
    stop_backend,
)
from satrap.admin_utils.state import ensure_state

st.set_page_config(page_title="仪表盘", page_icon="", layout="wide")


def _check_backend():
    """检测后端是否在线并返回健康信息"""
    return check_backend(st.session_state.config)


def _start_backend():
    """启动后端子进程"""
    result = start_backend(st.session_state.config)
    show_notification(result.message, result.level)
    if result.ok:
        st.rerun()


def _stop_backend(rerun: bool = True):
    """停止后端子进程"""
    result = stop_backend(st.session_state.config)
    show_notification(result.message, result.level)
    if rerun and result.ok:
        st.rerun()


def _render_start_stop_buttons(backend_ok: bool):
    """渲染启动/停止/重启按钮"""
    managed = managed_process_alive()

    col1, col2, col3 = st.columns([1, 1, 3])

    with col1:
        if backend_ok:
            if st.button("重启后端", type="primary"):
                result = restart_backend(st.session_state.config)
                show_notification(result.message, result.level)
                if result.ok:
                    st.rerun()
        elif not backend_ok:
            if st.button("启动后端", type="primary"):
                _start_backend()

    with col2:
        if backend_ok or managed:
            if st.button("停止后端"):
                _stop_backend()

    with col3:
        if managed:
            st.caption("后端由此面板管理")
        elif backend_ok:
            st.caption("后端由外部启动, 可通过本面板关闭")
        else:
            st.caption("后端未运行")


def render():
    ensure_state()
    st.subheader("仪表盘")

    # 渲染之前滞留的通知
    render_notification()

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
        st.info(f"后端 API 地址可用 ({daemon_info(st.session_state.config).base_url}), 运行中适配器: {adapter_count} 个")
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
        st.caption(f"运行 `python -m satrap.main run` 启动后端, 当前检测地址: {daemon_info(st.session_state.config).base_url}")

    st.divider()
    st.caption(f"页面刷新时间: {time.strftime('%H:%M:%S')}")
    if st.button("刷新"):
        st.rerun()


render()
