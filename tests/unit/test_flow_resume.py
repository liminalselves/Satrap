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
from satrap.edictum.flow.store import FlowStore, StateCommit


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
    """会话重建后阈值位置/状态槽不变, 落后批次按已提交位置重新触发 (经会话层 _run_workflow)"""
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

    session = AsyncFlowSession(
        "sess1", llm=None,
        flow=_flow_definition(),
        plugin_catalog=None,
        message_store=store,
    )
    session._flow_store = FlowStore(db, "a", "sess1")
    session._flow_store.scope_key = scope.key
    session._scope_key = scope.key
    session._bound_scope = scope

    fired: list[tuple[str, int, int]] = []
    # 经会话层 _run_workflow 验证: 它应被调用并拿到正确批次边界
    async def fake_run(wf_id, event):
        fired.append((wf_id, event.batch_from, event.batch_to))
        # 模拟成功: 推进位置
        session._flow_store.commit_states_by_scope_key(
            scope.key, event.tile, wf_id, event.batch_to, [])
    session._on_wf_trigger = fake_run  # type: ignore[assignment]

    async def _init():
        from satrap.edictum.flow.definition import migrate_flow_definition, parse_flow_definition
        session._refresh_registry()
        definition, _, _ = migrate_flow_definition(parse_flow_definition(session._raw_flow), session._registry)
        session._definition = definition
        session._node_types = {n.id: session._registry.get(n.type) for n in definition.nodes.values()}
        session._refresh_resolved()
        session._initialized = True
    asyncio.run(_init())

    async def _resume():
        await session._ensure_engine()
        await session._check_thresholds()
        await asyncio.sleep(0.05)
    asyncio.run(_resume())

    # fired 经 fire -> _on_wf_trigger -> fake_run 走到
    assert fired, "重启后落后的批次应重新触发"
    wf_id, batch_from, batch_to = fired[0]
    assert wf_id == "w1" and batch_from == 2 and batch_to == 5


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


def _flow_with_state_and_threshold() -> dict:
    """阈值 + WF + 状态槽: 验证 /new 的四种 on_new 组合"""
    return {
        "version": 3,
        "nodes": [
            {"id": "src", "type": "source", "title": "群消息", "config": {}},
            {"id": "t1", "type": "threshold", "title": "阈值",
             "config": {"messages": 1, "chars": 0, "idle_seconds": 0, "min_messages": 1, "max_batch": 50,
                        "on_new": "drop"}},
            {"id": "s1", "type": "state", "title": "槽",
             "config": {"on_new": "keep", "max_chars": 6000, "schema": "text"}},
            {"id": "w1", "type": "react_workflow", "title": "后台",
             "config": {"model_name": "", "max_steps": 1, "max_calls_per_run": 0, "on_busy": "queue",
                        "prompt": "", "plugins": "flow_snapshot", "keep_history": False}},
        ],
        "edges": [
            {"from": "src.out", "to": "t1.messages"},
            {"from": "t1.event", "to": "w1.trigger"},
            {"from": "w1.text", "to": "s1.write"},
        ],
    }


def _bind_session(session: AsyncFlowSession, db: Path, scope: MessageScope, store: PlatformMessageStore,
                  *, new_from: str | None = None, history_start: float | None = None) -> None:
    """装配一个不接 LLM 的最小流程会话, 供 on_new 结算测试"""
    session._flow_store = FlowStore(db, "a", session._flow_name())
    session._flow_store.scope_key = scope.key
    session._scope_key = scope.key
    session._message_store = store
    if new_from is not None:
        session._new_from = new_from
        session._new_done = False
    if history_start is not None:
        session._history_start = history_start


