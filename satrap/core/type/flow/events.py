"""流程事件与条件结果。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
import time
from satrap.core.type.flow.ports import EventContextKind, FlowMessage

ConditionResult = Literal[True, False, "error"]
"""条件求值三值"""


@dataclass(frozen=True)
class FlowEvent:
    """一次触发事件"""

    event_id: str
    """触发事件标识, 用于共享求值和关联诊断, WF 完成事件沿用原标识"""
    kind: EventContextKind
    """message: 唤醒的单条消息; batch: 阈值批次; call: 交接工具或完成事件"""
    tile: str
    """发出事件的触发磁贴或完成运行的 WF 节点 id"""
    why: str
    """触发原因摘要, 传给运行日志和诊断以解释为何启动"""
    message: FlowMessage | None = None
    """触发消息快照, 默认 None; 前台交接事件也可携带来源消息"""
    batch: tuple[FlowMessage, ...] | None = None
    """kind=batch 时的批次消息"""
    batch_from: int = 0
    """阈值批次起点之前的已提交消息序号, 默认 0, 批次不包含该位置"""
    batch_to: int = 0
    """阈值批次末尾消息序号, 默认 0, 成功后用于推进提交位置"""
    payload: str = ""
    """kind=call 时为交接参数或上游输出; 手动 message 时为本次附加 prompt 或合成窗口"""
    payload_message_ids: tuple[str, ...] = ()
    """payload 已包含的窗口消息 ID, source/assemble 拉取档案时据此避免重复输入"""
    source_wf: str = ""
    """kind=call 时调用方 WF 节点 id"""
    source_event: Any = None
    """来源平台消息事件, 供前台回复轮次和身份使用; 无来源时为 None, 交接及完成事件可继承"""
    occurred_at: float = field(default_factory=time.monotonic)
    """事件创建时的单调时钟秒数, 用于逻辑门时间窗而非墙上时间"""


@dataclass(frozen=True)
class EventContext:
    """条件函数拿到的只读上下文"""

    kind: str
    """本次条件求值的事件上下文种类, 通常为 message, batch 或 call"""
    now: float
    """UTC 时间戳"""
    message: FlowMessage | None = None
    """单消息触发的消息快照, 默认 None 表示没有该上下文"""
    batch: tuple[FlowMessage, ...] | None = None
    """阈值触发的消息批次, 默认 None 表示没有该上下文, 空元组表示批次为空"""
    calls_used: int = 0
    """由宿主填充的调用计数, 条件用 ctx.calls() 读取"""
