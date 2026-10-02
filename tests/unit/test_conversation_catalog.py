from contextlib import closing
from unittest.mock import MagicMock
import sqlite3
import json

from satrap.core.config.conversation_catalog import platform_catalog, filter_records, record_facets
from satrap.core.storage.database import snapshot_session_domain, delete_session_domain_rows, restore_session_domain
from satrap.core.config.conversation_data import ConversationDataService
from satrap.core.storage.context_catalog import bind_context
from satrap.core.utils.context import ContextManager
from satrap.core.storage.layout import StorageLayout
from satrap.core.platform import registry


def test_exact_owner_beats_prefix_and_archive_preserves_unrelated_context(tmp_path):
    database = tmp_path / "catalog.db"
    context = ContextManager("custom-workflow-id", db_path=str(database))
    context.add_user_message("工作流")
    bind_context(context, "root", "workflow", "检索")
    unrelated = ContextManager("root_other", db_path=str(database))
    unrelated.add_user_message("其它会话")
    bind_context(unrelated, "different", "main")
    service = ConversationDataService(database)
    records = service.catalog_records()
    assert {row["conversation_id"] for row in records} == {"root", "different"}
    root = next(row for row in records if row["conversation_id"] == "root")
    assert root["context_ids"] == ["custom-workflow-id"]
    assert root["contexts"][0]["name"] == "检索"
    archived = snapshot_session_domain(database, "root")
    assert [row["context_id"] for row in archived["context_catalog"]] == ["custom-workflow-id"]
    assert [row["content"] for row in archived["chat_history"]] == ["工作流"]
    delete_session_domain_rows(database, "root")
    assert service.read("root_other", "context")["items"][0]["content"] == "其它会话"
    restore_session_domain(database, "root", archived)
    assert service.read("custom-workflow-id", "context")["items"][0]["content"] == "工作流"
    context.close()
    unrelated.close()


def test_legacy_child_hidden_but_searchable_by_explicit_category(tmp_path):
    database = tmp_path / "catalog.db"
    context = ContextManager("sub_agent_legacy", db_path=str(database))
    context.reset_system_prompt("系统")
    service = ConversationDataService(database)
    records = service.catalog_records()
    assert filter_records(records) == []
    assert filter_records(records, filters={"kind": "legacy_child"})[0]["facets"]["source"] == ["unknown"]
    assert record_facets(records)["kind"] == [{"value": "legacy_child", "label": "旧子代理记录"}]
    context.close()


def test_read_only_legacy_catalog_never_migrates_or_fakes_time(tmp_path):
    database = tmp_path / "old.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE chat_history (conversation_id TEXT, role TEXT, content TEXT)")
        connection.execute("INSERT INTO chat_history VALUES ('unknown', 'user', '旧消息')")
    before = database.read_bytes()
    row = ConversationDataService(database).catalog_records()[0]
    assert row["last_activity_at"] is None
    assert row["facets"]["source"] == ["unknown"]
    assert database.read_bytes() == before


def test_dynamic_platform_and_adapter_labels_without_platform_type_branches(tmp_path, monkeypatch):
    class NewAdapter:
        display_name = "新增平台"

        @classmethod
        def conversation_catalog_metadata(cls, connection, route):
            return {"target": "新增频道", "channel": "自定义频道分类"}

    monkeypatch.setitem(registry._mapping, "future-type", NewAdapter)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future-instance")
    platforms = platform_catalog(layout, {"platforms": [{"id": "future-instance", "type": "future-type"}]})
    descriptor = next(item for item in platforms if item["id"] == "future-instance")
    assert descriptor["type_label"] == "新增平台"
    context = ContextManager("root_main", db_path=str(layout.platform_db("future-instance")))
    context.add_user_message("消息")
    bind_context(context, "root", "main")
    with closing(sqlite3.connect(context.db_path)) as connection, connection:
        connection.execute("CREATE TABLE context_sessions (context_key TEXT, session_id TEXT, user_id TEXT, platform TEXT, provider_name TEXT, session_type TEXT)")
        key = "scoped:v1:" + json.dumps(["future-instance", "edictum", "群聊", "group", "bot", "channel-1", ""])
        connection.execute("INSERT INTO context_sessions VALUES (?, 'root', '', 'future-instance', 'edictum', '群聊')", (key,))
    before = layout.platform_db("future-instance").read_bytes()
    row = ConversationDataService(context.db_path).catalog_records(descriptor)[0]
    assert row["title"] == "新增频道"
    assert row["facets"]["target"] == ["future-instance/bot/channel-1"]
    assert row["facets"]["channel"] == ["自定义频道分类"]
    assert row["facets"]["agent"] == ["群聊"]
    assert layout.platform_db("future-instance").read_bytes() == before
    context.close()


def test_legacy_user_route_never_claims_group(tmp_path):
    database = tmp_path / "catalog.db"
    context = ContextManager("root", db_path=str(database))
    context.add_user_message("消息")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE context_sessions (context_key TEXT, session_id TEXT, user_id TEXT, platform TEXT, provider_name TEXT, session_type TEXT)")
        connection.execute("INSERT INTO context_sessions VALUES ('old-key', 'root', 'user', 'adapter', 'edictum', '旧群聊')")
    row = ConversationDataService(database).catalog_records({"id": "adapter", "type": "unregistered"})[0]
    assert row["facets"]["scope"] == ["legacy_user"]
    assert "target" not in row["facets"]
    assert row["facets"]["user"] == ["adapter/user"]
    assert row["facet_labels"]["scope:legacy_user"] == "旧版用户共享"
    context.close()


def test_activity_tracks_appends_and_edits_separately(tmp_path):
    context = ContextManager("main", db_path=str(tmp_path / "catalog.db"))
    context.add_user_message("新消息")
    before = ConversationDataService(context.db_path).catalog_records()[0]["last_activity_at"]
    context.replace_messages([{"role": "user", "content": "编辑"}])
    after = ConversationDataService(context.db_path).catalog_records()[0]["last_activity_at"]
    assert before == after
    context.close()


def test_empty_shared_context_does_not_hide_main_workflow_messages(tmp_path):
    from satrap.core.framework.Base import Session

    session = Session("root", db_path=str(tmp_path / "catalog.db"))
    workflow = ContextManager("root_main", db_path=session.session_ctx.db_path)
    workflow.add_user_message("实际模型上下文")
    session._track_workflow_context("root_main", workflow)
    row = ConversationDataService(workflow.db_path).catalog_records()[0]
    assert row["context_ids"][0] == "root_main"
    assert row["contexts"][0]["kind"] == "main"
    assert next(context for context in row["contexts"] if context["id"] == "root")["kind"] == "shared"
    workflow.close()
    session.session_ctx.close()
