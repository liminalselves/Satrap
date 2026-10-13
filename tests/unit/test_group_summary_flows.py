"""业务链路场景与集中边界契约"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import pytest
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.group_chat.summaries import SummaryLimits, SummaryStore, _cursor
from satrap.core.group_chat.types import GroupChatError
from types import SimpleNamespace
from dataclasses import replace
import json
from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.group_chat.reply import bind_reply_turn
from satrap.core.group_chat.types import GroupChatLimits
from support.group_reply import setup as _group_chat_summary_service_setup, restore_manager
from support.group_reply import SCOPE as _group_chat_summary_service_SCOPE, send_mock
from support.group_models import session, Script, Invoker
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.type import LLMCallResponse
from satrap.core.components import PlatformComponentType
from urllib.parse import urlencode
from satrap.core.backend import control_server
from satrap.core.storage.layout import StorageLayout
from support.http import _request, _json_body, _use_config

NOW = 1_800_000_000.0

_group_chat_summaries_SCOPE = MessageScope("test-platform", "bot:id", "group", "room:/alpha")

OTHER = MessageScope("test-platform", "bot:id", "group", "room:/beta")

def _group_chat_summaries_setup(tmp_path: Path):
    clock = [NOW]
    archive = PlatformMessageStore(tmp_path / "platform.db", _group_chat_summaries_SCOPE.adapter_id, clock=lambda: clock[0])
    for index in range(5):
        archive.record(_group_chat_summaries_SCOPE, ArchiveMessage(f"m{index}", "member:a", NOW - 10 + index, f"讨论 {index}"))
    archive.record(OTHER, ArchiveMessage("other", "member:a", NOW - 5, "不能混入"))
    archive.record(_group_chat_summaries_SCOPE, ArchiveMessage("bot", _group_chat_summaries_SCOPE.self_id, NOW - 4, "旧摘要", direction="outbound"))
    return archive, SummaryStore(archive), clock

def prepare(store: SummaryStore, *, limits=None, owner="request:a"):
    return store.prepare(_group_chat_summaries_SCOPE, owner, start_time=datetime.fromtimestamp(NOW - 20, timezone.utc).isoformat(),
                         end_time=datetime.fromtimestamp(NOW, timezone.utc).isoformat(), limits=limits)

def save(store: SummaryStore, snapshot: dict, *, owner="request:a"):
    return store.save(_group_chat_summaries_SCOPE, owner, snapshot["snapshot_id"], "讨论摘要",
                      [{"text": "大家讨论了项目", "source_message_ids": ["m0", "m4"]}])

def test_frozen_pages_source_validation_and_idempotent_save(tmp_path):
    archive, store, _ = _group_chat_summaries_setup(tmp_path)
    snapshot = prepare(store, limits=SummaryLimits(page_limit=2))
    assert [item["message_id"] for item in snapshot["items"]] == ["m0", "m1"]
    assert snapshot["selection"]["all_local_matches_selected"]
    assert not snapshot["archive_coverage"]["platform_history_complete"]
    archive.record(_group_chat_summaries_SCOPE, ArchiveMessage("new", "member:a", NOW - 1, "快照后新增"))
    with pytest.raises(GroupChatError, match="全部分页"):
        save(store, snapshot)
    with pytest.raises(GroupChatError, match="跳过"):
        store.read_sources(_group_chat_summaries_SCOPE, "request:a", snapshot["snapshot_id"], _cursor([1, snapshot["snapshot_id"], 4]))
    page = store.read_sources(_group_chat_summaries_SCOPE, "request:a", snapshot["snapshot_id"], snapshot["next_cursor"])
    last = store.read_sources(_group_chat_summaries_SCOPE, "request:a", snapshot["snapshot_id"], page["next_cursor"])
    assert [item["message_id"] for item in last["items"]] == ["m4"]
    assert last["next_cursor"] is None
    result = save(store, snapshot)
    replay = save(store, snapshot)
    assert replay["replayed"] and replay["summary"]["summary_id"] == result["summary"]["summary_id"]
    assert store.list(_group_chat_summaries_SCOPE)["items"] == [result["summary"]]

@pytest.mark.parametrize("operation", ["delete", "recall", "expire"])
def test_removed_source_erases_derived_content_in_same_maintenance_transaction(tmp_path, operation):
    archive, store, clock = _group_chat_summaries_setup(tmp_path)
    snapshot = prepare(store)
    saved = save(store, snapshot)["summary"]
    if operation == "delete":
        backup = archive.delete(_group_chat_summaries_SCOPE, message_ids=["m0"], expected_revision=0)
    elif operation == "recall":
        archive.recall(_group_chat_summaries_SCOPE, "m0")
    else:
        clock[0] += 31 * 86400
        archive.purge()
    with sqlite3.connect(archive.database) as connection:
        row = connection.execute("SELECT state, title, points_json FROM group_chat_summaries").fetchone()
        assert row == ("source_unavailable", "", "[]")
        assert connection.execute("SELECT COUNT(*) FROM group_chat_summary_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM group_chat_summary_refs").fetchone()[0] == 0
    if operation == "delete":
        archive.restore(_group_chat_summaries_SCOPE, backup["backup_id"], expected_revision=1)
    detail = store.get(_group_chat_summaries_SCOPE, saved["summary_id"])["summary"]
    assert detail["state"] == "source_unavailable" and detail["revision"] == 2
    assert detail["points"] == []

def test_scope_owner_and_arbitrary_citations_are_rejected(tmp_path):
    _, store, _ = _group_chat_summaries_setup(tmp_path)
    snapshot = prepare(store)
    for scope, owner in ((OTHER, "request:a"), (_group_chat_summaries_SCOPE, "request:b")):
        with pytest.raises(GroupChatError):
            store.read_sources(scope, owner, snapshot["snapshot_id"], _cursor([1, snapshot["snapshot_id"], 0]))
    with pytest.raises(GroupChatError, match="之外"):
        store.save(_group_chat_summaries_SCOPE, "request:a", snapshot["snapshot_id"], "摘要", [{"text": "伪造", "source_message_ids": ["other"]}])
    saved = save(store, snapshot)["summary"]
    with pytest.raises(GroupChatError):
        store.get(OTHER, saved["summary_id"])
    with pytest.raises(GroupChatError):
        store.delete(_group_chat_summaries_SCOPE, saved["summary_id"], expected_revision=2)
    assert store.get(_group_chat_summaries_SCOPE, saved["summary_id"])["summary"]["state"] == "active"

def test_budget_and_empty_results_never_claim_complete_platform_history(tmp_path):
    _, store, _ = _group_chat_summaries_setup(tmp_path)
    snapshot = prepare(store, limits=SummaryLimits(message_limit=2))
    assert snapshot["selection"] == {"selected_count": 2, "all_local_matches_selected": False,
                                      "truncated": True, "reasons": ["message_limit"]}
    result = store.prepare(_group_chat_summaries_SCOPE, "request:a", start_time=datetime.fromtimestamp(NOW - 20, timezone.utc).isoformat(),
                           end_time=datetime.fromtimestamp(NOW, timezone.utc).isoformat(), keyword="不存在的文字")
    assert result["items"] == [] and result["selection"]["selected_count"] == 0
    with pytest.raises(GroupChatError):
        store.save(_group_chat_summaries_SCOPE, "request:a", result["snapshot_id"], "无人讨论", [{"text": "无", "source_message_ids": ["m0"]}])

def test_cold_list_prepare_and_purge_do_not_create_database(tmp_path):
    archive = PlatformMessageStore(tmp_path / "platform.db", _group_chat_summaries_SCOPE.adapter_id)
    store = SummaryStore(archive)
    assert store.list(_group_chat_summaries_SCOPE)["items"] == []
    assert prepare(store)["snapshot_id"] is None
    assert archive.purge() == {"expired_count": 0, "backup_count": 0}
    assert not archive.database.exists()

def test_snapshot_expiry_quota_and_source_restore_do_not_revive_snapshot(tmp_path):
    _, store, clock = _group_chat_summaries_setup(tmp_path)
    first = prepare(store)
    for index in range(4):
        prepare(store, owner=f"other:{index}")
    with pytest.raises(GroupChatError, match="过多"):
        prepare(store, owner="sixth")
    clock[0] += 901
    with pytest.raises(GroupChatError):
        save(store, first)
    assert prepare(store, owner="sixth")["snapshot_id"] != first["snapshot_id"]

def test_large_source_is_explicitly_partial_and_not_silently_complete(tmp_path):
    archive, store, _ = _group_chat_summaries_setup(tmp_path)
    archive.record(_group_chat_summaries_SCOPE, ArchiveMessage("long", "member:a", NOW - 15, "字" * 2000))
    snapshot = prepare(store, limits=SummaryLimits(text_budget=1000, page_budget=500))
    assert snapshot["items"][0]["truncated"]
    assert "page_budget" in snapshot["selection"]["reasons"]
    assert "text_budget" in snapshot["selection"]["reasons"]
    assert not snapshot["selection"]["all_local_matches_selected"]

def test_list_cursor_bound_to_scope_and_keyword_and_revision_delete(tmp_path):
    _, store, _ = _group_chat_summaries_setup(tmp_path)
    snapshot = prepare(store)
    a = save(store, snapshot)["summary"]
    b = store.save(_group_chat_summaries_SCOPE, "request:a", snapshot["snapshot_id"], "另一个摘要",
                   [{"text": "其它概括", "source_message_ids": ["m1"]}])["summary"]
    page = store.list(_group_chat_summaries_SCOPE, limit=1)
    assert page["has_more"]
    with pytest.raises(ValueError):
        store.list(OTHER, cursor=page["next_cursor"])
    rest = store.list(_group_chat_summaries_SCOPE, limit=1, cursor=page["next_cursor"])
    assert {item["summary_id"] for item in page["items"] + rest["items"]} == {a["summary_id"], b["summary_id"]}
    assert store.delete(_group_chat_summaries_SCOPE, a["summary_id"], a["revision"])["status"] == "deleted"
    assert len(store.list(_group_chat_summaries_SCOPE)["items"]) == 1

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_main_summary_write_is_independent_of_reply_tool_and_subagents_cannot_save(tmp_path):
    adapter, event, service = _group_chat_summary_service_setup(tmp_path)
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
    assert len(SummaryStore(adapter.message_archive).list(_group_chat_summary_service_SCOPE)["items"]) == 1

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_session_first_turn_summary_tools_and_reply_disabled(tmp_path, asynchronous):
    adapter, event, _ = _group_chat_summary_service_setup(tmp_path)
    script = Script([LLMCallResponse("tools_call", "", tool_calls=[{"id": "summary-1", "name": "group_chat_prepare_summary", "arguments": {
        "start_time": "2020-01-01T00:00:00", "end_time": datetime.now(timezone.utc).isoformat()}}]), LLMCallResponse("message", "已读取指定时段")])
    instance = await session(tmp_path, script, asynchronous, False)
    instance._wf.tools_manager.disable_tool("group_chat_reply")
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    assert "总结一段讨论" in str(script.requests[0][0])
    result = json.loads(script.requests[1][0][-1]["content"])
    assert result["ok"] and result["items"][0]["message_id"] == "77"
    assert result["archive_coverage"]["platform_history_complete"] is False

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_summary_host_returns_stable_source_error_and_invalid_argument(tmp_path):
    _, event, service = _group_chat_summary_service_setup(tmp_path)
    workflow = object()
    with bind_call_origin(event.call_origin), bind_reply_turn(event) as turn, bind_tool_workflow(workflow):
        turn.workflow, turn.workflow_manager = workflow, SimpleNamespace(is_tool_enabled=lambda _: True)
        result = await service.execute("group_chat_read_summary_sources", {"snapshot_id": "unknown", "cursor": "bad"})
        assert result["error"]["code"] == "invalid_argument"
        missing = await service.execute("group_chat_prepare_summary", {"start_time": "2026-10-04T09:00:00"})
        assert missing["error"]["code"] == "invalid_argument"
        assert (await service.execute("group_chat_recent_messages", {}))["ok"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_agent_reads_saves_and_sends_summary_once_without_second_model(tmp_path, asynchronous):
    adapter, event, _ = _group_chat_summary_service_setup(tmp_path)

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
    assert len(SummaryStore(adapter.message_archive).list(_group_chat_summary_service_SCOPE)["items"]) == 1
    send_mock(adapter).assert_awaited_once()
    assert "".join(component.text for component in send_mock(adapter).call_args.args[1].components if component.type == PlatformComponentType.Plain) == "甲提及了乙"

@pytest.mark.asyncio
async def test_authenticated_cold_summary_api_scope_revision_and_delete_retry(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot:/a", "group", "群/中文")
    import time
    archive.record(scope, ArchiveMessage("source:a", "user:a", time.time(), "真实讨论"))
    store = SummaryStore(archive)
    snapshot = store.prepare(scope, "request:a", start_time="2020-01-01T00:00:00+08:00", end_time="2030-01-01T00:00:00+08:00")
    saved = store.save(scope, "request:a", snapshot["snapshot_id"], "讨论摘要", [{"text": "真实概括", "source_message_ids": ["source:a"]}])["summary"]
    query = urlencode({"self_id": scope.self_id, "chat_id": scope.chat_id, "conversation_kind": scope.conversation_kind})
    path = "/api/platforms/future/group-chat/summaries"
    page = _json_body(await _request(path + "?" + query))
    assert b"401" in (await _request(path + "?" + query, authorized=False)).split(b"\r\n", 1)[0]
    assert page["items"][0]["summary_id"] == saved["summary_id"]
    wrong = urlencode({"self_id": "other-bot", "chat_id": scope.chat_id})
    response = await _request(path + "/" + saved["summary_id"] + "?" + wrong)
    assert response.startswith(b"HTTP/1.1 404")
    target = path + "/" + saved["summary_id"] + "?" + query
    conflict = await _request(target, "DELETE", json.dumps({"expected_revision": 2, "idempotency_key": "intent-a"}).encode())
    assert _json_body(conflict)["code"] == "revision_conflict"
    payload = json.dumps({"expected_revision": 1, "idempotency_key": "intent-a"}).encode()
    first = _json_body(await _request(target, "DELETE", payload))
    repeated = _json_body(await _request(target, "DELETE", payload))
    assert first["status"] == repeated["status"] == "deleted" and repeated["replayed"]
    assert _json_body(await _request(path + "?" + query))["items"] == []
    assert archive.get(scope, "source:a")["text"] == "真实讨论"
