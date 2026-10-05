"""验证 main 安全修复与独立插件, 表情库及持久审批的组合契约"""
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
import base64
import asyncio

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import EdictumProvider
from satrap.core.group_chat.stickers import StickerStore
from satrap.core.platform import PlatformAdapterManager, PlatformConfig, set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.event import MessageChain
from satrap.core.storage import StorageLayout
from satrap.core.components import Image
from satrap.core.type import SessionConfig
from satrap.core.utils import paths
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.plugin_compatibility import PluginEnvironment
from .test_group_chat_assets import png
from .test_group_admin_plugin import _setup_adapter, _async_tools, _origin


@pytest.mark.asyncio
async def test_managed_sticker_reaches_onebot_without_authorizing_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    store = StickerStore(StorageLayout(tmp_path))
    payload = png()
    store.create({"name": "验收表情", "collection": "测试", "tags": [], "idempotency_key": "create-1"}, payload)
    blob = next(store.files.iterdir())
    image = Image.fromFileSystem(str(blob))
    assert await image.convert_to_base64() == base64.b64encode(payload).decode()
    adapter = OneBotAdapter(PlatformConfig(id="test", type="onebot"))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 42}
    receipt = await adapter.send_message("group%456", MessageChain([image]))
    assert receipt.status == "success" and receipt.message_ids == ("42",)
    adapter._bot.send_group_msg.assert_awaited_once()
    for denied in [store.database, tmp_path / "group-chat" / "secret.db", store.files / "secret.db"]:
        with pytest.raises(paths.MediaSourcePermissionError):
            paths.ensure_allowed_media_path(str(denied))
    paths.set_media_allowed_roots([str(tmp_path / "explicit")])
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(blob))


def test_sticker_symlink_cannot_authorize_external_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    outside = tmp_path / "private.db"
    outside.write_bytes(b"private")
    link = tmp_path / "group-chat" / "stickers" / ("a" * 64)
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 会话无符号链接权限")
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(link))


@pytest.mark.asyncio
async def test_empty_write_list_and_read_list_are_independent():
    adapter = _setup_adapter()
    adapter._bot.get_group_honor_info.return_value = {}
    try:
        with bind_call_origin(_origin()):
            tools = _async_tools({"write_tools_enabled": True})
            write = next(t for t in tools if t.tool_name == "group_admin_kick")
            denied = await write.execute(user_id="321")
            assert denied["status"] == "error" and "permission_denied" in denied["error"]
            read = next(t for t in tools if t.tool_name == "group_admin_get_honors")
            assert (await read.execute())["status"] == "ok"
            read.config["allowed_read_callers"] = "999"
            assert (await read.execute())["status"] == "error"
        adapter._bot.set_group_kick.assert_not_awaited()
        adapter._bot.get_group_honor_info.assert_awaited_once()
    finally:
        set_current_adapter_manager(None)


@pytest.mark.asyncio
async def test_forced_approval_cannot_fall_back_to_direct_sdk():
    adapter = _setup_adapter()
    try:
        tool = next(t for t in _async_tools({"write_tools_enabled": True, "allowed_callers": "123", "high_risk_approval": True})
                    if t.tool_name == "group_admin_kick")
        with bind_call_origin(_origin()):
            result = await tool.execute(user_id="321")
        assert result["status"] == "error" and "审批服务" in result["error"]
        adapter._bot.set_group_kick.assert_not_awaited()
    finally:
        set_current_adapter_manager(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin_required,host_required", [(True, False), (False, True), (True, True), (False, False)])
@pytest.mark.parametrize("revocation", ["callers", "approval"])
async def test_approval_is_single_durable_request_and_sources_are_rechecked(tmp_path, plugin_required, host_required, revocation):
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{
        "id": "bot", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings,
    }]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.patch_account("100", expected_revision=1, mode="all", approval_defaults={
        "ban_group_member": "approval_required" if host_required else "auto_execute",
    })
    directory.confirm_membership("100", "456", True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    plugin_config = {"write_tools_enabled": True, "allowed_callers": "123", "high_risk_approval": plugin_required}
    configs.create("assistant", {"edictum_type": "async_simple", "model_name": "base", "plugins": [
        {"name": "group_admin", "config": plugin_config},
    ]})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(backend.platform_db_path("bot")))
    manager = SessionManager(db_path=backend.platform_db_path("bot"), platform_id="bot")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="source-session", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session: Any = provider.create_session(cfg, llm=cast(Any, object()))
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    session.user_input_provider = AsyncMock(side_effect=AssertionError("不能增加第二套即时审批"))
    manager.pool.put(cfg.session_id, session, "assistant")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    action_id = "approval-1"
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
        "bot", "100", gid, action_id, action, params, actor_kind="model",
    )
    set_current_adapter_manager(backend._adapter_mgr)
    try:
        await provider.prepare_session_async(session)
        tool = session._wf.tools_manager.tools["group_admin_ban"]
        origin = CallOrigin("bot", "100", "GroupMessage", "456", "123", "message-1", "request-1")
        with bind_call_origin(origin):
            result = await tool.execute(user_id="42", duration=60)
        if plugin_required or host_required:
            assert result["data"]["state"] == "pending"
            adapter._bot.set_group_ban.assert_not_awaited()
            result = {"data": await backend.decide_group_action("bot", "100", "456", action_id, approve=True)}
        assert result["data"]["state"] == "succeeded"
        adapter._bot.set_group_ban.assert_awaited_once()
        session.user_input_provider.assert_not_awaited()
        action_id = "approval-2"
        plugin_config["high_risk_approval"] = True
        configs.update("assistant", {"plugins": [{"name": "group_admin", "config": plugin_config}]})
        tool.config["high_risk_approval"] = True
        with bind_call_origin(origin):
            submitted = await tool.execute(user_id="43", duration=60)
        assert submitted["data"]["state"] == "pending"
        if revocation == "callers":
            plugin_config["allowed_callers"] = ""
        else:
            plugin_config["high_risk_approval"] = False
        configs.update("assistant", {"plugins": [{"name": "group_admin", "config": plugin_config}]})
        revoked = await backend.decide_group_action("bot", "100", "456", action_id, approve=True)
        assert revoked["state"] == "failed" and revoked["result"]["reason"] == "model_permission_revoked"
        adapter._bot.set_group_ban.assert_awaited_once()
    finally:
        await provider.release_session_async(session)
        set_current_adapter_manager(None)
