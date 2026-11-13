"""会话层单测: 预算, CAS, 阈值 per-WF 重试, 失败告知, 私聊回复, 实例关闭"""
import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.config.platform_schema import ensure_platform_tables
from satrap.core.type import LLMCallResponse
from satrap.edictum.flow.budget import BudgetExceeded, FlowCallBudget
from satrap.edictum.flow.session import AsyncFlowSession, _MeteredClient, current_flow_run
from satrap.edictum.flow.store import FlowConflictError, FlowStore, StateCommit
from tests.support.flow_runtime import (
    FlowAdapter, MockLLM, archive_event, database, make_event, response, tool_call, wait_until,
)


SCOPE = MessageScope("a", "s", "group", "c")


def _archive(db: Path, n: int, scope=SCOPE) -> PlatformMessageStore:
    store = PlatformMessageStore(db, "a")
    for i in range(n):
        store.record(scope, ArchiveMessage(
            message_id=f"m{i}", sender_id="u", message_time=time.time(),
            text=f"t{i}", direction="inbound",
        ))
    return store


def _init_session(flow: dict, db: Path, store: PlatformMessageStore, **kw: Any) -> AsyncFlowSession:
    session = AsyncFlowSession("s1", llm=None, flow=flow, message_store=store, **kw)
    session._flow_store = FlowStore(db, "a", session._flow_name())
    session._flow_store.scope_key = SCOPE.key
    session._scope_key = SCOPE.key
    session._bound_scope = SCOPE
    return session


async def _init_def(session: AsyncFlowSession) -> None:
    from satrap.edictum.flow.definition import migrate_flow_definition, parse_flow_definition
    session._refresh_registry()
    d, _, _ = migrate_flow_definition(
        parse_flow_definition(session._raw_flow), session._registry)
    session._definition = d
    session._node_types = {n.id: session._registry.get(n.type) for n in d.nodes.values()}
    session._refresh_resolved()
    session._initialized = True


def _threshold_flow(wf_ids=("w1",), *, on_busy="queue", messages=2, snapshot=False) -> dict[str, Any]:
    nodes = [
        {"id": "src", "type": "source", "title": "s", "config": {}},
        {"id": "t1", "type": "threshold", "title": "t", "config": {
            "messages": messages, "chars": 0, "idle_seconds": 0,
            "min_messages": 1, "max_batch": 50, "on_new": "drop"}},
    ]
    edges = [{"from": "src.out", "to": "t1.messages"}]
    for wf_id in wf_ids:
        nodes.append({"id": wf_id, "type": "react_workflow", "title": wf_id, "config": {
            "on_busy": on_busy, "model_name": "test", "max_steps": 4,
            "plugins": "flow_snapshot" if snapshot else "",
            "timeout_seconds": 10,
        }})
        edges.append({"from": "t1.event", "to": f"{wf_id}.trigger"})
    if snapshot:
        nodes.append({"id": "state", "type": "state", "title": "state", "config": {}})
        edges.append({"from": "w1.text", "to": "state.write"})
    else:
        nodes.extend([
            {"id": "wake", "type": "wake", "title": "wake", "config": {
                "use_platform_policy": False, "mention": False, "new_messages": 1000, "cooldown": 0}},
            {"id": "out", "type": "output", "title": "out", "config": {"send_mode": "final_text"}},
        ])
        edges.append({"from": "src.out", "to": "wake.messages"})
        edges.extend({"from": "wake.event", "to": f"{wf_id}.trigger"} for wf_id in wf_ids)
        edges.extend({"from": f"{wf_id}.text", "to": "out.reply"} for wf_id in wf_ids)
    return {"version": 3, "nodes": nodes, "edges": edges}


def _wake_flow(*, on_busy="queue") -> dict[str, Any]:
    return {"version": 3, "nodes": [
        {"id": "src", "type": "source", "title": "s", "config": {}},
        {"id": "wake", "type": "wake", "title": "wake", "config": {
            "use_platform_policy": False, "mention": True, "new_messages": 0, "cooldown": 0}},
        {"id": "w1", "type": "react_workflow", "title": "w1", "config": {
            "on_busy": on_busy, "model_name": "test", "max_steps": 2, "timeout_seconds": 10}},
    ], "edges": [
        {"from": "src.out", "to": "wake.messages"}, {"from": "wake.event", "to": "w1.trigger"},
    ]}


def _seed_positions(db: Path, flow_name: str, tile: str, wf_ids: Any, seq=0, scope=SCOPE) -> None:
    # 模拟已有部署的提交位置, 使这些测试可驱动待重试/待处理批次；首次部署不应预置。
    store = FlowStore(db, scope.adapter_id, flow_name)
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("BEGIN")
        for wf_id in wf_ids:
            store.commit_position(connection, scope.key, tile, wf_id, seq)
    connection.close()


