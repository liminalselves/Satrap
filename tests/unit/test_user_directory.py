from contextlib import closing
import sqlite3
import json

import pytest

from satrap.core.config.user_directory import UserDirectoryService, UserDirectoryConflict, missing_profile_revision
from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.storage.context_catalog import bind_context
from satrap.core.type import UserInfo
from satrap.core.utils.context import ContextManager


def _service(tmp_path):
    database = tmp_path / "platform.db"
    store = UserInfoStore(database)
    store.upsert(UserInfo(user_id="member", user_nickname="昵称", user_platform="future", user_session=["manual", "missing"]))
    store.upsert(UserInfo(user_id="empty", user_nickname="无对话"))
    for identity in ("manual", "owned", "group"):
        context = ContextManager(identity, db_path=str(database))
        context.add_user_message(identity)
        bind_context(context, identity, "main")
        context.close()
    with closing(sqlite3.connect(database)) as connection, connection:
        for scope, owner, session in (("group_member", "member", "owned"), ("group", "", "group"), ("group_member", "unprofiled", "owned")):
            key = "scoped:v1:" + json.dumps(["future-instance", "edictum", "test", scope, "bot", "group-id", owner])
            connection.execute("INSERT INTO context_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (key, owner, "future", "edictum", "test", session, 1, 1))
    return UserDirectoryService(database, {"id": "future-instance", "type": "future", "label": "未来平台"})


def test_readonly_missing_database_and_missing_tables(tmp_path):
    service = UserDirectoryService(tmp_path / "missing" / "platform.db", {"id": "other"})
    assert service.records() == []
    assert not service.database.parent.exists()
    service.database.parent.mkdir()
    with closing(sqlite3.connect(service.database)) as connection:
        connection.execute("CREATE TABLE other (value TEXT)")
        connection.commit()
    before = service.database.read_bytes()
    assert service.records() == []
    assert service.database.read_bytes() == before


def test_directory_ownership_keeps_empty_users_and_does_not_assign_shared_groups(tmp_path):
    service = _service(tmp_path)
    before = service.database.read_bytes()
    users = {item["user_id"]: item for item in service.records()}
    assert users["empty"]["conversations"] == []
    assert not users["unprofiled"]["has_profile"]
    member = {item["conversation_id"]: item for item in users["member"]["conversations"]}
    assert member["owned"]["routed"] and not member["owned"]["manual"]
    assert member["manual"]["manual"] and not member["manual"]["routed"]
    assert not member["missing"]["exists"]
    assert "group" not in member
    assert service.database.read_bytes() == before


def test_edit_clear_nickname_cas_and_deletion_preserve_messages_routes(tmp_path):
    service = _service(tmp_path)
    user = next(item for item in service.records() if item["user_id"] == "member")
    updated = service.mutate({"action": "update", "user_id": "member", "nickname": "", "expected_revision": user["revision"]})["user"]
    assert updated["user_nickname"] == ""
    with pytest.raises(UserDirectoryConflict):
        service.mutate({"action": "delete", "user_id": "member", "expected_revision": user["revision"]})
    deleted = service.mutate({"action": "delete", "user_id": "member", "expected_revision": updated["revision"]})["user"]
    assert not deleted["has_profile"]
    assert [item["conversation_id"] for item in deleted["conversations"]] == ["owned"]
    with closing(sqlite3.connect(service.database)) as connection:
        assert connection.execute("SELECT count(*) FROM chat_history").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM context_sessions").fetchone()[0] == 3


def test_associations_validate_platform_sessions_and_leave_route_intact(tmp_path):
    service = _service(tmp_path)
    user = next(item for item in service.records() if item["user_id"] == "member")
    with pytest.raises(KeyError):
        service.mutate({"action": "associate", "user_id": "member", "session_id": "ghost", "expected_revision": user["revision"]})
    assert next(item for item in service.records() if item["user_id"] == "member")["revision"] == user["revision"]
    added = service.mutate({"action": "associate", "user_id": "member", "session_id": "owned", "expected_revision": user["revision"]})["user"]
    removed = service.mutate({"action": "dissociate", "user_id": "member", "session_id": "owned", "expected_revision": added["revision"]})["user"]
    assert next(item for item in removed["conversations"] if item["conversation_id"] == "owned")["routed"]
    assert "owned" not in removed["user_session"]


def test_create_profile_and_duplicate_conflict(tmp_path):
    service = UserDirectoryService(tmp_path / "platform.db", {"id": "new", "type": "new-type"})
    data = {"action": "create", "user_id": "id", "nickname": "", "expected_revision": missing_profile_revision()}
    result = service.mutate(data)
    assert result["user"]["user_platform"] == "new-type"
    with pytest.raises(UserDirectoryConflict):
        service.mutate(data)
    with pytest.raises(ValueError):
        service.mutate({"action": "delete", "user_id": "id"})


def test_broken_association_is_visible_and_does_not_hide_other_users(tmp_path):
    service = _service(tmp_path)
    with closing(sqlite3.connect(service.database)) as connection, connection:
        connection.execute("UPDATE user_info SET user_session='invalid-json' WHERE user_id='member'")
    users = {item["user_id"]: item for item in service.records()}
    assert users["member"]["warning"]
    assert users["member"]["conversations"][0]["conversation_id"] == "owned"
    assert users["empty"]["has_profile"]
    with pytest.raises(ValueError):
        service.mutate({"action": "associate", "user_id": "member", "session_id": "manual", "expected_revision": users["member"]["revision"]})
