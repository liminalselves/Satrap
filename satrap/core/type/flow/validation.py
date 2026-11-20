"""流程静态分析数据契约。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from satrap.core.type.flow.ports import PortType

IssueLevel = Literal["error", "warning"]
IssueCategory = Literal["结构", "触发", "逻辑", "端口", "状态槽", "workflow", "配置", "成本", "环境"]


@dataclass(frozen=True)
class Issue:
    """一条静态检查问题, 与 demo Issue 形状一致"""

    code: str
    """稳定的问题代码, 用于识别具体静态检查规则"""
    level: IssueLevel
    """问题严重程度, error 阻止运行, warning 提醒潜在问题"""
    category: IssueCategory
    """问题所属检查类别, 供前端分组展示"""
    message: str
    """面向编辑者的问题说明, 描述当前不满足的约束"""
    hint: str = ""
    """可选修复建议, 默认空时不输出到前端"""
    node: str = ""
    """关联节点实例 id, 默认空表示不定位节点"""
    edge: str = ""
    """关联连线的标识, 默认空表示不定位连线"""
    field: str = ""
    """关联配置字段名, 默认空表示不定位字段"""

    def to_payload(self) -> dict[str, Any]:
        """序列化给前端"""
        return {
            "code": self.code,
            "level": self.level,
            "category": self.category,
            "message": self.message,
            **({"hint": self.hint} if self.hint else {}),
            **({"node": self.node} if self.node else {}),
            **({"edge": self.edge} if self.edge else {}),
            **({"field": self.field} if self.field else {}),
        }


@dataclass(frozen=True)
class Origin:
    """一次触发的来源: 触发磁贴, 或上游 WF 的完成事件"""

    kind: Literal["tile", "done"]
    """来源类型, tile 表示触发磁贴, done 表示上游 WF 完成事件"""
    id: str
    """发出触发的磁贴或上游 WF 的节点 id"""
    via: tuple[str, ...]
    """从目标向上追踪时经过的逻辑门 id, 空元组表示直接连接"""


@dataclass
class WfInfo:
    """一个 WF 节点的静态分析结果"""

    id: str
    """被分析的 WF 节点实例 id, 也作为分析结果映射的键"""
    values: dict[str, Any]
    """该 WF 的生效配置, 缺少解析结果时为空字典"""
    pulls: set[str] = field(default_factory=set)
    """拉取链中的上游节点 id, 包含状态槽但不继续向其上游或 WF 追踪"""
    origins: list[Origin] = field(default_factory=list)
    """沿触发图追溯到的直接触发来源, 默认空列表表示未发现来源"""
    roots: set[str] = field(default_factory=set)
    """根触发类型: wake / threshold"""
    background: bool = False
    """静态推断该 WF 是否仅由后台路径驱动, 默认 False"""
    handoff_tools: list[dict[str, str]] = field(default_factory=list)
    """该 WF 可调用的交接工具, 每项记录 tool 名称和 tile 节点 id, 默认空"""
    writes_state: list[str] = field(default_factory=list)
    """由 WF 文本输出写入的状态槽节点 id, 默认空列表"""
    replies_directly: bool = False
    """文本输出是否直连回复类 sink, 默认 False"""


@dataclass
class Analysis:
    """静态分析产物, 供 validate 复用"""

    wfs: dict[str, WfInfo] = field(default_factory=dict)
    """按 WF 节点 id 索引的分析结果, 默认空字典"""
    pulled_by: dict[str, set[str]] = field(default_factory=dict)
    """上游节点 id 到会拉取它的 WF id 集合的反向索引, 默认空"""
    edge_types: dict[str, PortType] = field(default_factory=dict)
    """连线标识到推断端口类型的映射, 无法推断的连线不收录"""
    drives: dict[str, set[str]] = field(default_factory=dict)
    """触发磁贴 id 到其直接驱动的 WF id 集合的映射, 默认空"""
    cond_contexts: dict[str, set[str]] = field(default_factory=dict)
    """条件节点 id 到下游可能使用的事件上下文种类集合, 默认空"""
