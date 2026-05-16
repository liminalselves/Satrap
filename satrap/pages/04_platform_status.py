from __future__ import annotations

import streamlit as st
from satrap.admin_utils.config_editor import configured_platform_types, load_config_document, find_config_path
from satrap.cli.client import DaemonClient, DaemonInfo
from satrap.admin_utils.state import ensure_state

st.set_page_config(page_title="平台状态", page_icon="", layout="wide")


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
        st.write("**配置中的平台**")
        config = st.session_state.config
        platforms = config.platforms
        if platforms:
            plat_rows = []
            for p in platforms:
                plat_rows.append({
                    "id": p.get("id", "-"),
                    "type": p.get("type", "-"),
                    "settings": str(p.get("settings", {})),
                })
            st.dataframe(plat_rows, use_container_width=True, hide_index=True)
        else:
            st.info("配置文件中未定义平台")

    if st.button("刷新"):
        st.rerun()


render()
