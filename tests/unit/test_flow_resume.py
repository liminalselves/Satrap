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
from tests.support.flow_runtime import (
    FlowAdapter, MockLLM, archive_event, database, make_event, response, tool_call, wait_until,
)


def _flow_definition() -> dict:
    """阈值 -> WF 的最小流程: 阈值 messages=2 驱动 wf1"""
    return {
        "version": 3,
        "nodes": [
            {"id": "src", "type": "source", "title": "群消息", "config": {}},
            {"id": "t1", "type": "threshold", "title": "阈值",
             "config": {"messages": 2, "chars": 0, "idle_seconds": 0, "min_messages": 1, "max_batch": 50, "on_new": "drop"}},
            {"id": "w1", "type": "react_workflow", "title": "后台",
             "config": {"model_name": "test", "max_steps": 4, "max_calls_per_run": 0, "on_busy": "queue",
                        "prompt": "", "plugins": "flow_snapshot", "keep_history": False}},
            {"id": "s1", "type": "state", "title": "快照", "config": {}},
        ],
        "edges": [
            {"from": "src.out", "to": "t1.messages"},
            {"from": "t1.event", "to": "w1.trigger"},
            {"from": "w1.text", "to": "s1.write"},
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


@pytest.mark.asyncio
async def test_restart_recovers_positions_states_and_retriggers_batch(tmp_path):
    """真实 observe 完成 _async_init, 重启后的落后批次运行真实 WF 并提交"""
    db = database(tmp_path / "single.db")
    session_db = db
    scope = MessageScope("adapter1", "self1", "group", "chat1")
    store = _archive(db, scope, 5)
    adapter = FlowAdapter(store)
    fs = FlowStore(session_db, scope.adapter_id, "sess1")
    with sqlite3.connect(session_db) as connection:
        connection.execute("BEGIN")
        fs.commit_position(connection, scope.key, "t1", "w1", 2)
        connection.execute("COMMIT")

    def script(messages, tools, count):
        if count == 1:
            return response("", [tool_call("snapshot_write", {
                "state_id": "s1", "content": "重启后快照"})])
        return response("done")

    llm = MockLLM(script)
    session = AsyncFlowSession("sess1", llm=llm, flow=_flow_definition(),
                               db_path=str(session_db), message_store=store, flow_name="sess1")
    session.error_feedback = False
    try:
        event = make_event(adapter, scope, "new", 6)
        archive_event(store, scope, event)
        await session.observe(event)
        await wait_until(lambda: len(llm.calls) == 2)
        await wait_until(lambda: not session._engine.running_workflows())
        user_input = repr(llm.calls[0]["messages"])
        assert "本批消息 #3–#6" in user_input
        assert "text2" in user_input and "new" in user_input
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 6
        state = session._flow_store.state_by_scope_key(scope.key, "s1")
        assert state["version"] == 1 and state["covers_to"] == 6
    finally:
        await session.close_flow_nodes()


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
             "config": {"model_name": "test", "max_steps": 4, "max_calls_per_run": 0, "on_busy": "queue",
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


@pytest.mark.asyncio
async def test_new_settle_keep_state_and_drain(tmp_path):
    """真实 observe 后台 drain 积压; state keep 保留旧版本, 位置由真实完成路径推进"""
    db = database(tmp_path / "single.db")
    session_db = db
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 4)
    adapter = FlowAdapter(store)
    fs = FlowStore(session_db, scope.adapter_id, "f1")
    with sqlite3.connect(session_db) as connection:
        connection.execute("BEGIN")
        fs.commit_position(connection, scope.key, "t1", "w1", 2)
        connection.execute("COMMIT")
    fs.commit_states_by_scope_key(scope.key, "t1", "w1", 2, [
        StateCommit("s1", "旧快照", base_version=0, covers_to=2, run_id="r")])

    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    flow["nodes"][2]["config"]["on_new"] = "keep"
    llm = MockLLM(response("done"))
    session = AsyncFlowSession("s2", llm=llm, flow=flow, db_path=str(session_db),
                               message_store=store, flow_name="f1", new_from="s1")
    session.error_feedback = False
    try:
        event = make_event(adapter, scope, "new", 5)
        archive_event(store, scope, event)
        await session.observe(event)
        await wait_until(lambda: len(llm.calls) == 1)
        await wait_until(lambda: session._new_done and not session._engine.running_workflows())
        assert "本批消息 #3–#5" in repr(llm.calls[0]["messages"])
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 5
        assert session._flow_store.state_by_scope_key(scope.key, "s1")["version"] == 1
    finally:
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_new_drain_failure_falls_back_to_drop(tmp_path):
    """真实 WF 失败位置不推进, drain 随后按 drop 结算到最新"""
    db = database(tmp_path / "single.db")
    session_db = db
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 4)
    adapter = FlowAdapter(store)
    fs = FlowStore(session_db, scope.adapter_id, "f1")
    with sqlite3.connect(session_db) as connection:
        connection.execute("BEGIN")
        fs.commit_position(connection, scope.key, "t1", "w1", 2)
        connection.execute("COMMIT")
    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    llm = MockLLM(lambda messages, tools, count: (_ for _ in ()).throw(RuntimeError("offline")))
    session = AsyncFlowSession("s2", llm=llm, flow=flow, db_path=str(session_db),
                               message_store=store, flow_name="f1", new_from="s1")
    session.error_feedback = False
    try:
        event = make_event(adapter, scope, "new", 5)
        archive_event(store, scope, event)
        await session.observe(event)
        await wait_until(lambda: session._new_done)
        assert len(llm.calls) == 1
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 5
    finally:
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_new_observe_nonblocking_and_replay_respects_busy(tmp_path):
    """observe 不等待 drain; 结算期间消息暂存, 回放与 drain 不并发"""
    db = database(tmp_path / "single.db")
    session_db = db
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 2)
    adapter = FlowAdapter(store)
    fs = FlowStore(session_db, scope.adapter_id, "f1")
    with sqlite3.connect(session_db) as connection:
        connection.execute("BEGIN")
        fs.commit_position(connection, scope.key, "t1", "w1", 0)
        connection.execute("COMMIT")
    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    started, release = asyncio.Event(), asyncio.Event()
    active, max_active = 0, 0

    async def script(messages, tools, count):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            if count == 1:
                started.set()
                await release.wait()
            return response("done")
        finally:
            active -= 1

    llm = MockLLM(script)
    session = AsyncFlowSession("s2", llm=llm, flow=flow, db_path=str(session_db),
                               message_store=store, flow_name="f1", new_from="s1")
    session.error_feedback = False
    try:
        first = make_event(adapter, scope, "third", 3)
        archive_event(store, scope, first)
        before = time.monotonic()
        await session.observe(first)
        assert time.monotonic() - before < 1.0
        await started.wait()
        second = make_event(adapter, scope, "fourth", 4)
        archive_event(store, scope, second)
        before = time.monotonic()
        await session.observe(second)
        assert time.monotonic() - before < 0.2
        assert len(llm.calls) == 1 and list(session._new_events) == [first, second]
        release.set()
        await wait_until(lambda: len(llm.calls) == 2 and session._new_done)
        await wait_until(lambda: not session._engine.running_workflows())
        assert max_active == 1
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 4
    finally:
        release.set()
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_new_drain_deadline_cancels_old_workflow_then_drops(tmp_path, monkeypatch):
    """120 秒 drain deadline 到期后取消旧 WF, drop 位置, 不允许晚发送或晚提交"""
    from satrap.edictum.flow import session as session_module

    assert session_module.NEW_DRAIN_TIMEOUT_SECONDS == 120.0
    db = database(tmp_path / "single.db")
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 4)
    adapter = FlowAdapter(store)
    fs = FlowStore(db, scope.adapter_id, "f1")
    with sqlite3.connect(db) as connection:
        connection.execute("BEGIN")
        fs.commit_position(connection, scope.key, "t1", "w1", 2)
        connection.execute("COMMIT")
    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(messages, tools, count):
        started.set()
        await release.wait()
        return response("", [tool_call("snapshot_write", {
            "state_id": "s1", "content": "不允许晚提交"})])

    original_wait_for = session_module.asyncio.wait_for

    async def drain_wait_for(awaitable, timeout):
        # drain 的 remaining 在模型调用前计算; 等模型确实阻塞后再缩短本次等待,
        # 不让代际登记和 WF 准备耗时抢先耗尽测试的 drain 窗口。
        if timeout is not None and timeout > 60:
            await started.wait()
            timeout = 0.05
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(session_module.asyncio, "wait_for", drain_wait_for)
    llm = MockLLM(slow)
    session = AsyncFlowSession("s2", llm=llm, flow=flow, db_path=str(db),
                               message_store=store, flow_name="f1", new_from="s1")
    session.error_feedback = False
    diagnostics = []
    original_record = session_module.record_debug

    def record(category, phase, **kwargs):
        diagnostics.append((category, phase, kwargs))
        return original_record(category, phase, **kwargs)

    monkeypatch.setattr(session_module, "record_debug", record)
    try:
        event = make_event(adapter, scope, "new", 5)
        archive_event(store, scope, event)
        await session.observe(event)
        await wait_until(lambda: started.is_set())
        await wait_until(lambda: session._new_done)
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 5
        assert any(phase == "drain_timeout" for _, phase, _ in diagnostics)
        assert not session._engine.running_workflows()
        release.set()
        await asyncio.sleep(0.1)
        assert adapter.scheduled == [] and adapter.sent == []
        assert session._flow_store.state_by_scope_key(scope.key, "s1")["version"] == 0
        assert len(llm.calls) == 1
    finally:
        release.set()
        await session.close_flow_nodes()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_fails", [False, True])
