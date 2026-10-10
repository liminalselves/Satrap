"""插件配置域适配器: 复用通用会话覆盖服务, 合并全局和命名配置"""
from __future__ import annotations
from satrap.edictum.plugin_compatibility import PluginEnvironment

from dataclasses import replace
from pathlib import Path
from typing import Any

from satrap.core.config.session_overrides import SessionOverrideService, SessionOverrideStore
from satrap.edictum.plugin_resources import model_reference_fingerprint, MODEL_TYPES, named_model_config
from satrap.edictum.plugin_config import ConfigField, PluginConfigManager, schema_to_payload, validate_config_values

from satrap.edictum.plugin_spec import parse_plugin_specs


class PluginInstallConfig(dict[str, Any]):
    """安装参数携带首次技能状态, 不混入插件业务配置字段"""

    def __init__(self, values: dict[str, Any], *, initial_skills: dict[str, bool] | None = None):
        """
        构建兼容字典的安装参数

        参数:
        - values: 插件业务配置
        - initial_skills: 首次安装的技能独立开关, 默认 None 使用启用状态
        """
        super().__init__(values)
        self.initial_skills = dict(initial_skills or {})


class EffectivePluginConfig(PluginInstallConfig):
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
        from satrap.edictum.friend_migration import FRIEND_SWITCHES
        self.overrides.register(namespace, lambda values: validate_config_values(schema, {
            key: value for key, value in values.items() if name != "friend_manager" or key not in FRIEND_SWITCHES
        }, session_override=True))
        return namespace

    def get(
        self, session_id: str, name: str, schema: dict[str, ConfigField], named: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """返回显式覆盖, 生效配置, 上层配置和逐字段来源"""
        namespace = self._register(name, schema)
        if name in {"base_take", "memory"}:
            from satrap.edictum.memory_migration import migrate_memory_overrides
            migrate_memory_overrides(self.overrides.store, session_id)
        global_values = self.manager.load_global_explicit(name, schema)
        layers = [("default", {key: item.default for key, item in schema.items()}), ("global", global_values)]
        if named:
            layers.append(("named", validate_config_values(schema, named)))
        result = self.overrides.resolve(session_id, namespace, layers)
        if name == "friend_manager":
            from satrap.edictum.friend_migration import FRIEND_SWITCHES
            result["overrides"] = {key: value for key, value in result["overrides"].items() if key not in FRIEND_SWITCHES}
        inherited: dict[str, Any] = {}
        inherited_sources: dict[str, str] = {}
        for source, values in layers:
            inherited.update(values)
            inherited_sources.update({key: source for key in values})
        return {**result, "inherited": inherited, "inherited_sources": inherited_sources, "schema": schema_to_payload(schema)}

    def save(
        self, session_id: str, name: str, schema: dict[str, ConfigField], values: dict[str, Any], *,
        expected_revision: int, named: dict[str, Any] | None = None,
        tool_overrides: dict[str, bool] | None = None, expected_tool_revision: int | None = None,
    ) -> dict[str, Any]:
        """
        校验并保存实例参数和可选工具覆盖, 关联修订冲突时整体回滚

        参数:
        - session_id: 当前会话 ID
        - name: 插件名称
        - schema: 插件配置字段定义
        - values: 实例显式参数, 删除字段即恢复继承
        - expected_revision: 参数覆盖的当前修订号
        - named: 可选的上层命名配置, 默认 None
        - tool_overrides: 可选的好友工具显式状态, 默认 None 保持原工具覆盖
        - expected_tool_revision: 携带工具覆盖时必填的当前工具修订号

        返回:
        - 保存后的参数, 来源和修订号; 校验或冲突失败时抛出异常供 API 捕获
        """
        namespace = self._register(name, schema)
        cleaned = validate_config_values(schema, values, session_override=True)
        inherited = self.get(session_id, name, schema, named)["inherited"]
        validate_plugin_settings(name, schema, {**inherited, **cleaned}, models=self.models, rag=self.rag)
        if tool_overrides is not None:
            from satrap.edictum.friend_migration import FRIEND_SWITCHES
            if name != "friend_manager" or not isinstance(tool_overrides, dict) or set(tool_overrides) - set(FRIEND_SWITCHES.values()) or any(type(state) is not bool for state in tool_overrides.values()) or type(expected_tool_revision) is not int or expected_tool_revision < 0:
                raise ValueError("实例工具覆盖或修订号无效")
            self.overrides.store.replace_many(session_id, {namespace: (cleaned, expected_revision),
                                               "plugin_capabilities.friend_manager": (tool_overrides, expected_tool_revision)})
        else:
            self.overrides.save(session_id, namespace, values, expected_revision=expected_revision)
        return self.get(session_id, name, schema, named)

    def tool_settings(self, session_id: str, spec: Any, entry: Any) -> dict[str, Any]:
        """
        在现有实例设置中展示迁移后的工具覆盖, 保留修改和恢复继承入口

        参数:
        - session_id: 当前会话 ID
        - spec: 当前上层安装规格
        - entry: 插件目录条目

        返回:
        - 好友写工具的覆盖, 上层状态和独立修订号, 其它插件返回空对象
        """
        if spec.name != "friend_manager":
            return {}
        from satrap.edictum.friend_migration import FRIEND_SWITCHES, migrate_friend_overrides
        migrate_friend_overrides(self.overrides.store, session_id, spec)
        record = self.overrides.store.read(session_id, "plugin_capabilities.friend_manager")
        return {"tool_overrides": record["overrides"], "tool_revision": record["revision"],
                "inherited_tools": {tool: spec.capabilities.get("tools", {}).get(tool, True) for tool in FRIEND_SWITCHES.values()},
                "tool_descriptions": {tool: entry.capabilities.get("tools", {}).get(tool, tool) for tool in FRIEND_SWITCHES.values()}}


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
        for kind, target in MODEL_TYPES.items()
    }


