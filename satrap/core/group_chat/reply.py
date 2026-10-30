"""群聊工具的轮次身份与直接发送宿主, 等待平台回执后返回模型"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TYPE_CHECKING
import asyncio
import uuid

from satrap.core.call_context import current_call_origin, current_tool_workflow
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform import current_adapter_manager
from satrap.core.group_chat.types import GroupChatError
from satrap.core.log import logger
from satrap.core.platform.event import MessageChain
from satrap.core.framework.Base import AsyncSession

if TYPE_CHECKING:
    from satrap.core.platform.event import MessageEvent
    from satrap.core.utils.TCBuilder.async_manager import AsyncToolsManager
    from satrap.core.utils.TCBuilder.manager import ToolsManager


class ReplyTurn:
    """只持有当前主工作流身份与不确定发送状态, 不保存回复草稿"""

    def __init__(self, event: MessageEvent) -> None:
        """
        建立本轮直接发送的权限与串行化边界

        参数:
        - event: 当前可信平台事件
        """
        self._event: MessageEvent | None = event
        self.origin = event.call_origin
        self.loop = asyncio.get_running_loop()
        self.active = True
        self.enabled = False
        self.failed = False
        self.workflow: object | None = None
        self.workflow_manager: AsyncToolsManager | ToolsManager | None = None
        self.operation_owner = self.origin.request_id + ":" + uuid.uuid4().hex
        self._enabled: Callable[[], bool] = lambda: False
        self._lock = asyncio.Lock()
        self._unresolved: SendReceipt | None = None
        self._partial_results: dict[str, SendReceipt] = {}

    @property
    def event(self) -> MessageEvent:
        """
        读取仍由本轮持有的平台事件

        返回:
        - 当前事件, 已结束时抛出 stale_call
        """
        if self._event is None:
            raise GroupChatError("stale_call", "回复轮次已经结束")
        return self._event

    def require_owner(self) -> None:
        """核验当前主工作流及回复工具仍有效"""
        if not self.active or self.failed or not self.enabled or current_call_origin() is not self.origin or not self._enabled():
            raise GroupChatError("stale_call", "当前没有有效的群聊回复轮次")
        if current_tool_workflow() is not self.workflow or self.workflow is None:
            raise GroupChatError("wrong_executor", "只有当前主 Agent 可以发送回复")

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

    def abort(self) -> None:
        """禁止结束或失败后的复制上下文继续发起发送"""
        self.failed = True

    async def send(self, validate: Callable[[], Awaitable[MessageChain]], *, fingerprint: str) -> dict[str, object]:
        """
        校验组件并等待实际平台回执, 不在工具返回后追加发送

        参数:
        - validate: 在当前群重新核验组件并取得媒体租约的回调
        - fingerprint: 已冻结组件的摘要, 用于阻止部分成功后原样重发整条内容

        返回:
        - sent, failed, partial 或 unknown; 部分送达禁止原样重发, 未知结果禁止同轮再次发送
        """
        async with self._lock:
            # Step.1 核验本轮执行者和不能重复提交的发送结果
            self.require_owner()
            if self._unresolved is not None:
                return self._result(self._unresolved)
            if fingerprint in self._partial_results:
                return self._result(self._partial_results[fingerprint])
            message: MessageChain | None = None
            submitted = False
            connection = self.event.adapter.group_chat_connection_token()
            try:
                # Step.2 整体验证组件, 等待后复核来源与平台连接
                message = await validate()
                self.require_owner()
                manager = current_adapter_manager()
                if (manager is None or manager.get_adapter(self.origin.adapter_id) is not self.event.adapter
                        or connection != self.event.adapter.group_chat_connection_token() or not self.event.agent_route_is_current()):
                    raise GroupChatError("stale_call", "发送前平台实例或 Agent 路由已经变化")
                submitted = True
                # Step.3 等待本次真实回执, 再向模型返回执行结果
                receipt = await self.event.send(message, explicit_reply=True)
                if receipt is None:
                    receipt = SendReceipt("unknown", reason="missing_confirmation")
                if receipt.status == "unknown":
                    self._unresolved = receipt
                elif receipt.status == "partial":
                    self._partial_results[fingerprint] = receipt
                log = logger.info if receipt.status == "success" else logger.warning
                log(f"[群聊回复] 发送结束, 轮次={self.origin.request_id}, 状态={receipt.status}, 分段={len(receipt.message_ids)}")
                return self._result(receipt)
            except asyncio.CancelledError:
                if submitted:
                    self._unresolved = SendReceipt("unknown", reason="cancelled_after_submit", error_message="发送期间取消, 结果未确认", stage="publish")
                    self.event._record_send_result(self._unresolved)
                raise
            finally:
                for component in message.components if message is not None else ():
                    lease = getattr(component, "asset_lease", None)
                    if lease is not None:
                        lease.release()

    @staticmethod
    def _result(receipt: SendReceipt) -> dict[str, object]:
        """
        将平台证据映射为工具结果, 不把调用完成当成发送成功

        参数:
        - receipt: 本次实际回执或此前尚未解决的回执

        返回:
        - 包含确认 ID, 失败位置与具体原因的模型可读结果
        """
        success = receipt.status == "success"
        result: dict[str, object] = {"ok": success, "status": "sent" if success else receipt.status,
                                    "sent": success, "message_ids": list(receipt.message_ids)}
        if not success:
            result["failed_index"] = receipt.failed_index
            result["error"] = receipt.public_error()
        return result


_REPLY_TURN: ContextVar[ReplyTurn | None] = ContextVar("satrap_reply_turn", default=None)


def current_reply_turn() -> ReplyTurn | None:
    """
    读取当前有效的发送宿主

    返回:
    - 轮次对象, 无请求或作用域结束时为 None
    """
    turn = _REPLY_TURN.get()
    return turn if turn is not None and turn.active else None


def abort_reply_turn() -> None:
    """会话失败时撤销后续发送权限, 已发送的消息保留真实结果"""
    turn = current_reply_turn()
    if turn is not None:
        turn.abort()


@contextmanager
def bind_reply_turn(event: MessageEvent) -> Iterator[ReplyTurn]:
    """
    为平台请求建立可撤销的发送身份

    参数:
    - event: 当前平台事件

    返回:
    - 本轮宿主, 退出后复制的工具上下文也不可继续发送
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
def bind_session_reply(session: object) -> Iterator[None]:
    """
    绑定主工作流并关闭逐块外发, 显式工具发送与最终文字回复负责交付

    参数:
    - session: 已完成插件协调的主会话

    返回:
    - 临时回调作用域, 退出时恢复原回调
    """
    turn = current_reply_turn()
    workflow: object | None = getattr(session, "_wf", None)
    manager: AsyncToolsManager | ToolsManager | None = getattr(workflow, "tools_manager", None)
    tools: Mapping[str, Any] = getattr(manager, "tools", {})
    if turn is not None and not turn.event.is_private_chat():
        turn.workflow, turn.workflow_manager = workflow, manager
    reply = tools.get("group_chat_reply")
    if (turn is None or turn.origin.conversation_kind not in {"", "group"} or turn.event.is_private_chat()
            or reply is None or not getattr(reply, "direct_platform_reply", False) or manager is None):
        yield
        return

    def enabled() -> bool:
        """
        核验回复工具的当前注册状态

        返回:
        - 回复工具实例, 独立开关和插件权限均有效时为 True
        """
        guard = getattr(manager, "effectiveness_guard", None)
        return manager.tools.get("group_chat_reply") is reply and manager.is_tool_enabled("group_chat_reply") and (guard is None or guard("group_chat_reply"))

    if not enabled():
        yield
        return
    turn.enabled, turn._enabled = True, enabled
    saved: list[tuple[Any, str, Any]] = []

    def discard(content: str) -> None:
        """
        禁止正文增量在工具执行前提前外发

        参数:
        - content: 已由工作流收集的正文或思考增量, 不在工具执行前提前发到平台
        """

    async def discard_async(content: str) -> None:
        """
        异步禁止正文增量在工具执行前提前外发

        参数:
        - content: 已由工作流收集的正文或思考增量, 不在工具执行前提前发到平台
        """

    try:
        for target in (session, workflow):
            for name in ("content_callback", "thinking_callback"):
                if hasattr(target, name):
                    saved.append((target, name, getattr(target, name)))
                    setattr(target, name, discard_async if isinstance(session, AsyncSession) else discard)
        yield
    except BaseException:
        turn.abort()
        raise
    finally:
        for target, name, original in reversed(saved):
            setattr(target, name, original)
