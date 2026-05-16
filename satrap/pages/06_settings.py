from __future__ import annotations

import json

import streamlit as st
from satrap.admin_utils.backend_control import (
    daemon_client,
    daemon_info,
    restart_backend,
    show_notification,
    start_backend,
    stop_backend,
)
from satrap.admin_utils.config_editor import (
    find_config_path,
    load_config_document,
    load_raw_config,
    parse_platforms_text,
    parse_raw_config,
    save_config_document,
    update_common_fields,
)
from satrap.admin_utils.state import ensure_state, reset_state_managers

st.set_page_config(page_title="系统设置", page_icon="", layout="wide")


def _save_config_and_maybe_restart(path, data: dict, backend_running: bool):
    """保存配置, 并在后端运行时重启"""
    try:
        old_config = st.session_state.config
        new_config = save_config_document(path, data)
        reset_state_managers(new_config)
        st.success(f"配置已保存: {path}")
        if backend_running:
            stopped = stop_backend(old_config)
            if stopped.ok:
                started = start_backend(new_config)
                show_notification(started.message, started.level)
                if started.ok:
                    st.success("后端已按新配置重启")
                else:
                    st.warning(started.message)
            else:
                show_notification(stopped.message, stopped.level)
                st.warning(stopped.message)
        st.rerun()
    except Exception as e:
        st.error(f"保存配置失败: {e}")


def render():
    ensure_state()
    st.subheader("系统设置")

    with st.container(border=True):
        st.write("**后端控制**")
        daemon = daemon_info(st.session_state.config)
        client = daemon_client(st.session_state.config, timeout=2)
        health = client.health()
        backend_running = health.get("running", False)
        if backend_running:
            st.success(f"后端运行中: {daemon.base_url}")
            col_reload, col_restart, col_stop = st.columns(3)
            with col_reload:
                reload_clicked = st.button("重载配置")
            with col_restart:
                restart_clicked = st.button("重启后端", type="primary")
            with col_stop:
                stop_clicked = st.button("停止后端")

            if reload_clicked:
                try:
                    result = client.reload_config()
                    if "error" in result:
                        st.error(f"重载失败: {result['error']}")
                    else:
                        st.success("配置已重载")
                except Exception as e:
                    st.error(f"重载失败: {e}")
            if restart_clicked:
                result = restart_backend(st.session_state.config)
                show_notification(result.message, result.level)
                st.rerun()
            if stop_clicked:
                result = stop_backend(st.session_state.config)
                show_notification(result.message, result.level)
                st.rerun()
        else:
            st.warning("后端未运行")
            if health.get("error"):
                st.caption(f"连接错误: {health['error']}")
            st.caption(f"运行 `python -m satrap.main run` 启动后端, 当前检测地址: {daemon.base_url}")

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
            reset_state_managers(new_config)
            st.success("配置已重新加载")
            st.rerun()

    config_path = find_config_path()
    try:
        config_data = load_config_document(config_path)
    except Exception as e:
        config_data = {}
        st.error(f"读取配置失败: {e}")

    with st.container(border=True):
        st.write("**配置编辑**")
        st.caption(f"当前配置文件: `{config_path}`")

        tab_common, tab_raw = st.tabs(["常用配置", "原始配置"])
        with tab_common:
            current_api = dict(config_data.get("api") or {})
            col1, col2 = st.columns(2)
            with col1:
                api_host = st.text_input("API Host", value=str(current_api.get("host", config.api_host)))
                default_session_type = st.text_input(
                    "默认会话类型",
                    value=str(config_data.get("default_session_type", config.default_session_type)),
                )
                max_sessions = st.number_input(
                    "最大会话数",
                    min_value=1,
                    value=int(config_data.get("max_sessions", config.max_sessions)),
                )
                rate_limit = st.number_input(
                    "限流速率",
                    min_value=0.0,
                    value=float(config_data.get("rate_limit", config.rate_limit)),
                    format="%f",
                )
            with col2:
                api_port = st.number_input(
                    "API Port",
                    min_value=1,
                    max_value=65535,
                    value=int(current_api.get("port", config.api_port)),
                )
                idle_timeout = st.number_input(
                    "会话闲置超时",
                    min_value=0,
                    value=int(config_data.get("idle_timeout", config.idle_timeout)),
                )
                llm_timeout = st.number_input(
                    "LLM 超时",
                    min_value=0.0,
                    value=float(config_data.get("llm_timeout", config.llm_timeout)),
                    format="%f",
                )
                rate_burst = st.number_input(
                    "限流突发数",
                    min_value=1,
                    value=int(config_data.get("rate_burst", config.rate_burst)),
                )

            platforms_text = st.text_area(
                "平台配置 platforms (JSON 或 YAML 列表)",
                value=json.dumps(config_data.get("platforms", []), ensure_ascii=False, indent=2),
                height=260,
            )
            if st.button("保存常用配置并按需重启"):
                try:
                    platforms = parse_platforms_text(platforms_text)
                    updated = update_common_fields(
                        config_data,
                        api_host=api_host,
                        api_port=int(api_port),
                        default_session_type=default_session_type,
                        max_sessions=int(max_sessions),
                        idle_timeout=int(idle_timeout),
                        llm_timeout=float(llm_timeout),
                        rate_limit=float(rate_limit),
                        rate_burst=int(rate_burst),
                        platforms=platforms,
                    )
                    _save_config_and_maybe_restart(config_path, updated, backend_running)
                except Exception as e:
                    st.error(f"常用配置无效: {e}")

        with tab_raw:
            raw_text = st.text_area("完整配置", value=load_raw_config(config_path), height=520)
            if st.button("保存原始配置并按需重启"):
                try:
                    parsed = parse_raw_config(config_path, raw_text)
                    _save_config_and_maybe_restart(config_path, parsed, backend_running)
                except Exception as e:
                    st.error(f"原始配置无效: {e}")

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