def resolve_session_plugin_config(
    session: Any, name: str, schema: dict[str, ConfigField], base: dict[str, Any],
) -> dict[str, Any]:
    """在调用方合并结果之上应用数据库覆盖, 独立会话可显式注入存储"""
    store = getattr(session, "plugin_override_store", None)
    if store is None:
        return dict(base)
    if name in {"base_take", "memory"}:
        from satrap.edictum.memory_migration import migrate_memory_overrides
        migrate_memory_overrides(store, session.session_id)
    values = store.read(session.session_id, f"plugins.{name}")["overrides"]
    if name == "friend_manager":
        from satrap.edictum.friend_migration import FRIEND_SWITCHES
        values = {key: value for key, value in values.items() if key not in FRIEND_SWITCHES}
    return {**base, **validate_config_values(schema, values, session_override=True)}


def resolve_runtime_specs(session: Any, specs: list[Any], catalog: Any) -> list[Any]:
    """把全局, 命名和会话配置合成为可比较的运行目标"""
    manager = PluginConfigManager()
    store = getattr(session, "plugin_override_store", None)
    if store is not None and any(spec.name == "base_take" for spec in specs):
        from satrap.edictum.memory_migration import migrate_memory_overrides
        if migrate_memory_overrides(store, session.session_id) and not any(spec.name == "memory" for spec in specs):
            specs = [*specs, *parse_plugin_specs(["memory"], catalog, require_available=True)]
    models = getattr(session, "plugin_model_manager", None)
    resolved: list[Any] = []
    for spec in specs:
        entry = catalog.get(spec.name)
        if entry is None:
            resolved.append(spec)
            continue
        if spec.name == "friend_manager" and store is not None:
            from satrap.edictum.friend_migration import migrate_friend_overrides
            tool_overrides = migrate_friend_overrides(store, session.session_id, spec)
            spec = replace(spec, capabilities={**spec.capabilities, "tools": {**spec.capabilities.get("tools", {}), **tool_overrides}})
        config = manager.resolve(spec.name, entry.config_schema, spec.config)
        config = resolve_session_plugin_config(session, spec.name, entry.config_schema, config)
        revision = model_reference_fingerprint(models, entry.config_schema, config) if models is not None else ""
        resolved.append(replace(spec, config=config, resources_revision=revision, config_resolved=True,
                                availability=entry.check_environment(getattr(session, "plugin_environment", PluginEnvironment()))))
    return resolved
