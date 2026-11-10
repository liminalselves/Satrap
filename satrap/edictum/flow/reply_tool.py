"""
平台回复节点的 flow_reply 工具与后台发送通道

flow_reply 由宿主注入到连着平台回复节点的 WF, 参数 {text, quote_trigger?};
发送路径按有无来源消息选择: 有来源走 ReplyTurn, 无来源走 ScheduledTarget
"""
from __future__ import annotations

from satrap.core.config.platform_messages import MessageScope
from satrap.core.platform.scheduled import ScheduledRecorder, ScheduledTarget, execute_scheduled_segments
from satrap.core.utils.TCBuilder import AsyncTool

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class FlowSendPath:
    """发送路径选择结果"""

    foreground: bool
    """有来源消息 (ReplyTurn 可用)"""
    target: Any = None
    """后台发送时的 ScheduledTarget 冻结对象"""


class AsyncFlowReplyTool(AsyncTool):
    """宿主注入的 flow_reply: 模型调用即发送, 不调用即沉默"""

    tool_name = "flow_reply"
    description = "把文本发送到当前对话; 不调用即不回复 (沉默)"
    params_dict = {
        "text": ("string", "要发送的文本"),
        "quote_trigger": ("bool", "是否引用触发消息, 默认 false; 后台发送时忽略"),
    }

    def __init__(self, send: Callable[[str, bool], Awaitable[bool]], quote: bool = False) -> None:
        """
        初始化回复工具

        参数:
        - send: 实际发送回调 (text, quote_trigger) -> 是否成功
        - quote: 默认引用设置 (模型可逐次覆盖)
        """
        super().__init__()
        self._send = send
        self._quote = quote
        self.config: dict[str, Any] = {}
        self.called = False
        """模型是否已调用过, 宿主据此区分沉默与已回复"""

    async def execute(self, text: str, quote_trigger: bool | None = None) -> dict[str, Any]:
        """
        发送回复文本

        参数:
        - text: 发送内容
        - quote_trigger: 引用触发消息; None 用工具默认

        返回:
        - {ok, sent}; 发送失败返回 ok=False, 不算 WF 失败由宿主决定
        """
        if not text.strip():
            return {"ok": False, "error": "发送内容为空"}
        self.called = True
        sent = await self._send(text, self._quote if quote_trigger is None else quote_trigger)
        return {"ok": sent, "sent": sent}


class _FlowScheduledRecorder:
    """流程专用的 ScheduledRecorder: 只保留内存证据, 不落库 (首版)"""

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []

    def plan(self, target: ScheduledTarget, segments: list[dict[str, Any]]) -> bool:
        """登记分段计划"""
        self.attempts = [{"index": i, "status": "planned"} for i in range(len(segments))]
        return True

    def submitted(self, index: int) -> bool:
        """登记提交标记"""
        if 0 <= index < len(self.attempts):
            self.attempts[index]["status"] = "submitted"
        return True

    def result(self, index: int, receipt: Any) -> bool:
        """登记分段回执"""
        if 0 <= index < len(self.attempts):
            self.attempts[index]["status"] = getattr(receipt, "status", "unknown")
        return True

    def complete(self, receipt: Any) -> bool:
        """结算"""
        self.final = getattr(receipt, "status", "unknown")
        return True


async def send_flow_reply(
    *,
    send_scheduled: Callable[[MessageScope, str], Awaitable[Any]] | None,
    scope: MessageScope,
    text: str,
    foreground_sender: Callable[[str], Awaitable[bool]] | None = None,
    quote_trigger: bool = False,
) -> bool:
    """
    按有无来源消息选择发送路径

    参数:
    - send_scheduled: 后台发送回调 (scope, text) -> receipt; 无来源消息时使用
    - scope: 当前对话范围
    - text: 发送文本
    - foreground_sender: 有来源消息时的前台发送回调; 缺省只能走后台
    - quote_trigger: 是否引用触发消息, 仅前台生效

    返回:
    - 是否已发出
    """
    if foreground_sender is not None:
        return await foreground_sender(text)
    if send_scheduled is None:
        return False
    receipt = await send_scheduled(scope, text)
    return getattr(receipt, "status", "") == "success"
