from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import streamlit as st
from satrap.admin_utils.state import ensure_state, trigger_backend_reload
from satrap.cli.client import DaemonClient, DaemonInfo
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.session_discovery import create_default_session_dir, discover_session_classes

st.set_page_config(page_title="会话管理", page_icon="", layout="wide")


def _reload_caches():
    st.cache_data.clear()


def _adapter_options() -> list[str]:
    """获取可绑定的平台适配器实例 ID 列表"""
    config = st.session_state.config
    ids = [str(p.get("id", "")).strip() for p in config.platforms if str(p.get("id", "")).strip()]

    daemon = DaemonInfo.from_config(config)
    client = DaemonClient(daemon=daemon, timeout=1)
    health = client.health()
    if health.get("running", False):
        for adapter_id in health.get("adapters", {}):
            adapter_id = str(adapter_id).strip()
            if adapter_id and adapter_id not in ids:
                ids.append(adapter_id)
    return ids


def _class_name_to_config_name(class_name: str) -> str:
    """将类名转换为默认配置名"""
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower()
    return name.removesuffix("_session") or name


def _scan_path_options() -> list[str]:
    """获取 Session 扫描目录选项"""
    paths = [str(item) for item in getattr(st.session_state.config, "session_scan_paths", []) or []]
    if not paths:
        paths = [".satrap/session"]
    return paths


@st.dialog("注册新会话类")
def _register_dialog():
    scm: SessionClassConfigManager = st.session_state.scm
    st.write("注册新会话类")

    mode = st.radio("注册方式", ["扫描目录", "手动 Class Path"], horizontal=True)

    selected_class_path = ""
    default_name = ""
    if mode == "扫描目录":
        scan_paths = _scan_path_options()
        selected_scan_path = st.selectbox("Session Path", scan_paths)
        col_create, col_refresh = st.columns(2)
        with col_create:
            if st.button("创建默认 Session 目录"):
                path = create_default_session_dir([selected_scan_path])
                st.success(f"已创建: {path}")
        with col_refresh:
            refresh_clicked = st.button("刷新扫描结果")

        cache_key = f"session_scan_results_{selected_scan_path}"
        if refresh_clicked or cache_key not in st.session_state:
            st.session_state[cache_key] = [item.to_dict() for item in discover_session_classes([selected_scan_path])]

        results = st.session_state.get(cache_key, [])
        errors = [item for item in results if item.get("error")]
        for item in errors:
            st.warning(f"{Path(item.get('file_path', '')).name}: {item.get('error')}")

        choices = [item for item in results if item.get("class_path")]
        if choices:
            labels = [
                f"{Path(item['file_path']).name} -> {item['class_name']} ({'async' if item.get('is_async') else 'sync'})"
                for item in choices
            ]
            selected_index = st.selectbox("选择会话类", range(len(labels)), format_func=lambda i: labels[i])
            selected = choices[int(selected_index)]
            selected_class_path = str(selected.get("class_path", ""))
            default_name = _class_name_to_config_name(str(selected.get("class_name", "")))
            st.code(selected_class_path)
            st.caption("参数模板")
            st.json(selected.get("init_params", {}))
        else:
            st.info("未发现可注册的 Session/AsyncSession 子类")
    else:
        selected_class_path = st.text_input("Class Path (如 `misskey_session.MisskeySession`)")

    name = st.text_input("名称 (name)", value=default_name)
    class_path = selected_class_path if mode == "扫描目录" else selected_class_path
    desc = st.text_input("描述 (description, 可选)")
    model_key = st.text_input("模型键 (model_key, 可选)")

    if st.button("注册"):
        if not name or not class_path:
            st.error("名称和 Class Path 为必填项")
            return
        try:
            scm.register_by_class_path(name, class_path, description=desc, model_key=model_key)
            st.success(f"已注册: {name}")
            _reload_caches()
            st.rerun()
        except Exception as e:
            st.error(f"注册失败: {e}")


@st.dialog("编辑参数")
def _param_dialog(name: str):
    scm: SessionClassConfigManager = st.session_state.scm
    st.write(f"编辑参数: {name}")

    params = scm.get_params(name)
    params_str = json.dumps(params, ensure_ascii=False, indent=2)

    st.caption("params (JSON 格式)")
    new_params_str = st.text_area("params", value=params_str, height=300)

    if st.button("保存参数"):
        try:
            new_params = json.loads(new_params_str)
            if not isinstance(new_params, dict):
                raise ValueError("params 必须是 JSON 对象")
            scm.set_config(name, new_params)
            st.success("已保存")
            _reload_caches()
            trigger_backend_reload()
            st.rerun()
        except json.JSONDecodeError as e:
            st.error(f"JSON 格式错误: {e}")
        except Exception as e:
            st.error(f"保存失败: {e}")


