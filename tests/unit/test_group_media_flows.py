"""业务链路场景与集中边界契约"""
from __future__ import annotations

from dataclasses import replace
from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot

from pathlib import Path
import asyncio
import pytest
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.assets import AssetStore, inspect_image, _LEASES
from satrap.core.group_chat.types import GroupChatError
from satrap.core.platform.onebot.outbound import OutboundTurns
from support.media import png
from unittest.mock import AsyncMock
from satrap.core.call_context import bind_call_origin
from satrap.core.group_chat.types import GroupChatLimits
from satrap.core.components import Image as _group_chat_media_service_Image
from support.group_reply import setup, owner, SCOPE
from satrap.core.storage import StorageLayout as _group_chat_media_service_StorageLayout
from satrap.core.utils import paths
from urllib.parse import urlencode
from typing import Any, cast
import json
import time
from satrap.core.backend import control_server
from satrap.core.storage.layout import StorageLayout as _group_chat_media_management_StorageLayout
from satrap.core.config.group_chat_data import parse_sticker_upload
from support.http import _request, _json_body, _use_config, _BufferWriter
from satrap.core.group_chat.stickers import StickerStore
from satrap.core.storage.layout import StorageLayout as _group_chat_stickers_StorageLayout

def test_asset_scope_owner_source_and_lease(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    archive = PlatformMessageStore(tmp_path / "platform.db", "future", clock=lambda: now[0])
    scope = MessageScope("future", "bot:opaque", "circle", "group/opaque")
    archive.record(scope, ArchiveMessage("message:id", "user:id", now[0], "图片"))
    assets = AssetStore(archive)
    entry = assets.register(scope, png(), source_message_id="message:id", media_index=0)
    assert "path" not in entry and entry["mime_type"] == "image/png"
    with pytest.raises(GroupChatError, match="不属于"):
        assets.acquire(MessageScope("future", "bot:other", "circle", "group/opaque"), entry["asset_id"], "")
    lease, _ = assets.acquire(scope, entry["asset_id"], "")
    file = lease.path
    archive.recall(scope, "message:id")
    with pytest.raises(GroupChatError):
        assets.acquire(scope, entry["asset_id"], "")
    assets.purge()
    assert file.is_file()
    lease.release()
    assets.purge()
    assert not file.exists()
    tool = assets.register(scope, png(), owner="turn:one")
    with pytest.raises(GroupChatError):
        assets.acquire(scope, tool["asset_id"], "turn:two")
    lease, _ = assets.acquire(scope, tool["asset_id"], "turn:one")
    lease.release()
    now[0] += 86401
    with pytest.raises(GroupChatError):
        assets.acquire(scope, tool["asset_id"], "turn:one")
    assets.purge()
    assert not list(assets.root.iterdir())

@pytest.mark.parametrize("payload,mime", [(b"bad", None), (png(), "image/jpeg"), (b"x" * (10 * 1024 * 1024 + 1), None)],
                         ids=["invalid-content", "mime-mismatch", "oversized"])
def test_content_not_extension_controls_acceptance(payload: bytes, mime: str | None) -> None:
    with pytest.raises(GroupChatError):
        inspect_image(payload, mime)

@pytest.mark.asyncio
async def test_queue_holds_file_until_actual_child_settles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from satrap.core.group_chat.assets import lease_file
    import satrap.core.platform.onebot.outbound as module

    monkeypatch.setattr(module, "CANCEL_SETTLE_TIMEOUT", 0.01)
    path = tmp_path / "blob"
    path.write_bytes(png())
    draft = lease_file(path)
    child = draft.fork()
    started, finish = asyncio.Event(), asyncio.Event()
    async def operation():
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
        assert path.is_file()
        return "sent"
    queue = OutboundTurns()
    parent = asyncio.create_task(queue.run("opaque", operation, on_settle=child.release))
    await started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    draft.release()
    assert path in _LEASES and len(queue.tasks) == 1
    finish.set()
    await asyncio.sleep(0.02)
    assert path not in _LEASES and not queue.tasks
    rejected = lease_file(path)
    queue.closed = True
    with pytest.raises(RuntimeError):
        await queue.run("opaque", operation, on_settle=rejected.release)
    assert rejected.released

@pytest.mark.asyncio
async def test_reply_revalidates_images_and_releases_failed_draft(tmp_path: Path) -> None:
    from support.group_reply import setup, owner, SCOPE, send_mock

    adapter, event, service = setup(tmp_path)
    assert adapter.message_archive is not None
    assets = AssetStore(adapter.message_archive)
    entry = assets.register(SCOPE, png(), source_message_id="77", media_index=0)
    with owner(event) as turn:
        bad = await service.execute("group_chat_reply", {"components": [
            {"type": "image", "asset_id": entry["asset_id"]}, {"type": "image", "asset_id": "ga_foreign"},
        ]})
        assert not bad["ok"] and turn.draft is None and not _LEASES
        result = await service.execute("group_chat_reply", {"components": [
            {"type": "text", "text": "图片"}, {"type": "image", "asset_id": entry["asset_id"]},
        ]})
        assert result["status"] == "prepared" and _LEASES
        adapter.message_archive.recall(SCOPE, "77")
        await turn.commit("重复正文")
        assert event.last_business_receipt.status == "failed"
    send_mock(adapter).assert_not_awaited()
    assert not _LEASES

@pytest.mark.asyncio
async def test_reply_commits_media_once_and_preserves_order(tmp_path: Path) -> None:
    from support.group_reply import setup, owner, SCOPE, send_mock
    from satrap.core.components import Image as ComponentImage, Plain

    adapter, event, service = setup(tmp_path)
    assert adapter.message_archive is not None
    assets = AssetStore(adapter.message_archive)
    entry = assets.register(SCOPE, png(), source_message_id="77")
    with owner(event) as turn:
        result = await service.execute("group_chat_reply", {"components": [
            {"type": "text", "text": "前"}, {"type": "image", "asset_id": entry["asset_id"]}, {"type": "text", "text": "后"},
        ]})
        assert result["ok"]
        await turn.commit("不重复发送")
        await turn.commit("不重复发送")
        chain = send_mock(adapter).call_args.args[1]
        assert [type(item) for item in chain.components] == [Plain, ComponentImage, Plain]
    send_mock(adapter).assert_awaited_once()
    assert not _LEASES

@pytest.fixture()
def _group_chat_media_service_managed_media_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    monkeypatch.setattr(paths, "_configured_media_roots", ())

@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
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
        result = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"}, limits=GroupChatLimits(history_scope="group_archive"))
        repeated = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"}, limits=GroupChatLimits(history_scope="group_archive"))
        disabled = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"}, limits=GroupChatLimits(media_reply_enabled=False, history_scope="group_archive"))
        foreign = await service.execute("group_chat_get_message_assets", {"message_id": "image:source", "chat_id": "foreign"}, limits=GroupChatLimits(history_scope="group_archive"))
    assert result["ok"] and result["items"][0]["available"]
    assert repeated["items"][0]["asset_id"] == result["items"][0]["asset_id"]
    assert result["items"][1]["reason"] == "unsupported_media"
    assert download.await_count == 2 and refresh.await_count == 1
    assert disabled["error"]["code"] == "unsupported" and foreign["error"]["code"] == "invalid_argument"

@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
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
    event.platform_message.message = [_group_chat_media_service_Image(file="native.png", resolved_path=str(temporary))]
    download = AsyncMock()
    monkeypatch.setattr("satrap.core.pipeline.attachments._download", download)
    with owner(event) as turn:
        result = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"}, limits=GroupChatLimits(history_scope="group_archive"))
        assert result["items"][0]["available"]
        temporary.unlink()
        prepared = await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": result["items"][0]["asset_id"]}]})
        assert prepared["status"] == "prepared" and turn.draft.components[0].path != str(temporary)
    download.assert_not_awaited()
    assert not _LEASES

@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
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
        result = await service.execute("group_chat_get_message_assets", {"message_id": "image:source"}, limits=GroupChatLimits(history_scope="group_archive"))
    assert not result["ok"] and result["error"]["code"] == "asset_unavailable"
    assert not AssetStore(adapter.message_archive).root.exists()

@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
@pytest.mark.asyncio
async def test_trusted_sdk_rejects_wrong_workflow_and_only_authorizes_same_turn(tmp_path: Path):
    from types import SimpleNamespace
    from satrap.core.call_context import bind_tool_workflow
    from satrap.core.group_chat.sdk import register_group_chat_asset

    adapter, event, service = setup(tmp_path)
    with owner(event) as turn:
        turn.workflow_manager = cast(Any, SimpleNamespace(is_tool_enabled=lambda name: True, effectiveness_guard=None))
        result = await register_group_chat_asset(png(), "image/png", "trusted_producer")
        assert result["ok"]
        with bind_tool_workflow(object()):
            wrong = await register_group_chat_asset(png(), "image/png", "trusted_producer")
        assert wrong["error"]["code"] == "wrong_executor"
        assert (await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": result["asset"]["asset_id"]}]}))["ok"]
    with owner(event):
        wrong_turn = await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": result["asset"]["asset_id"]}]})
    assert wrong_turn["error"]["code"] == "asset_unavailable" and not _LEASES


@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_agent_draws_imports_and_replies_with_sandbox_image(tmp_path: Path, asynchronous: bool):
    """真实沙箱绘图经同轮资产登记发群图, 同步与异步会话均只提交一次"""
    from support.group_models import Script, session, Invoker
    from support.group_reply import send_mock
    from satrap.core.pipeline.scheduler import PipelineScheduler
    from satrap.core.type import LLMCallResponse

    adapter, event, _ = setup(tmp_path)
    root = tmp_path / "current-session" / "sandbox"
    source = """import numpy as np
import matplotlib.pyplot as plt
t = np.linspace(0, 2*np.pi, 200)
x = 16*np.sin(t)**3
y = 13*np.cos(t)-5*np.cos(2*t)-2*np.cos(3*t)-np.cos(4*t)
fig, ax = plt.subplots()
ax.fill(x, y, color='pink')
ax.plot(x, y, color='darkred')
ax.set_title('af2333 <3')
ax.axis('equal')
fig.savefig('heart.png')
plt.close(fig)
"""

    def importing(messages, kwargs):
        result = json.loads(messages[-1]["content"])
        assert result["returncode"] == 0 and (root / "heart.png").is_file(), result
        assert "group_chat_import_sandbox_image" in {item["function"]["name"] for item in kwargs["tools"]}
        return LLMCallResponse("tools_call", "", tool_calls=[{"id": "import", "name": "group_chat_import_sandbox_image",
                                                            "arguments": {"path": "heart.png"}}])

    def replying(messages, kwargs):
        result = json.loads(messages[-1]["content"])
        assert result["ok"] and result["asset"]["origin"] == "tool", result
        asset = result["asset"]
        assert asset["mime_type"] == "image/png" and "path" not in asset
        (root / "heart.png").unlink()
        return LLMCallResponse("tools_call", "", tool_calls=[{"id": "reply", "name": "group_chat_reply", "arguments": {
            "components": [{"type": "text", "text": "画好了"}, {"type": "image", "asset_id": asset["asset_id"]}]}}])

    script = Script([LLMCallResponse("tools_call", "", tool_calls=[{"id": "draw", "name": "code_sandbox",
                                                                  "arguments": {"operation": "run", "code": source}}]),
                     importing, replying, LLMCallResponse("message", "不重复发送")])
    instance = await session(tmp_path, script, asynchronous, False)
    bind_environment(instance, replace(environment_snapshot(instance), sandbox_root=Path(str(root))))
    plugin = Path(__file__).resolve().parents[2] / "satrap/expend/plugins/base_take"
    if asynchronous:
        await instance.install_plugin(str(plugin))
    else:
        instance.install_plugin(str(plugin))
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    send_mock(adapter).assert_awaited_once()
    chain = send_mock(adapter).call_args.args[1]
    assert isinstance(chain.components[1], _group_chat_media_service_Image)
    assert Path(chain.components[1].path).is_file() and not (root / "heart.png").exists()
    assert len(script.requests) == 4 and not _LEASES


@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
@pytest.mark.asyncio
async def test_sandbox_image_import_scope_paths_and_live_permissions(tmp_path: Path, monkeypatch):
    """真实工具拒绝越界和跨工作流文件, 重新登记后仍受同群同轮资产权限约束"""
    from support.group_models import Script, session
    from satrap.core.call_context import bind_tool_workflow
    from satrap.core.plugin_interop.artifacts import read_sandbox_image
    import satrap.core.plugin_interop.artifacts as artifacts_module

    adapter, event, service = setup(tmp_path)
    instance = await session(tmp_path, Script([]), True, False)
    root = tmp_path / "current" / "sandbox"
    foreign = tmp_path / "other" / "sandbox"
    root.mkdir(parents=True)
    foreign.mkdir(parents=True)
    (root / "image.png").write_bytes(png())
    (root / "invalid.png").write_text("not an image", encoding="utf-8")
    (foreign / "private.png").write_bytes(png())
    (root / "empty.png").touch()
    with (root / "oversized.png").open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    bind_environment(instance, replace(environment_snapshot(instance), sandbox_root=Path(str(root))))
    tool = instance.tools_manager.tools["group_chat_import_sandbox_image"]
    with owner(event) as turn, bind_tool_workflow(instance._wf):
        turn.workflow, turn.workflow_manager = instance._wf, instance.tools_manager
        assert tool.is_available_for_call()
        cases = [
            ({"path": "../other/sandbox/private.png"}, "invalid_argument"),
            ({"path": str(foreign / "private.png")}, "invalid_argument"),
            ({"path": "C:/Windows/private.png"}, "invalid_argument"),
            ({"path": "https://example.com/image.png"}, "invalid_argument"),
            ({"path": "image.png:stream"}, "invalid_argument"),
            ({"path": "image.png", "root": str(foreign)}, "invalid_argument"),
            ({"path": 12}, "invalid_argument"),
            ({"path": "missing.png"}, "asset_unavailable"),
            ({"path": "invalid.png"}, "invalid_media"),
            ({"path": "empty.png"}, "invalid_media"),
            ({"path": "oversized.png"}, "invalid_media"),
            ({"path": "."}, "invalid_media"),
        ]
        try:
            (root / "linked").symlink_to(foreign, target_is_directory=True)
        except OSError:
            pass
        else:
            cases.append(({"path": "linked/private.png"}, "invalid_argument"))
        for arguments, expected in cases:
            result = await instance.tools_manager.execute_tool("group_chat_import_sandbox_image", arguments)
            assert result["error"]["code"] == expected, (arguments, result)

        with bind_tool_workflow(object()):
            assert (await instance.tools_manager.execute_tool("group_chat_import_sandbox_image", {"path": "image.png"}))["error"]["code"] == "wrong_executor"
        result = await instance.tools_manager.execute_tool("group_chat_import_sandbox_image", {"path": "image.png"})
        assert result["ok"], result
        asset_id = result["asset"]["asset_id"]
        with pytest.raises(GroupChatError):
            AssetStore(adapter.message_archive).acquire(MessageScope("ob", "10000", "group", "other"), asset_id, turn.operation_owner)

        tool.config["media_reply_enabled"] = False
        assert not tool.is_available_for_call()
        assert (await tool.execute(path="image.png"))["error"]["code"] == "unsupported"
        tool.config["media_reply_enabled"] = True

        def change_root(root_path, path):
            payload = read_sandbox_image(root_path, path)
            bind_environment(instance, replace(environment_snapshot(instance), sandbox_root=Path(str(foreign))))
            return payload

        monkeypatch.setattr(artifacts_module, "read_sandbox_image", change_root)
        assert (await instance.tools_manager.execute_tool("group_chat_import_sandbox_image", {"path": "image.png"}))["error"]["code"] == "stale_call"
        bind_environment(instance, replace(environment_snapshot(instance), sandbox_root=Path(str(root))))

    with owner(event) as turn, bind_tool_workflow(instance._wf):
        turn.workflow, turn.workflow_manager = instance._wf, instance.tools_manager
        denied = await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": asset_id}]})
        assert denied["error"]["code"] == "asset_unavailable"
        monkeypatch.setattr(artifacts_module, "read_sandbox_image", read_sandbox_image)
        renewed = await instance.tools_manager.execute_tool("group_chat_import_sandbox_image", {"path": "image.png"})
        assert renewed["ok"] and renewed["asset"]["asset_id"] != asset_id
        prepared = await service.execute("group_chat_reply", {"components": [{"type": "image", "asset_id": renewed["asset"]["asset_id"]}]})
        assert prepared["ok"] and prepared["status"] == "prepared"
    assert not _LEASES

@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_agent_gets_image_and_prepares_reply_once(tmp_path: Path, monkeypatch, asynchronous):
    from support.group_models import Script, session, Invoker
    from support.group_reply import send_mock
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
    assert isinstance(chain.components[1], _group_chat_media_service_Image) and chain.components[1].path
    assert len(script.requests) == 3 and not _LEASES

@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
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

@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
@pytest.mark.asyncio
@pytest.mark.parametrize("upload_failure", [False, True])
async def test_misskey_one_attachment_and_upload_failure_never_send_partial_text(tmp_path: Path, monkeypatch, upload_failure):
    from support.misskey import make_adapter
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
    adapter.message_archive = PlatformMessageStore(_group_chat_media_service_StorageLayout(tmp_path).platform_db("mk"), "mk")
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

@pytest.mark.usefixtures('_group_chat_media_service_managed_media_layout')
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

def multipart(fields=None):
    fields = fields or {"name": "收到", "tags": '["确认"]', "collection": "常用", "idempotency_key": "upload-one"}
    boundary = "satrap-test-boundary"
    body = b""
    for name, value in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="../../任意.png"\r\nContent-Type: image/png\r\n\r\n'.encode("utf-8") + png() + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body

async def upload(authorized=True):
    content_type, body = multipart()
    reader = asyncio.StreamReader()
    header = ("POST /api/group-chat/stickers/upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
              f"Authorization: Bearer {control_server._CONTROL_AUTH.token if authorized else 'invalid'}\r\n"
              f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    reader.feed_data(header + body)
    reader.feed_eof()
    writer = _BufferWriter()
    await control_server._handle_request(reader, cast(asyncio.StreamWriter, writer))
    return bytes(writer.data)

def test_multipart_is_utf8_and_never_uses_client_filename():
    content_type, body = multipart()
    metadata, image = parse_sticker_upload(content_type, body)
    assert metadata["name"] == "收到" and metadata["tags"] == ["确认"] and image == png()
    with pytest.raises(ValueError):
        parse_sticker_upload("image/png", body)
    with pytest.raises(ValueError):
        parse_sticker_upload(*multipart({"name": "收到", "tags": "[]", "collection": "常用", "idempotency_key": "one", "path": "C:/secret"}))

@pytest.mark.asyncio
async def test_authenticated_upload_preview_cold_scope_and_delete_replay(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = _group_chat_media_management_StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda document=None: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot:/a", "circle", "群/中文")
    archive.record(scope, ArchiveMessage("source:a", "user:a", time.time(), "真实讨论"))
    assert (await upload(False)).startswith(b"HTTP/1.1 401")
    first = _json_body(await upload())
    assert first["ok"] and first["sticker"]["name"] == "收到"
    repeated = _json_body(await upload())
    assert repeated["replayed"] and repeated["sticker"]["sticker_id"] == first["sticker"]["sticker_id"]
    identity = first["sticker"]["sticker_id"]
    target = "/api/group-chat/stickers/" + identity
    assert (await _request(target + "/preview", authorized=False)).startswith(b"HTTP/1.1 401")
    assert _json_body(await _request(target + "/preview"))["preview"].startswith("data:image/png;base64,")
    query = urlencode({"self_id": scope.self_id, "chat_id": scope.chat_id, "conversation_kind": scope.conversation_kind})
    settings = "/api/platforms/future/group-chat/sticker-settings?" + query
    assert _json_body(await _request(settings))["collections"] == []
    payload = json.dumps({"collections": ["常用"], "expected_revision": 0, "idempotency_key": "intent-enable"}).encode()
    assert _json_body(await _request(settings, "PUT", payload))["revision"] == 1
    assert _json_body(await _request(settings, "PUT", payload))["revision"] == 1
    foreign = settings.replace("bot%3A%2Fa", "bot%3A%2Fb")
    assert (await _request(foreign)).startswith(b"HTTP/1.1 404")
    delete = json.dumps({"expected_revision": 1, "idempotency_key": "intent-delete"}).encode()
    assert _json_body(await _request(target, "DELETE", delete))["deleted"]
    assert _json_body(await _request(target, "DELETE", delete))["replayed"]
    assert (await _request(target + "/preview")).startswith(b"HTTP/1.1 404")
    assert _json_body(await _request("/api/group-chat/stickers"))["items"] == []

@pytest.mark.asyncio
async def test_native_catalog_comes_from_adapter_and_rejects_unknown_mapping(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = _group_chat_media_management_StorageLayout(tmp_path / "data")
    layout.ensure_platform("ob")
    document = json.loads(control_server.CONFIG_PATH.read_text(encoding="utf-8"))
    document["platforms"] = [{"id": "ob", "type": "onebot", "settings": {}}]
    control_server.CONFIG_PATH.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    archive = PlatformMessageStore(layout.platform_db("ob"), "ob")
    scope = MessageScope("ob", "bot", "group", "群")
    archive.record(scope, ArchiveMessage("source", "user", time.time(), "表情", components=[{"type": "Face", "id": "14"}]))
    catalog = _json_body(await _request("/api/group-chat/native-stickers?platform_id=ob"))
    assert catalog["adapter_type"] == "onebot" and catalog["items"] == [{"key": "face:14", "name": "平台表情 14"}]
    payload = {"platform_id": "ob", "native_key": "face:14", "name": "微笑", "tags": ["友好"], "collection": "常用", "idempotency_key": "native-one"}
    result = _json_body(await _request("/api/group-chat/stickers/native", "POST", json.dumps(payload).encode()))
    assert result["ok"] and result["sticker"]["kind"] == "native" and "native_key" not in result["sticker"]
    response = await _request("/api/group-chat/stickers/native", "POST", json.dumps({**payload, "native_key": "face:9999", "idempotency_key": "native-other"}).encode())
    assert response.startswith(b"HTTP/1.1 400")

@pytest.fixture()
def _group_chat_stickers_managed_media_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    monkeypatch.setattr(paths, "_configured_media_roots", ())

def metadata(key="upload-one"):
    return {"name": "收到", "tags": ["确认", "回复"], "collection": "常用", "idempotency_key": key}

@pytest.mark.usefixtures('_group_chat_stickers_managed_media_layout')
def test_library_defaults_scope_compatibility_cursor_and_cas(tmp_path: Path):
    store = StickerStore(_group_chat_stickers_StorageLayout(tmp_path))
    scope = MessageScope("future", "bot:a", "circle", "群/a")
    foreign = MessageScope("future", "bot:b", "circle", "群/a")
    image = store.create(metadata(), png())["sticker"]
    assert store.create(metadata(), png())["replayed"]
    native = store.create(metadata("native"), adapter_type="future", native_key="emoji:hello")["sticker"]
    assert store.list(scope=scope, formats=("image/png",))["items"] == []
    settings = {"collections": ["常用"], "expected_revision": 0, "idempotency_key": "enable"}
    assert store.settings(scope, settings)["revision"] == 1
    assert store.settings(scope, settings)["revision"] == 1
    assert len(store.list(scope=scope, adapter_type="future", formats=("image/png",), native=True)["items"]) == 2
    assert store.list(scope=foreign, formats=("image/png",))["items"] == []
    assert len(store.list(scope=scope, adapter_type="other", formats=("image/png",), native=True)["items"]) == 1
    page = store.list(scope=scope, adapter_type="future", formats=("image/png",), native=True, limit=1)
    assert page["has_more"]
    with pytest.raises(ValueError):
        store.list(scope=foreign, formats=("image/png",), cursor=page["next_cursor"])
    update = {"name": "知道了", "tags": ["确认"], "collection": "常用", "enabled": False,
              "expected_revision": 1, "idempotency_key": "disable"}
    assert store.mutate(image["sticker_id"], update)["sticker"]["content_revision"] == 2
    assert store.mutate(image["sticker_id"], update)["replayed"]
    with pytest.raises(GroupChatError):
        store.mutate(image["sticker_id"], {**update, "name": "覆盖旧值", "idempotency_key": "other-intent"})
    with pytest.raises(GroupChatError):
        store.acquire(scope, image["sticker_id"], "future", ("image/png",), True)
    row, lease = store.acquire(scope, native["sticker_id"], "future", ("image/png",), True)
    assert row["native_key"] == "emoji:hello" and lease is None
    with pytest.raises(GroupChatError):
        store.acquire(scope, native["sticker_id"], "other", ("image/png",), True)

@pytest.mark.usefixtures('_group_chat_stickers_managed_media_layout')
def test_delete_during_send_and_authenticated_thumbnail(tmp_path: Path):
    store = StickerStore(_group_chat_stickers_StorageLayout(tmp_path))
    scope = MessageScope("future", "bot", "group", "群")
    image = store.create(metadata(), png())["sticker"]
    store.settings(scope, {"collections": ["常用"], "expected_revision": 0, "idempotency_key": "enable"})
    assert store.preview(image["sticker_id"])["preview"].startswith("data:image/png;base64,")
    row, lease = store.acquire(scope, image["sticker_id"], "future", ("image/png",), False)
    assert lease is not None
    payload = {"expected_revision": 1, "idempotency_key": "delete-one"}
    assert store.mutate(image["sticker_id"], payload, delete=True)["deleted"]
    assert store.mutate(image["sticker_id"], payload, delete=True)["replayed"]
    with pytest.raises(GroupChatError):
        store.acquire(scope, image["sticker_id"], "future", ("image/png",), False)
    store.purge()
    assert lease.path.exists()
    lease.release()
    store.purge()
    assert not lease.path.exists() and not store.list()["items"]
    with pytest.raises(GroupChatError):
        store.preview(image["sticker_id"])

@pytest.mark.usefixtures('_group_chat_stickers_managed_media_layout')
def test_control_process_purge_respects_backend_file_lease(tmp_path: Path):
    import subprocess
    import sys
    import os

    store = StickerStore(_group_chat_stickers_StorageLayout(tmp_path))
    scope = MessageScope("future", "bot", "group", "群")
    image = store.create(metadata(), png())["sticker"]
    store.settings(scope, {"collections": ["常用"], "expected_revision": 0, "idempotency_key": "enable"})
    row, lease = store.acquire(scope, image["sticker_id"], "future", ("image/png",), False)
    assert lease is not None
    store.mutate(image["sticker_id"], {"expected_revision": 1, "idempotency_key": "delete"}, delete=True)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    code = "import sys; from satrap.core.group_chat.stickers import StickerStore; from satrap.core.storage.layout import StorageLayout; StickerStore(StorageLayout(sys.argv[1])).purge()"
    try:
        subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        assert lease.path.is_file()
    finally:
        lease.release()
    store.purge()
    assert not lease.path.exists()

@pytest.mark.usefixtures('_group_chat_stickers_managed_media_layout')
@pytest.mark.asyncio
async def test_reply_sticker_authorization_is_rechecked_before_commit(tmp_path: Path):
    from support.group_reply import setup, owner, SCOPE, send_mock
    from satrap.core.group_chat.stickers import library_for
    from satrap.core.group_chat.assets import _LEASES

    adapter, event, service = setup(tmp_path)
    store = library_for(adapter.message_archive)
    sticker = store.create(metadata(), png())["sticker"]
    with owner(event) as turn:
        args = {"components": [{"type": "sticker", "sticker_id": sticker["sticker_id"]}]}
        result = await service.execute("group_chat_reply", args)
        assert result["error"]["code"] == "asset_unavailable" and not _LEASES
        store.settings(SCOPE, {"collections": ["常用"], "expected_revision": 0, "idempotency_key": "enable"})
        assert (await service.execute("group_chat_reply", args))["status"] == "prepared"
        store.settings(SCOPE, {"collections": [], "expected_revision": 1, "idempotency_key": "disable"})
        await turn.commit("没有二次发送")
    send_mock(adapter).assert_not_awaited()
    assert not _LEASES

@pytest.mark.usefixtures('_group_chat_stickers_managed_media_layout')
@pytest.mark.asyncio
async def test_native_sticker_and_image_keep_original_onebot_order(tmp_path: Path):
    from support.group_reply import setup, owner, SCOPE, send_mock
    from satrap.core.group_chat.stickers import library_for
    from satrap.core.group_chat.assets import AssetStore, _LEASES
    from satrap.core.components import Face, Image, Plain
    from satrap.core.platform.onebot.onebot_utils import message_chain_to_onebot_segments

    adapter, event, service = setup(tmp_path)
    library = library_for(adapter.message_archive)
    sticker = library.create(metadata(), adapter_type="onebot", native_key="face:14")["sticker"]
    library.settings(SCOPE, {"collections": ["常用"], "expected_revision": 0, "idempotency_key": "enable"})
    entry = AssetStore(adapter.message_archive).register(SCOPE, png(), source_message_id="77")
    with owner(event) as turn:
        result = await service.execute("group_chat_reply", {"components": [
            {"type": "text", "text": "收到"}, {"type": "sticker", "sticker_id": sticker["sticker_id"]},
            {"type": "image", "asset_id": entry["asset_id"]}, {"type": "text", "text": "后"},
        ]})
        assert result["ok"]
        await turn.commit("忽略重复正文")
        chain = send_mock(adapter).call_args.args[1]
        assert [type(component) for component in chain.components] == [Plain, Face, Image, Plain]
        segments = await message_chain_to_onebot_segments(chain)
        assert [segment["type"] for segment in segments] == ["text", "face", "image", "text"]
        assert segments[1]["data"]["id"] == "14"
    assert not _LEASES
