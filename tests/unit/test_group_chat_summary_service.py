from datetime import datetime, timezone
from types import SimpleNamespace
from dataclasses import replace
import json

import pytest

from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.group_chat.reply import bind_reply_turn
from satrap.core.group_chat.summaries import SummaryStore
from satrap.core.group_chat.types import GroupChatLimits
from .test_group_chat_reply import setup, restore_manager, SCOPE
from .test_group_chat_reply import send_mock
from .test_group_chat_plugin import session, Script, Invoker
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.type import LLMCallResponse
from satrap.core.components import PlatformComponentType


@pytest.mark.asyncio
async def test_main_summary_write_is_independent_of_reply_tool_and_subagents_cannot_save(tmp_path):
    adapter, event, service = setup(tmp_path)
    workflow = object()
    with bind_call_origin(event.call_origin), bind_reply_turn(event) as turn, bind_tool_workflow(workflow):
        turn.workflow, turn.workflow_manager = workflow, SimpleNamespace(is_tool_enabled=lambda _: True)
        end = datetime.now(timezone.utc)
        args = {"start_time": "2020-01-01T00:00:00", "end_time": end.isoformat()}
        snapshot = await service.execute("group_chat_prepare_summary", args)
        assert snapshot["ok"] and snapshot["items"][0]["message_id"] == "77"
        save_args = {"snapshot_id": snapshot["snapshot_id"], "title": "讨论摘要",
                     "points": [{"text": "甲提及了乙", "source_message_ids": ["77"]}]}
        with bind_tool_workflow(object()):
            denied = await service.execute("group_chat_save_summary", save_args)
        assert denied["error"]["code"] == "wrong_executor"
        saved = await service.execute("group_chat_save_summary", save_args)
        assert saved["status"] == "saved"
        disabled = await service.execute("group_chat_get_summary", {"summary_id": saved["summary"]["summary_id"]}, limits=GroupChatLimits(summary_enabled=False))
        assert disabled["error"]["code"] == "unsupported"
        no_budget = await service.execute("group_chat_prepare_summary", args, limits=GroupChatLimits(summary_input_budget=0))
        assert no_budget["error"]["code"] == "quota_exceeded"
    assert not turn.enabled
    assert len(SummaryStore(adapter.message_archive).list(SCOPE)["items"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_session_first_turn_summary_tools_and_reply_disabled(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)
    script = Script([LLMCallResponse("tools_call", "", tool_calls=[{"id": "summary-1", "name": "group_chat_prepare_summary", "arguments": {
        "start_time": "2020-01-01T00:00:00", "end_time": datetime.now(timezone.utc).isoformat()}}]), LLMCallResponse("message", "已读取指定时段")])
    instance = await session(tmp_path, script, asynchronous, False)
    instance._wf.tools_manager.disable_tool("group_chat_reply")
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    assert "总结一段讨论" in str(script.requests[0][0])
    result = json.loads(script.requests[1][0][-1]["content"])
    assert result["ok"] and result["items"][0]["message_id"] == "77"
    assert result["archive_coverage"]["platform_history_complete"] is False


@pytest.mark.asyncio
async def test_summary_host_returns_stable_source_error_and_invalid_argument(tmp_path):
    _, event, service = setup(tmp_path)
    workflow = object()
    with bind_call_origin(event.call_origin), bind_reply_turn(event) as turn, bind_tool_workflow(workflow):
        turn.workflow, turn.workflow_manager = workflow, SimpleNamespace(is_tool_enabled=lambda _: True)
        result = await service.execute("group_chat_read_summary_sources", {"snapshot_id": "unknown", "cursor": "bad"})
        assert result["error"]["code"] == "invalid_argument"
        missing = await service.execute("group_chat_prepare_summary", {"start_time": "2026-10-04T09:00:00"})
        assert missing["error"]["code"] == "invalid_argument"
        assert (await service.execute("group_chat_recent_messages", {}))["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_agent_reads_saves_and_sends_summary_once_without_second_model(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)

    def saving(messages, _):
        snapshot = json.loads(messages[-1]["content"])
        return LLMCallResponse("tools_call", "", tool_calls=[{"id": "save-summary", "name": "group_chat_save_summary", "arguments": {
            "snapshot_id": snapshot["snapshot_id"], "title": "讨论摘要", "points": [{"text": "甲提及了乙", "source_message_ids": ["77"]}]}}])

    def replying(messages, _):
        saved = json.loads(messages[-1]["content"])
        assert saved["status"] == "saved"
        return LLMCallResponse("tools_call", "", tool_calls=[{"id": "reply-summary", "name": "group_chat_reply", "arguments": {
            "components": [{"type": "quote", "message_id": "77"}, {"type": "text", "text": saved["summary"]["points"][0]["text"]}]}}])

    script = Script([LLMCallResponse("tools_call", "", tool_calls=[{"id": "prepare-summary", "name": "group_chat_prepare_summary", "arguments": {
        "start_time": "2020-01-01T00:00:00", "end_time": datetime.now(timezone.utc).isoformat()}}]), saving, replying, LLMCallResponse("message", "重复的总结正文")])
    instance = await session(tmp_path, script, asynchronous, False)
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    assert len(script.requests) == 4
    assert len(SummaryStore(adapter.message_archive).list(SCOPE)["items"]) == 1
    send_mock(adapter).assert_awaited_once()
    assert "".join(component.text for component in send_mock(adapter).call_args.args[1].components if component.type == PlatformComponentType.Plain) == "甲提及了乙"
