"""
流程会话端口类型与宿主对象定义

固定 7 种端口类型, 插件节点可用其中 5 种 (messages/context/text/state/condition);
同时定义插件节点可见的数据载体 (FlowMessage, ContextBlock, StateView) 与
节点装饰器 node() 和类写法基类 FlowNode
"""
from __future__ import annotations

from satrap.core.utils.async_worker import BoundedAsyncWorker

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal
import datetime


PortType = Literal["messages", "context", "text", "state", "condition", "event", "handoff"]
"""端口类型, 首版固定 7 种, 插件不能新增"""

DATA_PORT_TYPES: tuple[PortType, ...] = ("messages", "context", "text", "state", "condition", "event", "handoff")
"""全部端口类型"""

PLUGIN_PORT_TYPES: tuple[PortType, ...] = ("messages", "context", "text", "state", "condition")
"""插件节点允许声明的端口类型; event/handoff 由宿主内部使用"""

SIGNAL_PORT_TYPES: tuple[PortType, ...] = ("event", "condition", "handoff")
"""信号类端口: 决定 WF 何时运行或放行, 不携带数据值"""

PORT_TYPE_LABELS: dict[PortType, str] = {
    "messages": "消息流",
    "context": "上下文",
    "text": "文本",
    "state": "状态",
    "condition": "真/假",
    "event": "触发",
    "handoff": "交接",
}
"""端口类型的中文显示名, 用于推断插件节点的端口 label"""

# 端口类型的运行时取值与空值约定 (契约文档 1.3 节):
# messages -> tuple[FlowMessage, ...], 空值 ()
# context  -> tuple[ContextBlock, ...], 空值 ()
# text     -> str, 空值 ""
# state    -> StateView, 空值为版本 0 的空视图
# condition-> bool, 只能作为插件条件节点的唯一输出
# event / handoff -> 宿主内部, 不传给插件函数

EMPTY_PORT_VALUES: dict[PortType, Any] = {
    "messages": (),
    "context": (),
    "text": "",
    "state": None,   # 占位, 实际空值是版本 0 的 StateView, 由 StateView.empty() 生成
    "condition": False,
    "event": None,
    "handoff": None,
}
"""各端口类型的空值; 已连线但上游输出空值时输入口收到空值而不是 None"""


@dataclass(frozen=True)
class FlowMessage:
    """流程节点间传递的单条档案消息, 不可变"""

    message_id: str
    position: int
    """桶内单调序号, 即 platform_messages.seq"""
    sender_id: str
    nickname: str
    text: str
    is_bot: bool
    mentions_bot: bool
    time: float

    def with_text(self, text: str) -> FlowMessage:
        """
        生成替换了文本的新消息对象

        参数:
        - text: 新的消息文本

        返回:
        - FlowMessage: 其余字段不变的新实例
        """
        return FlowMessage(
            message_id=self.message_id,
            position=self.position,
            sender_id=self.sender_id,
            nickname=self.nickname,
            text=text,
            is_bot=self.is_bot,
            mentions_bot=self.mentions_bot,
            time=self.time,
        )


@dataclass(frozen=True)
class ContextBlock:
    """拼装好的模型输入块, 不可变"""

    kind: str
    """块类型: snapshot / messages / summary / reference"""
    text: str
    source_node: str = ""
    """产出该块的节点 id, 供诊断展示"""


@dataclass(frozen=True)
class StateView:
    """状态槽的只读视图, 不可变"""

    content: str
    version: int
    covers_to: int
    """本版覆盖到的消息序号, 未由阈值批次写入时保持上一版的值"""
    committed_at: float
    """提交时间, 版本 0 时为 0"""

    @classmethod
    def empty(cls) -> StateView:
        """返回版本 0 的空视图, 即 state 输入的空值"""
        return cls(content="", version=0, covers_to=0, committed_at=0.0)


EMPTY_PORT_VALUES["state"] = StateView.empty()
# state 的空值是版本 0 的空视图, 不是 None


EventContextKind = Literal["message", "batch", "call"]
"""触发事件携带的上下文: 单条消息 (唤醒), 消息批次 (阈值), 调用 (交接/完成)"""


def node(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    为节点函数指定节点名, 用于函数名不能用作节点名的场合

    参数:
    - name: meta.yaml flow_nodes 里声明的节点名

    返回:
    - 装饰器: 给函数打上 __flow_node_name__ 标记
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, "__flow_node_name__", name)
        return func
    return decorator


class FlowNode:
    """插件节点类写法基类, 需要一次性初始化资源时继承"""

    node_name: str = ""
    """对应 meta.yaml flow_nodes 里声明的节点名"""

    async def setup(self, ctx: Any) -> None:
        """会话创建或生效配置变化重建实例后调用, 可放置慢初始化"""

    async def close(self) -> None:
        """会话释放或实例被替换时调用, 失败只写日志"""

    def __call__(self, ctx: Any, *args: Any, **kwargs: Any) -> Any:
        """节点求值入口, 签名与函数写法一致"""
        raise NotImplementedError


FLOW_CONDITION_WORKERS = BoundedAsyncWorker("satrap-flow-cond", workers=4, capacity=32)
"""插件条件函数专用的有界线程池: 条件不做 IO, 超时或池满判为错误"""
