"""流程定义数据契约和公开异常。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal

class FlowDefinitionError(ValueError):
    """流程定义解析或迁移失败"""

    def __init__(self, code: str, message: str, *, node: str = "", field: str = "") -> None:
        """
        记录错误码与定位

        参数:
        - code: flow_* 错误码
        - message: 可读失败原因
        - node: 相关节点 id, 默认空
        - field: 相关字段名, 默认空
        """
        super().__init__(message)
        self.code = code
        self.node = node
        self.field = field


@dataclass(frozen=True)
class FlowBudget:
    """流程级模型调用硬限制"""

    max_calls: int
    """滚动窗口内允许的模型调用上限, 解析时必须为正整数"""
    window_minutes: int
    """预算统计窗口长度, 单位为分钟, 存档未提供时解析为 60"""
    count: Literal["all", "wf"] = "all"
    """all: WF 和插件模型调用都计; wf: 只算 WF"""


@dataclass(frozen=True)
class FlowNodeInstance:
    """存档中的一个节点实例"""

    id: str
    """流程内唯一的节点实例标识, 连线和布局通过它引用"""
    type: str
    """完整类型名; 内置不带点, 插件为 插件名.节点名"""
    version: int
    """保存时的节点契约版本; 低于当前版本时读取侧迁移"""
    title: str
    """画布上的节点标题, 存档未提供或为空时使用节点 id"""
    config: dict[str, Any]
    """只保存与上层不同的字段"""


@dataclass(frozen=True)
class FlowEdge:
    """存档中的一条连线"""

    source: str
    """连线起点的节点实例 id, 从存档 from 端点拆出"""
    source_port: str
    """起点节点的输出端口名, 随节点契约版本迁移"""
    target: str
    """连线终点的节点实例 id, 从存档 to 端点拆出"""
    target_port: str
    """终点节点的输入端口名, 随节点契约版本迁移"""
    label: str = ""
    """用户注释, 可选"""

    @property
    def key(self) -> str:
        """边标识, 供问题列表和诊断引用"""
        return f"{self.source}.{self.source_port}->{self.target}.{self.target_port}"


@dataclass(frozen=True)
class FlowDefaultsEntry:
    """一类节点的流程默认配置"""

    version: int
    """此组流程默认值所依据的节点契约版本, 存档缺失时按 1 迁移"""
    config: dict[str, Any]
    """流程层覆盖的配置, 空字典表示该类型全部继承更低层"""


@dataclass(frozen=True)
class FlowDefinition:
    """解析完成的流程定义"""

    version: int
    """流程存档格式版本, 与节点契约版本独立, 缺失时使用宿主当前版本"""
    nodes: dict[str, FlowNodeInstance]
    """以节点实例 id 为键的节点集合, 存档必须提供列表且可为空"""
    edges: list[FlowEdge]
    """按存档顺序保存的连线, 未提供时为空列表"""
    defaults: dict[str, FlowDefaultsEntry]
    """流程默认, 键为完整类型名"""
    layout: dict[str, dict[str, float]]
    """画布布局, 按节点 id 保存 x 和 y 坐标, 缺失时为空字典"""
    budget: FlowBudget | None
    """流程级模型调用硬预算, 未声明时为 None 表示不设流程级上限"""


ConfigSource = Literal["type", "plugin", "flow", "node"]
"""字段生效值来源层"""


@dataclass(frozen=True)
class ResolvedNodeConfig:
    """一个节点实例的生效配置"""

    values: dict[str, Any]
    """按类型, 插件, 流程, 节点优先级合并后的实际配置"""
    sources: dict[str, ConfigSource]
    """每个生效配置键的最终来源层, 供编辑器展示覆盖关系"""
    inherited: dict[str, Any]
    """去掉本节点值之后的值, 即"恢复继承"预览"""



class FlowContractError(ValueError):
    """flow_nodes 契约校验失败, 带稳定错误码"""

    def __init__(self, code: str, message: str) -> None:
        """
        记录错误码与可读原因

        参数:
        - code: flow_contract_* 错误码
        - message: 面向插件作者的失败原因
        """
        super().__init__(message)
        self.code = code