@st.dialog("创建会话")
def _create_session_dialog(name: str):
    scm: SessionClassConfigManager = st.session_state.scm
    st.write(f"创建会话实例: {name}")

    cfg_entry = scm.get_config(name)
    model_key = (cfg_entry or {}).get("model_key", "")

    session_id = st.text_input("Session ID (留空自动生成)")
    adapter_ids = _adapter_options()
    adapter_choices = ["自动(按消息来源)"] + adapter_ids
    adapter_choice = st.selectbox("绑定适配器", adapter_choices)
    adapter_id = "" if adapter_choice == "自动(按消息来源)" else adapter_choice

    llm_name = ""
    if model_key:
        mcm = st.session_state.mcm
        llm_configs = mcm.list_llm_configs(mask_api_key=True)
        llm_names = list(llm_configs.keys())
        current_name = st.selectbox("LLM 模型", llm_names) if llm_names else st.text_input("LLM 名称")
        llm_name = current_name

    if st.button("创建"):

        config = st.session_state.config
        sm = SessionManager(db_path=config.session_db_path)
        extra = {}
        if llm_name and model_key:
            extra[model_key] = llm_name
        elif llm_name:
            extra["model_name"] = llm_name
        if adapter_id:
            extra["adapter_id"] = adapter_id

        try:
            sid = session_id or uuid.uuid4().hex
            cfg = sm.register_session_from_class_config(
                name, scm, session_id=sid, extra_params=extra,
            )
            st.success(f"已创建会话: `{cfg.session_id}`")
            _reload_caches()
            st.rerun()
        except Exception as e:
            st.error(f"创建失败: {e}")


@st.dialog("确认注销")
def _unregister_dialog(name: str):
    scm: SessionClassConfigManager = st.session_state.scm
    st.error(f"确定要注销会话类 `{name}` ?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("确认注销"):
            if scm.remove_config(name):
                st.success("已注销")
                _reload_caches()
                st.rerun()
            else:
                st.error("注销失败")
    with col2:
        if st.button("取消"):
            st.rerun()


@st.dialog("编辑模型键")
def _edit_model_key_dialog(name: str):
    scm: SessionClassConfigManager = st.session_state.scm
    st.write(f"编辑模型键: {name}")

    cfg_entry = scm.get_config(name)
    current_model_key = (cfg_entry or {}).get("model_key", "")
    st.caption("模型键决定了 LLM 模型名称以哪个参数名传给会话构造函数。留空则使用默认值 'model_name'。")
    new_model_key = st.text_input("模型键", value=current_model_key) or ""

    if st.button("保存"):
        try:
            scm.set_model_key(name, new_model_key)
            st.success("已保存")
            _reload_caches()
            trigger_backend_reload()
            st.rerun()
        except Exception as e:
            st.error(f"保存失败: {e}")


@st.cache_data(ttl=2)
def _get_session_configs(_scm) -> dict[str, dict[str, Any]]:
    return _scm.list_configs()


def render():
    ensure_state()
    st.subheader("会话类管理")

    scm = st.session_state.scm

    if st.button("+ 注册新会话类"):
        _register_dialog()

    configs = _get_session_configs(scm)

    if not configs:
        st.info("暂无已注册的会话类")
        return

    rows = []
    for sname, entry in configs.items():
        status = "启用" if entry.get("enabled", True) else "停用"
        mk = entry.get("model_key", "") or "-"
        cp = entry.get("class_path", "")
        rows.append({
            "名称": sname,
            "状态": status,
            "模型键": mk,
            "Class Path": cp,
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.divider()
    st.caption("对单个会话类的操作:")

    name_list = list(configs.keys())
    if not name_list:
        return

    col1, col2, col3, col4, col5, col6 = st.columns([2, 1, 1, 1, 1, 1])
    with col1:
        target = st.selectbox("选择会话类", name_list, label_visibility="collapsed")
    with col2:
        entry = configs.get(target, {})
        enabled = entry.get("enabled", True)
        if enabled:
            if st.button("停用"):
                try:
                    scm.disable(target)
                    _reload_caches()
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        else:
            if st.button("启用"):
                try:
                    scm.enable(target)
                    _reload_caches()
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
    with col3:
        if st.button("参数"):
            _param_dialog(target)
    with col4:
        if st.button("创建会话"):
            _create_session_dialog(target)
    with col5:
        if st.button("注销"):
            _unregister_dialog(target)
    with col6:
        if st.button("模型键"):
            _edit_model_key_dialog(target)


render()
