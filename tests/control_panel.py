# -*- coding: utf-8 -*-
"""Satrap 控制面板

运行方式:
    streamlit run tests/control_panel.py
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.type import EmbeddingConfig, LLMConfig, ReRankConfig, SessionConfig, UserCall


# -------------------------------
# Demo Session 类型（用于面板演示）
# -------------------------------
class EchoSession(Session):
    """同步回显会话，用于验证 SessionManager 管理能力。"""

    def __init__(self, session_config: SessionConfig):
        super().__init__(session_id=session_config.session_id or "")
        cfg = dict(session_config.session_config or {})
        self.prefix = str(cfg.get("prefix", "echo"))

    def run(self, message: str) -> str:
        self.session_ctx.add_user_message(message)
        return f"{self.prefix}:{message}"


class AsyncEchoSession(AsyncSession):
    """异步回显会话，用于验证异步会话管理能力。"""

    def __init__(self, session_config: SessionConfig):
        super().__init__(session_id=session_config.session_id or "")
        cfg = dict(session_config.session_config or {})
        self.prefix = str(cfg.get("prefix", "async-echo"))

    async def run(self, message: str) -> str:
        await self.session_ctx.add_user_message(message)
        return f"{self.prefix}:{message}"


def _builtin_class_catalog() -> Dict[str, type[Session] | type[AsyncSession]]:
    """内置可注册会话类型。"""
    return {
        "EchoSession": EchoSession,
        "AsyncEchoSession": AsyncEchoSession,
    }


def _register_builtin_types(manager: SessionManager):
    """向 SessionManager 注册内置 demo 类型。"""
    manager.register_session_type("echo", EchoSession)
    manager.register_session_type("async-echo", AsyncEchoSession)


# -------------------------------
# 动态扫描与加载 Session 子类
# -------------------------------
def _base_name(base: ast.expr) -> str:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        chain: List[str] = []
        node: ast.AST = base
        while isinstance(node, ast.Attribute):
            chain.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            chain.append(node.id)
        return ".".join(reversed(chain))
    return ""


def discover_session_candidates(scan_root: str) -> List[Dict[str, str]]:
    """在指定目录递归扫描 .py 文件，提取直接继承 Session/AsyncSession 的类。

    说明:
    - 这里先用 AST 做静态扫描，避免扫描阶段执行目标文件顶层代码。
    - 真正注册时再按文件动态加载并校验 issubclass。
    """
    root = Path(scan_root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return []

    candidates: List[Dict[str, str]] = []
    for py_file in root.rglob("*.py"):
        try:
            src = py_file.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception:
            continue

        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {_base_name(b) for b in node.bases}
            if any(name.endswith("Session") for name in bases):
                kind = "async" if any(name.endswith("AsyncSession") for name in bases) else "sync"
                candidates.append(
                    {
                        "label": f"{py_file.name}:{node.name}",
                        "class_name": node.name,
                        "file_path": str(py_file),
                        "kind": kind,
                    }
                )

    candidates.sort(key=lambda x: (x["file_path"], x["class_name"]))
    return candidates


def load_session_class_from_file(file_path: str, class_name: str) -> Optional[type[Session] | type[AsyncSession]]:
    """按文件路径动态加载类并校验其确实继承 Session/AsyncSession。"""
    path = Path(file_path)
    if not path.exists():
        return None

    module_name = f"control_panel_dynamic_{abs(hash((str(path), class_name)))}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = getattr(module, class_name, None)

    if not isinstance(target, type):
        return None
    if not issubclass(target, (Session, AsyncSession)):
        return None
    return target


# -------------------------------
# State 初始化
# -------------------------------
def create_managers(model_config_path: str, session_db_path: str):
    """基于路径创建管理器实例。"""
    model_manager = ModelConfigManager(storage_path=model_config_path)
    session_manager = SessionManager(
        default_session_type="echo",
        db_path=session_db_path,
        max_size=100,
        idle_timeout=1800,
    )
    _register_builtin_types(session_manager)
    return model_manager, session_manager


def init_state():
    """初始化全局状态（管理器实例与默认参数）。"""
    if "model_config_path" not in st.session_state:
        st.session_state.model_config_path = str(Path.cwd() / ".satrap" / "model_config.json")
    if "session_db_path" not in st.session_state:
        st.session_state.session_db_path = str(Path.cwd() / ".satrap" / "session_config.db")

    if "scan_root" not in st.session_state:
        st.session_state.scan_root = str(Path.cwd())
    if "discovered_candidates" not in st.session_state:
        st.session_state.discovered_candidates = []

    if "model_manager" not in st.session_state or "session_manager" not in st.session_state:
        model_manager, session_manager = create_managers(
            st.session_state.model_config_path,
            st.session_state.session_db_path,
        )
        st.session_state.model_manager = model_manager
        st.session_state.session_manager = session_manager

    if "selected_model_target" not in st.session_state:
        st.session_state.selected_model_target = "llm"


def reinitialize_managers():
    """按当前路径重建管理器实例。"""
    model_manager, session_manager = create_managers(
        st.session_state.model_config_path,
        st.session_state.session_db_path,
    )
    st.session_state.model_manager = model_manager
    st.session_state.session_manager = session_manager


# -------------------------------
# Model Config 管理区
# -------------------------------
def _target_configs(manager: ModelConfigManager, target: str) -> Dict[str, Dict[str, Any]]:
    if target == "llm":
        return manager.list_llm_configs(mask_api_key=False)
    if target == "embedding":
        return manager.list_embedding_configs(mask_api_key=False)
    return manager.list_rerank_configs(mask_api_key=False)


def _set_target_config(manager: ModelConfigManager, target: str, payload: Dict[str, Any]):
    if target == "llm":
        manager.set_llm_config(LLMConfig(**payload), name=payload.get("name"))
    elif target == "embedding":
        manager.set_embedding_config(EmbeddingConfig(**payload), name=payload.get("name"))
    else:
        manager.set_rerank_config(ReRankConfig(**payload), name=payload.get("name"))


def _remove_target_config(manager: ModelConfigManager, target: str, name: str) -> bool:
    if target == "llm":
        return manager.remove_llm_config(name)
    if target == "embedding":
        return manager.remove_embedding_config(name)
    return manager.remove_rerank_config(name)


def _blank_payload(target: str, name: str) -> Dict[str, Any]:
    if target == "llm":
        return asdict(LLMConfig(name=name))
    if target == "embedding":
        return asdict(EmbeddingConfig(name=name))
    return asdict(ReRankConfig(name=name))


def render_model_config_panel(manager: ModelConfigManager):
    st.subheader("模型参数管理")

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        target = st.selectbox("配置类型", ["llm", "embedding", "rerank"], key="selected_model_target")
    with col_b:
        configs = _target_configs(manager, target)
        names = sorted(configs.keys()) if configs else ["default"]
        selected_name = st.selectbox("配置名", names)
    with col_c:
        new_name = st.text_input("新配置名", value="", placeholder="如: prod-llm")

    active = configs.get(selected_name) or _blank_payload(target, selected_name)

    st.caption("API Key 以明文存储用于运行时读取；展示时可使用脱敏视图。")

    name_val = st.text_input("name", value=str(active.get("name") or selected_name))
    model_val = st.text_input("model", value=str(active.get("model") or ""))
    base_url_val = st.text_input("base_url", value=str(active.get("base_url") or ""))
    api_key_val = st.text_input("api_key", value=str(active.get("api_key") or ""), type="password")
    lock_api_key_val = st.checkbox("lock_api_key", value=bool(active.get("lock_api_key", True)))

    extra: Dict[str, Any] = {}
    if target == "llm":
        temperature_raw = st.text_input("temperature", value="" if active.get("temperature") is None else str(active.get("temperature")))
        top_p_raw = st.text_input("top_p", value="" if active.get("top_p") is None else str(active.get("top_p")))
        max_tokens_raw = st.text_input("max_tokens", value="" if active.get("max_tokens") is None else str(active.get("max_tokens")))
        extra["temperature"] = float(temperature_raw) if temperature_raw.strip() else None
        extra["top_p"] = float(top_p_raw) if top_p_raw.strip() else None
        extra["max_tokens"] = int(max_tokens_raw) if max_tokens_raw.strip() else None
    elif target == "embedding":
        dimensions_raw = st.text_input("dimensions", value="" if active.get("dimensions") is None else str(active.get("dimensions")))
        max_batch_raw = st.text_input("max_batch_size", value="" if active.get("max_batch_size") is None else str(active.get("max_batch_size")))
        extra["dimensions"] = int(dimensions_raw) if dimensions_raw.strip() else None
        extra["max_batch_size"] = int(max_batch_raw) if max_batch_raw.strip() else None
    else:
        top_k_raw = st.text_input("top_k", value="" if active.get("top_k") is None else str(active.get("top_k")))
        min_score_raw = st.text_input("min_score", value="" if active.get("min_score") is None else str(active.get("min_score")))
        extra["top_k"] = int(top_k_raw) if top_k_raw.strip() else None
        extra["min_score"] = float(min_score_raw) if min_score_raw.strip() else None

    payload = {
        "name": name_val.strip() or selected_name,
        "model": model_val.strip() or None,
        "base_url": base_url_val.strip() or None,
        "api_key": api_key_val.strip() or None,
        "lock_api_key": lock_api_key_val,
        **extra,
    }

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("保存当前配置", use_container_width=True):
            _set_target_config(manager, target, payload)
            st.success(f"已保存 {target}:{payload['name']}")
            st.rerun()
    with c2:
        if st.button("按新名称复制", use_container_width=True) and new_name.strip():
            copy_payload = dict(payload)
            copy_payload["name"] = new_name.strip()
            _set_target_config(manager, target, copy_payload)
            st.success(f"已复制为 {target}:{new_name.strip()}")
            st.rerun()
    with c3:
        if st.button("删除当前配置", use_container_width=True):
            ok = _remove_target_config(manager, target, selected_name)
            if ok:
                st.success(f"已删除 {target}:{selected_name}")
            else:
                st.warning("删除失败或配置不存在")
            st.rerun()

    st.markdown("**运行时配置（脱敏视图）**")
    st.json(manager.get_all_configs(mask_api_key=True), expanded=False)


# -------------------------------
# Session Manager 管理区
# -------------------------------
def _pretty_ts(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _registration_options() -> Dict[str, Dict[str, str]]:
    """汇总可注册候选项（内置 + 扫描结果）。"""
    options: Dict[str, Dict[str, str]] = {}

    for name in _builtin_class_catalog().keys():
        options[f"builtin:{name}"] = {
            "source": "builtin",
            "class_name": name,
            "file_path": "",
        }

    for item in st.session_state.get("discovered_candidates", []):
        label = f"scan:{item['label']}"
        options[label] = {
            "source": "scan",
            "class_name": item["class_name"],
            "file_path": item["file_path"],
        }

    return options


def _resolve_registration_class(option_meta: Dict[str, str]) -> Optional[type[Session] | type[AsyncSession]]:
    source = option_meta.get("source")
    class_name = option_meta.get("class_name") or ""

    if source == "builtin":
        return _builtin_class_catalog().get(class_name)

    file_path = option_meta.get("file_path") or ""
    try:
        return load_session_class_from_file(file_path=file_path, class_name=class_name)
    except Exception:
        return None


def render_session_manager_panel(manager: SessionManager):
    st.subheader("Session Manager 管理")
    st.caption("支持: 扫描 Session 子类、注册会话类型、创建/更新持久化会话配置、查看活跃会话、执行清理与删除。")

    with st.expander("扫描与注册会话类型", expanded=True):
        scan_root = st.text_input("扫描目录", value=st.session_state.scan_root)
        st.session_state.scan_root = scan_root

        c_scan, c_refresh = st.columns(2)
        with c_scan:
            if st.button("扫描目录下 Session 子类", use_container_width=True):
                st.session_state.discovered_candidates = discover_session_candidates(scan_root)
                st.success(f"扫描完成，找到 {len(st.session_state.discovered_candidates)} 个候选类")
                st.rerun()
        with c_refresh:
            if st.button("清空扫描结果", use_container_width=True):
                st.session_state.discovered_candidates = []
                st.rerun()

        found_rows: List[Dict[str, Any]] = []
        for item in st.session_state.get("discovered_candidates", []):
            found_rows.append(
                {
                    "label": item["label"],
                    "kind": item["kind"],
                    "file_path": item["file_path"],
                    "class_name": item["class_name"],
                }
            )
        if found_rows:
            st.dataframe(found_rows, use_container_width=True, hide_index=True)

        reg_type = st.text_input("session_type_name", value="echo")
        options = _registration_options()
        option_labels = sorted(options.keys())
        selected_option = st.selectbox("注册对象", option_labels)

        if st.button("注册类型", use_container_width=True):
            cls = _resolve_registration_class(options[selected_option])
            if cls is None:
                st.error("注册失败：无法加载目标类，或目标类未继承 Session/AsyncSession")
            else:
                manager.register_session_type(reg_type.strip(), cls)
                st.success(f"已注册类型: {reg_type.strip()} -> {cls.__name__}")
                st.rerun()

    left, right = st.columns(2)

    with left:
        st.markdown("**创建/更新持久化会话配置**")
        available_types = manager.list_registered_session_types() or ["echo"]
        session_type = st.selectbox("会话类型", available_types)
        session_id = st.text_input("session_id（留空自动生成）", value="")
        prefix = st.text_input("demo prefix", value="echo")
        custom_json = st.text_area("额外 session_config(JSON)", value="{}", height=120)

        if st.button("创建会话配置", use_container_width=True):
            try:
                parsed = json.loads(custom_json or "{}")
                if not isinstance(parsed, dict):
                    raise ValueError("session_config 必须是 JSON object")
            except Exception as e:
                st.error(f"JSON 解析失败: {e}")
                parsed = None

            if parsed is not None:
                parsed["prefix"] = prefix
                session_cls = manager.registry.get_class(session_type)
                if session_cls is None:
                    st.error("当前 session_type 未注册，请先在上方注册类型")
                else:
                    cfg = manager.register_session(
                        session_class=session_cls,
                        session_type_name=session_type,
                        session_config=parsed,
                        session_id=session_id.strip() or None,
                    )
                    st.success(f"已创建/更新会话配置: {cfg.session_id}")
                    st.rerun()

    with right:
        st.markdown("**会话操作**")
        target_sid = st.text_input("目标 session_id", value="")
        test_msg = st.text_input("测试消息", value="hello")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("同步调用", use_container_width=True):
                if not target_sid.strip():
                    st.warning("请填写 session_id")
                else:
                    result = manager.handle_call(UserCall(session_id=target_sid.strip(), message=test_msg))
                    st.info(f"返回: {result}")
        with c2:
            if st.button("异步调用", use_container_width=True):
                if not target_sid.strip():
                    st.warning("请填写 session_id")
                else:
                    result = asyncio.run(
                        manager.handle_call_async(UserCall(session_id=target_sid.strip(), message=test_msg))
                    )
                    st.info(f"返回: {result}")

        c3, c4 = st.columns(2)
        with c3:
            if st.button("清理闲置会话", use_container_width=True):
                manager.cleanup_idle_sessions(max_idle_seconds=manager.pool.idle_timeout)
                st.success("已执行闲置清理")
                st.rerun()
        with c4:
            if st.button("删除会话(含配置)", use_container_width=True):
                if not target_sid.strip():
                    st.warning("请填写 session_id")
                else:
                    manager.remove_session(target_sid.strip(), remove_config=True)
                    st.success("已删除")
                    st.rerun()

    st.markdown("**活跃会话（内存池）**")
    active_rows: List[Dict[str, Any]] = []
    for item in manager.list_sessions():
        active_rows.append(
            {
                "session_id": item.session_id,
                "session_type": item.session_type,
                "created_at": _pretty_ts(item.created_at),
                "last_used_at": _pretty_ts(item.last_used_at),
                "message_count": item.message_count,
            }
        )
    st.dataframe(active_rows, use_container_width=True, hide_index=True)

    st.markdown("**持久化 SessionConfig（SQLite）**")
    persisted_rows: List[Dict[str, Any]] = []
    for cfg in manager.list_session_configs(limit=500):
        persisted_rows.append(
            {
                "session_id": cfg.session_id,
                "session_type_name": cfg.session_type_name,
                "created_at": _pretty_ts(cfg.created_at),
                "last_used_at": _pretty_ts(cfg.last_used_at),
                "message_count": cfg.message_count,
                "session_config": cfg.session_config,
            }
        )
    st.dataframe(persisted_rows, use_container_width=True, hide_index=True)


def render_sidebar():
    st.subheader("存储路径")
    model_path = st.text_input("Model Config 路径", value=st.session_state.model_config_path)
    session_db = st.text_input("Session DB 路径", value=st.session_state.session_db_path)

    if st.button("应用路径并重载管理器", use_container_width=True):
        st.session_state.model_config_path = model_path.strip() or st.session_state.model_config_path
        st.session_state.session_db_path = session_db.strip() or st.session_state.session_db_path
        reinitialize_managers()
        st.success("路径已应用，管理器已重载")
        st.rerun()

    st.caption(f"当前 Model Config: `{st.session_state.model_config_path}`")
    st.caption(f"当前 Session DB: `{st.session_state.session_db_path}`")


def main():
    st.set_page_config(page_title="Satrap Control Panel", layout="wide")
    st.title("Satrap Control Panel")

    init_state()

    with st.sidebar:
        render_sidebar()

    tabs = st.tabs(["模型参数", "会话管理"])

    with tabs[0]:
        render_model_config_panel(st.session_state.model_manager)

    with tabs[1]:
        render_session_manager_panel(st.session_state.session_manager)


if __name__ == "__main__":
    main()
