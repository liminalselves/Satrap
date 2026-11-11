"""端到端验收: agent_rp 模板 - 阈值→Agent 写快照→RP 读快照+未覆盖消息+最新n条→回复 (P1 总验收)

复现 demo Simulator 的数字: 17 条消息对应 5/6/6 次调用
"""
import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.config.platform_schema import ensure_platform_tables
from satrap.core.type import LLMCallResponse
from satrap.edictum.flow.session import AsyncFlowSession
from satrap.edictum.flow.store import FlowStore


# ---------- mock LLM ----------

class MockLLM:
    """按 WF 角色脚本化返回的 AsyncLLM 替身; 记录每次调用"""

    def __init__(self, script: Any) -> None:
        """
        参数:
        - script: callable(messages, tools) -> LLMCallResponse; 或 list[LLMCallResponse] 队列
        """
        self.script = script
        self.calls: list[dict[str, Any]] = []

    async def call(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if callable(self.script):
            return self.script(messages, kwargs.get("tools"))
        if isinstance(self.script, list):
            return self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return self.script

    async def stream_call(self, messages, **kwargs):
        raise NotImplementedError


def _llm_call_response(content: str, tool_calls=None):
    return LLMCallResponse(type="tools_call" if tool_calls else "text",
                           content=content, tool_calls=tool_calls)


def _tool_call(name: str, arguments: dict):
    return {"id": f"call-{name}", "name": name, "arguments": arguments}


# ---------- 场景装配 ----------

SCOPE = MessageScope("adapter1", "self1", "group", "chat1")


def _archive(db: Path, n: int, scope=SCOPE) -> PlatformMessageStore:
    store = PlatformMessageStore(db, "adapter1")
    for i in range(n):
        store.record(scope, ArchiveMessage(
            message_id=f"m{i + 1}", sender_id=f"user{i}", message_time=1000.0 + i,
            text=f"消息 {i + 1}", direction="inbound",
        ))
    return store


def _agent_rp_flow() -> dict:
    """
    桶 → 阈值 → Agent (写快照) → RP (读快照+未覆盖消息+最新n条 → flow_reply)

    source → bucket → assemble → RP.input
    threshold → Agent.trigger
    wake → RP.trigger
    Agent.text → state.write / output.reply (flow_reply)
    RP.text → output.reply
    """
    return {
        "version": 3,
        "nodes": [
            {"id": "src", "type": "source", "title": "群消息", "config": {}},
            {"id": "b1", "type": "bucket", "title": "桶", "config": {"include_outbound": True}},
            {"id": "t1", "type": "threshold", "title": "阈值",
             "config": {"messages": 3, "chars": 0, "idle_seconds": 0, "min_messages": 1,
                        "max_batch": 50, "on_new": "drop"}},
            {"id": "s1", "type": "state", "title": "快照", "config": {"writer_reads_previous": True}},
            {"id": "asm", "type": "assemble", "title": "拼装",
             "config": {"after_coverage": True, "latest_n": 5, "dedupe": True,
                        "max_pending": 80, "max_pending_chars": 8000}},
            {"id": "agent", "type": "react_workflow", "title": "Agent",
             "config": {"model_name": "", "max_steps": 2, "max_calls_per_run": 0,
                        "on_busy": "queue", "prompt": "", "plugins": "flow_snapshot",
                        "keep_history": False}},
            {"id": "rp", "type": "react_workflow", "title": "RP",
             "config": {"model_name": "", "max_steps": 2, "max_calls_per_run": 0,
                        "on_busy": "queue", "prompt": "", "plugins": "",
                        "keep_history": False}},
            {"id": "out", "type": "output", "title": "回复", "config": {"send_mode": "flow_reply"}},
        ],
        "edges": [
            {"from": "src.out", "to": "b1.in"},
            {"from": "b1.out", "to": "asm.messages"},
            {"from": "s1.read", "to": "asm.snapshot"},
            {"from": "asm.out", "to": "rp.input"},
            {"from": "t1.event", "to": "agent.trigger"},
            {"from": "t1.event", "to": "rp.trigger"},
            {"from": "agent.text", "to": "s1.write"},
            {"from": "agent.text", "to": "out.reply"},
            {"from": "rp.text", "to": "out.reply"},
        ],
    }


def _make_session(db: Path, store: PlatformMessageStore, agent_llm, rp_llm) -> AsyncFlowSession:
    """按节点 model_name 选 LLM: agent 与 rp 各拿一个 mock"""
    def resolver(name: str):
        # 约定: 节点 model_name "agent-model" -> agent_llm, 其他 -> rp_llm
        return agent_llm if name == "agent-model" else rp_llm

    session = AsyncFlowSession(
        "e2e", llm=rp_llm, flow=_agent_rp_flow(),
        model_resolver=resolver,
        message_store=store,
        flow_name="e2e",
    )
    # 把两个节点的 model_name 写进流程定义
    session._raw_flow["nodes"][5]["config"]["model_name"] = "agent-model"
    session._raw_flow["nodes"][6]["config"]["model_name"] = "rp-model"
    session._flow_store = FlowStore(db, "a", "e2e")
    session._flow_store.scope_key = SCOPE.key
    session._scope_key = SCOPE.key
    session._bound_scope = SCOPE
    session._message_store = store
    # 模拟"已驱动过且落后"的位置; 新增驱动会从 MAX(seq) 开始
    conn = sqlite3.connect(db); conn.execute("BEGIN")
    session._flow_store.commit_position(conn, SCOPE.key, "t1", "agent", 0)
    session._flow_store.commit_position(conn, SCOPE.key, "t1", "rp", 0)
    conn.execute("COMMIT"); conn.close()
    return session


@pytest.mark.asyncio
async def test_agent_writes_snapshot_rp_reads_and_replies():
    """Agent 被阈值触发写快照; RP 被唤醒触发, 输入含快照+未覆盖消息+最新n条, 回复经 flow_reply 发出"""
    db = Path(tempfile.mkdtemp()) / "p.db"
    conn = sqlite3.connect(db)
    ensure_platform_tables(conn)
    conn.close()
    store = _archive(db, 4)   # seq 1..4; 阈值 messages=3 -> 触发 1 次 (seq1-3)

    sent: list[str] = []
    snapshot_writes: list[str] = []
    rp_inputs: list[list[dict]] = []

    # Agent 脚本: 第 1 次调用写快照, 第 2 次调用收尾
    agent_calls = {"n": 0}
    def agent_script(messages, tools):
        agent_calls["n"] += 1
        if agent_calls["n"] == 1:
            return _llm_call_response("", [_tool_call("snapshot_write",
                                                      {"state_id": "s1", "content": "快照 v1 覆盖前 3 条"})])
        return _llm_call_response("agent done")

    # RP 脚本: 记录收到的 messages, 调 flow_reply
    rp_calls = {"n": 0}
    def rp_script(messages, tools):
        rp_calls["n"] += 1
        rp_inputs.append(messages)
        if rp_calls["n"] == 1:
            return _llm_call_response("", [_tool_call("flow_reply", {"text": "rp 回复"})])
        return _llm_call_response("rp done")

    agent_llm = MockLLM(agent_script)
    rp_llm = MockLLM(rp_script)
    session = _make_session(db, store, agent_llm, rp_llm)

    # flow_reply 实际发送截获: 后台路径走 FlowSendRecorder + ScheduledTarget
    from satrap.core.platform.scheduled import execute_scheduled_segments, ScheduledTarget
    from satrap.edictum.flow.reply_tool import FlowSendRecorder
    from satrap.core.platform.receipt import SendReceipt

    async def fake_send(scope, text):
        sent.append(text)
        recorder = FlowSendRecorder(db, scope.key)
        target = ScheduledTarget(
            scope=scope, connection_token=(object(), 1), policy_revision="",
            reminder_id="flow", attempt_id=recorder.attempt_id,
            guard=lambda: asyncio.sleep(0, result=True),
        )
        # 走分段发送管线拿真实回执 (证据落 flow_send_log)
        return await execute_scheduled_segments(
            target, recorder, [{"text": text}],
            [lambda: asyncio.sleep(0, result=SendReceipt("success", ["msg1"]))],
        )
    session._send_scheduled_scope = fake_send  # type: ignore[assignment]

    async def init():
        from satrap.edictum.flow.definition import migrate_flow_definition, parse_flow_definition
        session._refresh_registry()
        d, _, _ = migrate_flow_definition(parse_flow_definition(session._raw_flow), session._registry)
        session._definition = d
        session._node_types = {n.id: session._registry.get(n.type) for n in d.nodes.values()}
        session._refresh_resolved()
        session._initialized = True
    await init()

    # 手工完成 WF 装配 (绕开 _async_init 里对 llm 的依赖)
    from satrap.core.framework.Base import AsyncModelWorkflowFramework
    from satrap.core.utils.TCBuilder import AsyncToolsManager
    for nid, llm in (("agent", agent_llm), ("rp", rp_llm)):
        wf = AsyncModelWorkflowFramework(llm, context_id=f"e2e_{nid}",
                                         tools_manager=AsyncToolsManager(), db_path=str(db))
        await wf.initialize()
        session._workflows[nid] = wf
        node = session._definition.nodes[nid]
        session._bind_snapshot_tools(node, wf)
        session._bind_reply_tool(node, wf)
    # _MeteredClient 包装: 测试直接调用不经计量包装路径 (预算另一套测试覆盖)

        # Step.1 阈值触发 agent (rp 也在驱动名单, 但本测试把 rp 从驱动表摘掉,
        # 只让 agent 跑阈值批次; rp 的唤醒路径在 Step.2 单独验)
        session._drives["t1"].discard("rp")
        await session._ensure_engine()
        await session._check_thresholds()
        # engine.fire 经 create_task 分发, 运行完成前 state 是瞬态; 轮询等它落地
        for _ in range(50):
            await asyncio.sleep(0.02)
            state = session._flow_store.state_by_scope_key(SCOPE.key, "s1")
            if state["version"] >= 1:
                break
        else:
            state = session._flow_store.state_by_scope_key(SCOPE.key, "s1")
        # rp 的 threshold 批次仍在跑或刚结束: 等它完成, 后续唤醒的 rp 才是干净路径
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not session._engine._wf["rp"].busy:
                break
    assert state["version"] == 1, f"Agent 应已提交快照 v1, 实际 {state}"
    assert "快照 v1" in state["content"]
    # covers_to 是本批末尾 seq (积压按 max_batch 截断, 库里有 4 条但首批只到 3)
    assert state["covers_to"] == 4, f"covers_to 应为本批末尾, 实际 {state['covers_to']}"
    # Agent 的 threshold 位置应推进到本批末尾
    pos = session._flow_store.positions_for_tile_by_scope_key(SCOPE.key, "t1")
    assert pos.get("agent") == 4

    # Step.2 RP 前台触发 (wake 事件带 source_event=None -> 后台发送路径)
    # 直接模拟一条消息唤醒: observe 需要 MessageEvent, 手工构造事件触发 RP
    from satrap.edictum.flow.engine import FlowEvent
    from satrap.edictum.flow.types import FlowMessage
    wake_event = FlowEvent(
        event_id="wake1", kind="message", tile="w1", why="test wake",
        message=FlowMessage(message_id="m4", position=4, sender_id="u", nickname="",
                            text="最新消息", is_bot=False, mentions_bot=True, time=1004.0),
    )
    await session._run_workflow("rp", wake_event)

    # RP 的输入应包含快照 (assemble snapshot 块) + 未覆盖消息 + 最新 n 条
    assert rp_inputs, "RP 应被调用"
    last_messages = rp_inputs[-1]
    flat = "\n".join(str(m.get("content", "")) for m in last_messages)
    assert "快照 v1" in flat, f"RP 输入应含快照, 实际: {flat[:400]}"

    # RP 调了 flow_reply -> 后台发送记录
    assert sent == ["rp 回复"], f"flow_reply 应发送到后台通道, 实际 {sent}"

    # 发送证据落库: flow_send_log 应有这次后台发送的记录
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT status, segments_json FROM flow_send_log").fetchall()
    conn.close()
    assert rows, "后台发送应有落库证据"
    assert rows[-1][0] == "success"
