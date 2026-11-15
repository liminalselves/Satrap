"""Edictum 冷配置共享领域服务"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast


from satrap.core.log import logger
from satrap.edictum.plugin.settings import validate_plugin_settings
from satrap.edictum.plugin.catalog import PluginCatalog
from satrap.edictum.plugin.config import PluginConfigManager, validate_config_values
from satrap.edictum.registry import EdictumTypeRegistry
from satrap.edictum.config import EdictumConfigManager


_ALLOWED_FIELDS = {
    "name",
    "edictum_type",
    "enabled",
    "description",
    "model_name",
    "params",
    "plugins",
}
"""Edictum 冷配置允许写入的字段"""

from satrap.core.config.reference_guard import guard_agent_reference


def config_revision(config: dict[str, Any]) -> str:
    """
    计算命名配置的修订值, 与 EdictumConfigManager.update 的 expected_revision 校验口径一致

    参数:
    - config: get_config 返回的公开配置 (不含 _updated_at)

    返回:
    - str: 与字段顺序无关的 sha256 摘要
    """
    from satrap.core.config.document import config_document_revision

    return config_document_revision(config)


def split_expected_revision(payload: object) -> tuple[object, str | None]:
    """
    从更新请求体中取出 expected_revision, 其余字段原样交给配置校验

    参数:
    - payload: 原始请求体

    返回:
    - tuple[object, str | None]: 去掉修订字段后的请求体与期望修订; 未提供时为 None
    """
    if not isinstance(payload, dict) or "expected_revision" not in payload:
        return payload, None
    body = dict(cast(dict[str, Any], payload))
    expected = body.pop("expected_revision")
    if expected is None:
        return body, None
    if not isinstance(expected, str) or not expected.strip():
        raise ValueError("expected_revision 必须是非空字符串")
    return body, expected


class EdictumConfigService:
    """供平台后端和控制服务共用的 Edictum 冷配置服务"""

    def __init__(
        self,
        manager: EdictumConfigManager,
        type_registry: EdictumTypeRegistry,
        *, models: Any = None, rag: Any = None,
        reference_checker: Callable[[str], list[dict[str, str]]] | None = None,
        adapter_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        """
        初始化 Edictum 冷配置服务

        参数:
        - manager: Edictum 冷配置管理器
        - type_registry: Edictum 类型注册表
        """
        self.manager = manager
        self.type_registry = type_registry
        self.plugin_catalog = PluginCatalog()
        self.models = models
        self.rag = rag
        self.reference_checker = reference_checker
        self.adapter_resolver = adapter_resolver

    def _guard_reference(self, name: str) -> None:
        """
        命名配置仍被 Agent 路由绑定时拒绝删除或重命名

        参数:
        - name: 待变更的配置名称
        """

        guard_agent_reference("edictum", name, self.reference_checker)

    def list_types(self) -> list[dict[str, Any]]:
        """
        列出可创建的 Edictum 类型, flow 类型附带节点注册表与端口类型表

        返回:
        - list[dict[str, Any]]: 类型能力与配置元数据; flow 项包含
          node_types (内置+插件), port_types, defaults, models
        """
        types = self.type_registry.list_types()
        for item in types:
            if item.get("name") == "flow":
                item["flow"] = self._flow_type_metadata()
        return types

    def _flow_type_metadata(self) -> dict[str, Any]:
        """
        组装 flow 类型的前端元数据: 节点类型, 端口类型表, 各层默认值, 模型能力

        返回:
        - dict[str, Any]: 编辑器需要的完整契约
        """
        from satrap.edictum.flow.registry import FlowNodeRegistry
        from satrap.core.type.flow.ports import DATA_PORT_TYPES, PORT_TYPE_LABELS, SIGNAL_PORT_TYPES

        registry = FlowNodeRegistry()
        plugin_catalog = {e.name: e for e in self.plugin_catalog.scan()}
        for entry in plugin_catalog.values():
            if entry.flow_nodes:
                registry.register_plugin_nodes(entry.name, list(entry.flow_nodes))
        node_types = [t.to_payload() for t in registry.all_types()]
        port_types = {
            t: {"label": PORT_TYPE_LABELS[t], "signal": t in SIGNAL_PORT_TYPES}
            for t in DATA_PORT_TYPES
        }
        defaults = {
            "flow": {},   # 流程默认, 编辑器初始为空
            "plugin": {   # 插件全局默认 (P0-7 接口读写, 当前返回当前值)
                name: {node.type.rsplit(".", 1)[-1]: {} for node in entry.flow_nodes}
                for name, entry in plugin_catalog.items() if entry.flow_nodes
            },
        }
        models = []
        if self.models is not None:
            try:
                configs = self.models.list_llm_configs(mask_api_key=True)
                for name, cfg in sorted(configs.items()):
                    models.append({
                        "name": name,
                        "ctx": int(cfg.get("context_window") or 0),
                        "tools": True,   # LLM 配置没有工具能力开关, 工具调用一律视为可用
                    })
            except Exception as error:
                logger.warning(f"[Edictum 类型] 读取模型列表失败, 流程编辑器模型选项为空: {error}")
                models = []
        return {
            "node_types": node_types,
            "port_types": port_types,
            "defaults": defaults,
            "models": models,
        }

    def validate_flow(self, payload: object) -> dict[str, Any]:
        """
        只校验不保存: 解析 + 迁移 + 静态检查, 返回问题列表

        参数:
        - payload: {flow: params.flow, conversation_kinds?: ["group","private"], platform_id?: 平台实例}

        返回:
        - dict[str, Any]: {"ok": bool, "issues": [...]}
        """
        if not isinstance(payload, dict):
            raise ValueError("校验请求必须是对象")
        body = dict(cast(dict[str, Any], payload))
        raw_flow = body.get("flow")
        if raw_flow is None:
            raise ValueError("缺少 flow 字段")
        kinds = body.get("conversation_kinds")
        if kinds is not None and not (isinstance(kinds, list) and all(k in ("group", "private") for k in kinds)):
            raise ValueError("conversation_kinds 只能是 group/private 列表")
        platform_id = body.get("platform_id")
        if platform_id is not None and (not isinstance(platform_id, str) or not platform_id.strip()):
            raise ValueError("platform_id 必须是非空字符串")
        adapter = self.adapter_resolver(platform_id.strip()) if platform_id and self.adapter_resolver else None
        group = getattr(adapter, "supports_scheduled_group_send", None)
        private = getattr(adapter, "supports_scheduled_private_send", None)
        from satrap.edictum.flow.definition import migrate_flow_definition, parse_flow_definition
        from satrap.edictum.flow.registry import FlowNodeRegistry
        from satrap.edictum.flow.validate import validate

        registry = FlowNodeRegistry()
        plugin_catalog = {e.name: e for e in self.plugin_catalog.scan()}
        for entry in plugin_catalog.values():
            if entry.flow_nodes:
                registry.register_plugin_nodes(entry.name, list(entry.flow_nodes))
        try:
            definition, _, errors = migrate_flow_definition(parse_flow_definition(raw_flow), registry)
        except Exception as error:
            return {"ok": False, "issues": [{"code": "flow_parse_error", "level": "error", "category": "结构", "message": str(error)}]}
        plugin_config = PluginConfigManager()
        execution_enabled = {name: plugin_config.host_state(name)["execution_enabled"] for name in plugin_catalog}
        issues = [i.to_payload() for i in validate(
            definition, registry, plugin_catalog=plugin_catalog,
            plugin_execution_enabled=execution_enabled,
            conversation_kinds=[str(k) for k in kinds] if kinds else None,
            scheduled_send_group=group if type(group) is bool else None,
            scheduled_send_private=private if type(private) is bool else None,
        )]
        issues = [{"code": e.code, "level": "error", "category": "结构", "message": str(e), "node": e.node, "field": e.field} for e in errors] + issues
        return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues}

    def list_configs(self) -> dict[str, dict[str, Any]]:
        """
        列出全部 Edictum 冷配置

        返回:
        - dict[str, dict[str, Any]]: 按名称组织的配置
        """
        return self.manager.list_configs()

    def list_with_metadata(self) -> dict[str, dict[str, Any]]:
        """
        为只读目录提供更新时间, 不改变内部配置或 CAS 正文

        返回:
        - 命名配置及实际保存时间, 旧记录的时间为空
        """
        return {
            name: {**config, "updated_at": self.manager.config_updated_at(name), "revision": config_revision(config)}
            for name, config in self.manager.list_configs().items()
        }

    def list_plugins(self) -> list[dict[str, Any]]:
        """
        扫描可供 Edictum 会话使用的插件

        返回:
        - list[dict[str, Any]]: 插件元数据, 配置结构和能力清单
        """
        return self.plugin_catalog.list_payloads()

    def get(self, name: str) -> dict[str, Any] | None:
        """
        获取单个 Edictum 冷配置

        参数:
        - name: 配置名称

        返回:
        - dict[str, Any] | None: 配置不存在时返回 None
        """
        return self.manager.get_config(name)

    def create(self, payload: object) -> dict[str, Any]:
        """
        创建命名 Edictum 冷配置

        参数:
        - payload: 包含 name 和 edictum_type 的配置对象

        返回:
        - dict[str, Any]: 已创建的完整配置
        """
        cleaned = self._validate_payload(payload)
        name = str(cleaned.pop("name", "")).strip()
        if not name:
            raise ValueError("Edictum 配置名称不能为空")
        if not str(cleaned.get("edictum_type", "")).strip():
            raise ValueError("edictum_type 不能为空")
        self._validate_plugin_values(cleaned.get("plugins", []))
        return self.manager.create(name, cleaned)

    def update(self, name: str, payload: object, *, expected_revision: str | None = None) -> tuple[str, dict[str, Any]]:
        """
        更新并按需重命名 Edictum 冷配置

        参数:
        - name: 当前配置名称
        - payload: 待更新字段
        - expected_revision: 可选的完整配置版本, 用于拒绝并发覆盖

        返回:
        - tuple[str, dict[str, Any]]: 最终名称和完整配置
        """
        cleaned = self._validate_payload(payload)
        if "plugins" in cleaned:
            self._validate_plugin_values(cleaned["plugins"])
        new_name = str(cleaned.pop("name")).strip() if "name" in cleaned else None
        from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

        with REFERENCE_SCAN_LOCK:
            if new_name is not None and new_name != name:
                self._guard_reference(name)
            return self.manager.update(name, cleaned, new_name=new_name, expected_revision=expected_revision)

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """
        设置 Edictum 冷配置启用状态

        参数:
        - name: 配置名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 更新后的完整配置
        """
        return self.manager.set_enabled(name, enabled)

    def delete(self, name: str) -> bool:
        """
        删除 Edictum 冷配置

        参数:
        - name: 配置名称

        返回:
        - bool: 是否找到并删除配置
        """
        from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

        with REFERENCE_SCAN_LOCK:
            self._guard_reference(name)
            return self.manager.delete(name)

    def _validate_plugin_values(self, plugins: list[Any]) -> None:
        """命名配置保存前校验显式参数及继承后的模型和知识库引用"""
        manager = PluginConfigManager()
        for item in plugins:
            values: dict[str, Any] = {}
            if isinstance(item, str):
                name = item
            elif isinstance(item, dict):
                definition = cast(dict[str, Any], item)
                name = definition.get("name", "")
                values = definition.get("config", {})
            else:
                continue
            entry = self.plugin_catalog.get(name)
            if entry is None:
                continue
            values = validate_config_values(entry.config_schema, values)
            effective = manager.resolve(name, entry.config_schema, values)
            validate_plugin_settings(name, entry.config_schema, effective, models=self.models, rag=self.rag)

    @staticmethod
    def _validate_payload(payload: object) -> dict[str, Any]:
        """
        校验 API 配置对象和字段类型

        参数:
        - payload: 原始请求体

        返回:
        - dict[str, Any]: 允许传给管理器的字段
        """
        if not isinstance(payload, dict):
            raise ValueError("Edictum 配置必须是对象")
        raw = dict(cast(dict[str, Any], payload))
        unknown = set(raw) - _ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"未知 Edictum 配置字段: {', '.join(sorted(unknown))}")
        if "enabled" in raw and not isinstance(raw["enabled"], bool):
            raise ValueError("enabled 必须是布尔值")
        if "params" in raw and not isinstance(raw["params"], dict):
            raise ValueError("params 必须是对象")
        if "plugins" in raw and not isinstance(raw["plugins"], list):
            raise ValueError("plugins 必须是数组")
        for key in ("name", "edictum_type", "description", "model_name"):
            if key in raw and not isinstance(raw[key], str):
                raise ValueError(f"{key} 必须是字符串")
        return raw
