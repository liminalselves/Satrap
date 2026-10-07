"""群记忆的真实事务, 所有权, 来源失效和审批冲突"""
from types import SimpleNamespace
from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

from satrap.core.call_context import CallOrigin, bind_call_origin, bind_tool_workflow
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.memory.scoped import MemoryError, ScopedMemories
from satrap.core.memory.service import MemoryService
from satrap.core.memory.store import MemoryStore


@pytest.fixture
def memories(tmp_path: Path) -> tuple[ScopedMemories, list[float]]:
    clock = [1_800_000_000.0]
    archive = PlatformMessageStore(tmp_path / "platform.db", "platform:custom", clock=lambda: clock[0])
    scope = MessageScope("platform:custom", "account:bot", "group", "group/特殊")
    for identity, sender in (("m1", "user:a"), ("m2", "user:a"), ("m3", "user:b")):
        archive.record(scope, ArchiveMessage(identity, sender, clock[0], "明确要求记住"))
    return ScopedMemories(archive, scope), clock


def add_preference(repository, *, owner="user:a", source="m1", key="preferred_name", operation="first"):
    return repository.mutate("create", {"kind": "member_preference", "key": key, "title": "称呼", "content": "称呼我为小明", "source_message_ids": [source]},
                             actor=owner, current_message=source, operation_id=operation)


def test_preference_owner_and_revision_are_enforced(memories):
    repository, _ = memories
    result = add_preference(repository)
    record = result["memory"]
    assert record["owner_user_id"] == "user:a" and record["revision"] == 1
    values = {"memory_id": record["memory_id"], "content": "简短回答", "source_message_ids": ["m3"], "expected_revision": 1}
    with pytest.raises(MemoryError, match="本人"):
        repository.mutate("update", values, actor="user:b", current_message="m3", operation_id="bad-owner")
    values["source_message_ids"] = ["m2"]
    changed = repository.mutate("update", values, actor="user:a", current_message="m2", operation_id="good-update")
    assert changed["memory"]["revision"] == 2
    with pytest.raises(MemoryError, match="已更新"):
        repository.mutate("update", values, actor="user:a", current_message="m2", operation_id="stale-update")


def test_source_must_include_current_request_and_belong_to_owner(memories):
    repository, _ = memories
    values = {"kind": "member_preference", "key": "style", "title": "风格", "content": "简洁", "source_message_ids": ["m1"]}
    with pytest.raises(MemoryError, match="本轮"):
        repository.mutate("create", values, actor="user:a", current_message="m2", operation_id="old-source")
    with pytest.raises(MemoryError, match="发言"):
        repository.mutate("create", values, actor="user:b", current_message="m1", operation_id="wrong-source")


def test_idempotency_does_not_duplicate_or_overwrite(memories):
    repository, _ = memories
    first = add_preference(repository)
    again = add_preference(repository)
    assert first["memory"]["memory_id"] == again["memory"]["memory_id"]
    existing = add_preference(repository, operation="another-request")
    assert existing["status"] == "already_exists"
    assert len(repository.list()["items"]) == 1
    with pytest.raises(MemoryError, match="不同内容"):
        add_preference(repository, key="changed", operation="first")


def test_group_rule_is_pending_until_transactionally_approved(memories):
    repository, _ = memories
    proposal = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "source_message_ids": ["m1"]},
                                 actor="user:a", current_message="m1", operation_id="rule")
    assert proposal["status"] == "pending"
    assert repository.list()["items"] == []
    result = repository.decide(proposal["proposal_id"], True, 0)
    assert result["status"] == "approved"
    assert repository.list()["items"][0]["content"] == "周五开会"
    assert repository.decide(proposal["proposal_id"], True, 0)["status"] == "approved"


def test_pending_update_cannot_overwrite_newer_operator_decision(memories):
    repository, _ = memories
    created = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会"}, actor="operator", operator=True, operation_id="operator-create")
    memory_id = created["memory_id"]
    proposal = repository.mutate("update", {"memory_id": memory_id, "expected_revision": 1, "content": "周六开会", "source_message_ids": ["m1"]},
                                 actor="user:a", current_message="m1", operation_id="rule-update")
    repository.mutate("update", {"memory_id": memory_id, "expected_revision": 1, "content": "周日开会"}, actor="operator", operator=True, operation_id="operator-update")
    assert repository.decide(proposal["proposal_id"], True, 1)["status"] == "conflicted"
    assert repository.get(memory_id)["memory"]["content"] == "周日开会"


def test_source_deleted_keeps_saved_memory_but_blocks_pending_approval(memories):
    repository, _ = memories
    preference = add_preference(repository)["memory"]
    pending = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "source_message_ids": ["m1"]},
                                actor="user:a", current_message="m1", operation_id="rule")
    repository.archive.recall(repository.scope, "m1")
    assert repository.get(preference["memory_id"])["memory"]["source_status"] == "unavailable"
    with pytest.raises(MemoryError, match="来源不可用"):
        repository.decide(pending["proposal_id"], True, 0)
    assert repository.proposals()["items"][0]["state"] == "pending"


