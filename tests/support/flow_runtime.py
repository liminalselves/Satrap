"""Flow 真实入口测试共享夹具; 仅替代模型响应与最底层平台发送"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import inspect
import time

from satrap.core.components import At, Plain
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.config.platform_schema import ensure_platform_tables
from satrap.core.platform import PlatformAdapter, PlatformConfig
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.scheduled import execute_scheduled_segments
from satrap.core.type import LLMCallResponse, MessageMember, PlatformMessage, PlatformMessageType


class MockLLM:
    """按脚本返回响应并记录真实 WF 模型输入"""

    def __init__(self, script: Any = None) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    async def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCallResponse:
        from satrap.core.call_context import current_flow_run
        self.calls.append({"messages": messages, "kwargs": kwargs, "run": current_flow_run.get()})
        if callable(self.script):
            result = self.script(messages, kwargs.get("tools"), len(self.calls))
            return await result if inspect.isawaitable(result) else result
        if isinstance(self.script, list):
            return self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return self.script or LLMCallResponse(type="text", content="done")

    async def stream_call(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        raise NotImplementedError


class FlowAdapter(PlatformAdapter):
    """真实适配器基类上的最底层发送替身"""

    adapter_type = "flow-test"
    supports_scheduled_group_send = True
    supports_scheduled_private_send = True

    def __init__(self, store: PlatformMessageStore) -> None:
        super().__init__(PlatformConfig(
            id=store.adapter_id, type=self.adapter_type,
            settings={"enable_private": True},
        ))
        self.message_archive = store
        self.sent: list[dict[str, Any]] = []
        self.scheduled: list[dict[str, Any]] = []
        self.reply_turns: list[Any] = []

    async def run(self) -> None:
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)

    async def send_message(
        self, session_id: str, message: MessageChain, *, request_id: str = "",
        purpose: str = "business", require_tracking: bool = False,
    ) -> SendReceipt:
        from satrap.core.group_chat.reply import current_reply_turn
        turn = current_reply_turn()
        self.reply_turns.append((turn, turn.event if turn is not None else None))
        self.sent.append({
            "session_id": session_id, "text": _chain_text(message),
            "purpose": purpose, "request_id": request_id,
        })
        return SendReceipt("success", [f"front-{len(self.sent)}"])

    async def group_chat_send_scheduled(self, target: Any, message: MessageChain, recorder: Any) -> SendReceipt:
        self.scheduled.append({"target": target, "text": _chain_text(message)})

        async def send() -> SendReceipt:
            return SendReceipt("success", [f"back-{len(self.scheduled)}"])

        return await execute_scheduled_segments(
            target, recorder, [{"text": _chain_text(message)}], [send])


def _chain_text(message: MessageChain) -> str:
    return "".join(str(getattr(component, "text", "")) for component in message)


def response(text: str = "done", tool_calls: list[dict[str, Any]] | None = None) -> LLMCallResponse:
    """构造真实 WF 循环使用的模型响应"""
    return LLMCallResponse(
        type="tools_call" if tool_calls else "text", content=text, tool_calls=tool_calls)


def workflow_flow(*, plugins: str = "") -> dict[str, Any]:
    """构造经真实解析、校验和初始化入口使用的最小唤醒流程"""
    return {"version": 3, "nodes": [
        {"id": "src", "type": "source", "title": "s", "config": {}},
        {"id": "wake", "type": "wake", "title": "wake", "config": {
            "use_platform_policy": False, "mention": True, "new_messages": 0, "cooldown": 1}},
        {"id": "w1", "type": "react_workflow", "title": "w1", "config": {
            "model_name": "test", "prompt": "test", "plugins": plugins,
            "max_steps": 2, "timeout_seconds": 10}},
    ], "edges": [
        {"from": "src.out", "to": "wake.messages"},
        {"from": "wake.event", "to": "w1.trigger"},
    ]}


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "") -> dict[str, Any]:
    """构造一个模型工具调用"""
    return {"id": call_id or f"call-{name}", "name": name, "arguments": arguments}


def make_event(
    adapter: FlowAdapter, scope: MessageScope, text: str, index: int,
    *, mention: bool = False,
) -> MessageEvent:
    """构造真实 MessageEvent, 身份与冻结 scope 一致"""
    private = scope.conversation_kind == "private"
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE if private else PlatformMessageType.GROUP_MESSAGE
    message.self_id = scope.self_id
    message.session_id = scope.chat_id
    message.group_id = "" if private else scope.chat_id
    message.message_id = f"m{index}"
    message.sender = MessageMember(user_id=f"u{index % 3}", nickname=f"用户{index % 3}")
    message.message = ([At(qq=scope.self_id, name=scope.self_id)] if mention else []) + [Plain(text)]
    message.message_str = text
    message.raw_message = {}
    message.received_at = time.time() + index / 1000
    return MessageEvent(
        text, message, adapter.meta(), scope.chat_id, adapter,
        session_provider="edictum", session_type="flow",
    )


def archive_event(store: PlatformMessageStore, scope: MessageScope, event: MessageEvent) -> None:
    """模拟调度前已完成的真实入档; observe 本身不负责写入"""
    store.record(scope, ArchiveMessage(
        message_id=event.platform_message.message_id,
        sender_id=event.get_sender_id(), message_time=event.platform_message.received_at,
        text=event.get_message_str(), direction="inbound",
        mentions=(scope.self_id,) if any(isinstance(c, At) for c in event.get_messages()) else (),
        received_at=event.platform_message.received_at,
    ))


def database(path: Path) -> Path:
    """创建带当前平台表结构的临时数据库"""
    import sqlite3
    connection = sqlite3.connect(path)
    ensure_platform_tables(connection)
    connection.close()
    from satrap.edictum.flow.store import FlowStore
    FlowStore(path, "", "")._connect().close()
    return path


async def wait_until(predicate: Any, timeout: float = 5.0) -> None:
    """等待真实后台任务达到断言状态"""
    import asyncio
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate(), "等待流程运行状态超时"