class TestBudget:
    def _session_for_spend(self, resolved: dict | None = None) -> AsyncFlowSession:
        session = AsyncFlowSession("s", llm=None, flow={"version": 3, "nodes": [], "edges": []})
        session._budget = FlowCallBudget(max_calls=0)
        session._resolved = resolved or {}
        # _config 需要 _definition 为 None 短路 -> 直接覆盖
        session._definition = None
        session._resolved = resolved or {}
        return session

    def test_wf_calls_counted_per_run(self):
        """WF 的每次模型调用前原子扣减; max_calls_per_run 只限自己 run"""
        budget = FlowCallBudget(max_calls=0)
        token = current_flow_run.set(("w1", "e1", "r1"))
        try:
            session = AsyncFlowSession("s", llm=None, flow={"version": 3, "nodes": [], "edges": []})
            session._budget = budget
            session._definition = None
            session._resolved = {}
            for _ in range(3):
                session._spend("wf", "w1", per_run=True)
        finally:
            current_flow_run.reset(token)
        calls = budget.calls(kind="all")
        assert calls == 3
        run_calls = budget._run_calls.get("r1", 0)
        assert run_calls == 3
        budget.end_run("r1")
        assert "r1" not in budget._run_calls

    def test_max_calls_per_run_enforced(self):
        budget = FlowCallBudget(max_calls=0)
        token = current_flow_run.set(("w1", "e1", "r1"))
        try:
            session = AsyncFlowSession("s", llm=None, flow={"version": 3, "nodes": [], "edges": []})
            session._budget = budget
            session._definition = None
            session._resolved = {"w1": {"max_calls_per_run": 2}}
            session._spend("wf", "w1", per_run=True)
            session._spend("wf", "w1", per_run=True)
            with pytest.raises(BudgetExceeded):
                session._spend("wf", "w1", per_run=True)
        finally:
            current_flow_run.reset(token)

    def test_metered_client_counts_calls(self):
        """_MeteredClient 包装后每次 call 都过计量"""
        calls = []
        inner = type("L", (), {})()
        async def fake_call(messages, **kw):
            calls.append(1)
            return LLMCallResponse(type="text", content="x")
        inner.call = fake_call
        metered = _MeteredClient(inner, lambda: calls.append("metered"))
        asyncio.run(metered.call([]))
        assert calls == ["metered", 1]


