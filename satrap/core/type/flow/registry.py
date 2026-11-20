"""流程节点注册契约。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from satrap.core.type.flow.ports import EventContextKind, PORT_TYPE_LABELS, PortType
from satrap.core.type.plugin.config import ConfigField, schema_to_payload

NodeRole = Literal["source", "transform", "workflow", "state", "sink", "trigger", "condition", "logic", "note"]
"""节点角色, 与前端 registry.ts 对齐; 插件节点只会是 transform 或 condition"""

EventContext = EventContextKind
"""触发事件携带的上下文类型"""


@dataclass(frozen=True)
class PortSpec:
    """一个输入或输出端口的声明"""

    name: str
    """节点内唯一的端口名, 连线和节点函数参数据此匹配"""
    types: tuple[PortType, ...]
    """该端口接受或输出的类型集合, 用于连线检查和推断显示名"""
    required: bool = False
    """输入是否必须连线, 默认 False, 未连线的可选输入传入 None"""
    multiple: bool = False
    """是否允许多条输入连线, 默认 False, 多值按存档连线顺序传入"""
    label: str = ""
    """面板显示名, 默认空时序列化按首个端口类型推断"""
    side: str = ""
    """面板方位, 缺省为输入在上, 输出在下; 信号口为左进右出"""
    derived: bool = False
    """逻辑门输出: 有触发输入时输出触发, 否则输出条件"""

    def to_payload(self) -> dict[str, Any]:
        """序列化给前端面板的端口描述"""
        return {
            "name": self.name,
            "label": self.label or PORT_TYPE_LABELS.get(self.types[0], self.name),
            "types": list(self.types),
            "required": self.required,
            "multiple": self.multiple,
            **({"side": self.side} if self.side else {}),
            **({"derived": True} if self.derived else {}),
        }


@dataclass(frozen=True)
class RenamedEntry:
    """一个契约版本的改名记录"""

    fields: dict[str, str] = field(default_factory=dict)
    """配置字段改名 {旧名: 新名}"""
    ports: dict[str, str] = field(default_factory=dict)
    """端口改名 {旧名: 新名}"""


@dataclass(frozen=True)
class FlowNodeType:
    """一个节点类型的完整契约"""

    type: str
    """完整类型名: 内置节点不带点, 插件节点为 插件名.节点名"""
    version: int
    """当前节点契约版本, 用于判断存档配置和端口是否需要迁移"""
    label: str
    """节点面板和画布卡片展示的标题"""
    description: str
    """节点用途说明, 供面板提示和属性编辑展示"""
    role: NodeRole
    """节点在流程图中的职责, 决定静态分析和调度方式"""
    inputs: tuple[PortSpec, ...] = ()
    """可连接的输入端口声明, 默认空元组表示没有输入"""
    outputs: tuple[PortSpec, ...] = ()
    """可连接的输出端口声明, 默认空元组表示没有输出"""
    config_schema: dict[str, ConfigField] = field(default_factory=dict)
    """节点配置字段, 键为字段名"""
    category: str = ""
    """面板分组, 缺省按角色推断"""
    max_per_flow: int | None = None
    """单个流程允许的该类型实例数上限, 默认 None 表示不设上限"""
    passthrough: dict[str, str | None] | None = None
    """启用条件为假时每个输出取哪个输入 {输出口: 输入口 或 None}; 宿主按必填输入推断"""
    needs: EventContext | None = None
    """仅条件节点: 求值需要的上下文"""
    emits: EventContext | None = None
    """仅触发磁贴: 产生的上下文"""
    calls_model: bool = False
    """配置里有模型字段, 视为会调用插件模型"""
    renamed: dict[int, RenamedEntry] = field(default_factory=dict)
    """每个版本的改名记录, 键为升到哪个版本"""
    reading: dict[str, str] = field(default_factory=dict)
    """连线上的读取说明 {输入端口名: 模板}, 模板可用 {字段名}"""
    plugin: str = ""
    """来源插件名, 内置节点为空"""

    def input_port(self, name: str) -> PortSpec | None:
        """按名查输入端口"""
        return next((p for p in self.inputs if p.name == name), None)

    def output_port(self, name: str) -> PortSpec | None:
        """按名查输出端口"""
        return next((p for p in self.outputs if p.name == name), None)

    def to_payload(self) -> dict[str, Any]:
        """序列化给前端面板的节点类型描述"""
        return {
            "type": self.type,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "role": self.role,
            "category": self.category,
            "inputs": [p.to_payload() for p in self.inputs],
            "outputs": [p.to_payload() for p in self.outputs],
            "schema": schema_to_payload(self.config_schema),
            **({"maxPerFlow": self.max_per_flow} if self.max_per_flow is not None else {}),
            **({"callsModel": True} if self.calls_model else {}),
            **({"needs": self.needs} if self.needs else {}),
            **({"emits": self.emits} if self.emits else {}),
            **({"plugin": self.plugin} if self.plugin else {}),
            **({"reading": dict(self.reading)} if self.reading else {}),
        }
