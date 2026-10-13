"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import base64
import json
import sqlite3
import pytest
from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.reminders import ReminderStore
from satrap.core.platform.scheduled import ScheduledTarget

SCOPE = MessageScope("future-platform", "bot:/账号", "group", "group:/群")

async def allowed():
    return True

def setup_store(tmp_path: Path) -> tuple[ReminderStore, list[float]]:
    now = [1791040000.0]
    store = ReminderStore(tmp_path / "platform.db", clock=lambda: now[0])
    return store, now

def create(store: ReminderStore, operation: str = "create", actor: str = "member", **kwargs: Any) -> dict[str, Any]:
    return store.create(SCOPE, actor=actor, text="检查联调结果", mentions=["member"], source_message_id="m1", operation_id=operation,
                        time_spec={"after_seconds": 10}, **kwargs)["reminder"]

def target(reminder: dict[str, Any], attempt: str = "attempt-1") -> ScheduledTarget:
    return ScheduledTarget(SCOPE, (object(), 1), "policy", reminder["reminder_id"], attempt, allowed)