class TestRealWorkflowCommit:
    @pytest.mark.asyncio
    async def test_real_workflow_max_calls_per_run_blocks_second_model_call(self, tmp_path):
        """真实 _MeteredClient 在第二次模型请求前失败, 状态和位置都不提交"""
        db = database(tmp_path / "messages.db")
        store = _archive(db, 2)
        adapter = FlowAdapter(store)
        _seed_positions(db, "budget", "t1", ("w1",))
        llm = MockLLM(response("", [tool_call("snapshot_write", {
            "state_id": "state", "content": "不应提交"})]))
        flow = _threshold_flow(snapshot=True)
        flow["nodes"][2]["config"]["max_calls_per_run"] = 1
        session = AsyncFlowSession("budget", llm, flow=flow, db_path=str(db),
                                   message_store=store, flow_name="budget")
        session.error_feedback = False
        try:
            event = make_event(adapter, SCOPE, "第三條", 3)
            archive_event(store, SCOPE, event)
            await session.observe(event)
            await asyncio.sleep(0.2)
            assert len(llm.calls) == 1, (
                session._events, session._issues, session._drives,
                session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1"))
            await wait_until(lambda: not session._engine.running_workflows())
            assert session._flow_store.state_by_scope_key(SCOPE.key, "state")["version"] == 0
            assert session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1")["w1"] == 0
            assert any(item["kind"] == "run_end" and item["status"] == "failed"
                       and "单次运行模型调用上限" in item.get("error", "") for item in session._events)
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_session_cas_conflict_reports_flow_state_conflict(self, tmp_path, monkeypatch):
        """运行中另一写入方先提交, 本会话 CAS 拒绝旧版本并写冲突诊断"""
        monkeypatch.setenv("SATRAP_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
        from satrap.core.log.debug.capture import debug_request, debug_store, debug_writer
        from satrap.core.log.debug.contracts import DebugObject
        from satrap.core.log.debug.policy import DebugPolicyStore

        policy = DebugPolicyStore()
        policy.set_default(True, policy.read().revision)
        db = database(tmp_path / "messages.db")
        store = _archive(db, 2)
        adapter = FlowAdapter(store)
        _seed_positions(db, "cas", "t1", ("w1",))
        external = FlowStore(db, SCOPE.adapter_id, "cas")

        def script(messages, tools, count):
            if count == 1:
                return response("", [tool_call("snapshot_write", {
                    "state_id": "state", "content": "旧版本写入"})])
            external.commit_states_by_scope_key(SCOPE.key, "", "other", 0, [
                StateCommit("state", "其他写入方", base_version=0, covers_to=0, run_id="external")])
            return response("done")

        llm = MockLLM(script)
        session = AsyncFlowSession("cas", llm, flow=_threshold_flow(snapshot=True),
                                   db_path=str(db), message_store=store, flow_name="cas")
        session.error_feedback = False
        writer = None
        try:
            event = make_event(adapter, SCOPE, "第三條", 3)
            archive_event(store, SCOPE, event)
            obj = DebugObject("flow-test", SCOPE.self_id, "group", SCOPE.chat_id)
            with debug_request(obj, "cas-request"):
                await session.observe(event)
                await wait_until(lambda: len(llm.calls) == 2)
                await wait_until(lambda: not session._engine.running_workflows())
            writer = debug_writer()
            assert writer.flush()
            state = session._flow_store.state_by_scope_key(SCOPE.key, "state")
            assert state["version"] == 1 and state["content"] == "其他写入方"
            assert session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1")["w1"] == 0
            events = debug_store().events("cas-request")["events"]
            assert any(item["category"] == "flow_state" and item["phase"] == "conflict"
                       and item["metadata"]["code"] == "flow_state_conflict"
                       and item["metadata"]["flow_name"] == "cas"
                       and item["metadata"]["scope_key"] == SCOPE.key
                       for item in events)
        finally:
            if writer is not None:
                writer.close()
            await session.close_flow_nodes()


class TestCAS:
    def test_state_conflict_rejected(self):
        """并发写同一状态槽: base_version 不一致时提交被拒"""
        db = Path(tempfile.mkdtemp()) / "p.db"
        conn = sqlite3.connect(db)
        ensure_platform_tables(conn)
        conn.close()
        fs = FlowStore(db, "a", "f")
        fs.scope_key = SCOPE.key
        # 两次并发提交同一槽: 第一个把 0->1, 第二个仍拿 0 作 base -> 冲突
        fs.commit_states_by_scope_key(SCOPE.key, "", "w", 0,
                                      [StateCommit("st", "v1", base_version=0, covers_to=0, run_id="r1")])
        with pytest.raises(FlowConflictError):
            fs.commit_states_by_scope_key(SCOPE.key, "", "w", 0,
                                          [StateCommit("st", "v2", base_version=0, covers_to=0, run_id="r2")])

    def test_commit_position_never_moves_backwards(self, tmp_path):
        """后到的旧批次写 3 不得覆盖已提交的 10"""
        db = database(tmp_path / "flow.db")
        store = FlowStore(db, SCOPE.adapter_id, "f")
        with sqlite3.connect(db) as connection:
            connection.execute("BEGIN")
            store.commit_position(connection, SCOPE.key, "t1", "w1", 10)
            connection.execute("COMMIT")
            connection.execute("BEGIN")
            store.commit_position(connection, SCOPE.key, "t1", "w1", 3)
            connection.execute("COMMIT")
        assert store.positions_for_tile_by_scope_key(SCOPE.key, "t1")["w1"] == 10

    def test_failed_run_clears_pending(self):
        """失败运行的 snapshot_write pending 应被清空"""
        db = Path(tempfile.mkdtemp()) / "p.db"
        conn = sqlite3.connect(db)
        ensure_platform_tables(conn)
        conn.close()
        store = _archive(db, 0)
        session = _init_session({"version": 3, "nodes": [
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
        ], "edges": []}, db, store)

        class FakeTool:
            def __init__(self):
                self._pending = {"s1": "x"}
            def pending(self):
                return dict(self._pending)
            def clear(self):
                self._pending.clear()

        tool = FakeTool()
        wf = type("W", (), {"tools_manager": type("M", (), {"tools": {"t": tool}})()})()
        session._workflows["w1"] = wf
        session._discard_pending("w1")
        assert tool._pending == {}


class TestThresholdPerWF:
    @pytest.mark.asyncio
    async def test_first_deployment_starts_at_archived_max_seq(self, tmp_path):
        """首次部署跳过已有档案, 只在之后累计的消息达到阈值时运行。"""
        db = database(tmp_path / "messages.db")
        store = _archive(db, 2)
        adapter = FlowAdapter(store)
        llm = MockLLM(response("ok"))
        session = AsyncFlowSession("fresh", llm, flow=_threshold_flow(), db_path=str(db),
                                   message_store=store, flow_name="fresh")
        session.error_feedback = False
        try:
            for index in (3, 4, 5):
                event = make_event(adapter, SCOPE, f"新消息 {index}", index)
                archive_event(store, SCOPE, event)
                await session.observe(event)
                if index == 3:
                    assert session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1") == {"w1": 3}
                if index < 5:
                    assert llm.calls == []
            await wait_until(lambda: len(llm.calls) == 1)
            await wait_until(lambda: not session._engine.running_workflows())
            assert session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1") == {"w1": 5}
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_only_failed_wf_retries(self, tmp_path):
        """真实 LLM 失败不推进位置; 下一条消息只重试失败 WF"""
        db = database(tmp_path / "messages.db")
        store = _archive(db, 4)
        adapter = FlowAdapter(store)
        _seed_positions(db, "retry", "t1", ("wa", "wb"))
        wa = MockLLM(response("ok"))

        def wb_script(messages, tools, count):
            if count == 1:
                raise RuntimeError("scripted failure")
            return response("recovered")

        wb = MockLLM(wb_script)
        session = AsyncFlowSession(
            "retry", wb, flow=_threshold_flow(("wa", "wb")), db_path=str(db),
            model_resolver=lambda name: wa if name == "wa" else wb,
            message_store=store, flow_name="retry")
        session.error_feedback = False
        # 模型名映射到独立客户端, 运行与预算归属仍是真实 WF
        for node in session._raw_flow["nodes"]:
            if node["id"] in ("wa", "wb"):
                node["config"]["model_name"] = node["id"]
        try:
            event = make_event(adapter, SCOPE, "第五條", 5)
            archive_event(store, SCOPE, event)
            await session.observe(event)
            await wait_until(lambda: len(wa.calls) == 1 and len(wb.calls) == 1)
            await wait_until(lambda: not session._engine.running_workflows())
            positions = session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1")
            assert positions == {"wa": 5, "wb": 0}, (
                positions, len(wa.calls), len(wb.calls),
                [(item["kind"], item["wf"], item.get("status"), item.get("event_id"))
                 for item in session._events])

            event = make_event(adapter, SCOPE, "第六條", 6)
            archive_event(store, SCOPE, event)
            await session.observe(event)
            await wait_until(lambda: len(wb.calls) == 2)
            await wait_until(lambda: not session._engine.running_workflows())
            assert len(wa.calls) == 1
            assert session._flow_store.positions_for_tile_by_scope_key(
                SCOPE.key, "t1") == {"wa": 5, "wb": 6}
        finally:
            await session.close_flow_nodes()


    @pytest.mark.asyncio
    async def test_no_rerun_after_success(self, tmp_path):
        """真实运行成功后, 低于阈值的新消息不会重跑已提交批次"""
        db = database(tmp_path / "messages.db")
        store = _archive(db, 2)
        adapter = FlowAdapter(store)
        _seed_positions(db, "once", "t1", ("w1",))
        llm = MockLLM(response("ok"))
        session = AsyncFlowSession("once", llm, flow=_threshold_flow(), db_path=str(db),
                                   message_store=store, flow_name="once")
        session.error_feedback = False
        try:
            event = make_event(adapter, SCOPE, "第三條", 3)
            archive_event(store, SCOPE, event)
            await session.observe(event)
            await wait_until(lambda: len(llm.calls) == 1)
            await wait_until(lambda: not session._engine.running_workflows())
            event = make_event(adapter, SCOPE, "不足下一批", 4)
            archive_event(store, SCOPE, event)
            await session.observe(event)
            await asyncio.sleep(0.05)
            assert len(llm.calls) == 1
            assert session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1")["w1"] == 3
        finally:
            await session.close_flow_nodes()


class TestRealBusyAndIdle:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode,expected", [("queue", 3), ("merge", 2), ("drop", 1)])
    async def test_threshold_never_force_clears_busy(self, tmp_path, mode, expected):
        """阈值检查期间同 WF 不并发; busy 只由真实结束路径释放"""
        db = database(tmp_path / "messages.db")
        store = PlatformMessageStore(db, SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        _seed_positions(db, "busy", "t1", ("w1",))
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
                return response("ok")
            finally:
                active -= 1

        llm = MockLLM(script)
        session = AsyncFlowSession("busy", llm, flow=_threshold_flow(on_busy=mode, messages=1),
                                   db_path=str(db), message_store=store, flow_name="busy")
        session.error_feedback = False
        try:
            for index in (1, 2, 3):
                event = make_event(adapter, SCOPE, f"m{index}", index)
                archive_event(store, SCOPE, event)
                await session.observe(event)
                if index == 1:
                    await started.wait()
            assert len(llm.calls) == 1 and session._engine._wf["w1"].busy
            release.set()
            await wait_until(lambda: len({call["run"][2] for call in llm.calls}) == expected)
            await wait_until(lambda: not session._engine.running_workflows())
            assert max_active == 1
        finally:
            release.set()
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_idle_threshold_rechecks_all_workflows_before_updating_tile(self, tmp_path):
        """同一磁贴第二轮 idle 同时触发两个 WF, 不被首个 WF 刷新时间拦截"""
        db = database(tmp_path / "messages.db")
        store = PlatformMessageStore(db, SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        _seed_positions(db, "idle", "t1", ("wa", "wb"))
        flow = _threshold_flow(("wa", "wb"), messages=0)
        flow["nodes"][1]["config"]["idle_seconds"] = 1
        llm = MockLLM(response("ok"))
        session = AsyncFlowSession("idle", llm, flow=flow, db_path=str(db),
                                   message_store=store, flow_name="idle")
        session.error_feedback = False
        try:
            for index in (1, 2):
                event = make_event(adapter, SCOPE, f"idle {index}", index)
                archive_event(store, SCOPE, event)
                await session.observe(event)
                await wait_until(lambda: len(llm.calls) == index * 2)
                await wait_until(lambda: not session._engine.running_workflows())
                if index == 1:
                    await asyncio.sleep(1.02)
            assert {call["run"][0] for call in llm.calls} == {"wa", "wb"}
            assert len({call["run"][2] for call in llm.calls}) == 4
        finally:
            await session.close_flow_nodes()


class TestRealFailureFeedback:
    @pytest.mark.asyncio
    async def test_real_foreground_failure_uses_explicit_source_and_resets_after_success(self, tmp_path):
        """contextvar 已复位仍回复来源消息; 连失败一次, 成功后再次失败再告知"""
        db = database(tmp_path / "messages.db")
        store = PlatformMessageStore(db, SCOPE.adapter_id)
        adapter = FlowAdapter(store)

        def script(messages, tools, count):
            if count in (1, 2, 4):
                raise RuntimeError(f"failure {count}")
            return response("ok")

        llm = MockLLM(script)
        session = AsyncFlowSession("feedback", llm, flow=_wake_flow(),
                                   db_path=str(database(tmp_path / "messages.db")),
                                   message_store=store, flow_name="feedback")
        try:
            for index in range(1, 5):
                event = make_event(adapter, SCOPE, f"failure {index}", index, mention=True)
                archive_event(store, SCOPE, event)
                await session.observe(event)
                await wait_until(lambda: len(llm.calls) == index)
                await wait_until(lambda: not session._engine.running_workflows())
                expected = 1 if index < 4 else 2
                assert len(adapter.sent) == expected
            assert all(item["purpose"] == "error_feedback" for item in adapter.sent)
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_real_background_failure_uses_scheduled_target(self, tmp_path):
        """阈值模型失败经真实后台通道告知并留下发送证据"""
        db = database(tmp_path / "messages.db")
        store = PlatformMessageStore(db, SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        _seed_positions(db, "background-error", "t1", ("w1",))
        llm = MockLLM(lambda messages, tools, count: (_ for _ in ()).throw(RuntimeError("offline")))
        session = AsyncFlowSession("background-error", llm,
                                   flow=_threshold_flow(messages=1), db_path=str(db),
                                   message_store=store, flow_name="background-error")
        try:
            event = make_event(adapter, SCOPE, "trigger", 1)
            archive_event(store, SCOPE, event)
            await session.observe(event)
            await wait_until(lambda: len(adapter.scheduled) == 1)
            await wait_until(lambda: not session._engine.running_workflows())
            assert "本次运行失败" in adapter.scheduled[0]["text"]
            assert adapter.scheduled[0]["target"].scope == SCOPE
            assert not adapter.sent
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_real_background_failure_honors_error_feedback_disabled(self, tmp_path):
        """真实后台失败路径在 error_feedback=False 时不发送, 仍保留失败运行证据"""
        db = database(tmp_path / "single.db")
        store = PlatformMessageStore(db, SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        _seed_positions(db, "background-no-feedback", "t1", ("w1",))
        llm = MockLLM(lambda messages, tools, count: (_ for _ in ()).throw(RuntimeError("offline")))
        session = AsyncFlowSession(
            "background-no-feedback", llm, flow=_threshold_flow(messages=1), db_path=str(db),
            message_store=store, flow_name="background-no-feedback")
        session.error_feedback = False
        try:
            event = make_event(adapter, SCOPE, "trigger", 1)
            archive_event(store, SCOPE, event)
            await session.observe(event)
            await wait_until(lambda: llm.calls and not session._engine.running_workflows())
            assert adapter.scheduled == [] and adapter.sent == []
            assert any(item["kind"] == "run_end" and item["status"] == "failed"
                       for item in session._events)
        finally:
            await session.close_flow_nodes()


class TestObservationBinding:
    @pytest.mark.asyncio
    async def test_same_adapter_outbound_isolated_by_scope_and_close_unregisters(self, tmp_path):
        """两个会话共享 adapter 时, scope2 出站只驱动 scope2, 不改写 scope1"""
        store = PlatformMessageStore(database(tmp_path / "messages.db"), SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        other = MessageScope(SCOPE.adapter_id, SCOPE.self_id, "group", "other")
        first_llm, second_llm = MockLLM(response("one")), MockLLM(response("two"))
        first = AsyncFlowSession("first", first_llm, flow=_threshold_flow(messages=1),
                                 db_path=str(database(tmp_path / "messages.db")),
                                 message_store=store, flow_name="first")
        second = AsyncFlowSession("second", second_llm, flow=_threshold_flow(messages=1),
                                  db_path=str(database(tmp_path / "messages.db")),
                                  message_store=store, flow_name="second")
        first.error_feedback = second.error_feedback = False
        try:
            for session, scope, index in ((first, SCOPE, 1), (second, other, 2)):
                event = make_event(adapter, scope, "bind", index)
                archive_event(store, scope, event)
                await session.observe(event)
            assert len(adapter._flow_observers) == 2
            snapshot = ArchiveMessage(message_id="out-other", sender_id=SCOPE.self_id,
                                      message_time=time.time(), text="outbound", direction="outbound")
            store.record(other, snapshot)
            for observer in tuple(adapter._flow_observers):
                await observer(other, snapshot)
            await wait_until(lambda: len(second_llm.calls) == 1)
            await wait_until(lambda: not second._engine.running_workflows())
            await asyncio.sleep(0.05)
            assert first_llm.calls == []
            assert first._bound_scope == SCOPE and first._scope_key == SCOPE.key
            assert second._bound_scope == other
            await first.close_flow_nodes()
            assert len(adapter._flow_observers) == 1
        finally:
            await first.close_flow_nodes()
            await second.close_flow_nodes()
        assert not adapter._flow_observers

    @pytest.mark.asyncio
    async def test_source_default_view_is_bounded_to_latest_messages(self, tmp_path):
        """source 无查询参数也不翻页读全量, 默认只返回最新 200 条。"""
        store = _archive(database(tmp_path / "messages.db"), 1005)
        adapter = FlowAdapter(store)
        session = AsyncFlowSession("paging", MockLLM(), flow=_wake_flow(),
                                   db_path=str(database(tmp_path / "messages.db")),
                                   message_store=store, flow_name="paging")
        try:
            event = make_event(adapter, SCOPE, "bind", 2000)
            await session.observe(event)
            messages = session._source_messages()
            assert len(messages) == 200
            assert messages[0].text == "t805" and messages[-1].text == "t1004"
            assert [message.position for message in messages] == list(range(806, 1006))
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [499, 500, 5000])
    async def test_direct_source_limit_boundaries_read_real_archive(self, tmp_path, limit):
        """499/500/配置最大值都经真实 transform 和窗口查询读取直连输入。"""
        db = database(tmp_path / "messages.db")
        store = _archive(db, 1)
        adapter = FlowAdapter(store)
        flow = _wake_flow()
        flow["nodes"][0]["config"]["max_messages"] = limit
        flow["edges"].append({"from": "src.out", "to": "w1.input"})
        session = AsyncFlowSession("source-limit", MockLLM(), flow=flow, db_path=str(db),
                                   message_store=store, flow_name="source-limit")
        try:
            await session.observe(make_event(adapter, SCOPE, "绑定", 2))
            from satrap.edictum.flow.engine import FlowEvent
            event = FlowEvent(event_id="source-limit", kind="message", tile="", why="test")
            pulled = await session._pull_inputs("w1", "source-limit", event)
            assert [[message.text for message in value] for value in pulled] == [["t0"]]
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_source_window_read_failure_writes_diagnostic(self, tmp_path, monkeypatch):
        """窗口读取异常仍返回空视图，但必须留下 flow_source.read_failed 诊断。"""
        from satrap.edictum.flow import session as session_module

        db = database(tmp_path / "messages.db")
        store = _archive(db, 1)
        session = _init_session(_wake_flow(), db, store)
        diagnostics = []
        original_record = session_module.record_debug

        def record(category, phase, **kwargs):
            diagnostics.append((category, phase, kwargs))
            return original_record(category, phase, **kwargs)

        def fail_read(*args, **kwargs):
            raise RuntimeError("archive unavailable")

        monkeypatch.setattr(session_module, "record_debug", record)
        monkeypatch.setattr(store, "flow_window_by_scope_key", fail_read)
        try:
            assert session._source_messages({"latest_n": 1}) == ()
            failure = next(item for item in diagnostics
                           if item[:2] == ("flow_source", "read_failed"))
            assert failure[2]["status"] == "failed"
            assert failure[2]["metadata"]["latest_n"] == 1
            assert "archive unavailable" in failure[2]["metadata"]["error"]
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_source_assemble_window_reads_only_pending_and_latest(self, tmp_path):
        """assemble 的有界请求只取最新未覆盖段与 latest_n, 不翻页构造整段保留期档案"""
        store = _archive(database(tmp_path / "messages.db"), 20)
        adapter = FlowAdapter(store)
        session = AsyncFlowSession("window", MockLLM(), flow=_wake_flow(),
                                   db_path=str(database(tmp_path / "messages.db")),
                                   message_store=store, flow_name="window")
        try:
            await session.observe(make_event(adapter, SCOPE, "bind", 2000))
            messages = session._source_messages({
                "after": 10, "pending_limit": 3, "latest_n": 2,
            })
            assert [message.position for message in messages] == [18, 19, 20]
            latest = session._source_messages({
                "after": 0, "pending_limit": 0, "latest_n": 2,
            })
            assert [message.text for message in latest] == ["t18", "t19"]
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_structured_mention_and_private_custom_wake(self, tmp_path):
        """文本含 @ 不算提及; 私聊无 At 组件也始终命中自定义唤醒"""
        store = PlatformMessageStore(database(tmp_path / "messages.db"), SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        llm = MockLLM(response("ok"))
        session = AsyncFlowSession("mentions", llm, flow=_wake_flow(),
                                   db_path=str(database(tmp_path / "messages.db")),
                                   message_store=store, flow_name="mentions")
        try:
            plain = make_event(adapter, SCOPE, "@self 只是文本", 1)
            archive_event(store, SCOPE, plain)
            await session.observe(plain)
            await asyncio.sleep(0.05)
            assert llm.calls == []
            mentioned = make_event(adapter, SCOPE, "结构化提及", 2, mention=True)
            archive_event(store, SCOPE, mentioned)
            await session.observe(mentioned)
            await wait_until(lambda: len(llm.calls) == 1)
        finally:
            await session.close_flow_nodes()

        private = MessageScope(SCOPE.adapter_id, SCOPE.self_id, "private", "u1")
        private_llm = MockLLM(response("private"))
        session = AsyncFlowSession("private", private_llm, flow=_wake_flow(),
                                   db_path=str(database(tmp_path / "messages.db")),
                                   message_store=store, flow_name="private")
        try:
            event = make_event(adapter, private, "没有 At 的私聊", 3)
            archive_event(store, private, event)
            await session.observe(event)
            await wait_until(lambda: len(private_llm.calls) == 1)
            flow_message = session._event_to_flow_message(event)
            assert flow_message.mentions_bot is True
        finally:
            await session.close_flow_nodes()


class TestRuntimeIntegration:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["group", "private"])
    async def test_observe_validation_uses_conversation_kind_and_adapter_capability(self, tmp_path, kind):
        """实际 observe 初始化把私聊/群聊和对应后台发送能力交给 validator"""
        store = PlatformMessageStore(database(tmp_path / "messages.db"), SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        adapter.supports_scheduled_group_send = kind != "group"
        adapter.supports_scheduled_private_send = kind != "private"
        scope = MessageScope(SCOPE.adapter_id, SCOPE.self_id, kind, "target")
        session = AsyncFlowSession(
            f"validation-{kind}", MockLLM(), flow=_threshold_flow(messages=1),
            db_path=str(database(tmp_path / "messages.db")),
            message_store=store, flow_name=f"validation-{kind}")
        try:
            event = make_event(adapter, scope, "绑定并校验", 1)
            archive_event(store, scope, event)
            await session.observe(event)
            issue = next(issue for issue in session.issues
                         if issue.code == "flow_background_send_unsupported")
            assert issue.node == "w1"
            assert session._bound_scope == adapter.message_archive_scope(event.platform_message)
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_runtime_status_reads_recent_persisted_flow_trigger_summaries(
            self, tmp_path, monkeypatch):
        """运行状态读真实诊断库最近 50 条, 展示字段齐全且不混用内存降级队列"""
        monkeypatch.setenv("SATRAP_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
        from satrap.core.log.debug.capture import debug_request, debug_writer
        from satrap.core.log.debug.contracts import DebugObject
        from satrap.core.log.debug.policy import DebugPolicyStore

        policy = DebugPolicyStore()
        policy.set_default(True, policy.read().revision)
        store = PlatformMessageStore(database(tmp_path / "messages.db"), SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        session = AsyncFlowSession(
            "diagnostics", MockLLM(), flow=_wake_flow(),
            db_path=str(database(tmp_path / "messages.db")),
            message_store=store, flow_name="diagnostics")
        writer = None
        try:
            event = make_event(adapter, SCOPE, "只初始化", 1)
            archive_event(store, SCOPE, event)
            await session.observe(event)
            obj = DebugObject("flow-test", SCOPE.self_id, "group", SCOPE.chat_id)
            with debug_request(obj, "flow-runtime-status"):
                for index in range(55):
                    session._record_flow_diagnostic(
                        "flow_trigger", "fire", bodies={"output": {
                            "tile": "t1", "gate": "wake", "wf": "w1",
                            "event_id": f"source-{index:02}", "run_id": f"run-{index:02}",
                            "why": f"reason-{index:02}", "reason": "threshold",
                        }})
            writer = debug_writer()
            assert writer.flush()
            session._events.append({"kind": "memory-only"})
            status = await session.runtime_status()
            assert status["events_source"] == "debug_store"
            events = status["events"]
            assert len(events) == 50
            assert [item["metadata"]["event_id"] for item in events] == [
                f"source-{index:02}" for index in range(54, 4, -1)]
            assert "memory-only" not in repr(events)
            metadata = events[0]["metadata"]
            assert metadata["flow_name"] == "diagnostics"
            assert metadata["scope_key"] == SCOPE.key
            assert metadata["tile"] == "t1" and metadata["gate"] == "wake"
            assert metadata["wf"] == "w1" and metadata["run_id"] == "run-54"
            assert metadata["why"] == "reason-54" and metadata["reason"] == "threshold"
            policy.set_default(False, policy.read().revision)
            disabled_status = await session.runtime_status()
            assert disabled_status["events_source"] == "debug_store"
            assert disabled_status["events"][0]["metadata"]["event_id"] == "source-54"
        finally:
            if writer is not None:
                writer.close()
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_runtime_status_falls_back_to_memory_when_debug_events_empty(self, tmp_path):
        """诊断关闭或该流程没有持久化事件时, 状态接口如实使用内存摘要"""
        session = AsyncFlowSession(
            "empty-diagnostics", MockLLM(), flow=_wake_flow(),
            db_path=str(database(tmp_path / "messages.db")), flow_name="empty-diagnostics")
        session._events.append({"at": 123.0, "kind": "run_end", "status": "ok", "run_id": "r1"})
        try:
            status = await session.runtime_status()
            assert status["events"] == [{
                "event_id": "", "request_id": "", "sequence": -1,
                "phase": "run_end", "status": "ok", "recorded_at": 123.0,
                "metadata": {"run_id": "r1"},
            }]
            assert status["events_source"] == "memory_fallback"
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_unchanged_empty_catalog_does_not_rebuild_or_reresolve(self, tmp_path):
        """无 flow 插件也算稳定配置; 第二次 observe 不重建 registry/resolved/analysis"""
        from satrap.edictum.plugin.catalog import PluginCatalog

        store = PlatformMessageStore(database(tmp_path / "messages.db"), SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        catalog = PluginCatalog(tmp_path / "empty-plugins", tmp_path / "empty-user-plugins")
        session = AsyncFlowSession(
            "unchanged", MockLLM(), flow=_wake_flow(), plugin_catalog=catalog,
            db_path=str(database(tmp_path / "messages.db")),
            message_store=store, flow_name="unchanged")
        try:
            first = make_event(adapter, SCOPE, "第一次", 1)
            archive_event(store, SCOPE, first)
            await session.observe(first)
            registry = session._registry
            resolved = session._resolved
            drives = session._drives
            node_types = session._node_types
            second = make_event(adapter, SCOPE, "第二次", 2)
            archive_event(store, SCOPE, second)
            await session.observe(second)
            assert session._catalog_stamp == ()
            assert session._registry is registry
            assert session._resolved is resolved
            assert session._drives is drives
            assert session._node_types is node_types
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_plugin_default_stamp_avoids_reads_and_detects_edit_and_delete(
            self, tmp_path, monkeypatch):
        """真实默认文件不变不读内容; 修改与删除仅通过 mtime+size 触发重建"""
        import yaml
        from pathlib import Path
        from satrap.edictum.plugin.catalog import PluginCatalog
        from satrap.edictum.plugin.config import PluginConfigManager

        plugin = tmp_path / "plugins" / "stamp_probe"
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(yaml.safe_dump({
            "name": "stamp_probe", "version": "1",
            "flow_nodes": [{"name": "probe", "label": "探针", "out": "text",
                            "passthrough": {"out": None}}],
        }), encoding="utf-8")
        (plugin / "flow_nodes.py").write_text(
            "async def probe(ctx):\n    return 'ok'\n", encoding="utf-8")
        configs = PluginConfigManager(tmp_path / "plugin-config")
        configs.save_flow_nodes_global(
            "stamp_probe", {"probe": {"version": 1, "config": {"marker": "a"}}},
            expected_revision="missing")
        flow = _wake_flow()
        flow["nodes"].append({
            "id": "probe", "type": "stamp_probe.probe", "title": "探针", "config": {}})
        flow["edges"].append({"from": "probe.out", "to": "w1.input"})
        store = PlatformMessageStore(database(tmp_path / "messages.db"), SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        session = AsyncFlowSession(
            "stamp", MockLLM(), flow=flow,
            plugin_catalog=PluginCatalog(tmp_path / "plugins", tmp_path / "unused"),
            plugin_config=configs, db_path=str(database(tmp_path / "messages.db")),
            message_store=store, flow_name="stamp")
        target = configs.flow_nodes_path("stamp_probe")
        original_read_bytes = Path.read_bytes
        reads = []

        def counted_read_bytes(path):
            if Path(path) == target:
                reads.append(path)
            return original_read_bytes(path)

        async def observe(index):
            event = make_event(adapter, SCOPE, f"消息{index}", index)
            archive_event(store, SCOPE, event)
            await session.observe(event)

        try:
            await observe(1)
            registry = session._registry
            monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
            await observe(2)
            assert session._registry is registry
            assert reads == []
            revision = configs.flow_nodes_revision("stamp_probe")
            configs.save_flow_nodes_global(
                "stamp_probe", {"probe": {"version": 1, "config": {"marker": "changed"}}},
                expected_revision=revision)
            reads.clear()
            await observe(3)
            assert reads == []
            assert session._registry is not registry
            registry = session._registry
            revision = configs.flow_nodes_revision("stamp_probe")
            configs.save_flow_nodes_global(
                "stamp_probe", {}, expected_revision=revision)
            reads.clear()
            await observe(4)
            assert session._registry is not registry
            assert reads == []
        finally:
            await session.close_flow_nodes()

    @pytest.mark.asyncio
    async def test_real_workflow_input_pull_consumes_total_timeout(self, tmp_path):
        """真实慢 transform 在输入阶段耗尽 WF timeout, 不再给模型循环另起完整超时"""
        import yaml
        from satrap.edictum.plugin.catalog import PluginCatalog

        plugin = tmp_path / "plugins" / "slow_input"
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(yaml.safe_dump({
            "name": "slow_input", "version": "1",
            "flow_nodes": [{"name": "slow", "label": "慢输入", "out": "text",
                            "passthrough": {"out": None}}],
        }), encoding="utf-8")
        (plugin / "flow_nodes.py").write_text(
            "import asyncio\n"
            "async def slow(ctx):\n"
            "    await asyncio.sleep(3)\n"
            "    return 'too late'\n",
            encoding="utf-8")
        flow = _wake_flow()
        flow["nodes"].extend([
            {"id": "slow", "type": "slow_input.slow", "title": "慢输入", "config": {}},
            {"id": "out", "type": "output", "title": "输出", "config": {"send_mode": "final_text"}},
        ])
        flow["edges"].extend([
            {"from": "slow.out", "to": "w1.input"},
            {"from": "w1.text", "to": "out.reply"},
        ])
        flow["nodes"][2]["config"]["timeout_seconds"] = 1
        store = PlatformMessageStore(database(tmp_path / "messages.db"), SCOPE.adapter_id)
        adapter = FlowAdapter(store)
        llm = MockLLM(response("不应调用"))
        session = AsyncFlowSession(
            "input-timeout", llm, flow=flow,
            plugin_catalog=PluginCatalog(tmp_path / "plugins", tmp_path / "unused"),
            db_path=str(database(tmp_path / "messages.db")),
            message_store=store, flow_name="input-timeout")
        session.error_feedback = False
        try:
            event = make_event(adapter, SCOPE, "触发慢输入", 1, mention=True)
            archive_event(store, SCOPE, event)
            before = time.monotonic()
            await session.observe(event)
            await wait_until(lambda: not session._engine.running_workflows())
            elapsed = time.monotonic() - before
            assert elapsed < 2, elapsed
            assert llm.calls == []
            assert any(item["kind"] == "run_end" and item["status"] == "failed"
                       for item in session._events)
        finally:
            await session.close_flow_nodes()


class TestPrivateReply:
    @pytest.mark.asyncio
    async def test_private_flow_reply_uses_private_target(self):
        """私聊 scope 的后台发送走 private_session_id, 不报错"""
        db = Path(tempfile.mkdtemp()) / "p.db"
        conn = sqlite3.connect(db)
        ensure_platform_tables(conn)
        conn.close()
        scope = MessageScope("a", "s", "private", "u1")
        store = PlatformMessageStore(db, "a")
        session = _init_session({"version": 3, "nodes": [], "edges": []}, db, store)
        session._bound_scope = scope
        session._bound_adapter = type("A", (), {
            "supports_scheduled_private_send": True,
            "supports_scheduled_group_send": False,
            "config": type("C", (), {"enable": True, "settings": {"enable_private": True}})(),
            "group_chat_connection_token": lambda self: (object(), 1),
        })()
        receipts = []
        async def sender(target, chain, recorder):
            receipts.append(target)
            from satrap.core.platform.receipt import SendReceipt
            return SendReceipt("success", ["m1"])
        session._bound_adapter.group_chat_send_scheduled = sender
        session._message_store = store
        receipt = await session._send_scheduled_scope(scope, "hi")
        assert receipt.status == "success"
        assert receipts[0].scope.conversation_kind == "private"

    @pytest.mark.asyncio
    async def test_unsupported_adapter_returns_failed(self):
        db = Path(tempfile.mkdtemp()) / "p.db"
        conn = sqlite3.connect(db)
        ensure_platform_tables(conn)
        conn.close()
        store = PlatformMessageStore(db, "a")
        session = _init_session({"version": 3, "nodes": [], "edges": []}, db, store)
        session._bound_scope = SCOPE
        session._bound_adapter = type("A", (), {
            "supports_scheduled_group_send": False,
            "supports_scheduled_private_send": False,
        })()
        receipt = await session._send_scheduled_scope(SCOPE, "hi")
        assert receipt.status == "failed"
        assert receipt.reason == "scheduled_send_unsupported"


class TestInstanceClose:
    @pytest.mark.asyncio
    async def test_retired_instance_closed(self):
        """配置指纹变化后旧实例 close"""
        from satrap.edictum.flow.instances import FlowNodeInstancePool
        from satrap.edictum.flow.types import FlowNode
        closed: list[str] = []

        class N(FlowNode):
            node_name = "n"
            async def close(self):
                closed.append("close")

        pool = FlowNodeInstancePool()
        await pool.ensure("n1", N, "fp1", None)
        await pool.ensure("n1", N, "fp2", None)
        assert closed == ["close"]

    @pytest.mark.asyncio
    async def test_close_all(self):
        from satrap.edictum.flow.instances import FlowNodeInstancePool
        from satrap.edictum.flow.types import FlowNode
        closed: list[str] = []

        class N(FlowNode):
            node_name = "n"
            async def close(self):
                closed.append("close")

        pool = FlowNodeInstancePool()
        await pool.ensure("n1", N, "fp", None)
        await pool.ensure("n2", N, "fp", None)
        await pool.close_all()
        assert closed == ["close", "close"]
