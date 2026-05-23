from __future__ import annotations

import json
import sys
import os

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
    config_exists,
    create_default_config,
    delete_platform,
    find_config_path,
    load_config_document,
    load_raw_config,
    parse_platforms_text,
    parse_raw_config,
    save_config_document,
    upsert_platform,
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


def _platform_index(options: list[str], value: str) -> int:
    """获取 selectbox 默认索引"""
    try:
        return options.index(value)
    except ValueError:
        return 0


def _platform_settings_form(platform_type: str, settings: dict, key_prefix: str) -> dict:
    """按平台类型渲染 settings 表单"""
    if platform_type == "onebot":
        col1, col2 = st.columns(2)
        with col1:
            host = st.text_input("Host", value=str(settings.get("host", "127.0.0.1")), key=f"{key_prefix}_host")
            access_token = st.text_input(
                "Access Token",
                value=str(settings.get("access_token", "")),
                type="password",
                key=f"{key_prefix}_access_token",
            )
            enable_private = st.checkbox(
                "启用私聊",
                value=bool(settings.get("enable_private", True)),
                key=f"{key_prefix}_enable_private",
            )
            self_id = st.text_input("Self ID", value=str(settings.get("self_id", "")), key=f"{key_prefix}_self_id")
        with col2:
            port = st.number_input(
                "Port",
                min_value=1,
                max_value=65535,
                value=int(settings.get("port", 8080)),
                key=f"{key_prefix}_port",
            )
            secret = st.text_input(
                "Secret",
                value=str(settings.get("secret", "")),
                type="password",
                key=f"{key_prefix}_secret",
            )
            enable_group = st.checkbox(
                "启用群聊",
                value=bool(settings.get("enable_group", True)),
                key=f"{key_prefix}_enable_group",
            )
        result = {
            "host": host,
            "port": int(port),
            "access_token": access_token,
            "secret": secret,
            "enable_private": enable_private,
            "enable_group": enable_group,
        }
        if self_id.strip():
            result["self_id"] = self_id.strip()
        return result

    col1, col2 = st.columns(2)
    with col1:
        base_url = st.text_input("Base URL", value=str(settings.get("base_url", "")), key=f"{key_prefix}_base_url")
        api_token = st.text_input(
            "API Token",
            value=str(settings.get("api_token", "")),
            type="password",
            key=f"{key_prefix}_api_token",
        )
        chat_enabled = st.checkbox(
            "启用 Chat",
            value=bool(settings.get("chat_enabled", settings.get("misskey_enable_chat", True))),
            key=f"{key_prefix}_chat_enabled",
        )
        local_only = st.checkbox(
            "Local Only",
            value=bool(settings.get("misskey_local_only", False)),
            key=f"{key_prefix}_local_only",
        )
    with col2:
        room_enabled = st.checkbox(
            "启用 Room",
            value=bool(settings.get("room_enabled", False)),
            key=f"{key_prefix}_room_enabled",
        )
        max_message_length = st.number_input(
            "最大消息长度",
            min_value=1,
            value=int(settings.get("max_message_length", 3000)),
            key=f"{key_prefix}_max_message_length",
        )
        visibility_options = ["public", "home", "followers", "specified"]
        visibility = st.selectbox(
            "默认可见性",
            visibility_options,
            index=_platform_index(visibility_options, str(settings.get("misskey_default_visibility", "public"))),
            key=f"{key_prefix}_visibility",
        )
    return {
        "base_url": base_url,
        "api_token": api_token,
        "chat_enabled": chat_enabled,
        "room_enabled": room_enabled,
        "max_message_length": int(max_message_length),
        "misskey_default_visibility": visibility,
        "misskey_local_only": local_only,
    }


def _render_platform_editor(config_data: dict, config_path, backend_running: bool):
    """渲染平台快捷新增/修改表单"""
    platforms = list(config_data.get("platforms", []) or [])
    platform_ids = [str(item.get("id", "")).strip() for item in platforms if str(item.get("id", "")).strip()]
    selected = st.selectbox("选择已有平台", ["新增平台配置"] + platform_ids, key="platform_editor_selected")
    current = next((item for item in platforms if item.get("id") == selected), None)
    current_settings = dict((current or {}).get("settings") or {})
    current_type = str((current or {}).get("type") or "misskey")

    type_options = ["misskey", "onebot"]
    if current_type and current_type not in type_options:
        type_options.append(current_type)

    col_id, col_type = st.columns(2)
    with col_id:
        platform_id = st.text_input(
            "平台名称",
            value=str((current or {}).get("id") or ""),
            placeholder="misskey_main",
            key=f"platform_editor_id_{selected}",
        )
    with col_type:
        platform_type = st.selectbox(
            "类型",
            type_options,
            index=_platform_index(type_options, current_type),
            key=f"platform_editor_type_{selected}",
        )

    settings = _platform_settings_form(platform_type, current_settings, f"platform_editor_{selected}_{platform_type}")

    col_save, col_delete = st.columns(2)
    with col_save:
        if st.button("保存平台配置", type="primary", key=f"save_platform_{selected}"):
            try:
                updated_platforms = upsert_platform(
                    platforms,
                    original_id=None if selected == "新增平台配置" else selected,
                    platform_id=platform_id,
                    platform_type=platform_type,
                    settings=settings,
                )
                updated = dict(config_data)
                updated["platforms"] = updated_platforms
                _save_config_and_maybe_restart(config_path, updated, backend_running)
            except Exception as e:
                st.error(f"平台配置无效: {e}")
    with col_delete:
        if selected != "新增平台配置" and st.button("删除平台配置", key=f"delete_platform_{selected}"):
            try:
                updated = dict(config_data)
                updated["platforms"] = delete_platform(platforms, selected)
                _save_config_and_maybe_restart(config_path, updated, backend_running)
            except Exception as e:
                st.error(f"删除平台失败: {e}")


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
        st.caption(f"当前配置文件: `{find_config_path()}`")

        col_create, col_reload = st.columns(2)
        with col_create:
            has_config = config_exists()
            create_clicked = st.button("配置已存在" if has_config else "快速创建配置", disabled=has_config)
        with col_reload:
            reload_frontend_clicked = st.button("重新加载配置")

        if create_clicked:
            new_config = create_default_config()
            reset_state_managers(new_config)
            st.success(f"配置已创建: {find_config_path()}")
            st.rerun()

        if reload_frontend_clicked:
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

        tab_common, tab_platforms, tab_raw = st.tabs(["常用配置", "平台配置", "原始配置"])
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

        with tab_platforms:
            _render_platform_editor(config_data, config_path, backend_running)
            st.divider()
            st.write("**当前 platforms**")
            st.json(config_data.get("platforms", []) or [])

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
        st.code(f"Python Version:         {sys.version}")
        st.code(f"Working Directory:      {os.getcwd()}")

    with st.container(border=True):
        st.write("**提醒**")
        st.caption("修改模型配置或会话类配置后, 后端的配置管理器会自动感知文件变更")
        st.caption("如果后端运行中, 可以通过上方[重载配置]按钮热加载配置")
        st.caption("Streamlit 管理面板和后端是独立的两个进程, 各自独立运行")


render()
