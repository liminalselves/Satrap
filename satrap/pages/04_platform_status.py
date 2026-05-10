from __future__ import annotations

import streamlit as st
from satrap.pages._state import ensure_state
from satrap.cli.client import DaemonClient
from satrap.core.platform import registry as platform_registry

st.set_page_config(page_title="平台状态", page_icon="", layout="wide")


def render():
    ensure_state()
    st.subheader("平台适配器状态")

    registered_types = platform_registry.list_types()
    if registered_types:
        st.write(f"已注册适配器类型: {', '.join(registered_types)}")
    else:
        st.info("暂无已注册的适配器类型")

    st.divider()

    client = DaemonClient()
    if client.is_alive():
        try:
            health = client.health()
            adapters = health.get("adapters", {})
            if adapters:
                for aid, info in adapters.items():
                    with st.container(border=True):
                        cols = st.columns([2, 1, 1, 3])
                        with cols[0]:
                            st.write(f"**{aid}**")
                        with cols[1]:
                            status = info.get("status", "?")
                            if status == "running":
                                st.success(f"状态: {status}")
                            elif status == "error":
                                st.error(f"状态: {status}")
                            else:
                                st.info(f"状态: {status}")
                        with cols[2]:
                            started = info.get("started", False)
                            st.write("已启动" if started else "未启动")
                        with cols[3]:
                            pass
            else:
                st.info("当前无运行中的适配器实例")
        except Exception as e:
            st.error(f"获取适配器状态失败: {e}")
    else:
        st.warning("后端未运行, 无法获取适配器运行时状态")
        st.caption("运行 `python -m satrap.main run` 启动后端后刷新页面")

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
