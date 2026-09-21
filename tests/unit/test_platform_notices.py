"""平台通知/请求事件的归一, 过滤, 去重与分发"""
from pathlib import Path
from typing import Any
import asyncio

import pytest

from satrap.core.platform.notices import (
    NoticePayload,
    PlatformEventHub,
    build_onebot_notice,
    notice_attachment,
    set_current_hub,
)
from satrap.core.components.message import File
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig, PlatformEvent


@pytest.fixture(autouse=True)
def _reset_current_hub():
    """每个用例前后复位进程级通知 hub, 隔离其他测试文件的 BackendManager 残留"""
    set_current_hub(None)
    yield
    set_current_hub(None)


def raw_notice(**extra: object) -> dict[str, Any]:
    return {"post_type": "notice", "notice_type": "group_increase", "sub_type": "approve", "self_id": 10000,
            "group_id": 456, "user_id": 321, "operator_id": 111, "time": 1700000000, **extra}


def test_build_onebot_notice_narrows_fields_and_rejects_foreign_account():
    payload = build_onebot_notice(raw_notice(file={"id": "f1", "name": "a.txt", "size": 3, "secret": "x"}), "10000")
    assert payload is not None
    assert (payload.category, payload.kind, payload.sub_type, payload.group_id, payload.operator_id) == ("notice", "group_increase", "approve", "456", "111")
    assert payload.file == {"id": "f1", "name": "a.txt", "size": 3}
    assert build_onebot_notice(raw_notice(self_id=2), "10000") is None
    assert build_onebot_notice({"post_type": "message"}, "10000") is None
    request = build_onebot_notice({"post_type": "request", "request_type": "friend", "user_id": 5, "flag": "abc", "comment": "hi"}, "")
    assert request is not None and (request.kind, request.flag, request.comment) == ("friend", "abc", "hi")


def test_dedup_key_uses_stable_fields_only():
    a = build_onebot_notice(raw_notice(), "10000")
    b = build_onebot_notice(raw_notice(), "10000")
    c = build_onebot_notice(raw_notice(user_id=999), "10000")
    assert a is not None and b is not None and c is not None
    assert a.dedup_key("ob") == b.dedup_key("ob") != c.dedup_key("ob")
    assert a.dedup_key("ob") != a.dedup_key("other")


@pytest.mark.asyncio
async def test_hub_dispatches_once_per_event_and_deduplicates(monkeypatch: pytest.MonkeyPatch):
    hub = PlatformEventHub(dedup_ttl=10)
    seen: list[str] = []

    async def on_increase(event: PlatformEvent) -> None:
        seen.append(f"exact:{event.group_id}")

    def on_any(event: PlatformEvent) -> None:
        seen.append(f"any:{event.event_type}")

    unsubscribe = hub.subscribe("notice.group_increase", on_increase)
    hub.subscribe("*", on_any)
    payload = build_onebot_notice(raw_notice(), "10000")
    event = PlatformEvent("ob", "onebot", "notice.group_increase", group_id="456", extras={"payload": payload})
    hub(event)
    hub(event)
    await asyncio.sleep(0.02)
    assert seen == ["exact:456", "any:notice.group_increase"]
    assert hub.stats["duplicate"] == 1 and hub.stats["dispatched"] == 2
    unsubscribe()
    unsubscribe()
    other = PlatformEvent("ob", "onebot", "notice.group_increase", group_id="457", extras={"payload": build_onebot_notice(raw_notice(group_id=457), "10000")})
    hub(other)
    await asyncio.sleep(0.02)
    assert seen[-1] == "any:notice.group_increase" and len(seen) == 3


@pytest.mark.asyncio
async def test_hub_without_subscribers_counts_and_handler_errors_are_isolated():
    hub = PlatformEventHub()
    hub(PlatformEvent("ob", "onebot", "notice.group_recall"))
    assert hub.stats["unsubscribed"] == 1

    def boom(event: PlatformEvent) -> None:
        raise RuntimeError("bad")

    calls: list[str] = []
    hub.subscribe("request.friend", boom)
    hub.subscribe("request.friend", lambda event: calls.append(event.user_id))
    hub(PlatformEvent("ob", "onebot", "request.friend", user_id="5"))
    await asyncio.sleep(0.02)
    assert calls == ["5"] and hub.stats["failed"] == 1 and hub.stats["dispatched"] == 1


@pytest.mark.asyncio
async def test_hub_inflight_bound_and_close_cancels_tasks():
    hub = PlatformEventHub(max_inflight=2)
    gate = asyncio.Event()

    async def slow(event: PlatformEvent) -> None:
        await gate.wait()

    hub.subscribe("*", slow)
    for i in range(3):
        hub(PlatformEvent("ob", "onebot", "notice.x", extras={"payload": NoticePayload("notice", "x", message_id=str(i))}))
    await asyncio.sleep(0)
    assert hub.stats["dropped"] == 1 and len(hub._tasks) == 2
    await hub.close()
    assert not hub._tasks
    hub(PlatformEvent("ob", "onebot", "notice.x"))
    assert hub.stats["dropped"] == 2


