from __future__ import annotations

import streamlit as st
from satrap.admin_utils.config_editor import (
    _platform_settings_form,
    configured_platform_types,
    delete_platform,
    find_config_path,
    load_config_document,
    save_config_document,
    upsert_platform,
)
from satrap.admin_utils.state import ensure_state, reset_state_managers
from satrap.cli.client import DaemonClient, DaemonInfo

st.set_page_config(page_title="平台状态", page_icon="", layout="wide")


def _type_options(health: dict) -> list[str]:
    base = ["misskey", "onebot"]
    for info in health.get("adapters", {}).values():
        typ = str(info.get("config_type") or info.get("type") or "").strip()
        if typ and typ not in base:
            base.append(typ)
    return base


def _save_and_reload(data: dict):
    path = find_config_path()
    try:
        new_config = save_config_document(path, data)
        reset_state_managers(new_config)
        client = DaemonClient(daemon=DaemonInfo.from_config(new_config), timeout=2)
        health = client.health()
        if health.get("running", False):
            result = client.reload_config()
            if "error" in result:
                st.warning(f"配置已保存，但热加载失败: {result['error']}")
            else:
                st.success("配置已保存并热加载")
        else:
            st.success("配置已保存")
        st.rerun()
    except Exception as e:
        st.error(f"保存失败: {e}")


@st.dialog("添加平台配置")
def _add_platform_dialog(health: dict):
    st.write("添加新平台")

    platform_id = st.text_input("平台名称", placeholder="misskey_main")
    platform_type = st.selectbox("类型", _type_options(health))
    settings = _platform_settings_form(platform_type, {}, f"add")

    if st.button("保存"):
        if not platform_id.strip():
            st.error("平台名称不能为空")
            return

        config_data = load_config_document(find_config_path())
        platforms = list(config_data.get("platforms", []) or [])
        try:
            platforms = upsert_platform(
                platforms,
                original_id=None,
                platform_id=platform_id.strip(),
                platform_type=platform_type,
                settings=settings,
            )
            config_data["platforms"] = platforms
            _save_and_reload(config_data)
        except Exception as e:
            st.error(str(e))


@st.dialog("编辑平台配置")
def _edit_platform_dialog(original_id: str, health: dict):
    st.write(f"编辑平台: {original_id}")

    config_data = load_config_document(find_config_path())
    platforms = list(config_data.get("platforms", []) or [])
    current = next((p for p in platforms if p.get("id") == original_id), None)
    if not current:
        st.error(f"未找到平台: {original_id}")
        return

    type_opts = _type_options(health)
    current_type = str(current.get("type", "misskey"))
    default_idx = type_opts.index(current_type) if current_type in type_opts else 0
    platform_id = st.text_input("平台名称", value=str(current.get("id", "")), disabled=True)
    platform_type = st.selectbox("类型", type_opts, index=default_idx)
    settings = _platform_settings_form(platform_type, current.get("settings", {}), f"edit_{original_id}")

    if st.button("保存"):
        try:
            platforms = upsert_platform(
                platforms,
                original_id=original_id,
                platform_id=original_id,
                platform_type=platform_type,
                settings=settings,
            )
            config_data = dict(config_data)
            config_data["platforms"] = platforms
            _save_and_reload(config_data)
        except Exception as e:
            st.error(str(e))


@st.dialog("确认删除")
def _delete_platform_dialog(platform_id: str):
    st.error(f"确定要删除平台 `{platform_id}` ?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("确认删除"):
            config_data = load_config_document(find_config_path())
            platforms = list(config_data.get("platforms", []) or [])
            config_data["platforms"] = delete_platform(platforms, platform_id)
            _save_and_reload(config_data)
    with col2:
        if st.button("取消"):
            st.rerun()


def render():
    ensure_state()
    st.subheader("平台适配器状态")

    daemon = DaemonInfo.from_config(st.session_state.config)
    client = DaemonClient(daemon=daemon, timeout=2)
    health = client.health()
    try:
        config_data = load_config_document(find_config_path())
    except Exception:
        config_data = {"platforms": st.session_state.config.platforms}

    adapter_types = configured_platform_types(
        config_data,
        health if health.get("running", False) else None,
    )
    if adapter_types:
        st.write(f"配置/运行中的适配器类型: {', '.join(adapter_types)}")
    else:
        st.info("配置和运行态中均未发现适配器类型")

    st.divider()

    if health.get("running", False):
        try:
            adapters = health.get("adapters", {})
            if adapters:
                for aid, info in adapters.items():
                    with st.container(border=True):
                        cols = st.columns([2, 1, 1, 1, 2])
                        with cols[0]:
                            st.write(f"**{aid}**")
                        with cols[1]:
                            st.write(info.get("config_type") or info.get("type") or "-")
                        with cols[2]:
                            status = info.get("status", "?")
                            if status == "running":
                                st.success(f"状态: {status}")
                            elif status == "error":
                                st.error(f"状态: {status}")
                            else:
                                st.info(f"状态: {status}")
                        with cols[3]:
                            started = info.get("started", False)
                            st.write("已启动" if started else "未启动")
                        with cols[4]:
                            last_error = info.get("last_error")
                            if last_error:
                                st.caption(str(last_error))
            else:
                st.info("当前无运行中的适配器实例")
        except Exception as e:
            st.error(f"获取适配器状态失败: {e}")
    else:
        detail = f": {health.get('error')}" if health.get("error") else ""
        st.warning(f"后端未运行, 无法获取适配器运行时状态{detail}")
        st.caption(f"运行 `python -m satrap.main run` 启动后端后刷新页面, 当前检测地址: {daemon.base_url}")

    st.divider()

    with st.container(border=True):
        col_title, col_add = st.columns([7, 1])
        with col_title:
            st.write("**配置中的平台**")
        with col_add:
            if st.button("+ 添加"):
                _add_platform_dialog(health)

        config = st.session_state.config
        platforms = list(config.platforms)
        if platforms:
            for p in platforms:
                pid = str(p.get("id", ""))
                with st.container(border=True):
                    cols = st.columns([2, 1, 3, 1, 1])
                    with cols[0]:
                        st.write(f"**{pid}**")
                    with cols[1]:
                        st.write(str(p.get("type", "-")))
                    with cols[2]:
                        settings_preview = str(p.get("settings", {}))
                        st.caption(settings_preview[:120] + ("..." if len(settings_preview) > 120 else ""))
                    with cols[3]:
                        if st.button("编辑", key=f"edit_{pid}"):
                            _edit_platform_dialog(pid, health)
                    with cols[4]:
                        if st.button("删除", key=f"del_{pid}"):
                            _delete_platform_dialog(pid)
        else:
            st.info("配置文件中未定义平台")

    if st.button("刷新"):
        st.rerun()


render()
