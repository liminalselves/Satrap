"""消息取图的实际内容验证, 按需下载, 临时文件复制和异步来源失效"""
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from satrap.core.config.platform_messages import ArchiveMessage
from satrap.core.call_context import bind_call_origin
from satrap.core.group_chat.types import GroupChatLimits
from satrap.core.group_chat.assets import AssetStore, _LEASES
from satrap.core.components import Image
from .test_group_chat_assets import png
from .test_group_chat_reply import setup, owner, SCOPE
from satrap.core.storage import StorageLayout
from satrap.core.utils import paths


@pytest.fixture(autouse=True)
def managed_media_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    monkeypatch.setattr(paths, "_configured_media_roots", ())


@pytest.mark.asyncio
async def test_get_assets_downloads_once_refreshes_expired_url_and_preserves_inventory(tmp_path: Path, monkeypatch):
    adapter, event, service = setup(tmp_path)
    import time

    adapter.message_archive.record(SCOPE, ArchiveMessage("image:source", "123", time.time(), "图片", media=[
        {"type": "Image", "url": "https://example.com/expired", "native_id": "native.png"}, {"type": "Record"},
    ]))
    download = AsyncMock(side_effect=[OSError("expired"), png()])
    monkeypatch.setattr("satrap.core.pipeline.attachments._download", download)
    refresh = AsyncMock(return_value="https://example.com/fresh")
    monkeypatch.setattr(adapter, "group_chat_refresh_image", refresh)
    with bind_call_origin(event.call_origin):
        result = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"})
        repeated = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"})
        disabled = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"}, limits=GroupChatLimits(media_reply_enabled=False))
        foreign = await service.execute("group_chat_get_message_assets", {"message_id": "image:source", "chat_id": "foreign"})
    assert result["ok"] and result["items"][0]["available"]
    assert repeated["items"][0]["asset_id"] == result["items"][0]["asset_id"]
    assert result["items"][1]["reason"] == "unsupported_media"
    assert download.await_count == 2 and refresh.await_count == 1
    assert disabled["error"]["code"] == "unsupported" and foreign["error"]["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_resolved_inbound_image_is_copied_before_temporary_cleanup(tmp_path: Path, monkeypatch):
    adapter, event, service = setup(tmp_path)
    import time

    from dataclasses import replace

    adapter.message_archive.record(SCOPE, ArchiveMessage("image:source", "123", time.time(), "图片", media=[{"type": "Image"}]))
    event._call_origin = replace(event.call_origin, source_message_id="image:source")
    event.platform_message.message_id = "image:source"
    temporary = tmp_path / "inbound.png"
    temporary.write_bytes(png())
    event.platform_message.message = [Image(file="native.png", resolved_path=str(temporary))]
    download = AsyncMock()
    monkeypatch.setattr("satrap.core.pipeline.attachments._download", download)
    with owner(event) as turn:
        result = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"})
        assert result["items"][0]["available"]
        temporary.unlink()
        prepared = await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": result["items"][0]["asset_id"]}]})
        assert prepared["status"] == "prepared" and turn.draft.components[0].path != str(temporary)
    download.assert_not_awaited()
    assert not _LEASES


@pytest.mark.asyncio
async def test_recall_during_download_never_exposes_asset_id(tmp_path: Path, monkeypatch):
    adapter, event, service = setup(tmp_path)
    import time

    adapter.message_archive.record(SCOPE, ArchiveMessage("image:source", "123", time.time(), "图片", media=[{"type": "Image", "url": "https://example.com/image"}]))
    async def download(*args):
        adapter.message_archive.recall(SCOPE, "image:source")
        return png()
    monkeypatch.setattr("satrap.core.pipeline.attachments._download", download)
    with bind_call_origin(event.call_origin):
        result = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"})
    assert not result["ok"] and result["error"]["code"] == "asset_unavailable"
    assert not AssetStore(adapter.message_archive).root.exists()


@pytest.mark.asyncio
async def test_trusted_sdk_rejects_wrong_workflow_and_only_authorizes_same_turn(tmp_path: Path):
    from types import SimpleNamespace
    from satrap.core.call_context import bind_tool_workflow
    from satrap.core.group_chat.sdk import register_group_chat_asset

    adapter, event, service = setup(tmp_path)
    with owner(event) as turn:
        turn.workflow_manager = SimpleNamespace(is_tool_enabled=lambda name: True, effectiveness_guard=None)
        result = await register_group_chat_asset(png(), "image/png", "trusted_producer")
        assert result["ok"]
        with bind_tool_workflow(object()):
            wrong = await register_group_chat_asset(png(), "image/png", "trusted_producer")
        assert wrong["error"]["code"] == "wrong_executor"
        assert (await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": result["asset"]["asset_id"]}]}))["ok"]
    with owner(event):
        wrong_turn = await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": result["asset"]["asset_id"]}]})
    assert wrong_turn["error"]["code"] == "asset_unavailable" and not _LEASES


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_agent_gets_image_and_prepares_reply_once(tmp_path: Path, monkeypatch, asynchronous):
    from .test_group_chat_plugin import Script, session, Invoker
    from .test_group_chat_reply import send_mock
    from satrap.core.pipeline.scheduler import PipelineScheduler
    from satrap.core.type import LLMCallResponse
    import json
    import time

    adapter, event, _ = setup(tmp_path)
    adapter.message_archive.record(SCOPE, ArchiveMessage("image:source", "123", time.time(), "图片", media=[{"type": "Image", "url": "https://example.com/image"}]))
    monkeypatch.setattr("satrap.core.pipeline.attachments._download", AsyncMock(return_value=png()))
    def replying(messages, kwargs):
        asset = json.loads(messages[-1]["content"])["items"][0]
        assert asset["available"]
        definitions = {tool["function"]["name"] for tool in kwargs["tools"]}
        assert {"group_chat_get_message_assets", "group_chat_list_stickers"} <= definitions
        return LLMCallResponse("tools_call", "", tool_calls=[{"id": "reply-image", "name": "group_chat_reply", "arguments": {
            "components": [{"type": "text", "text": "这是那张图片"}, {"type": "image", "asset_id": asset["asset_id"]}]}}])
    script = Script([LLMCallResponse("tools_call", "", tool_calls=[{"id": "get-image", "name": "group_chat_get_message_assets", "arguments": {"message_id": "image:source"}}]),
                     replying, LLMCallResponse("message", "不重复发送")])
    instance = await session(tmp_path, script, asynchronous, False)
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    send_mock(adapter).assert_awaited_once()
    chain = send_mock(adapter).call_args.args[1]
    assert isinstance(chain.components[1], Image) and chain.components[1].path
    assert len(script.requests) == 3 and not _LEASES


@pytest.mark.asyncio
async def test_real_onebot_queue_sends_scoped_image_with_receipt(tmp_path: Path):
    from satrap.core.platform.onebot.adapter import OneBotAdapter

    adapter, event, service = setup(tmp_path)
    adapter.send_message = OneBotAdapter.send_message.__get__(adapter, OneBotAdapter)
    adapter._bot.send_group_msg.return_value = {"message_id": 987}
    entry = AssetStore(adapter.message_archive).register(SCOPE, png(), source_message_id="77")
    with owner(event) as turn:
        result = await service.execute("group_chat_reply", {"components": [{"type": "text", "text": "图片"}, {"type": "image", "asset_id": entry["asset_id"]}]})
        assert result["ok"]
        await turn.commit("不重复")
        assert event.last_business_receipt.status == "success" and event.last_business_receipt.message_ids == ("987",)
    assert adapter._bot.send_group_msg.await_count == 1
    segments = adapter._bot.send_group_msg.call_args.kwargs["message"]
    assert [component["type"] for component in segments] == ["text", "image"]
    await adapter.drain_message_archive()
    assert adapter.message_archive.get(SCOPE, "987")["direction"] == "outbound" and not _LEASES


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_failure", [False, True])
async def test_misskey_one_attachment_and_upload_failure_never_send_partial_text(tmp_path: Path, monkeypatch, upload_failure):
    from .test_misskey_adapter import make_adapter
    from satrap.core.platform import PlatformAdapterManager, PlatformAdapterRegistry, set_current_adapter_manager
    from satrap.core.config.platform_messages import PlatformMessageStore, MessageScope
    from satrap.core.platform.misskey.adapter import MisskeyAdapter
    from satrap.core.platform.event import MessageEvent

    adapter, original, service = setup(tmp_path)
    adapter = make_adapter()
    adapter._running = True
    registry = PlatformAdapterRegistry()
    registry.register("misskey", MisskeyAdapter)
    manager = PlatformAdapterManager(registry)
    manager._adapters["mk"] = adapter
    set_current_adapter_manager(manager)
    adapter.message_archive = PlatformMessageStore(StorageLayout(tmp_path).platform_db("mk"), "mk")
    import time
    scope = MessageScope("mk", "bot-id", "group", "r1")
    adapter.message_archive.record(scope, ArchiveMessage("source", "member", time.time(), "图片"))
    message = original.platform_message
    message.self_id, message.session_id, message.group_id, message.message_id = "bot-id", "room%r1", "r1", "source"
    event = MessageEvent("回复图片", message, adapter.meta(), "room%r1", adapter)
    entry = AssetStore(adapter.message_archive).register(scope, png(), source_message_id="source")
    with owner(event) as turn:
        result = await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": entry["asset_id"]}] * 2})
        assert result["error"]["code"] == "quota_exceeded" and turn.draft is None and not _LEASES
        result = await service.execute("group_chat_reply", {"components": [{"type": "text", "text": "图片"}, {"type": "image", "asset_id": entry["asset_id"]}]})
        assert result["ok"]
        if upload_failure:
            monkeypatch.setattr(adapter, "_upload_component", AsyncMock(return_value=None))
        await turn.commit("不重复")
        if upload_failure:
            assert event.last_business_receipt.status == "failed" and event.last_business_receipt.reason == "media_upload_failed"
        else:
            assert event.last_business_receipt.status == "success" and event.last_business_receipt.message_ids == ("room-1",)
    sends = [call[1] for call in adapter._client.calls if call[0] == "send_room_message"]
    assert sends == ([] if upload_failure else [{"toRoomId": "r1", "text": "图片", "fileId": "file-local"}])
    assert not _LEASES


@pytest.mark.asyncio
async def test_reply_definition_hides_unusable_components_and_disconnected_tool(tmp_path: Path, monkeypatch):
    from satrap.expend.plugins.group_chat.tools import _available, _GroupChatMixin, get_tools

    adapter, event, _ = setup(tmp_path)
    tool = next(item for item in get_tools(object(), {"media_reply_enabled": True}) if item.tool_name == "group_chat_reply")
    with bind_call_origin(event.call_origin):
        monkeypatch.setattr(adapter, "group_chat_capabilities", lambda: {"text": {"state": "supported"}, "image": {"state": "unsupported"}})
        assert _available(tool)
        schema = _GroupChatMixin.get_tool_defined(tool)["function"]["parameters"]["properties"]["components"]
        assert [variant["properties"]["type"]["const"] for variant in schema["items"]["oneOf"]] == ["text"]
        monkeypatch.setattr(adapter, "group_chat_capabilities", lambda: {})
        assert not _available(tool)
