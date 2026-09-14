"""精确请求恢复、持久化边界和批量摘要查询测试"""
import copy
import json
import sqlite3

import pytest

from satrap.core.framework.Base.execution import codec
from satrap.core.framework.Base.execution.store import RunStore, RunConflictError
from satrap.core.storage.database import snapshot_session_domain, delete_session_domain_rows, restore_session_domain


def request(messages):
    return {"messages": messages, "tools": [{"function": {"name": "read"}}], "thinking": "high", "img_urls": ["https://example.test/image"], "_prepared": {"model": "test"}}


def test_exact_requests_across_append_truncation_summary_and_long_chain(tmp_path):
    store = RunStore(tmp_path / "p.db", "a")
    run = store.create({}, "c", "f")
    messages = [{"role": "system", "content": "系统" * 1000}, {"role": "user", "content": [{"type": "text", "text": "问题"}, {"type": "image_url", "image_url": {"url": "https://example.test/image"}}]}]
    expected = []
    for index in range(20):
        if index == 12:
            messages = [{"role": "system", "content": "摘要"}, *messages[-2:]]
        messages = [*messages, {"role": "assistant", "content": None, "reasoning_content": "推理", "tool_calls": [{"id": str(index), "type": "function", "function": {"name": "read", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": str(index), "content": str(index)}]
        value = request(copy.deepcopy(messages))
        store.start_model_step(run, f"model:{index}", value)
        store.finish_step(run, f"model:{index}", {"content": "ok"})
        expected.append(value)
    reopened = RunStore(store.database, "a")
    for index, value in enumerate(expected):
        actual, depth = reopened.model_request(run, f"model:{index}")
        assert actual == value
        assert depth <= codec.MAX_DEPTH
    with store.connect() as db:
        assert all("messages" not in json.loads(row[0])["_prepared"] for row in db.execute("SELECT input FROM agent_steps"))
        body = json.loads(db.execute("SELECT body FROM agent_step_inputs WHERE step_key='model:8'").fetchone()[0])
        assert body["depth"] == 0
    snapshot = snapshot_session_domain(store.database, "a")
    snapshot["agent_steps"].sort(key=lambda row: row["step_key"])
    delete_session_domain_rows(store.database, "a")
    restore_session_domain(store.database, "a", snapshot)
    assert reopened.model_request(run, "model:19")[0] == expected[-1]


@pytest.mark.parametrize("damage", ["missing", "forward", "digest", "version", "prefix"])
def test_corrupt_reference_stops_recovery(tmp_path, damage):
    store = RunStore(tmp_path / "p.db", "a")
    run = store.create({}, "c", "f")
    store.start_model_step(run, "model:0", request([{"role": "user", "content": "x" * 2000}]))
    with store.connect() as db:
        if damage == "missing":
            db.execute("DELETE FROM agent_step_inputs")
        else:
            value = json.loads(db.execute("SELECT body FROM agent_step_inputs").fetchone()[0])
            if damage == "forward":
                value["base"] = "model:0"
            elif damage == "digest":
                value["suffix"][0]["content"] = "changed"
            elif damage == "version":
                value["version"] = 99
            else:
                value["prefix"] = -1
            db.execute("UPDATE agent_step_inputs SET body=?", (json.dumps(value),))
    with pytest.raises(RunConflictError):
        store.model_request(run, "model:0")


def test_legacy_read_atomic_intent_and_scope(tmp_path):
    store = RunStore(tmp_path / "p.db", "a")
    run = store.create({}, "c", "f")
    legacy = request([{"role": "user", "content": "old"}])
    store.start_step(run, "model:0", "model", legacy)
    assert store.model_request(run, "model:0")[0] == legacy
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_probe BEFORE INSERT ON agent_step_inputs BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.start_model_step(run, "model:1", legacy)
    assert store.step(run, "model:1") is None
    other = RunStore(store.database, "b")
    with pytest.raises(RunConflictError):
        other.model_request(run, "model:0")
    with pytest.raises(RunConflictError):
        other.finish_step(run, "model:0", {})


def test_archive_preserves_requests_and_rejects_foreign_body(tmp_path):
    store = RunStore(tmp_path / "p.db", "a_main")
    run = store.create({}, "c", "f")
    first = request([{"role": "user", "content": "中文" * 2000}])
    second = request(first["messages"] + [{"role": "assistant", "content": "ok"}])
    store.start_model_step(run, "model:0", first)
    store.start_model_step(run, "model:1", second)
    snapshot = snapshot_session_domain(store.database, "a")
    delete_session_domain_rows(store.database, "a")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM agent_step_inputs").fetchone()[0] == 0
    restore_session_domain(store.database, "a", snapshot)
    assert store.model_request(run, "model:1")[0] == second
    older_database = tmp_path / "older.db"
    older = RunStore(older_database, "a_main")
    with older.connect() as db:
        db.execute("DROP TABLE agent_step_inputs")
    restore_session_domain(older_database, "a", snapshot)
    assert older.model_request(run, "model:1")[0] == second
    delete_session_domain_rows(store.database, "a")
    snapshot["agent_step_inputs"][0]["run_id"] = "foreign"
    with pytest.raises(ValueError):
        restore_session_domain(store.database, "a", snapshot)


def test_pagination_stable_ties_scope_filters_and_no_payload_reads(tmp_path):
    store = RunStore(tmp_path / "p.db", "a")
    runs = [store.create({"secret": "正文"}, "c", "f") for _ in range(43)]
    for run in runs:
        store.start_step(run, "tool:0", "tool", {})
    store.update(runs[0], status="completed")
    with store.connect() as db:
        db.execute("UPDATE agent_runs SET created_at=1,payload='invalid json'")
    other = RunStore(store.database, "b")
    other.create({}, "c", "f")
    seen, cursor = [], None
    while True:
        page = store.summaries(20, cursor)
        assert len(page["runs"]) <= 20
        assert all(len(row["steps"]) == 1 for row in page["runs"])
        seen.extend(row["id"] for row in page["runs"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == sorted(runs, reverse=True)
    assert len(store.summaries()["runs"]) == 43
    assert len(store.summaries(100, unfinished=True)["runs"]) == 42
    for invalid in ("invalid", "e30=", "W10="):
        with pytest.raises(ValueError):
            store.summaries(20, invalid)
    with pytest.raises(ValueError):
        other.summaries(20, store.summaries(20)["next_cursor"])