async def test_new_drain_timeout_invalidates_commit_thread_before_drop(
        tmp_path, monkeypatch, finish_fails):
    """提交线程未进入事务时, 即使持久化作废失败, 恢复后也不能写状态。"""
    import threading
    from satrap.edictum.flow import session as session_module

    monkeypatch.setattr(session_module, "NEW_DRAIN_TIMEOUT_SECONDS", 2.0)
    db = database(tmp_path / "single.db")
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 4)
    adapter = FlowAdapter(store)
    fs = FlowStore(db, scope.adapter_id, "f1")
    with sqlite3.connect(db) as connection:
        connection.execute("BEGIN")
        fs.commit_position(connection, scope.key, "t1", "w1", 2)
        connection.execute("COMMIT")
    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    llm = MockLLM(lambda messages, tools, count: (
        response("", [tool_call("snapshot_write", {
            "state_id": "s1", "content": "超时运行的状态"})])
        if count == 1 else response("done")))
    session = AsyncFlowSession("s2", llm=llm, flow=flow, db_path=str(db),
                               message_store=store, flow_name="f1", new_from="s1")
    session.error_feedback = False
    commit_entered, allow_commit, commit_finished = (
        threading.Event(), threading.Event(), threading.Event())
    original_commit = FlowStore.commit_states_by_scope_key

    def blocked_commit(self, *args, **kwargs):
        commit_entered.set()
        assert allow_commit.wait(5)
        try:
            return original_commit(self, *args, **kwargs)
        finally:
            commit_finished.set()

    monkeypatch.setattr(FlowStore, "commit_states_by_scope_key", blocked_commit)
    if finish_fails:
        def failed_finish(self, *args, **kwargs):
            raise RuntimeError("token store unavailable")
        monkeypatch.setattr(FlowStore, "finish_run_token", failed_finish)
    try:
        event = make_event(adapter, scope, "new", 5)
        archive_event(store, scope, event)
        await session.observe(event)
        await wait_until(commit_entered.is_set)
        await wait_until(lambda: session._new_done)
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 5
        assert session._flow_store.state_by_scope_key(scope.key, "s1")["version"] == 0
        allow_commit.set()
        await wait_until(commit_finished.is_set)
        await asyncio.sleep(0.05)
        assert session._flow_store.state_by_scope_key(scope.key, "s1")["version"] == 0
    finally:
        allow_commit.set()
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_invalidated_run_does_not_start_scheduled_send(tmp_path):
    """已作废运行即使恢复到发送边界, 也不能调用适配器开始新的后台发送。"""
    db = database(tmp_path / "single.db")
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 1)
    adapter = FlowAdapter(store)
    session = AsyncFlowSession("send-generation", MockLLM(), flow=_flow_definition(),
                               db_path=str(db), message_store=store, flow_name="f1")
    try:
        event = make_event(adapter, scope, "历史", 2)
        archive_event(store, scope, event)
        await session.observe(event)
        run_id = "cancelled-run"
        await asyncio.to_thread(
            session._flow_store.begin_run_token, scope.key, "w1", run_id)
        await asyncio.to_thread(session._flow_store.finish_run_token, scope.key, run_id)
        receipt = await session._send_scheduled_scope(scope, "不得发送", run_id=run_id)
        assert receipt.status == "failed" and receipt.reason == "run_invalidated"
        assert adapter.scheduled == [] and adapter.sent == []
    finally:
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_invalidated_flow_reply_does_not_start_scheduled_send(tmp_path):
    """flow_reply 的无来源消息路径也携带运行代际，作废后不得开始发送。"""
    from satrap.core.call_context import current_flow_run
    from satrap.edictum.flow.session import current_flow_event

    db = database(tmp_path / "single.db")
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 1)
    adapter = FlowAdapter(store)
    session = AsyncFlowSession("reply-generation", MockLLM(), flow=_flow_definition(),
                               db_path=str(db), message_store=store, flow_name="f1")
    run_token = event_token = None
    try:
        event = make_event(adapter, scope, "历史", 2)
        archive_event(store, scope, event)
        await session.observe(event)
        run_id = "cancelled-flow-reply"
        await asyncio.to_thread(
            session._flow_store.begin_run_token, scope.key, "w1", run_id)
        await asyncio.to_thread(session._flow_store.finish_run_token, scope.key, run_id)
        run_token = current_flow_run.set(("w1", "event", run_id))
        event_token = current_flow_event.set(None)
        assert not await session._reply_send("不得发送", False)
        assert adapter.scheduled == [] and adapter.sent == []
    finally:
        if event_token is not None:
            current_flow_event.reset(event_token)
        if run_token is not None:
            current_flow_run.reset(run_token)
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_session_startup_clears_stale_run_tokens(tmp_path):
    """新会话初始化时清理本流程残留令牌，不影响同 scope 的其他流程。"""
    db = database(tmp_path / "single.db")
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 1)
    adapter = FlowAdapter(store)
    current = FlowStore(db, scope.adapter_id, "f1")
    other = FlowStore(db, scope.adapter_id, "f2")
    await asyncio.to_thread(current.begin_run_token, scope.key, "w1", "stale-run")
    await asyncio.to_thread(other.begin_run_token, scope.key, "w1", "other-flow-run")
    session = AsyncFlowSession("restart", MockLLM(), flow=_flow_definition(),
                               db_path=str(db), message_store=store, flow_name="f1")
    try:
        event = make_event(adapter, scope, "重启", 2)
        archive_event(store, scope, event)
        await session.observe(event)
        assert not await asyncio.to_thread(current.run_token_active, scope.key, "stale-run")
        assert await asyncio.to_thread(other.run_token_active, scope.key, "other-flow-run")
    finally:
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_commit_holding_write_lock_finishes_before_drain_drop(tmp_path, monkeypatch):
    """提交已持锁进入事务时取消 drain，提交先完成，drop 后状态不再变化。"""
    import threading
    from satrap.edictum.flow import session as session_module

    monkeypatch.setattr(session_module, "NEW_DRAIN_TIMEOUT_SECONDS", 2.0)
    db = database(tmp_path / "single.db")
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 4)
    adapter = FlowAdapter(store)
    fs = FlowStore(db, scope.adapter_id, "f1")
    with sqlite3.connect(db) as connection:
        connection.execute("BEGIN")
        fs.commit_position(connection, scope.key, "t1", "w1", 2)
        connection.execute("COMMIT")
    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    llm = MockLLM(lambda messages, tools, count: (
        response("", [tool_call("snapshot_write", {
            "state_id": "s1", "content": "已持锁提交的状态"})])
        if count == 1 else response("done")))
    session = AsyncFlowSession("s2", llm=llm, flow=flow, db_path=str(db),
                               message_store=store, flow_name="f1", new_from="s1")
    session.error_feedback = False
    lock_held = threading.Event()
    finish_entered = threading.Event()
    allow_commit = threading.Event()
    commit_finished = threading.Event()
    original_connect = FlowStore._connect
    original_commit = FlowStore.commit_states_by_scope_key
    original_finish = FlowStore.finish_run_token

    def traced_connect(self):
        connection = original_connect(self)

        def trace(statement):
            if "SELECT 1 FROM flow_run_tokens" in statement:
                lock_held.set()
                assert allow_commit.wait(5)

        connection.set_trace_callback(trace)
        return connection

    def tracked_commit(self, *args, **kwargs):
        try:
            return original_commit(self, *args, **kwargs)
        finally:
            commit_finished.set()

    def tracked_finish(self, *args, **kwargs):
        finish_entered.set()
        return original_finish(self, *args, **kwargs)

    monkeypatch.setattr(FlowStore, "_connect", traced_connect)
    monkeypatch.setattr(FlowStore, "commit_states_by_scope_key", tracked_commit)
    monkeypatch.setattr(FlowStore, "finish_run_token", tracked_finish)
    try:
        event = make_event(adapter, scope, "new", 5)
        archive_event(store, scope, event)
        await session.observe(event)
        await wait_until(lock_held.is_set)
        await wait_until(finish_entered.is_set)
        assert not session._new_done
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 2
        allow_commit.set()
        await wait_until(lambda: session._new_done)
        assert commit_finished.is_set()
        assert session._flow_store.state_by_scope_key(scope.key, "s1")["version"] == 1
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 5
        await asyncio.sleep(0.05)
        assert session._flow_store.state_by_scope_key(scope.key, "s1")["version"] == 1
    finally:
        allow_commit.set()
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_new_observation_queue_overflow_drops_and_stays_bounded(tmp_path, monkeypatch):
    """持续结算期间暂存到达容量后, 最旧事件按 drop 结算并写诊断"""
    from satrap.edictum.flow import session as session_module

    assert session_module.NEW_EVENT_QUEUE_LIMIT == 256
    monkeypatch.setattr(session_module, "NEW_EVENT_QUEUE_LIMIT", 2)
    db = database(tmp_path / "single.db")
    scope = MessageScope("a", "s", "group", "c")
    store = _archive(db, scope, 2)
    adapter = FlowAdapter(store)
    flow = _flow_with_state_and_threshold()
    flow["nodes"][1]["config"]["on_new"] = "drain"
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(messages, tools, count):
        started.set()
        await release.wait()
        return response("done")

    llm = MockLLM(slow)
    session = AsyncFlowSession("bounded", llm=llm, flow=flow, db_path=str(db),
                               message_store=store, flow_name="f1", new_from="s1")
    session.error_feedback = False
    diagnostics = []
    original_record = session_module.record_debug

    def record(category, phase, **kwargs):
        diagnostics.append((category, phase, kwargs))
        return original_record(category, phase, **kwargs)

    monkeypatch.setattr(session_module, "record_debug", record)
    try:
        for index in range(3, 8):
            event = make_event(adapter, scope, f"m{index}", index)
            archive_event(store, scope, event)
            await session.observe(event)
            if index == 3:
                await started.wait()
        assert len(session._new_events) == 2
        assert sum(phase == "queue_drop" for _, phase, _ in diagnostics) == 3
        assert session._flow_store.positions_for_tile_by_scope_key(scope.key, "t1")["w1"] == 7
    finally:
        release.set()
        await wait_until(lambda: session._new_done)
        await session.close_flow_nodes()


async def _init_def(session: AsyncFlowSession) -> None:
    """手工初始化 definition/node_types/flow_store, 跳过 WF 装配"""
    from satrap.edictum.flow.definition import migrate_flow_definition, parse_flow_definition
    session._refresh_registry()
    definition, _, _ = migrate_flow_definition(
        parse_flow_definition(session._raw_flow), session._registry)
    session._definition = definition
    session._node_types = {n.id: session._registry.get(n.type) for n in definition.nodes.values()}
    session._initialized = True
