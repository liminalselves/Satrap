"""图片资产隔离, 来源撤销和异步发送租约的真实存储反例"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import asyncio

from PIL import Image
import pytest

from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.assets import AssetStore, inspect_image, _LEASES
from satrap.core.group_chat.types import GroupChatError
from satrap.core.platform.onebot.outbound import OutboundTurns


def png() -> bytes:
    """生成实际可解码的测试图片"""
    stream = BytesIO()
    Image.new("RGB", (16, 8), "red").save(stream, format="PNG")
    return stream.getvalue()


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
    from tests.unit.test_group_chat_reply import setup, owner, SCOPE, send_mock

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
    from tests.unit.test_group_chat_reply import setup, owner, SCOPE, send_mock
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
