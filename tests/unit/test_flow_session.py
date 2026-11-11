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
    async def test_only_failed_wf_retries(self):
        """两个 WF 由同一磁贴驱动, 一失败一成功: 只重跑失败的那个"""
        db = Path(tempfile.mkdtemp()) / "p.db"
        conn = sqlite3.connect(db)
        ensure_platform_tables(conn)
        conn.close()
        store = _archive(db, 4)
        flow = {
            "version": 3,
            "nodes": [
                {"id": "src", "type": "source", "title": "s", "config": {}},
                {"id": "t1", "type": "threshold", "title": "t",
                 "config": {"messages": 2, "chars": 0, "idle_seconds": 0,
                            "min_messages": 1, "max_batch": 10, "on_new": "drop"}},
                {"id": "wa", "type": "react_workflow", "title": "a", "config": {}},
                {"id": "wb", "type": "react_workflow", "title": "b", "config": {}},
            ],
            "edges": [
                {"from": "src.out", "to": "t1.messages"},
                {"from": "t1.event", "to": "wa.trigger"},
                {"from": "t1.event", "to": "wb.trigger"},
            ],
        }
        session = _init_session(flow, db, store)
        await _init_def(session)
        # 预置两个 WF 的位置为 0 (模拟"已驱动过且落后"的状态; 新驱动会从 MAX(seq) 开始)
        fs = session._flow_store
        conn = sqlite3.connect(db); conn.execute("BEGIN")
        fs.commit_position(conn, SCOPE.key, "t1", "wa", 0)
        fs.commit_position(conn, SCOPE.key, "t1", "wb", 0)
        conn.execute("COMMIT"); conn.close()

        runs: list[str] = []
        async def ok_run(wf_id, event):
            runs.append(wf_id)
            # 模拟成功: 推进位置
            session._flow_store.commit_states_by_scope_key(
                session._scope_key, event.tile, wf_id, event.batch_to, [])
        async def fail_run(wf_id, event):
            # 模拟失败: 不推进位置也不抛 (fire 分发的任务异常由引擎侧记录, 这里只验证不推进)
            runs.append(wf_id)
        handlers = {"wa": ok_run, "wb": fail_run}
        session._on_wf_trigger = lambda w, e: handlers[w](w, e)  # type: ignore[assignment]

        await session._ensure_engine()
        await session._check_thresholds()
        # fire 是 async 分发; handler 经 create_task 调度, 给任务时间完成
        await asyncio.sleep(0.05)
        # 第一次: wa 与 wb 都触发 (位置都从 0 开始, pending 4 条 >= 2)
        assert sorted(runs) == ["wa", "wb"]
        positions = session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1")
        assert positions["wa"] == 4
        assert positions["wb"] == 0   # wb 未提交, 位置仍停在 0

        # 第二次: 只有 wb (位置仍在 0) 再触发, wa 不重跑
        runs.clear()
        await session._check_thresholds()
        await asyncio.sleep(0.05)
        assert runs == ["wb"]

    @pytest.mark.asyncio
    async def test_no_rerun_after_success(self):
        """成功后同一批消息不再触发"""
        db = Path(tempfile.mkdtemp()) / "p.db"
        conn = sqlite3.connect(db)
        ensure_platform_tables(conn)
        conn.close()
        store = _archive(db, 3)
        flow = {
            "version": 3,
            "nodes": [
                {"id": "src", "type": "source", "title": "s", "config": {}},
                {"id": "t1", "type": "threshold", "title": "t",
                 "config": {"messages": 2, "chars": 0, "idle_seconds": 0,
                            "min_messages": 1, "max_batch": 10, "on_new": "drop"}},
                {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
            ],
            "edges": [
                {"from": "src.out", "to": "t1.messages"},
                {"from": "t1.event", "to": "w1.trigger"},
            ],
        }
        session = _init_session(flow, db, store)
        await _init_def(session)
        # 模拟"已驱动过且落后"的位置; 新增驱动会从 MAX(seq) 开始
        fs = session._flow_store
        conn = sqlite3.connect(db); conn.execute("BEGIN")
        fs.commit_position(conn, SCOPE.key, "t1", "w1", 0)
        conn.execute("COMMIT"); conn.close()
        runs: list[int] = []
        async def run(wf_id, event):
            runs.append(event.batch_to)
            session._flow_store.commit_states_by_scope_key(
                session._scope_key, event.tile, wf_id, event.batch_to, [])
        session._on_wf_trigger = run  # type: ignore[assignment]
        await session._ensure_engine()
        await session._check_thresholds()
        await session._check_thresholds()
        assert runs == [3]


class TestFailureNotify:
    @pytest.mark.asyncio
    async def test_consecutive_failures_notify_once(self):
        """连续失败只告知一次; 成功后下一轮失败再告知一次"""
        db = Path(tempfile.mkdtemp()) / "p.db"
        conn = sqlite3.connect(db)
        ensure_platform_tables(conn)
        conn.close()
        store = _archive(db, 0)
        session = _init_session({"version": 3, "nodes": [
            {"id": "w1", "type": "react_workflow", "title": "W", "config": {}},
        ], "edges": []}, db, store)
        await _init_def(session)
        session.error_feedback = True
        sends: list[str] = []
        async def send(scope, text):
            sends.append(text)
            return type("R", (), {"status": "success"})()
        session._send_scheduled_scope = send  # type: ignore[assignment]

        await session._notify_failure("w1", "e1")
        await session._notify_failure("w1", "e2")
        await session._notify_failure("w1", "e3")
        assert len(sends) == 1
        # 成功后复位, 下一轮再告知
        session._failure_notified.pop("w1")
        await session._notify_failure("w1", "e4")
        assert len(sends) == 2

    @pytest.mark.asyncio
    async def test_notify_follows_error_feedback_switch(self):
        """error_feedback=False 时不发送, 仍记日志"""
        db = Path(tempfile.mkdtemp()) / "p.db"
        conn = sqlite3.connect(db)
        ensure_platform_tables(conn)
        conn.close()
        store = _archive(db, 0)
        session = _init_session({"version": 3, "nodes": [
            {"id": "w1", "type": "react_workflow", "title": "W", "config": {}},
        ], "edges": []}, db, store)
        await _init_def(session)
        session.error_feedback = False
        sends: list[str] = []
        session._send_scheduled_scope = lambda s, t: sends.append(t)  # type: ignore[assignment]
        await session._notify_failure("w1", "e")
        assert sends == []


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
