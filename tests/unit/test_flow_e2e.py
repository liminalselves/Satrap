"""端到端验收: 真实 MessageEvent -> observe -> _async_init -> 引擎 -> WF"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from satrap.edictum.plugin.catalog import PluginCatalog

import pytest

from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.edictum.flow.session import AsyncFlowSession
from satrap.edictum.flow.store import FlowStore
from tests.support.flow_runtime import (
    FlowAdapter, MockLLM, archive_event, database, make_event, response, tool_call, wait_until,
)


SCOPE = MessageScope("adapter1", "self1", "group", "chat1")
SCRIPT = [
    "周末有人去看展吗, 798 那个光影展", "我去过了, 人巨多, 建议工作日", "工作日请不了假啊",
    "周六早上开门就进呢", "9 点开门, 我可以 8:40 到", "那我再去一次, 上次没看完二楼",
    "算我一个, 我带相机", "要不要提前买票", "小程序就能买, 我等下发链接",
    "顺便中午吃什么, 附近有家云南菜", "云南菜可以, 我想吃汽锅鸡",
    "@小萨 汽锅鸡要提前多久订? 帮我查一下", "对了, 下周三是老周生日", "别说出来啊",
    "那周六顺便给老周过生日?", "好主意, 蛋糕我来订", "@小萨 帮我记一下: 周六 8:40 798 门口集合",
]


def _node(node_id: str, kind: str, title: str, **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": kind, "title": title, "config": config}


def _edge(source: str, target: str) -> dict[str, str]:
    return {"from": source, "to": target}


def agent_rp_flow() -> dict[str, Any]:
    """与 Simulator agent_rp 模板同构图; 回复使用流程内置工具"""
    return {
        "version": 3,
        "nodes": [
            _node("source", "source", "消息到达"),
            _node("bucket", "bucket", "桶", include_outbound=True),
            _node("th", "threshold", "Agent 阈值", messages=12, chars=0, idle_seconds=0,
                  max_batch=200, on_new="drop"),
            _node("agent", "react_workflow", "Agent", model_name="agent", max_steps=4,
                  plugins="flow_snapshot", on_busy="merge", timeout_seconds=10),
            _node("snapshot", "state", "Agent 输出快照", writer_reads_previous=True),
            _node("rp_input", "assemble", "RP 输入", after_coverage=True, latest_n=8,
                  max_pending=80, max_pending_chars=8000, dedupe=True),
            _node("wake", "wake", "RP 唤醒", use_platform_policy=False, mention=True,
                  new_messages=5, cooldown=0),
            _node("rp", "react_workflow", "RP", model_name="rp", max_steps=4,
                  on_busy="queue", timeout_seconds=10),
            _node("reply", "output", "最后回复", send_mode="flow_reply"),
        ],
        "edges": [
            _edge("source.out", "bucket.in"), _edge("bucket.out", "th.messages"),
            _edge("th.event", "agent.trigger"), _edge("agent.text", "snapshot.write"),
            _edge("snapshot.read", "rp_input.snapshot"), _edge("bucket.out", "rp_input.messages"),
            _edge("bucket.out", "wake.messages"), _edge("wake.event", "rp.trigger"),
            _edge("rp_input.context", "rp.input"), _edge("rp.text", "reply.reply"),
        ],
    }


def _run_ids(client: MockLLM) -> set[str]:
    return {call["run"][2] for call in client.calls if call["run"] is not None}


def _user_input(call: dict[str, Any]) -> str:
    """取该次模型调用的当前用户输入，不查历史或系统提示。"""
    return next(message["content"] for message in reversed(call["messages"])
                if message.get("role") == "user")


def _first_calls(client: MockLLM) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for call in client.calls:
        run_id = call["run"][2]
        if run_id not in seen:
            seen.add(run_id)
            result.append(call)
    return result


@pytest.mark.asyncio
async def test_agent_rp_17_messages_real_observe_snapshot_and_reply_turn(tmp_path):
    """17 条消息产生 Agent 1 次 + RP 4 次; RP 最后一轮读齐三类输入"""
    db = database(tmp_path / "flow.db")
    store = PlatformMessageStore(db, SCOPE.adapter_id)
    adapter = FlowAdapter(store)
    # 与生产 Simulator 同图的既有驱动: Agent 在本段消息前已从位置 0 开始。
    # 新驱动首条建立历史边界是另一种生命周期, 由下方后台 e2e 单独验证。
    flow_store = FlowStore(db, SCOPE.adapter_id, "agent-rp")
    with sqlite3.connect(db) as connection:
        connection.execute("BEGIN")
        flow_store.commit_position(connection, SCOPE.key, "th", "agent", 0)
        connection.execute("COMMIT")

    def agent_script(messages: list[dict[str, Any]], tools: Any, count: int):
        if count % 2 == 1:
            return response("", [tool_call("snapshot_write", {
                "state_id": "snapshot", "content": "快照 v1: 周六 8:40 798 集合"})])
        return response("快照已更新")

    def rp_script(messages: list[dict[str, Any]], tools: Any, count: int):
        if count % 2 == 1:
            return response("", [tool_call("flow_reply", {"text": f"RP 回复 {count // 2 + 1}"})])
        return response("")

    agent = MockLLM(agent_script)
    rp = MockLLM(rp_script)
    session = AsyncFlowSession(
        "unused-route-id", rp, flow=agent_rp_flow(), db_path=str(db),
        model_resolver=lambda name: agent if name == "agent" else rp,
        message_store=store, flow_name="agent-rp",
    )
    try:
        for index, text in enumerate(SCRIPT, 1):
            event = make_event(adapter, SCOPE, text, index, mention=index in (12, 17))
            archive_event(store, SCOPE, event)
            await session.observe(event)
            await wait_until(lambda: not session._engine.running_workflows())
        await wait_until(lambda: len(_run_ids(agent)) == 1 and len(_run_ids(rp)) == 4)
        await wait_until(lambda: not session._engine.running_workflows())

        # Simulator 的 5 次是 WF 运行, 一次运行里工具调用与最终收尾各有模型请求
        assert len(_run_ids(agent)) + len(_run_ids(rp)) == 5
        state = session._flow_store.state_by_scope_key(SCOPE.key, "snapshot")
        assert state["version"] == 1
        # 既有驱动从位置 0 运行, 与生产 Simulator 同图一样在 seq 12 提交。
        assert state["covers_to"] == 12
        assert "周六 8:40 798" in state["content"]

        snapshot, pending, latest = _user_input(_first_calls(rp)[-1]).split("\n\n")
        assert snapshot == "快照 v1: 周六 8:40 798 集合"
        # 与非流程会话同格式: 触发消息 (第 17 条 @) 只带来源标记, 其余消息包成先前窗口消息;
        # 夹具档案不带昵称, 只显示 ID
        labeled = [f"[用户 u{index % 3}, 消息 m{index}] {text}" if index == 17
                   else f"[先前窗口消息: [用户 u{index % 3}, 消息 m{index}] {text}]"
                   for index, text in enumerate(SCRIPT, 1)]
        assert pending.splitlines() == labeled[12:17]
        # 最新 8 条按消息 ID 去掉已在未覆盖段里的 5 条
        assert latest.splitlines() == labeled[9:12]
        source_view = session._source_messages()
        assert [(message.position, message.text) for message in source_view] == list(enumerate(SCRIPT, 1))
        assert len(adapter.sent) == 4
        assert all(turn is not None and event is not None for turn, event in adapter.reply_turns)
        assert all(item["purpose"] == "business" for item in adapter.sent)

        # 后台发送由 test_background... 覆盖, 本条保持 demo 原图不额外输出 Agent 文本
    finally:
        await session.close_flow_nodes()


def logic_flow() -> dict[str, Any]:
    flow = agent_rp_flow()
    nodes = flow["nodes"]
    nodes.extend([
        _node("daytime", "cond_time", "白天", start="00:00", end="24:00"),
        _node("remind", "tool", "提醒群里", tool_name="remind_group",
              tool_description="交接提醒内容"),
        _node("quiet", "quiet_hours.in_hours", "免打扰时段内", start="00:00", end="00:00"),
        _node("not_quiet", "not", "不在免打扰"),
        _node("mention", "cond_message", "被 @"),
        _node("or_ok", "or", "可以开口"),
        _node("budget", "llm_budget.within", "调用预算内", max_calls=100),
        _node("and_ok", "and", "可开口且有预算"),
        _node("and_wake", "and", "唤醒放行"),
        _node("or_trigger", "or", "RP 触发"),
    ])
    flow["edges"].remove(_edge("wake.event", "rp.trigger"))
    flow["edges"].extend([
        _edge("daytime.out", "th.when"), _edge("agent.tool", "remind.caller"),
        _edge("quiet.out", "not_quiet.in"), _edge("not_quiet.out", "or_ok.a"),
        _edge("mention.out", "or_ok.b"), _edge("or_ok.out", "and_ok.a"),
        _edge("budget.out", "and_ok.b"), _edge("wake.event", "and_wake.a"),
        _edge("and_ok.out", "and_wake.b"), _edge("and_wake.out", "or_trigger.a"),
        _edge("remind.event", "or_trigger.b"), _edge("or_trigger.out", "rp.trigger"),
    ])
    return flow


def handoff_flow() -> dict[str, Any]:
    return {
        "version": 3,
        "nodes": [
            _node("source", "source", "消息到达"),
            _node("bucket", "bucket", "桶", include_outbound=True),
            _node("ctx", "assemble", "接待输入", after_coverage=False, latest_n=30),
            _node("wake", "wake", "接待唤醒", use_platform_policy=False,
                  mention=True, new_messages=5, cooldown=0),
            _node("host", "react_workflow", "接待", model_name="host", max_steps=4,
                  on_busy="queue", timeout_seconds=10),
            _node("ask", "tool", "问专家", tool_name="ask_expert", tool_description="交接问题"),
            _node("expert", "react_workflow", "专家", model_name="expert", max_steps=4,
                  on_busy="queue", timeout_seconds=10),
            _node("polish", "react_workflow", "润色", model_name="polish", max_steps=4,
                  on_busy="queue", timeout_seconds=10),
            _node("reply", "output", "群回复", send_mode="flow_reply"),
        ],
        "edges": [
            _edge("source.out", "bucket.in"), _edge("bucket.out", "ctx.messages"),
            _edge("bucket.out", "wake.messages"), _edge("wake.event", "host.trigger"),
            _edge("ctx.context", "host.input"), _edge("host.tool", "ask.caller"),
            _edge("ask.event", "expert.trigger"), _edge("expert.done", "polish.trigger"),
            _edge("host.text", "reply.reply"), _edge("polish.text", "reply.reply"),
        ],
    }


async def _observe_script(session: AsyncFlowSession, adapter: FlowAdapter,
                          store: PlatformMessageStore) -> list[Any]:
    events = []
    for index, text in enumerate(SCRIPT, 1):
        event = make_event(adapter, SCOPE, text, index, mention=index in (12, 17))
        events.append(event)
        archive_event(store, SCOPE, event)
        await session.observe(event)
        await wait_until(lambda: not session._engine.running_workflows())
    return events


@pytest.mark.asyncio
async def test_logic_17_messages_handoff_and_foreground_reply_turn(tmp_path):
    store = PlatformMessageStore(database(tmp_path / "flow.db"), SCOPE.adapter_id)
    adapter = FlowAdapter(store)
    reminder = "提醒：周六 8:40 798 门口集合"

    def agent_script(messages, tools, count):
        if count == 1:
            return response("", [tool_call("snapshot_write", {
                "state_id": "snapshot", "content": "周六集合快照"}),
                tool_call("remind_group", {"payload": reminder})])
        return response("快照已更新")

    def rp_script(messages, tools, count):
        if count % 2:
            return response("", [tool_call("flow_reply", {"text": f"RP 回复 {count}"})])
        return response("")

    agent, rp = MockLLM(agent_script), MockLLM(rp_script)
    catalog = PluginCatalog(Path(__file__).resolve().parents[2] / "satrap/expend/plugins",
                            tmp_path / "user-plugins")
    session = AsyncFlowSession(
        "logic", rp, flow=logic_flow(), db_path=str(store.database),
        model_resolver=lambda name: agent if name == "agent" else rp,
        plugin_catalog=catalog, message_store=store, flow_name="logic",
    )
    try:
        events = await _observe_script(session, adapter, store)
        await wait_until(lambda: len(_run_ids(agent)) == 1 and len(_run_ids(rp)) == 5)
        await wait_until(lambda: not session._engine.running_workflows())
        assert len(_run_ids(agent) | _run_ids(rp)) == 6
        reminder_calls = [call for call in _first_calls(rp) if reminder in _user_input(call)]
        assert len(reminder_calls) >= 1
        assert len(adapter.sent) == 4
        assert all(turn is not None and event in events for turn, event in adapter.reply_turns)
        # 阈值本身没有 source_event, 其提醒交接仍应经过后台通道。
        assert len(adapter.scheduled) == 1
    finally:
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_logic_reminder_handoff_preserves_foreground_source_event(tmp_path):
    store = PlatformMessageStore(database(tmp_path / "flow.db"), SCOPE.adapter_id)
    adapter = FlowAdapter(store)
    reminder = "请记得周六集合"
    flow = logic_flow()
    # 额外的前台入口用于验证 Agent 携带来源消息交接时的身份传播。
    flow["edges"].append(_edge("wake.event", "agent.trigger"))

    def agent_script(messages, tools, count):
        if count == 1:
            return response("", [tool_call("remind_group", {"payload": reminder})])
        return response("")

    def rp_script(messages, tools, count):
        if count % 2:
            return response("", [tool_call("flow_reply", {"text": f"前台回复 {count}"})])
        return response("")

    agent, rp = MockLLM(agent_script), MockLLM(rp_script)
    catalog = PluginCatalog(Path(__file__).resolve().parents[2] / "satrap/expend/plugins",
                            tmp_path / "user-plugins")
    session = AsyncFlowSession(
        "logic-foreground", rp, flow=flow, db_path=str(store.database),
        model_resolver=lambda name: agent if name == "agent" else rp,
        plugin_catalog=catalog, message_store=store, flow_name="logic-foreground",
    )
    try:
        event = make_event(adapter, SCOPE, "请提醒大家", 1, mention=True)
        archive_event(store, SCOPE, event)
        await session.observe(event)
        await wait_until(lambda: len(_run_ids(agent)) == 1 and len(_run_ids(rp)) == 2)
        await wait_until(lambda: not session._engine.running_workflows())
        reminder_call = next(call for call in _first_calls(rp) if reminder in _user_input(call))
        reminder_number = rp.calls.index(reminder_call) + 1
        sent_index = next(i for i, sent in enumerate(adapter.sent)
                          if sent["text"] == f"前台回复 {reminder_number}")
        turn, source = adapter.reply_turns[sent_index]
        assert turn is not None and source is event
        assert not adapter.scheduled
    finally:
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_handoff_17_messages_expert_payload_and_done_polish(tmp_path):
    store = PlatformMessageStore(database(tmp_path / "flow.db"), SCOPE.adapter_id)
    adapter = FlowAdapter(store)
    question = "汽锅鸡需要提前多久订？"
    answer = "专家结论：建议提前一天预订。"

    def host_script(messages, tools, count):
        if count == 5:
            return response("", [tool_call("ask_expert", {"payload": question})])
        if count % 2:
            return response("", [tool_call("flow_reply", {"text": f"接待回复 {count}"})])
        return response("")

    def polish_script(messages, tools, count):
        if count == 1:
            return response("", [tool_call("flow_reply", {"text": "建议提前一天预订"})])
        return response("")

    host, expert, polish = MockLLM(host_script), MockLLM(response(answer)), MockLLM(polish_script)
    models = {"host": host, "expert": expert, "polish": polish}
    session = AsyncFlowSession(
        "handoff", host, flow=handoff_flow(), db_path=str(store.database),
        model_resolver=models.__getitem__, message_store=store, flow_name="handoff",
    )
    try:
        events = await _observe_script(session, adapter, store)
        await wait_until(lambda: len(_run_ids(host)) == 4 and len(_run_ids(expert)) == 1
                         and len(_run_ids(polish)) == 1)
        await wait_until(lambda: not session._engine.running_workflows())
        assert len(_run_ids(host) | _run_ids(expert) | _run_ids(polish)) == 6
        assert question in repr(_first_calls(expert)[0]["messages"])
        assert answer in repr(_first_calls(polish)[0]["messages"])
        assert len(adapter.sent) == 4
        assert all(turn is not None and event in events for turn, event in adapter.reply_turns)
        assert not adapter.scheduled
    finally:
        await session.close_flow_nodes()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["final_text", "flow_reply"])
async def test_background_threshold_scheduled_send_is_recorded(tmp_path, mode):
    store = PlatformMessageStore(database(tmp_path / "flow.db"), SCOPE.adapter_id)
    adapter = FlowAdapter(store)
    text = f"后台消息 {mode}"
    flow = {
        "version": 3,
        "nodes": [
            _node("source", "source", "消息到达"), _node("bucket", "bucket", "桶"),
            _node("th", "threshold", "阈值", messages=1, chars=0, idle_seconds=0,
                  max_batch=200, on_new="drop"),
            # 混合触发的 WF 可通过校验；本次消息不命中唤醒，仅阈值触发。
            _node("wake", "wake", "唤醒", use_platform_policy=False, mention=True,
                  new_messages=0, cooldown=0),
            _node("agent", "react_workflow", "Agent", model_name="agent", max_steps=4,
                  timeout_seconds=10),
            _node("reply", "output", "发送", send_mode=mode),
        ],
        "edges": [_edge("source.out", "bucket.in"), _edge("bucket.out", "th.messages"),
                  _edge("bucket.out", "wake.messages"), _edge("wake.event", "agent.trigger"),
                  _edge("th.event", "agent.trigger"), _edge("agent.text", "reply.reply")],
    }

    def script(messages, tools, count):
        if mode == "final_text":
            return response(text)
        if count == 1:
            return response("", [tool_call("flow_reply", {"text": text})])
        return response("")

    llm = MockLLM(script)
    session = AsyncFlowSession("background", llm, flow=flow,
                               db_path=str(store.database),
                               message_store=store, flow_name="background")
    try:
        event = make_event(adapter, SCOPE, "触发后台", 1)
        archive_event(store, SCOPE, event)
        await session.observe(event)
        await wait_until(lambda: not session._engine.running_workflows())
        assert not llm.calls and not adapter.scheduled
        with sqlite3.connect(store.database) as connection:
            position = connection.execute(
                "SELECT committed_seq FROM flow_threshold_positions "
                "WHERE scope_key=? AND flow_name=? AND tile_id=? AND wf_id=?",
                (SCOPE.key, "background", "th", "agent"),
            ).fetchone()
        assert position == (1,)
        # 首条已在初始化阈值时作为历史边界，第二条才触发。
        event = make_event(adapter, SCOPE, "触发后台第二条", 2)
        archive_event(store, SCOPE, event)
        await session.observe(event)
        await wait_until(lambda: len(adapter.scheduled) == 1)
        await wait_until(lambda: not session._engine.running_workflows())
        assert len(_run_ids(llm)) == 1
        assert not adapter.sent and not adapter.reply_turns
        scheduled = adapter.scheduled[0]
        assert scheduled["text"] == text
        assert scheduled["target"].scope == SCOPE
        with sqlite3.connect(store.database) as connection:
            row = connection.execute(
                "SELECT scope_key, status, segments_json FROM flow_send_log WHERE attempt_id=?",
                (scheduled["target"].attempt_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == SCOPE.key and row[1] == "success"
        segments = json.loads(row[2])
        assert len(segments) == 1
        assert segments[0]["status"] == "success"
        assert segments[0]["message_ids"] == ["back-1"]
    finally:
        await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_first_deployment_threshold_covers_only_messages_after_initial_boundary(tmp_path):
    """单库且不预置位置: 首条建立末尾边界, 第二条提交时 covers_to 固定为 2。"""
    db = database(tmp_path / "flow.db")
    store = PlatformMessageStore(db, SCOPE.adapter_id)
    adapter = FlowAdapter(store)
    flow = {
        "version": 3,
        "nodes": [
            _node("source", "source", "消息到达"), _node("bucket", "bucket", "桶"),
            _node("th", "threshold", "阈值", messages=1, chars=0, idle_seconds=0,
                  max_batch=200, on_new="drop"),
            _node("agent", "react_workflow", "Agent", model_name="agent", max_steps=4,
                  plugins="flow_snapshot", timeout_seconds=10),
            _node("snapshot", "state", "快照"),
        ],
        "edges": [_edge("source.out", "bucket.in"), _edge("bucket.out", "th.messages"),
                  _edge("th.event", "agent.trigger"), _edge("agent.text", "snapshot.write")],
    }

    def script(messages, tools, count):
        if count == 1:
            return response("", [tool_call("snapshot_write", {
                "state_id": "snapshot", "content": "只覆盖第二条"})])
        return response("done")

    llm = MockLLM(script)
    session = AsyncFlowSession("first-deployment", llm, flow=flow, db_path=str(db),
                               message_store=store, flow_name="first-deployment")
    try:
        first = make_event(adapter, SCOPE, "首次部署历史", 1)
        archive_event(store, SCOPE, first)
        await session.observe(first)
        assert llm.calls == []
        second = make_event(adapter, SCOPE, "边界后的消息", 2)
        archive_event(store, SCOPE, second)
        await session.observe(second)
        await wait_until(lambda: not session._engine.running_workflows())
        state = session._flow_store.state_by_scope_key(SCOPE.key, "snapshot")
        assert state["version"] == 1 and state["covers_to"] == 2
        assert state["content"] == "只覆盖第二条"
    finally:
        await session.close_flow_nodes()
