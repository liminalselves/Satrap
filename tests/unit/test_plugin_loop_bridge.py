"""四个插件同步入口把共享桥接的拒绝与超时映射成各自既有结果"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
import asyncio

import pytest

from satrap.expend.plugins.memory import runtime as memory_runtime
from satrap.expend.plugins.group_chat import tools as group_chat_tools
from satrap.expend.plugins.friend_manager import tools as friend_tools
from satrap.core.framework.Base import Session
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.platform import set_current_adapter_manager


def _origin(kind: str) -> CallOrigin:
    chat_type = "GroupMessage" if kind == "group" else "FriendMessage"
    return CallOrigin("ob", "10000", chat_type, "456", "123", "77", "r1", conversation_kind=kind)


class _ManagerStub:
    """固定来源适配器的管理器替身"""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def get_adapter(self, adapter_id: str) -> Any:
        return self._adapter


@pytest.fixture(autouse=True)
def _clear_manager() -> Any:
    yield
    set_current_adapter_manager(None)


def _group_chat_tool(loop: Any) -> Any:
    set_current_adapter_manager(cast(Any, _ManagerStub(SimpleNamespace(_loop=loop))))
    return next(t for t in group_chat_tools.get_tools(cast(Session, object()), {}) if t.tool_name == "group_chat_get_message")


def _never() -> Any:
    async def never() -> None:
        raise AssertionError("协程不应被提交")

    return never()


def _timeout(coroutine: Any, loop: Any, seconds: float) -> Any:
    coroutine.close()
    raise TimeoutError


def test_group_chat_without_loop_reports_unavailable() -> None:
    tool = _group_chat_tool(None)
    with bind_call_origin(_origin("group")):
        result = tool.execute(message_id="1")
    assert result == {"ok": False, "error": {"code": "unavailable", "message": "来源事件循环不可用", "retryable": True}}


@pytest.mark.asyncio
async def test_group_chat_inside_platform_loop_is_rejected() -> None:
    tool = _group_chat_tool(asyncio.get_running_loop())
    with bind_call_origin(_origin("group")):
        result = tool.execute(message_id="1")
    assert result == {"ok": False, "error": {
        "code": "unavailable", "message": "同步工具不能在平台事件循环内阻塞调用", "retryable": True}}


def test_group_chat_timeout_reports_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _group_chat_tool(None)
    monkeypatch.setattr(group_chat_tools, "run_on_platform_loop", _timeout)
    with bind_call_origin(_origin("group")):
        result = tool.execute(message_id="1")
    assert result == {"ok": False, "error": {"code": "unavailable", "message": "群聊宿主调用超时, 已取消", "retryable": True}}


def _friend_tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    adapter = SimpleNamespace(_loop=None)
    origin = _origin("private")
    tool = next(t for t in friend_tools.get_tools(cast(Session, object()), {}) if t.tool_name == "friend_manager_list_friends")
    monkeypatch.setattr(tool, "_resolve", lambda preview=False, recheck_entry=True: (adapter, origin))
    return tool


def test_friend_manager_without_loop_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _friend_tool(monkeypatch)
    with bind_call_origin(_origin("private")):
        result = tool.execute()
    assert result == {"ok": False, "error": {"code": "unavailable", "message": "来源平台事件循环不可用于同步工具", "retryable": False}}


def test_friend_manager_timeout_keeps_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _friend_tool(monkeypatch)
    monkeypatch.setattr(friend_tools, "run_on_platform_loop", _timeout)
    with bind_call_origin(_origin("private")):
        result = tool.execute()
    assert result == {"ok": False, "error": {"code": "unconfirmed", "message": "平台未确认结果, 请核查, 不要重复执行", "retryable": True}}


def test_memory_without_loop_reports_unavailable_and_closes_coroutine() -> None:
    set_current_adapter_manager(None)
    coroutine = _never()
    result = memory_runtime.wait_host(coroutine)
    assert result == {"ok": False, "error": "同步记忆操作需要有效的平台工作线程", "error_type": "unavailable"}
    assert coroutine.cr_frame is None


def test_memory_timeout_reports_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_runtime, "run_on_platform_loop", _timeout)
    result = memory_runtime.wait_host(_never())
    assert result == {"ok": False, "error": "记忆操作超时, 已取消等待", "error_type": "unavailable"}
