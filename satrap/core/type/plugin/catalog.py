"""插件目录条目数据契约。"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from satrap.core.type.flow.registry import FlowNodeType
from satrap.core.type.flow.workflow import FlowWorkflowDecl
from satrap.core.type.plugin.config import ConfigField, schema_to_payload
from satrap.core.type.plugin.compatibility import PluginEnvironment
from satrap.core.type.plugin.permissions import EMPTY_PERMISSIONS, PluginPermissions
from satrap.core.type.plugin_interop import CAPABILITY_KINDS

def _empty_flow_workflow() -> FlowWorkflowDecl:
    """为每个目录条目构造独立的默认流程声明。"""
    return FlowWorkflowDecl()


@dataclass(frozen=True)
class PluginCatalogEntry:
    """一个可用插件的稳定目录条目"""

    name: str
    """来自元数据的稳定插件名, 用作目录索引和安装标识"""
    path: Path
    """发现该插件的目录路径, 用于加载插件代码和资源"""
    version: str = ""
    """元数据中的插件版本, 未声明时为空串"""
    author: str = ""
    """元数据中的作者信息, 未声明时为空串"""
    description: str = ""
    """供目录展示的插件用途说明, 未声明时为空串"""
    config_schema: dict[str, ConfigField] = field(default_factory=dict[str, ConfigField])
    """从元数据解析的配置项定义, 空映射表示没有声明配置项"""
    capabilities: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    """按能力类别和名称索引的展示说明, 空映射表示没有能力描述"""
    compatibility: dict[str, Any] = field(default_factory=dict[str, Any])
    """经校验的框架版本约束, 空映射表示未声明约束"""
    applicability: dict[str, Any] = field(default_factory=dict[str, Any])
    """经校验的适用会话和平台范围, 空映射表示未声明限制"""
    permissions: PluginPermissions = field(default_factory=lambda: EMPTY_PERMISSIONS)
    """已解析的管理权限声明, 默认空声明保持旧插件授权行为"""
    config_version: int = 0
    """插件元数据声明的配置版本, 缺省为 0, 用于配置迁移"""
    interop: dict[str, object] = field(default_factory=dict)
    """导出, 依赖, 替代和冲突关系的普通数据声明, 默认无交互声明"""
    display_name: str = ""
    """界面展示名, 空串表示元数据未提供"""
    icon: str = ""
    """元数据提供的界面图标, 空串表示未提供"""
    license: str = ""
    """元数据提供的许可证信息, 空串表示未提供"""
    homepage: str = ""
    """元数据提供的项目主页地址, 空串表示未提供"""
    repository: str = ""
    """元数据提供的源码仓库地址, 空串表示未提供"""
    flow_nodes: tuple[FlowNodeType, ...] = ()
    """插件提供的流程节点契约, 解析失败即视为元数据损坏"""
    flow_workflow: FlowWorkflowDecl = field(default_factory=lambda: _empty_flow_workflow())
    """装到 WF 节点上时的限制与能力声明"""

    def check_environment(self, environment: PluginEnvironment):
        """根据指定环境计算适用性"""
        from satrap.edictum.plugin.compatibility import check_plugin_compatibility
        return check_plugin_compatibility({"compatibility": self.compatibility, "applicability": self.applicability}, environment)

    def to_payload(self) -> dict[str, Any]:
        """转换为前端可使用的插件元数据"""
        return {
            **{key: getattr(self, key) for key in ("display_name", "icon", "license", "homepage", "repository") if getattr(self, key)},
            **self.permissions.to_payload(),
            "name": self.name,
            "interop": deepcopy(self.interop),
            "config_version": self.config_version,
            "version": self.version,
            "compatibility": dict(self.compatibility),
            "applicability": dict(self.applicability),
            "author": self.author,
            "description": self.description,
            "config_schema": schema_to_payload(self.config_schema),
            "capabilities": {
                kind: dict(self.capabilities.get(kind, {}))
                for kind in CAPABILITY_KINDS
            },
            "flow_nodes": [node.to_payload() for node in self.flow_nodes],
            "flow_workflow": self.flow_workflow.to_payload(),
        }