@pytest.mark.asyncio
async def test_onebot_emits_typed_platform_event_and_respects_filters():
    received: list[PlatformEvent] = []
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}), event_handler=received.append)
    adapter.bot_self_id = "10000"
    await adapter._handle_notice(raw_notice())
    assert len(received) == 1
    event = received[0]
    assert event.event_type == "notice.group_increase" and event.session_id == "group%456" and event.group_id == "456"
    assert isinstance(event.extras["payload"], NoticePayload)
    assert adapter._event_queue.empty()

    await adapter._handle_request({"post_type": "request", "request_type": "group", "sub_type": "add", "self_id": 10000, "group_id": 456, "user_id": 7, "flag": "f"})
    assert received[-1].event_type == "request.group"

    adapter.config.settings["group_whitelist"] = ["789"]
    await adapter._handle_notice(raw_notice())
    assert len(received) == 2
    await adapter._handle_request({"post_type": "request", "request_type": "friend", "self_id": 10000, "user_id": 7, "flag": "f"})
    assert received[-1].event_type == "request.friend" and received[-1].session_id == "private%7"

    adapter.config.settings.pop("group_whitelist")
    adapter.config.settings["notice_types"] = ["notice.group_recall", "request"]
    await adapter._handle_notice(raw_notice())
    assert len(received) == 3
    await adapter._handle_notice(raw_notice(notice_type="group_recall", message_id=9))
    assert received[-1].event_type == "notice.group_recall"
    await adapter._handle_notice(raw_notice(self_id=2))
    assert adapter.get_stats()["ingress"]["account"] == 1


@pytest.mark.asyncio
async def test_notice_never_reaches_message_pipeline():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    await adapter._handle_notice(raw_notice())
    assert adapter._event_queue.empty()


@pytest.mark.parametrize("value", [["message"], ["notice.Group"], "notice", ["notice"] * 65])
def test_notice_types_validation(value: object):
    with pytest.raises(ValueError, match="notice_types"):
        validate_wake_policy({"notice_types": value})


def test_notice_types_accepts_categories_and_specific_types():
    validate_wake_policy({"notice_types": ["notice", "request.friend", "notice.group_upload"]})


@pytest.mark.asyncio
async def test_backend_registers_hub_for_plugins_and_reports_stats(tmp_path: Path):
    import json
    from satrap.core.platform.notices import current_hub
    from satrap.core.backend.BackendManager import BackendManager
    from satrap.core.config.loader import ConfigLoader

    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps({"data_root": str(tmp_path / "data"), "platforms": [
        {"id": "bot", "type": "onebot", "settings": {"port": 0}, "enable": False}]}), encoding="utf-8")
    manager = BackendManager(ConfigLoader.from_json(config_path))
    assert current_hub() is None
    await manager._init_platforms()
    try:
        assert current_hub() is manager.platform_events
        assert manager._adapter_mgr is not None
        adapter = manager._adapter_mgr.get_adapter("bot")
        assert adapter is not None and adapter.event_handler is manager.platform_events
        health = await manager.health()
        assert health["platform_events"]["received"] == 0
    finally:
        await manager.stop()
    assert current_hub() is None
    closed_hub = manager.platform_events
    assert closed_hub.closed
    await manager._init_platforms()
    try:
        assert manager.platform_events is not closed_hub and not manager.platform_events.closed
        assert current_hub() is manager.platform_events
    finally:
        await manager.stop()


def upload_notice(**extra: object) -> dict[str, Any]:
    return {"post_type": "notice", "notice_type": "group_upload", "self_id": 10000, "group_id": 456,
            "user_id": 321, "time": 1700000000,
            "file": {"id": "/file/abc", "name": "报表.xlsx", "size": 2048, "busid": 102, "url": "https://dl.example.com/f"},
            **extra}


def test_notice_attachment_normalizes_group_upload_only():
    payload = build_onebot_notice(upload_notice(), "10000")
    assert payload is not None
    attachment = notice_attachment(payload)
    assert isinstance(attachment, File)
    assert attachment.name == "报表.xlsx" and attachment.file_ == "/file/abc"
    assert attachment.url == "https://dl.example.com/f"
    assert payload.file.get("size") == 2048 and payload.file.get("busid") == 102

    other = build_onebot_notice(raw_notice(), "10000")
    assert other is not None and notice_attachment(other) is None
    request = build_onebot_notice({"post_type": "request", "request_type": "friend", "user_id": 5, "flag": "f"}, "10000")
    assert request is not None and notice_attachment(request) is None


def test_notice_attachment_requires_remote_handle_and_tolerates_missing_fields():
    no_file = build_onebot_notice(upload_notice(file=None), "10000")
    assert no_file is not None and no_file.file == {}
    assert notice_attachment(no_file) is None
    no_handle = build_onebot_notice(upload_notice(file={"name": "x.txt", "size": 1}), "10000")
    assert no_handle is not None and notice_attachment(no_handle) is None
    id_only = build_onebot_notice(upload_notice(file={"id": 42}), "10000")
    assert id_only is not None
    attachment = notice_attachment(id_only)
    assert attachment is not None and attachment.name == "file" and attachment.file_ == "42" and not attachment.url


@pytest.mark.asyncio
async def test_group_upload_notice_carries_attachment_component():
    received: list[PlatformEvent] = []
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}), event_handler=received.append)
    adapter.bot_self_id = "10000"
    await adapter._handle_notice(upload_notice())
    assert len(received) == 1
    event = received[0]
    assert event.event_type == "notice.group_upload" and event.session_id == "group%456"
    attachment = event.extras.get("attachment")
    assert isinstance(attachment, File) and attachment.name == "报表.xlsx"
    assert adapter._event_queue.empty()

    await adapter._handle_notice(raw_notice())
    assert "attachment" not in received[-1].extras
