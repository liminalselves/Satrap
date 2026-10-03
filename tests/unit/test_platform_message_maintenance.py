"""平台消息自动保留策略, 失效隔离与后台任务生命周期"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sqlite3
from time import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.config.platform_schema import PLATFORM_SCHEMA_VERSION
from satrap.core.storage.layout import StorageLayout
from satrap.core.storage.maintenance import StorageMaintenanceService


def _item(store: PlatformMessageStore, scope: MessageScope, message_id: str) -> dict[str, Any]:
    """
    核验被维护的消息仍保留身份标记

    参数:
    - store: 实际档案存储
    - scope: 预期消息范围
    - message_id: 已入档消息 ID

    返回:
    - 持久化的正文或擦除状态
    """
    item = store.get(scope, message_id)
    assert item is not None
    return item


def _record(layout: StorageLayout, platform_id: str, *, age: int, retention: int = 30) -> MessageScope:
    """
    在过去的接收时间生成真实平台数据库档案

    参数:
    - layout: 测试存储布局
    - platform_id: 平台实例身份
    - age: 距当前时间的消息天数
    - retention: 首次采集时的保留天数

    返回:
    - 已采集消息所属对话身份
    """
    layout.ensure_platform(platform_id)
    now = time() - age * 86400
    scope = MessageScope(platform_id, "bot", "group", "group")
    store = PlatformMessageStore(layout.platform_db(platform_id), platform_id, retention_days=retention, clock=lambda: now)
    store.record(scope, ArchiveMessage(str(age), "member", now, "原文"))
    return scope


def test_configured_and_detached_platforms_expire_under_last_policy(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path)
    configured = _record(layout, "configured", age=3)
    detached = _record(layout, "detached", age=10, retention=7)
    retained = _record(layout, "detached", age=3, retention=7)
    result = StorageMaintenanceService(layout).expire_platform_messages({"configured": 1, "cold": 1})
    assert {item["platform_id"]: item["expired_count"] for item in result["items"]} == {"configured": 1, "detached": 1}
    configured_store = PlatformMessageStore(layout.platform_db("configured"), "configured", retention_days=1)
    detached_store = PlatformMessageStore(layout.platform_db("detached"), "detached", retention_days=7)
    assert _item(configured_store, configured, "3")["status"] == "expired"
    assert _item(detached_store, detached, "10")["status"] == "expired"
    assert _item(detached_store, retained, "3")["text"] == "原文"
    assert configured_store.saved_retention_days() == 1 and detached_store.saved_retention_days() == 7
    assert not layout.platform_db("cold").exists()


def test_corrupt_platform_does_not_stop_other_platform_maintenance(tmp_path: Path, caplog) -> None:
    layout = StorageLayout(tmp_path)
    layout.ensure_platform("broken")
    layout.platform_db("broken").write_bytes(b"not sqlite")
    scope = _record(layout, "good", age=3)
    result = StorageMaintenanceService(layout).expire_platform_messages({"broken": 1, "good": 1})
    assert any(item["platform_id"] == "broken" and item["ok"] is False for item in result["items"])
    assert _item(PlatformMessageStore(layout.platform_db("good"), "good"), scope, "3")["status"] == "expired"
    assert "自动维护失败" in caplog.text


def test_manifest_cannot_redirect_to_other_platform(tmp_path: Path, caplog) -> None:
    layout = StorageLayout(tmp_path)
    scope = _record(layout, "saved", age=3)
    manifest = layout.platform_root("saved") / "platform.json"
    manifest.write_text(json.dumps({"platform_id": "other"}), encoding="utf-8")
    result = StorageMaintenanceService(layout).expire_platform_messages({})
    assert result["items"] == [{"platform_id": "", "ok": False, "error": "invalid_manifest"}]
    assert "身份与目录不一致" in caplog.text
    assert _item(PlatformMessageStore(layout.platform_db("saved"), "saved"), scope, "3")["text"] == "原文"


def test_v4_archive_policy_migrates_without_losing_messages(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path)
    scope = _record(layout, "legacy", age=3)
    database = layout.platform_db("legacy")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE platform_message_policy")
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'group_chat_%'").fetchall():
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute("PRAGMA user_version=4")
    store = PlatformMessageStore(database, "legacy", retention_days=7)
    assert _item(store, scope, "3")["text"] == "原文"
    assert store.saved_retention_days() is None
    store.purge()
    assert store.saved_retention_days() == 7
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION


def test_cold_maintenance_does_not_create_storage(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path / "cold")
    assert StorageMaintenanceService(layout).expire_platform_messages({"future": 30}) == {"items": []}
    assert not layout.platforms_root.exists()


@pytest.mark.asyncio
async def test_backend_maintenance_failure_recovers_and_uses_applied_policy(tmp_path: Path, monkeypatch, caplog) -> None:
    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._storage = StorageLayout(tmp_path)
    backend._platform_apply_lock = asyncio.Lock()
    backend._running = True
    backend.config = SimpleNamespace(platforms=[{"id": "disabled", "enable": False, "settings": {"message_archive_retention_days": 5}},
                                               {"id": "active", "settings": {"message_archive_retention_days": 1}}])
    backend._platform_active_configs = {"active": {"settings": {"message_archive_retention_days": 10}}}
    observed: list[dict[str, int]] = []

    def expire(service, policies) -> dict:
        observed.append(policies)
        if len(observed) == 1:
            raise OSError("临时故障")
        backend._running = False
        return {"items": []}

    sleeper = AsyncMock()
    monkeypatch.setattr(StorageMaintenanceService, "expire_platform_messages", expire)
    module = importlib.import_module("satrap.core.backend.BackendManager")
    monkeypatch.setattr(module.asyncio, "sleep", sleeper)
    await asyncio.wait_for(backend._maintain_message_archives(), timeout=5)
    assert observed == [{"disabled": 5, "active": 10}] * 2
    assert sleeper.call_args_list[0].args == (3600,)
    assert "自动维护轮次失败" in caplog.text


@pytest.mark.asyncio
async def test_backend_stop_cancels_owned_maintenance_task() -> None:
    backend = BackendManager.__new__(BackendManager)
    backend._running = True
    backend._http_server = None
    backend._platform_apply_lock = asyncio.Lock()
    backend._dispatch_task = None
    backend._scheduler = None
    backend._adapter_mgr = None
    backend._platform_runtimes = {}
    entered, closed = asyncio.Event(), asyncio.Event()

    async def maintain() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(maintain())
    backend._message_archive_maintenance_task = task
    await entered.wait()
    await backend.stop()
    assert closed.is_set() and task.cancelled()
    assert backend._message_archive_maintenance_task is None and backend._running is False
