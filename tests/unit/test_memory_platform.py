"""真实同步与异步 Agent 管线中的独立记忆, 来源与子工作流约束"""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, NoReturn
import sqlite3

import pytest

from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.memory.scoped import ScopedMemories
from satrap.core.memory.service import MemoryService
from satrap.core.memory.store import MemoryStore
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.type import LLMCallResponse
from .test_group_chat_reply import setup, restore_manager, SCOPE
from .test_group_chat_plugin import session, Script, Invoker


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_memory_plugin_works_without_group_chat_or_base_take(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)
    actor = event.call_origin.actor_id
    source = event.call_origin.source_message_id
    script = Script([
        LLMCallResponse("tools_call", "", tool_calls=[{"id": "remember", "name": "add_memory", "arguments": {
            "kind": "member_preference", "key": "preferred_name", "title": "称呼", "content": "称呼我为小明", "source_message_ids": [source]}}]),
        LLMCallResponse("message", "已记住你的称呼"),
        LLMCallResponse("message", "小明你好"),
    ])
    instance = await session(tmp_path, script, asynchronous, False)
    if asynchronous:
        await instance.uninstall_plugin("group_chat")
    else:
        instance.uninstall_plugin("group_chat")
    instance.memory_db = str(adapter.message_archive.database)
    plugin_dir = str(Path(__file__).resolve().parents[2] / "satrap/expend/plugins/memory")
    if asynchronous:
        await instance.install_plugin(plugin_dir, config={"group_write_enabled": True})
    else:
        instance.install_plugin(plugin_dir, config={"group_write_enabled": True})
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    saved = ScopedMemories(adapter.message_archive, SCOPE).list()["items"]
    assert len(saved) == 1 and saved[0]["owner_user_id"] == actor
    assert saved[0]["source_message_ids"] == [source]
    assert {plugin.name for plugin in instance.list_plugins()} == {"memory"}
    _, next_event, _ = setup(tmp_path)
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(next_event)
    assert "称呼我为小明" in str(script.requests[-1][0])
    assert "长期记忆资料" in str(script.requests[-1][0])


@pytest.mark.asyncio
async def test_group_write_switch_and_main_workflow_are_checked(tmp_path):
    adapter, event, _ = setup(tmp_path)
    workflow = object()
    instance = SimpleNamespace(_wf=workflow)
    service = MemoryService(MemoryStore(db_path=adapter.message_archive.database, scope="session:test"), session=instance)
    values = {"kind": "member_preference", "key": "name", "title": "称呼", "content": "小明", "source_message_ids": [event.call_origin.source_message_id]}
    with bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        disabled = await service.group_operation("create", values, access=lambda: {"memory_mode": "full", "group_write_enabled": False})
        assert disabled["code"] == "write_disabled"
        with bind_tool_workflow(object()):
            child = await service.group_operation("create", values, access=lambda: {"memory_mode": "full", "group_write_enabled": True})
        assert child["code"] == "read_only_workflow"
        spoof = await service.group_operation("create", {**values, "owner_user_id": "other"}, access=lambda: {"memory_mode": "full", "group_write_enabled": True})
        assert spoof["code"] == "invalid_argument"
    assert ScopedMemories(adapter.message_archive, SCOPE).list()["items"] == []


@pytest.mark.parametrize('change', ['route', 'account', 'disabled'])
def test_context_injection_rechecks_identity_after_database_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 caplog: pytest.LogCaptureFixture, change: str) -> None:
    adapter, event, _ = setup(tmp_path)
    origin = replace(event.call_origin, conversation_kind='group', conversation_id=SCOPE.chat_id)
    repository = ScopedMemories(adapter.message_archive, SCOPE)
    repository.mutate('create', {'kind': 'group_rule', 'key': 'rule', 'content': '旧范围记忆正文'}, actor='operator', operator=True, operation_id='create')
    original = ScopedMemories.list

    def changed_after_read(self: ScopedMemories, *, kind: str = "", user_id: str = "", keyword: str = "",
                           limit: int = 20, cursor: str = "", viewer: str = "") -> dict[str, Any]:
        result = original(self, kind=kind, user_id=user_id, keyword=keyword, limit=limit, cursor=cursor, viewer=viewer)
        if kind == 'member_preference':
            if change == 'route':
                adapter._agent_route_memory[(SCOPE.self_id, 'group', SCOPE.chat_id)] = ((), 1)
            elif change == 'account':
                adapter.bot_self_id = adapter.client_self_id = 'different-account'
            else:
                adapter.config.enable = False
        return result

    monkeypatch.setattr(ScopedMemories, 'list', changed_after_read)
    service = MemoryService(MemoryStore(db_path=adapter.message_archive.database, scope='unused'))
    with bind_call_origin(origin):
        assert service.group_context_sync(access=lambda: {'memory_mode': 'base'}) == ''
    records = [record for record in caplog.records if '注入来源核验失败' in record.getMessage()]
    assert [record.levelname for record in records] == ['WARNING']


@pytest.mark.parametrize('failure', ['storage', 'unexpected'])
def test_context_injection_degrades_with_matching_log_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           caplog: pytest.LogCaptureFixture, failure: str) -> None:
    """存储不可用只记录警告, 非预期异常记录完整堆栈, 两者都降级为不注入"""
    adapter, event, _ = setup(tmp_path)
    origin = replace(event.call_origin, conversation_kind='group', conversation_id=SCOPE.chat_id)
    archive = adapter.message_archive
    assert archive is not None

    def broken(self: ScopedMemories, **values: object) -> NoReturn:
        if failure == 'storage':
            raise sqlite3.OperationalError('database is locked')
        raise RuntimeError('模拟未预期损坏')

    monkeypatch.setattr(ScopedMemories, 'list', broken)
    service = MemoryService(MemoryStore(db_path=archive.database, scope='unused'))
    with bind_call_origin(origin):
        assert service.group_context_sync(access=lambda: {'memory_mode': 'base'}) == ''
    records = {record.levelname: record.getMessage() for record in caplog.records if '群记忆' in record.getMessage()}
    if failure == 'storage':
        assert 'WARNING' in records and '记忆存储不可用' in records['WARNING'] and 'Traceback' not in records['WARNING']
    else:
        assert 'ERROR' in records and '本轮记忆注入失败' in records['ERROR'] and 'Traceback' in records['ERROR']
