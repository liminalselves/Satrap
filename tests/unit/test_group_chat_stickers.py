"""表情目录的逐群授权, 编辑修订, 内容不可替换和删除租约"""
from pathlib import Path
import pytest

from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.stickers import StickerStore
from satrap.core.group_chat.types import GroupChatError
from satrap.core.storage.layout import StorageLayout
from .test_group_chat_assets import png
from satrap.core.utils import paths


@pytest.fixture(autouse=True)
def managed_media_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    monkeypatch.setattr(paths, "_configured_media_roots", ())


def metadata(key="upload-one"):
    return {"name": "收到", "tags": ["确认", "回复"], "collection": "常用", "idempotency_key": key}


def test_library_defaults_scope_compatibility_cursor_and_cas(tmp_path: Path):
    store = StickerStore(StorageLayout(tmp_path))
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


def test_delete_during_send_and_authenticated_thumbnail(tmp_path: Path):
    store = StickerStore(StorageLayout(tmp_path))
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


def test_control_process_purge_respects_backend_file_lease(tmp_path: Path):
    import subprocess
    import sys
    import os

    store = StickerStore(StorageLayout(tmp_path))
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


@pytest.mark.asyncio
async def test_reply_sticker_authorization_is_rechecked_before_commit(tmp_path: Path):
    from .test_group_chat_reply import setup, owner, SCOPE, send_mock
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


@pytest.mark.asyncio
async def test_native_sticker_and_image_keep_original_onebot_order(tmp_path: Path):
    from .test_group_chat_reply import setup, owner, SCOPE, send_mock
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
