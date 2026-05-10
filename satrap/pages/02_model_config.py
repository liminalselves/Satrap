from __future__ import annotations

from typing import Any

import streamlit as st
from satrap.pages._state import ensure_state
from satrap.core.type import LLMConfig, EmbeddingConfig, ReRankConfig

st.set_page_config(page_title="模型配置", page_icon="", layout="wide")

TYPE_LABELS = {
    "llm": ("LLM 配置", LLMConfig),
    "embedding": ("Embedding 配置", EmbeddingConfig),
    "rerank": ("ReRank 配置", ReRankConfig),
}

FIELD_META = {
    "llm": [
        ("model", "模型", "text"),
        ("base_url", "Base URL", "text"),
        ("api_key", "API Key", "password"),
        ("temperature", "Temperature", "number"),
        ("top_p", "Top P", "number"),
        ("max_tokens", "Max Tokens", "number"),
    ],
    "embedding": [
        ("model", "模型", "text"),
        ("base_url", "Base URL", "text"),
        ("api_key", "API Key", "password"),
        ("dimensions", "Dimensions", "number"),
        ("max_batch_size", "Max Batch Size", "number"),
    ],
    "rerank": [
        ("model", "模型", "text"),
        ("base_url", "Base URL", "text"),
        ("api_key", "API Key", "password"),
        ("top_k", "Top K", "number"),
        ("min_score", "Min Score", "number"),
    ],
}


@st.dialog("新增配置")
def _add_dialog(model_type: str):
    mcm = st.session_state.mcm
    label, cls = TYPE_LABELS[model_type]
    st.write(f"新增 {label}")

    fields = FIELD_META[model_type]
    values = {}
    for key, display, kind in fields:
        if kind == "password":
            values[key] = st.text_input(display, type="password", key=f"add_{key}")
        elif kind == "number":
            values[key] = st.number_input(display, value=None, format="%f", key=f"add_{key}")
        else:
            values[key] = st.text_input(display, key=f"add_{key}")

    name = st.text_input("配置名称 (name)", value="default")
    ok = st.button("保存")

    if ok:
        all_vals = {k: v for k, v in values.items() if v is not None and v != ""}
        all_vals["name"] = name
        try:
            cfg = cls(**all_vals)
            if model_type == "llm":
                mcm.set_llm_config(cfg, name=name)
            elif model_type == "embedding":
                mcm.set_embedding_config(cfg, name=name)
            else:
                mcm.set_rerank_config(cfg, name=name)
            st.success(f"已保存: {name}")
            st.rerun()
        except Exception as e:
            st.error(f"保存失败: {e}")


@st.dialog("编辑配置")
def _edit_dialog(model_type: str, name: str):
    mcm = st.session_state.mcm
    label, cls = TYPE_LABELS[model_type]

    if model_type == "llm":
        cfg_obj = mcm.get_llm_config(name)
    elif model_type == "embedding":
        cfg_obj = mcm.get_embedding_config(name)
    else:
        cfg_obj = mcm.get_rerank_config(name)

    st.write(f"编辑 {label}: {name}")

    fields = FIELD_META[model_type]
    values = {}
    for key, display, kind in fields:
        current = getattr(cfg_obj, key, None) or ""
        if kind == "password":
            if current:
                st.caption(f"{display}: 已设置 (留空则不修改)")
            values[key] = st.text_input(display, type="password", key=f"edit_{name}_{key}")
        elif kind == "number":
            try:
                num_val = float(current) if current else None
            except (ValueError, TypeError):
                num_val = None
            values[key] = st.number_input(display, value=num_val, format="%f", key=f"edit_{name}_{key}")
        else:
            values[key] = st.text_input(display, value=str(current), key=f"edit_{name}_{key}")

    ok = st.button("保存修改")
    if ok:
        updates = {}
        for key, _, kind in fields:
            v = values[key]
            if kind == "password" and not v:
                continue
            if v is not None and v != "":
                if kind == "number":
                    try:
                        v = float(v) if "." in str(v) else int(v)
                    except (ValueError, TypeError):
                        pass
                updates[key] = v
        try:
            if model_type == "llm":
                mcm.update_llm_config(name=name, **updates)
            elif model_type == "embedding":
                mcm.update_embedding_config(name=name, **updates)
            else:
                mcm.update_rerank_config(name=name, **updates)
            st.success("已更新")
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            st.error(f"更新失败: {e}")


@st.dialog("确认删除")
def _delete_dialog(model_type: str, name: str):
    mcm = st.session_state.mcm
    st.error(f"确定要删除 {model_type} 配置 `{name}` ?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("确认删除"):
            ok = False
            try:
                if model_type == "llm":
                    ok = mcm.remove_llm_config(name)
                elif model_type == "embedding":
                    ok = mcm.remove_embedding_config(name)
                else:
                    ok = mcm.remove_rerank_config(name)
            except Exception as e:
                st.error(str(e))
                return
            if ok:
                st.success("已删除")
                st.cache_data.clear()
                st.rerun()
            else:
                st.error("删除失败: 未找到该配置")
    with col2:
        if st.button("取消"):
            st.rerun()


@st.cache_data(ttl=2)
def _get_configs(_mcm, model_type: str) -> dict[str, Any]:
    if model_type == "llm":
        return _mcm.list_llm_configs(mask_api_key=True)
    elif model_type == "embedding":
        return _mcm.list_embedding_configs(mask_api_key=True)
    else:
        return _mcm.list_rerank_configs(mask_api_key=True)


def _render_card(model_type: str, name: str, cfg: dict[str, Any]):
    label = TYPE_LABELS[model_type][0]
    with st.container(border=True):
        cols = st.columns([3, 1, 1])
        with cols[0]:
            st.write(f"**{name}**")
            model_val = cfg.get("model") or cfg.get("base_url", "-")
            st.caption(f"Model: {model_val}")
            ak = cfg.get("api_key", "")
            if ak:
                st.caption(f"API Key: {ak}")
        with cols[1]:
            if st.button("编辑", key=f"edit_{model_type}_{name}"):
                _edit_dialog(model_type, name)
        with cols[2]:
            if st.button("删除", key=f"del_{model_type}_{name}"):
                _delete_dialog(model_type, name)


def render():
    ensure_state()
    st.subheader("模型配置管理")

    mcm = st.session_state.mcm

    col_btn, _ = st.columns([1, 5])
    with col_btn:
        model_type = st.selectbox("选择类型", list(TYPE_LABELS.keys()),
                                  format_func=lambda t: TYPE_LABELS[t][0],
                                  label_visibility="collapsed")

    if st.button("+ 新增配置", key=f"add_{model_type}"):
        _add_dialog(model_type)

    configs = _get_configs(mcm, model_type)

    if not configs:
        st.info("暂无配置, 点击上方按钮新增")
        return

    for name, cfg in configs.items():
        _render_card(model_type, name, cfg)


render()
