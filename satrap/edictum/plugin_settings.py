"""插件配置域适配器: 复用通用会话覆盖服务, 合并全局和命名配置"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from satrap.core.config.session_overrides import SessionOverrideService, SessionOverrideStore
from satrap.edictum.plugin_resources import model_reference_fingerprint, MODEL_TYPES, named_model_config
from satrap.edictum.plugin_config import ConfigField, PluginConfigManager, schema_to_payload, validate_config_values


class EffectivePluginConfig(dict[str, Any]):
    """运行协调器已合成的配置快照, 回滚时不能再次读取最新覆盖"""


class PluginSettingsService:
    """Chat 与平台插件共用的持久化配置入口"""

    def __init__(self, database: str | Path, manager: PluginConfigManager | None = None, *, models: Any = None, rag: Any = None) -> None:
        self.manager = manager or PluginConfigManager()
        self.models = models
        self.rag = rag
        self.overrides = SessionOverrideService(SessionOverrideStore(database))

    def _register(self, name: str, schema: dict[str, ConfigField]) -> str:
        namespace = f"plugins.{name}"
        self.overrides.register(namespace, lambda values: validate_config_values(schema, values, session_override=True))
        return namespace

    def get(
        self, session_id: str, name: str, schema: dict[str, ConfigField], named: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """返回显式覆盖, 生效配置, 上层配置和逐字段来源"""
        namespace = self._register(name, schema)
        global_values = self.manager.load_global_explicit(name, schema)
        layers = [("default", {key: item.default for key, item in schema.items()}), ("global", global_values)]
        if named:
            layers.append(("named", validate_config_values(schema, named)))
        result = self.overrides.resolve(session_id, namespace, layers)
        inherited: dict[str, Any] = {}
        inherited_sources: dict[str, str] = {}
        for source, values in layers:
            inherited.update(values)
            inherited_sources.update({key: source for key in values})
        return {**result, "inherited": inherited, "inherited_sources": inherited_sources, "schema": schema_to_payload(schema)}

    def save(
        self, session_id: str, name: str, schema: dict[str, ConfigField], values: dict[str, Any], *,
        expected_revision: int, named: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        namespace = self._register(name, schema)
        cleaned = validate_config_values(schema, values, session_override=True)
        inherited = self.get(session_id, name, schema, named)["inherited"]
        validate_plugin_settings(name, schema, {**inherited, **cleaned}, models=self.models, rag=self.rag)
        self.overrides.save(session_id, namespace, values, expected_revision=expected_revision)
        return self.get(session_id, name, schema, named)


def validate_model_values(models: Any, schema: dict[str, ConfigField], values: dict[str, Any]) -> None:
    """保存时校验显式模型引用, 允许未启用插件暂存空字段"""
    if not isinstance(values, dict):
        raise ValueError("插件配置必须是对象")
    for key, value in values.items():
        definition = schema.get(key)
        if definition is not None and definition.type in MODEL_TYPES and value:
            named_model_config(models, definition.type, value)


def validate_plugin_settings(name: str, schema: dict[str, ConfigField], values: dict[str, Any], *, models: Any = None, rag: Any = None) -> None:
    """所有入口共用字段, 模型引用与领域关联校验"""
    checked = {
        key: value for key, value in values.items()
        if not (value is None and key in schema and schema[key].default is None and not schema[key].nullable)
    }
    cleaned = validate_config_values(schema, checked)
    if models is not None:
        validate_model_values(models, schema, cleaned)
    if name == "rag":
        from satrap.core.rag import validate_rag_config
        # RAG 会加载可选的 FAISS 依赖, 仅在使用知识库功能时导入
        validate_rag_config(cleaned)
        if rag is not None:
            rag.validate_references(cleaned)


def model_options(models: Any) -> dict[str, list[dict[str, str]]]:
    """选择器只返回名称和模型标识, 不返回连接地址与凭据"""
    return {
        kind: [{"value": name, "label": name, "model": str(value.get("model") or "")}
               for name, value in getattr(models, f"list_{target}_configs")(mask_api_key=True).items()]
        for kind, target in {"llm": "llm", "embed": "embedding", "rerank": "rerank"}.items()
    }


def resolve_session_plugin_config(
    session: Any, name: str, schema: dict[str, ConfigField], base: dict[str, Any],
) -> dict[str, Any]:
    """在调用方合并结果之上应用数据库覆盖, 独立会话可显式注入存储"""
    store = getattr(session, "plugin_override_store", None)
    if store is None:
        return dict(base)
    values = store.read(session.session_id, f"plugins.{name}")["overrides"]
    return {**base, **validate_config_values(schema, values, session_override=True)}


def resolve_runtime_specs(session: Any, specs: list[Any], catalog: Any) -> list[Any]:
    """把全局, 命名和会话配置合成为可比较的运行目标"""
    manager = PluginConfigManager()
    models = getattr(session, "plugin_model_manager", None)
    resolved: list[Any] = []
    for spec in specs:
        entry = catalog.get(spec.name)
        if entry is None:
            resolved.append(spec)
            continue
        config = manager.resolve(spec.name, entry.config_schema, spec.config)
        config = resolve_session_plugin_config(session, spec.name, entry.config_schema, config)
        revision = model_reference_fingerprint(models, entry.config_schema, config) if models is not None else ""
        resolved.append(replace(spec, config=config, resources_revision=revision, config_resolved=True))
    return resolved
