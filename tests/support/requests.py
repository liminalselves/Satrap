"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from pathlib import Path
import pytest
from satrap.core.plugin_authorization import bind_plugin_factory_tools
from satrap.core.platform import set_current_adapter_manager
from satrap.expend.plugins.friend_manager.tools import AsyncFriendTool
from satrap.core.friends.service import FriendService
from satrap.core.platform import current_adapter_manager
from support.administration import _async_tools

@pytest.fixture()
def restore_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(FriendService, "_test_path", tmp_path / "friends.db", raising=False)
    yield
    set_current_adapter_manager(None)

def tool(name, *, write=True, **extra):
    if name.startswith("friend_manager_"):
        adapter = current_adapter_manager().get_adapter("ob")
        attach_friend_host(adapter)
        config = {"managers": extra.pop("request_managers", "123"), "write_callers": "123", **extra}
        item = AsyncFriendTool(name, "好友工具", {})
        item.config = config
        if name in {"friend_manager_handle_request", "friend_manager_delete_friend", "friend_manager_send_request"}:
            item.tool_enabled = write
        bind_plugin_factory_tools([item], str(Path(__file__).resolve().parents[2] / "satrap/expend/plugins/friend_manager/tools.py"))
        return item
    return next(t for t in _async_tools({"request_managers": "123", "allowed_callers": "123", "write_tools_enabled": write, **extra}) if t.tool_name == name)

def attach_friend_host(adapter):
    adapter._running = True
    if adapter.friend_host is None:
        def verify(source, target):
            source.verify(target)
            return "authorized"
        adapter.friend_host = FriendService("ob", FriendService._test_path, lambda: adapter, verify, lambda: ["123"])

async def incoming(adapter, kind="friend", flag="private-flag", **extra):
    adapter.bot_self_id = "10000"
    await adapter._register_request_flag({"self_id": "10000", "request_type": kind, "flag": flag,
                                          "user_id": "321", "comment": "我是测试好友", **extra})
