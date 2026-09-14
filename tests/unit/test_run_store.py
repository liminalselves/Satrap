"""执行记录的作用域和故障恢复基础约束"""
import pytest

from satrap.core.framework.Base.execution.store import RunStore, RunConflictError


def test_reopen_preserves_completed_and_uncertain_steps(tmp_path):
    store = RunStore(tmp_path / "platform.db", "session:main")
    with store.claim():
        run = store.create({"text": "test"}, "context", "config")
        store.start_step(run, "model:0", "model", {"messages": []}, "retry")
        store.finish_step(run, "model:0", {"content": "", "tools": ["write"]})
        store.start_step(run, "tool:0:0", "tool", {"name": "write"})
    reopened = RunStore(tmp_path / "platform.db", "session:main")
    assert reopened.step(run, "model:0")["status"] == "completed"
    assert reopened.step(run, "tool:0:0")["status"] == "running"
    assert reopened.step(run, "tool:0:0")["recovery_policy"] == "manual"


def test_scope_isolation_and_nonreentrant_claim(tmp_path):
    store = RunStore(tmp_path / "platform.db", "a")
    other = RunStore(tmp_path / "platform.db", "b")
    with store.claim():
        run = store.create({}, "c", "f")
        with pytest.raises(RunConflictError), store.claim():
            pass
        with other.claim():
            other.create({}, "c", "f")
    with pytest.raises(RunConflictError):
        other.get(run)
    with pytest.raises(RunConflictError):
        other.update(run, status="cancelled")
    with pytest.raises(RunConflictError):
        other.step(run, "model:0")


def test_explicit_retry_only_for_uncertain_tool(tmp_path):
    store = RunStore(tmp_path / "platform.db", "a")
    run = store.create({}, "c", "f")
    store.start_step(run, "tool:0:0", "tool", {"name": "write"})
    with pytest.raises(RunConflictError):
        store.authorize_retry(run, "tool:0:0")
    store.update(run, status="needs_attention")
    store.authorize_retry(run, "tool:0:0")
    assert store.step(run, "tool:0:0")["recovery_policy"] == "retry"
    store.abort(run)
    assert store.get(run)["status"] == "cancelled"


def test_result_must_be_serializable_without_destroying_step(tmp_path):
    store = RunStore(tmp_path / "platform.db", "a")
    run = store.create({}, "c", "f")
    store.start_step(run, "tool:0", "tool", {})
    with pytest.raises(TypeError):
        store.finish_step(run, "tool:0", object())
    assert store.step(run, "tool:0")["status"] == "running"


def test_messages_and_completion_commit_together(tmp_path):
    from satrap.core.utils.context import ContextManager

    path = tmp_path / "platform.db"
    context = ContextManager("a", db_path=str(path))
    store = RunStore(path, "a")
    try:
        run = store.create({}, store.history_signature(), "config")
        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]
        store.commit_messages(run, messages, "world")
        store.commit_messages(run, messages, "world")
        context.load_context()
        assert [item["content"] for item in context.get_context()] == ["hello", "world"]
        assert store.get(run)["status"] == "completed"
        other = store.create({}, store.history_signature(), "config")
        context.add_user_message("new input")
        with pytest.raises(RunConflictError):
            store.commit_messages(other, messages, "world")
        assert store.get(other)["status"] == "running"
    finally:
        context.close()


def test_commit_failure_rolls_back_messages_and_status(tmp_path):
    from satrap.core.utils.context import ContextManager

    path = tmp_path / "platform.db"
    context = ContextManager("a", db_path=str(path))
    store = RunStore(path, "a")
    try:
        run = store.create({}, store.history_signature(), "config")
        with pytest.raises(KeyError):
            store.commit_messages(run, [{"role": "user", "content": "hello"}, {}], "world")
        context.load_context()
        assert context.get_context() == []
        assert store.get(run)["status"] == "running"
    finally:
        context.close()


def test_os_lock_released_after_process_exit(tmp_path):
    import os
    import subprocess
    import sys

    database = tmp_path / "platform.db"
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,os; from satrap.core.framework.Base.execution.store import RunStore; "
         "s=RunStore(sys.argv[1],'a'); c=s.claim(); c.__enter__(); "
         "print('ready',flush=True); sys.stdin.readline(); os._exit(0)", str(database)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        store = RunStore(database, "a")
        with pytest.raises(RunConflictError), store.claim():
            pass
        child.communicate("exit\n", timeout=15)
        assert child.returncode == 0
        with store.claim():
            store.create({}, "context", "config")
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)


def test_archive_restores_run_steps_and_rejects_foreign_run(tmp_path):
    from satrap.core.storage.database import snapshot_session_domain, delete_session_domain_rows, restore_session_domain

    database = tmp_path / "platform.db"
    store = RunStore(database, "a_main")
    run = store.create({}, "context", "config")
    store.start_step(run, "tool:0", "tool", {})
    snapshot = snapshot_session_domain(database, "a")
    delete_session_domain_rows(database, "a")
    assert store.list() == []
    restore_session_domain(database, "a", snapshot)
    assert store.step(run, "tool:0")["status"] == "running"
    delete_session_domain_rows(database, "a")
    snapshot["agent_steps"][0]["run_id"] = "foreign"
    with pytest.raises(ValueError):
        restore_session_domain(database, "a", snapshot)
