"""实际同步与异步模型工具管线中的提醒, 不提前发送并保持幂等"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import jsonschema

from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.config.platform_messages import MessageScope
from satrap.core.group_chat.reminder_host import ReminderPolicy
from satrap.core.group_chat.reminder_service import execute_reminder_tool
from satrap.core.group_chat.reminders import ReminderStore
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import current_adapter_manager
from satrap.core.type import LLMCallResponse
from satrap.expend.plugins.group_chat.tools import get_tools
from .test_group_chat_reply import setup, restore_manager, SCOPE
from .test_group_chat_plugin import session, Script, Invoker


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_model_create_is_persisted_once_without_immediate_native_send(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)
    adapter._bot.get_group_member_info.side_effect = lambda **params: {"group_id": int(SCOPE.chat_id), "user_id": params["user_id"], "nickname": "成员"}
    manager = current_adapter_manager()
    manager.reminder_host = SimpleNamespace(
        backend=SimpleNamespace(get_platform_runtime=lambda platform: None),
        policy=lambda record: ReminderPolicy("ready", revision="same-policy", config={"reminders_enabled": True}),
    )
    arguments = {"text": "检查联调结果", "after_seconds": 30, "mention_user_ids": [event.call_origin.actor_id]}
    script = Script([
        LLMCallResponse("tools_call", "", tool_calls=[{"id": "first", "name": "group_chat_create_reminder", "arguments": arguments}]),
        LLMCallResponse("tools_call", "", tool_calls=[{"id": "retry", "name": "group_chat_create_reminder", "arguments": arguments}]),
        LLMCallResponse("message", "提醒已安排"),
    ])
    instance = await session(tmp_path, script, asynchronous, False)
    for tool in instance._wf.tools_manager.tools.values():
        if tool.tool_name.startswith("group_chat_"):
            tool.config["reminders_enabled"] = True
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    reminders = ReminderStore(adapter.message_archive.database).list(SCOPE, actor=event.call_origin.actor_id)["items"]
    assert len(reminders) == 1 and reminders[0]["state"] == "scheduled"
    assert reminders[0]["source_message_id"] == event.call_origin.source_message_id
    assert reminders[0]["source_agent"]["session_id"] == instance.session_id
    adapter._bot.send_group_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_owner_main_workflow_and_unknown_parameters_are_mechanical(tmp_path):
    adapter, event, _ = setup(tmp_path)
    workflow = object()
    instance = SimpleNamespace(_wf=workflow)
    with bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        with bind_tool_workflow(object()):
            child = await execute_reminder_tool("group_chat_create_reminder", {"text": "提醒", "after_seconds": 10},
                                                session=instance, config={"reminders_enabled": True}, authorize=lambda group: None)
        assert child["error"]["code"] == "read_only_workflow"
        spoof = await execute_reminder_tool("group_chat_create_reminder", {"text": "提醒", "after_seconds": 10, "chat_id": "other"},
                                            session=instance, config={"reminders_enabled": True}, authorize=lambda group: None)
        assert spoof["error"]["code"] == "invalid_argument"
        repository = ReminderStore(adapter.message_archive.database)
        other = repository.create(SCOPE, actor="other", text="私有正文", mentions=[], source_message_id="m", operation_id="other", time_spec={"after_seconds": 30})["reminder"]
        result = await execute_reminder_tool("group_chat_get_reminder", {"reminder_id": other["reminder_id"]},
                                             session=instance, config={}, authorize=lambda group: None)
        assert result["error"]["code"] == "not_found" and "私有正文" not in str(result)


def test_create_schema_enforces_exclusive_time_and_recovery_is_manual():
    instance = SimpleNamespace()
    tool = next(item for item in get_tools(instance, {}) if item.tool_name == "group_chat_create_reminder")
    schema = tool.get_tool_defined()["function"]["parameters"]
    jsonschema.validate({"text": "提醒", "after_seconds": 10}, schema)
    jsonschema.validate({"text": "提醒", "due_at": "2026-10-05T09:00:00"}, schema)
    for values in ({"text": "提醒"}, {"text": "提醒", "due_at": "2026-10-05T09:00:00", "after_seconds": 10}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(values, schema)
    assert tool.recovery_policy == "manual"