def test_delete_is_current_request_only_and_does_not_delete_history(memories):
    repository, _ = memories
    memory = add_preference(repository)["memory"]
    values = {"memory_id": memory["memory_id"], "expected_revision": 1, "request_message_id": "m1"}
    with pytest.raises(MemoryError, match="本轮"):
        repository.mutate("delete", values, actor="user:a", current_message="m2", operation_id="old-delete")
    values["request_message_id"] = "m2"
    assert repository.mutate("delete", values, actor="user:a", current_message="m2", operation_id="delete")["status"] == "deleted"
    assert repository.archive.get(repository.scope, "m1")["text"] == "明确要求记住"
    assert repository.list()["items"] == []


def test_expired_proposal_and_foreign_scope_cursor(memories):
    repository, clock = memories
    add_preference(repository)
    add_preference(repository, key="style", operation="second")
    page = repository.list(limit=1)
    other_scope = MessageScope(repository.scope.adapter_id, repository.scope.self_id, "group", "other")
    other = ScopedMemories(repository.archive, other_scope)
    with pytest.raises(MemoryError, match="重新查询"):
        other.list(limit=1, cursor=page["next_cursor"])
    with pytest.raises(MemoryError, match="不属于"):
        other.get(page["items"][0]["memory_id"])
    proposal = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "source_message_ids": ["m1"]},
                                 actor="user:a", current_message="m1", operation_id="rule")
    clock[0] += 86401
    assert repository.decide(proposal["proposal_id"], True, 0)["status"] == "expired"


def test_model_default_listing_only_contains_own_preferences(memories):
    repository, _ = memories
    add_preference(repository)
    add_preference(repository, owner="user:b", source="m3", operation="other")
    assert len(repository.list()["items"]) == 2
    assert {item["owner_user_id"] for item in repository.list(viewer="user:a")["items"]} == {"user:a"}


def test_model_cannot_supply_owner_identity(memories):
    repository, _ = memories
    result = repository.mutate("create", {"kind": "member_preference", "key": "name", "title": "称呼", "content": "小明", "owner_user_id": "user:b", "source_message_ids": ["m1"]},
                               actor="user:a", current_message="m1", operation_id="owner-spoof")
    assert result["memory"]["owner_user_id"] == "user:a"


def reference_rows(repository: ScopedMemories, *, kind: str = "", user_id: str = "", keyword: str = "", viewer: str = "") -> list[sqlite3.Row]:
    """
    改动前的逐行筛选参照实现, 仅用于对照 SQL 侧筛选

    参数:
    - repository: 当前群记忆仓库
    - kind: 可选的记忆类型
    - user_id: 可选的成员所有者
    - keyword: 普通文本关键词
    - viewer: 非空时只保留本群记忆与该成员偏好

    返回:
    - 与筛选条件一致的全部记忆行, 次序与分页一致
    """
    with closing(repository.store._connect()) as connection:
        rows = connection.execute("SELECT * FROM memories WHERE scope=? ORDER BY importance DESC, updated_at DESC, id",
                                  (repository.scope.key,)).fetchall()
    return [row for row in rows if (not viewer or row["kind"] == "group_rule" or row["owner_user_id"] == viewer)
            and (not kind or row["kind"] == kind)
            and (not user_id or row["kind"] == "member_preference" and row["owner_user_id"] == user_id)
            and (not keyword or keyword.casefold() in (row["title"] + "\n" + row["content"]).casefold())]


def fill_mixed_memories(repository: ScopedMemories) -> None:
    """铺两位成员的偏好与一条已应用群记忆, 覆盖全部筛选维度"""
    add_preference(repository)
    add_preference(repository, owner="user:b", source="m3", operation="other")
    repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会 Meeting",
                                 "source_message_ids": ["m1"]}, actor="user:a", current_message="m1", operator=True, operation_id="rule")


@pytest.mark.parametrize("filters", [{}, {"kind": "group_rule"}, {"kind": "member_preference"}, {"user_id": "user:a"},
                                     {"viewer": "user:b"}, {"keyword": "称呼"}, {"keyword": "meeting"},
                                     {"user_id": "user:a", "keyword": "小明"}])
def test_list_filters_and_pagination_match_reference(memories: tuple[ScopedMemories, list[float]], filters: dict[str, str]) -> None:
    repository, _ = memories
    fill_mixed_memories(repository)
    collected: list[str] = []
    cursor = ""
    while True:
        response = repository.list(limit=1, cursor=cursor, **filters)
        collected.extend(item["memory_id"] for item in response["items"])
        cursor = response["next_cursor"] or ""
        if not cursor:
            break
    assert collected == [row["id"] for row in reference_rows(repository, **filters)]