def test_new_settle_clear_state_and_drop_position():
    """/new: state on_new=clear 清零; threshold on_new=drop 跳到最新 seq"""
    db = Path(tempfile.mkdtemp()) / "p.db"
    conn = sqlite3.connect(db); ensure_platform_tables(conn); conn.close()
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 4)

    # 预置: 位置停在 seq=2, 状态槽已有版本
    fs = FlowStore(db, "a", "f1"); fs.scope_key = scope.key
    conn = sqlite3.connect(db); conn.execute("BEGIN")
    fs.ensure_position(conn, scope.key, "t1", "w1")
    fs.commit_position(conn, scope.key, "t1", "w1", 2)
    conn.execute("COMMIT"); conn.close()
    fs.commit_states_by_scope_key(scope.key, "t1", "w1", 2,
                                  [StateCommit("s1", "旧快照", base_version=0, covers_to=2, run_id="r")])

    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drop"
    flow["nodes"][2]["config"]["on_new"] = "clear"
    session = AsyncFlowSession("s2", llm=None, flow=flow, message_store=store, flow_name="f1")
    _bind_session(session, db, scope, store, new_from="s1")
    asyncio.run(_init_def(session))
    asyncio.run(session._settle_new())

    # state 清回 0, 位置跳到 4
    assert session._flow_store.state_by_scope_key(scope.key, "s1")["version"] == 0
    assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 4


def test_new_settle_keep_state_and_drain():
    """/new: state keep 保留; threshold drain 把积压批次跑完再推进位置"""
    db = Path(tempfile.mkdtemp()) / "p.db"
    conn = sqlite3.connect(db); ensure_platform_tables(conn); conn.close()
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 4)

    fs = FlowStore(db, "a", "f1"); fs.scope_key = scope.key
    conn = sqlite3.connect(db); conn.execute("BEGIN")
    fs.ensure_position(conn, scope.key, "t1", "w1")
    fs.commit_position(conn, scope.key, "t1", "w1", 2)
    conn.execute("COMMIT"); conn.close()
    fs.commit_states_by_scope_key(scope.key, "t1", "w1", 2,
                                  [StateCommit("s1", "旧快照", base_version=0, covers_to=2, run_id="r")])

    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    flow["nodes"][2]["config"]["on_new"] = "keep"
    session = AsyncFlowSession("s2", llm=None, flow=flow, message_store=store, flow_name="f1")
    _bind_session(session, db, scope, store, new_from="s1")
    asyncio.run(_init_def(session))

    ran: list[tuple[str, int, int]] = []
    async def fake_run(wf_id, event):
        ran.append((wf_id, event.batch_from, event.batch_to))
        # 模拟成功运行: 位置推进到批次末尾
        session._flow_store.commit_states_by_scope_key(
            session._scope_key, event.tile, wf_id, event.batch_to, [])
    session._run_workflow = fake_run  # type: ignore[assignment]
    asyncio.run(session._settle_new())

    # drain: 积压 [3,4] 应触发批次, 位置推进到 4
    assert ran == [("w1", 2, 4)]
    assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 4
    # state keep: 版本不变
    assert session._flow_store.state_by_scope_key(scope.key, "s1")["version"] == 1


def test_new_drain_timeout_falls_back_to_drop():
    """/new drain 超过 120s 或批次不推进位置时, 剩余按 drop 结算"""
    db = Path(tempfile.mkdtemp()) / "p.db"
    conn = sqlite3.connect(db); ensure_platform_tables(conn); conn.close()
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 4)

    fs = FlowStore(db, "a", "f1"); fs.scope_key = scope.key
    conn = sqlite3.connect(db); conn.execute("BEGIN")
    fs.ensure_position(conn, scope.key, "t1", "w1")
    fs.commit_position(conn, scope.key, "t1", "w1", 2)
    conn.execute("COMMIT"); conn.close()

    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    session = AsyncFlowSession("s2", llm=None, flow=flow, message_store=store, flow_name="f1")
    _bind_session(session, db, scope, store, new_from="s1")
    asyncio.run(_init_def(session))

    async def failing_run(wf_id, event):
        # 运行"失败": 不推进位置 -> 下一轮 while 检测到 committed<=batch_from 转 drop
        return None
    session._run_workflow = failing_run  # type: ignore[assignment]
    asyncio.run(session._settle_new())

    # 跑不动 -> 位置被 drop 到最新
    assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 4


async def _init_def(session: AsyncFlowSession) -> None:
    """手工初始化 definition/node_types/flow_store, 跳过 WF 装配"""
    from satrap.edictum.flow.definition import migrate_flow_definition, parse_flow_definition
    session._refresh_registry()
    definition, _, _ = migrate_flow_definition(
        parse_flow_definition(session._raw_flow), session._registry)
    session._definition = definition
    session._node_types = {n.id: session._registry.get(n.type) for n in definition.nodes.values()}
    session._initialized = True
