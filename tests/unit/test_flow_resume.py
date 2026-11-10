"""重启恢复: 阈值位置与状态槽持久化, 未完成批次重新触发 (P2-3)"""
import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.config.platform_schema import ensure_platform_tables
from satrap.edictum.flow.session import AsyncFlowSession
from satrap.edictum.flow.store import FlowStore


def _flow_definition() -> dict:
    """阈值 -> WF 的最小流程: 阈值 messages=2 驱动 wf1"""
    return {
        "version": 3,
        "nodes": [
            {"id": "src", "type": "source", "title": "群消息", "config": {}},
            {"id": "t1", "type": "threshold", "title": "阈值",
             "config": {"messages": 2, "chars": 0, "idle_seconds": 0, "min_messages": 1, "max_batch": 50, "on_new": "drop"}},
            {"id": "w1", "type": "react_workflow", "title": "后台",
             "config": {"model_name": "", "max_steps": 1, "max_calls_per_run": 0, "on_busy": "queue",
                        "prompt": "", "plugins": "", "keep_history": False}},
        ],
        "edges": [
            {"from": "src.out", "to": "t1.messages"},
            {"from": "t1.event", "to": "w1.trigger"},
        ],
    }


def _archive(db: Path, scope: MessageScope, n: int) -> PlatformMessageStore:
    store = PlatformMessageStore(db, scope.adapter_id)
    for i in range(n):
        store.record(scope, ArchiveMessage(
            message_id=f"m{i}", sender_id="u", message_time=time.time(),
            text=f"text{i}", direction="inbound",
        ))
    return store


class _FakeAdapter:
    def __init__(self, store: PlatformMessageStore, scope: MessageScope) -> None:
        self.message_archive = store
        self._scope = scope

    def message_archive_scope(self, message):  # noqa: ANN001
        return self._scope


class _FakeEvent:
    """最小 MessageEvent 替身: observe 只用到这些成员"""

    def __init__(self, adapter, message) -> None:
        self.adapter = adapter
        self.platform_message = message
        self._extra: dict = {}

    def set_extra(self, key, value) -> None:
        self._extra[key] = value

    def is_private_chat(self) -> bool:
        return False

    def get_message_str(self) -> str:
        return self.platform_message.text

    @property
    def call_origin(self):
        class O: source_message_id = "m"; actor_id = "u"
        return O()


class _Msg:
    def __init__(self, scope: MessageScope) -> None:
        self.self_id = scope.self_id
        self.group_id = scope.chat_id
        self.session_id = scope.chat_id
        self.received_at = time.time()
        self.text = "hi"

        class S: user_id = "u"
        self.sender = S()


def test_restart_recovers_positions_states_and_retriggers_batch():
    """会话重建后阈值位置/状态槽不变, 落后批次按已提交位置重新触发"""
    db = Path(tempfile.mkdtemp()) / "p.db"
    conn = sqlite3.connect(db); ensure_platform_tables(conn); conn.close()
    scope = MessageScope("adapter1", "self1", "group", "chat1")
    store = _archive(db, scope, 5)

    # 模拟重启前: t1 驱动 w1 的位置停在 seq=2 (还有 3 条未消费)
    fs = FlowStore(db, "a", "sess1"); fs.scope_key = scope.key
    conn = sqlite3.connect(db); conn.execute("BEGIN")
    fs.ensure_position(conn, scope.key, "t1", "w1")
    fs.commit_position(conn, scope.key, "t1", "w1", 2)
    conn.execute("COMMIT"); conn.close()

    # 新建会话 = '重启'; flow_name 用同一 session_id 才读到同一组位置
    session = AsyncFlowSession(
        "sess1", llm=None,
        flow=_flow_definition(),
        plugin_catalog=None,
        message_store=store,
    )
    session._flow_store = FlowStore(db, "a", "sess1")
    session._flow_store.scope_key = scope.key
    session._scope_key = scope.key

    fired: list[tuple[str, object]] = []
    async def spy(wf_id, event):
        fired.append((wf_id, event))
    # 不建真 WF: _async_init 需要 llm; 手工装配最小集
    async def fake_run(wf_id, event):
        fired.append((wf_id, event))
        return None
    session._on_wf_trigger = fake_run  # type: ignore[assignment]

    async def _init():
        from satrap.edictum.flow.definition import migrate_flow_definition, parse_flow_definition
        session._refresh_registry()
        definition, _, _ = migrate_flow_definition(parse_flow_definition(session._raw_flow), session._registry)
        session._definition = definition
        session._node_types = {n.id: session._registry.get(n.type) for n in definition.nodes.values()}
        session._initialized = True
    asyncio.run(_init())

    # 恢复: 已提交位置 seq=2, 积压 [3,4,5] -> 应触发一次批次
    async def _resume():
        await session._ensure_engine()
        await session._check_thresholds()
    asyncio.run(_resume())

    assert fired, "重启后落后的批次应重新触发"
    wf_id, event = fired[0]
    assert wf_id == "w1" and event.kind == "batch"
    assert event.batch_from == 2 and event.batch_to == 5
    assert len(event.batch) == 3


def test_state_and_positions_survive_recycle():
    """FlowStore 重建后读到同一库的版本与位置"""
    db = Path(tempfile.mkdtemp()) / "p.db"
    conn = sqlite3.connect(db); ensure_platform_tables(conn); conn.close()
    scope = MessageScope("a", "s", "group", "c")

    from satrap.edictum.flow.store import StateCommit
    fs = FlowStore(db, "a", "f"); fs.scope_key = scope.key
    fs.commit_states_by_scope_key(scope.key, "t1", "w1", 7,
                                  [StateCommit("st", "内容", base_version=0, covers_to=5, run_id="r")])

    fs2 = FlowStore(db, "a", "f")
    assert fs2.state_by_scope_key(scope.key, "st")["version"] == 1
    assert fs2.positions_for_tile_by_scope_key(scope.key, "t1") == {"w1": 7}
