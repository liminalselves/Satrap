"""
群聊轮次的结构化回复草稿与单次提交宿主

草稿只存在于可撤销的请求作用域, 不挂在可复用会话上;
主工作流成功结束后通过原发送队列提交, prepared 不代表平台送达
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TYPE_CHECKING
import asyncio
import uuid

from satrap.core.call_context import current_call_origin, current_tool_workflow, bind_call_origin
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform import current_adapter_manager
from satrap.core.group_chat.types import GroupChatError

from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.platform.event import MessageChain, MessageEvent


class ReplyTurn:
    """一次平台请求的输出权, 结束后连同复制上下文中的草稿一并撤销"""

    def __init__(self, event: MessageEvent) -> None:
        """
        创建尚未启用的轮次草稿

        参数:
        - event: 当前管线事件, 仅在本轮作用域保留
        """
        self._event: MessageEvent | None = event
        self.origin = event.call_origin
        self.loop = asyncio.get_running_loop()
        self.active = True
        self.enabled = False
        self.failed = False
        self.submitted = False
        self.workflow: object | None = None
        self.workflow_manager: Any = None
        self.operation_owner = self.origin.request_id + ":" + uuid.uuid4().hex
        self.draft: MessageChain | None = None
        self._verify: Callable[[], Awaitable[MessageChain]] | None = None
        self._enabled: Callable[[], bool] = lambda: False
        self._lock = asyncio.Lock()
        self._output: list[str] = []
        self._output_size = 0

    @property
    def event(self) -> MessageEvent:
        """
        获取仍由本轮持有的平台事件

        返回:
        - 当前事件, 作用域撤销后抛出 stale_call
        """
        if self._event is None:
            raise GroupChatError("stale_call", "回复轮次已经撤销")
        return self._event

    def require_owner(self) -> None:
        """拒绝已结束轮次, 未启用模式以及共享工具的子 Agent"""
        if not self.active or self.failed or not self.enabled or current_call_origin() is not self.origin or not self._enabled():
            raise GroupChatError("stale_call", "当前没有有效的群聊回复轮次")
        if current_tool_workflow() is not self.workflow or self.workflow is None:
            raise GroupChatError("wrong_executor", "只有当前主 Agent 可以准备最终回复")

    def require_main_tool(self, name: str) -> None:
        """
        核验主工作流的持久工具权限, 不要求回复工具同时启用

        参数:
        - name: 本次写入或来源快照工具名
        """
        if not self.active or self.failed or current_call_origin() is not self.origin:
            raise GroupChatError("stale_call", "当前群聊轮次已经失效")
        if self.workflow is None or current_tool_workflow() is not self.workflow:
            raise GroupChatError("wrong_executor", "只有当前主 Agent 可以提交此操作")
        manager = self.workflow_manager
        guard = getattr(manager, "effectiveness_guard", None)
        if manager is None or not manager.is_tool_enabled(name) or (guard is not None and not guard(name)):
            raise GroupChatError("stale_call", "本轮群聊工具已停用")

    async def prepare(self, validate: Callable[[], Awaitable[MessageChain]]) -> dict[str, object]:
        """
        一次性核验全部组件后暂存, 并发调用只有一个草稿可成功

        参数:
        - validate: 从当前群重新核验全部组件的回调

        返回:
        - prepared 状态, 验证失败或已有草稿时抛出明确错误
        """
        async with self._lock:
            self.require_owner()
            if self.draft is not None or self.submitted:
                raise GroupChatError("already_prepared", "本轮已经有最终回复草稿")
            draft = await validate()
            self.require_owner()
            self.draft, self._verify = draft, validate
            return {"ok": True, "status": "prepared", "sent": False, "request_id": self.origin.request_id}

    def capture(self, content: str) -> None:
        """
        缓冲主会话的正文, 不调用外部回调, 结束或取消后的回调直接丢弃

        参数:
        - content: 模型回调正文
        """
        if not self.active or self.failed or not content:
            return
        if self._output_size + len(content) > 100000:
            self.abort()
            logger.error(f"[群聊回复] 本轮缓冲超过上限, 轮次={self.origin.request_id}")
            raise ValueError("群聊回复正文超过 100000 字符")
        self._output.append(content)
        self._output_size += len(content)

    def abort(self) -> None:
        """撤销未提交的草稿和正文, 失败轮次不得发送已准备的回复"""
        self.failed = True
        self.draft = None
        self._verify = None
        self._output.clear()

    async def commit(self, response: str) -> bool:
        """
        主会话成功返回后单次提交, 草稿优先于最终文本, 未知结果不重发

        参数:
        - response: 主会话的最终文本, 无草稿时沿用原回复装饰策略

        返回:
        - True 表示本轮由宿主处理, False 表示未启用并交回原发送逻辑
        """
        if not self.enabled:
            return False
        if not self.active or self.failed or self.submitted:
            return True
        from satrap.core.platform.event import MessageChain

        try:
            with bind_call_origin(self.origin):
                if not self._enabled():
                    raise GroupChatError("stale_call", "回复工具已停用或从主会话移除")
                manager = current_adapter_manager()
                if manager is None or manager.get_adapter(self.origin.adapter_id) is not self.event.adapter:
                    raise GroupChatError("stale_call", "提交前来源平台实例已替换")
                await self.event.adapter.group_chat_scope(self.origin)
                if current_adapter_manager() is not manager or manager.get_adapter(self.origin.adapter_id) is not self.event.adapter:
                    raise GroupChatError("stale_call", "来源核验期间平台实例已替换")
                if self._verify is not None:
                    message = await self._verify()
                else:
                    if not response:
                        return True
                    message = MessageChain.from_text(response)
                if not self.active or self.failed or not self.event.agent_route_is_current():
                    raise GroupChatError("stale_call", "提交前来源路由已经失效")
                if not message.components or self.event.has_send_operation():
                    return True
                self.submitted = True
                await self.event.send(message, explicit_reply=self.draft is not None)
            receipt = self.event.last_business_receipt
            log = logger.info if receipt and receipt.status == "success" else logger.warning
            log(f"[群聊回复] 提交结束, 轮次={self.origin.request_id}, "
                f"状态={receipt.status if receipt else 'unknown'}, 分段={len(receipt.message_ids) if receipt else 0}")
        except asyncio.CancelledError:
            if self.submitted and self.event.last_business_receipt is None:
                self.event._record_send_result(SendReceipt("unknown", reason="cancelled_after_submit"))
                logger.warning(f"[群聊回复] 提交取消, 送达未知且不重发, 轮次={self.origin.request_id}")
            self.abort()
            raise
        except GroupChatError as exc:
            self.abort()
            self.event._record_send_result(SendReceipt("failed", reason=exc.code))
            logger.warning(f"[群聊回复] 提交拒绝, 轮次={self.origin.request_id}, 错误={exc.code}")
        except Exception:
            import traceback
            self.abort()
            self.event._record_send_result(SendReceipt("unknown" if self.submitted else "failed", reason="reply_commit_error"))
            logger.error(f"[群聊回复] 提交异常, 轮次={self.origin.request_id}: {traceback.format_exc()}")
        return True


_REPLY_TURN: ContextVar[ReplyTurn | None] = ContextVar("satrap_reply_turn", default=None)


def current_reply_turn() -> ReplyTurn | None:
    """
    读取本轮仍有效的输出宿主

    返回:
    - 轮次对象, 无请求或作用域结束时为 None
    """
    turn = _REPLY_TURN.get()
    return turn if turn is not None and turn.active else None


def abort_reply_turn() -> None:
    """在会话失败被转为兼容空字符串前显式撤销本轮草稿"""
    turn = current_reply_turn()
    if turn is not None:
        turn.abort()


@contextmanager
def bind_reply_turn(event: MessageEvent) -> Iterator[ReplyTurn]:
    """
    为平台模型调用及最终提交建立可撤销作用域

    参数:
    - event: 当前事件

    返回:
    - 本轮回复对象, 退出时清除事件引用与草稿
    """
    turn = ReplyTurn(event)
    token = _REPLY_TURN.set(turn)
    try:
        yield turn
    finally:
        turn.active = False
        turn.abort()
        turn._event = None
        turn.workflow = None
        turn._enabled = lambda: False
        _REPLY_TURN.reset(token)


@contextmanager
def buffer_session_reply(session: object) -> Iterator[None]:
    """
    按本轮实际启用的回复工具缓冲回调, 不在会话属性上保存平台事件

    参数:
    - session: 已完成插件协调的主会话, 动态扩展仅在此边界读取

    返回:
    - 会话执行上下文, 完成后恢复原回调
    """
    turn = current_reply_turn()
    workflow: Any = getattr(session, "_wf", None)
    manager: Any = getattr(workflow, "tools_manager", None)
    tools: Mapping[str, Any] = getattr(manager, "tools", {})
    if turn is not None and not turn.event.is_private_chat():
        turn.workflow, turn.workflow_manager = workflow, manager
        # 持久工具的主执行者身份独立于结构化回复开关
    reply = tools.get("group_chat_reply")
    if (turn is None or turn.origin.conversation_kind not in {"", "group"}
            or turn.event.is_private_chat() or reply is None
            or not getattr(reply, "deferred_platform_reply", False)
            or not manager.is_tool_enabled("group_chat_reply")):
        yield
        return
    turn.enabled, turn.workflow = True, workflow
    def enabled() -> bool:
        """
        提交前复核本轮所属工具仍在同一主会话启用

        返回:
        - 工具实例, 独立开关及插件状态均有效时为 True
        """
        guard = getattr(manager, "effectiveness_guard", None)
        return manager.tools.get("group_chat_reply") is reply and manager.is_tool_enabled("group_chat_reply") and (guard is None or guard("group_chat_reply"))

    if not enabled():
        turn.enabled = False
        yield
        return
    turn._enabled = enabled
    from satrap.core.framework.Base import AsyncSession

    asynchronous = isinstance(session, AsyncSession)
    saved: list[tuple[Any, str, Any]] = []

    async def capture_async(content: str) -> None:
        """
        异步缓冲正文

        参数:
        - content: 主工作流输出的正文
        """
        turn.capture(content)

    def discard(content: str) -> None:
        """
        暂存模式不将思考回调作为回复正文

        参数:
        - content: 主工作流输出的思考内容
        """

    async def discard_async(content: str) -> None:
        """
        异步暂存模式不将思考回调作为回复正文

        参数:
        - content: 主工作流输出的思考内容
        """

    try:
        for target in (session, workflow):
            for name in ("content_callback", "thinking_callback"):
                if hasattr(target, name):
                    saved.append((target, name, getattr(target, name)))
                    callback = (capture_async if asynchronous else turn.capture) if name == "content_callback" else (discard_async if asynchronous else discard)
                    setattr(target, name, callback)
        yield
    except BaseException:
        turn.abort()
        raise
    finally:
        for target, name, original in reversed(saved):
            setattr(target, name, original)
