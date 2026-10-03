from contextlib import closing
import sqlite3
import pytest

from satrap.core.storage.legacy_context_cleanup import LegacyContextCleanup
from satrap.expend.tools.agent.utils import SUB_AGENT_SYSTEM_PROMPT
from satrap.core.utils.context import ContextManager


@pytest.fixture
def old_database(tmp_path):
    database = tmp_path / "legacy.db"
    contexts = []
    for identity, prompt in (("sub_agent_candidate", SUB_AGENT_SYSTEM_PROMPT), ("sub_agent_custom", "自定义任务"), ("formal", "正常对话")):
        context = ContextManager(identity, db_path=str(database))
        context.reset_system_prompt(prompt)
        contexts.append(context)
    yield database
    for context in contexts:
        context.close()


def test_exact_cleanup_backup_restore_and_conflict(old_database, tmp_path):
    service = LegacyContextCleanup(old_database)
    preview = service.preview()
    assert set(preview["candidates"]) == {"sub_agent_candidate"}
    backup = tmp_path / "backup.json"
    assert service.apply(preview, backup)["deleted"] == 1
    with closing(sqlite3.connect(old_database)) as connection:
        assert set(row[0] for row in connection.execute("SELECT DISTINCT conversation_id FROM chat_history")) == {"sub_agent_custom", "formal"}
        assert not connection.execute("SELECT 1 FROM context_catalog WHERE context_id='sub_agent_candidate'").fetchone()
    assert service.restore(backup)["restored"] == 1
    assert service.preview()["revision"] == preview["revision"]
    with pytest.raises(ValueError, match="占用"):
        service.restore(backup)


def test_formal_reference_blocks_cleanup(old_database):
    with closing(sqlite3.connect(old_database)) as connection, connection:
        connection.execute("CREATE TABLE session_configs (session_id TEXT)")
        connection.execute("INSERT INTO session_configs VALUES ('sub_agent_candidate')")
    preview = LegacyContextCleanup(old_database).preview()
    assert not preview["candidates"]
    assert any(row["reason"] == "存在正式引用" for row in preview["rejected"])


def test_new_message_between_preview_and_apply_aborts_entire_operation(old_database, tmp_path):
    service = LegacyContextCleanup(old_database)
    preview = service.preview()
    with closing(sqlite3.connect(old_database)) as connection, connection:
        connection.execute("INSERT INTO chat_history (conversation_id, role, content) VALUES ('sub_agent_candidate', 'user', '新消息')")
    with pytest.raises(ValueError, match="变化"):
        service.apply(preview, tmp_path / "backup.json")
    assert not (tmp_path / "backup.json").exists()
    with closing(sqlite3.connect(old_database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_history WHERE conversation_id='sub_agent_candidate'").fetchone()[0] == 2


def test_backup_write_failure_does_not_delete(old_database, tmp_path):
    service = LegacyContextCleanup(old_database)
    backup = tmp_path / "occupied.json"
    backup.write_text("原始备份", encoding="utf-8")
    preview = service.preview()
    with pytest.raises(FileExistsError):
        service.apply(preview, backup)
    assert service.preview()["revision"] == preview["revision"]


def test_bad_checksum_prevents_restore(old_database, tmp_path):
    service = LegacyContextCleanup(old_database)
    backup = tmp_path / "backup.json"
    service.apply(service.preview(), backup)
    backup.write_text(backup.read_text(encoding="utf-8").replace(SUB_AGENT_SYSTEM_PROMPT.strip(), "篡改提示词"), encoding="utf-8")
    with pytest.raises(ValueError, match="校验"):
        service.restore(backup)
    assert not service.preview()["candidates"]
