"""验证凭据与进程文件归类后仍保持共享认证和单实例保护"""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from satrap.core import runtime_paths
from satrap.core.log import managed
from satrap.core.server_auth import load_or_create_api_token
from satrap.cli.backend_lock import BackendInstanceLock


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    root = tmp_path / ".satrap"
    root.mkdir()
    monkeypatch.setattr(runtime_paths, "_data_dir", lambda: root)
    for name in ["SATRAP_CREDENTIALS_ROOT", "SATRAP_RUNTIME_ROOT", "SATRAP_API_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    return root


def test_concurrent_token_migration_preserves_existing_credential(state_root):
    token = "existing-shared-credential-" + "a" * 32
    (state_root / "api-token").write_text(token, encoding="utf-8")
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _: load_or_create_api_token(runtime_paths.get_credential_path()), range(16)))
    assert set(tokens) == {token}
    assert not (state_root / "api-token").exists()
    assert (state_root / "credentials" / "api-token").read_text(encoding="utf-8") == token


@pytest.mark.parametrize("name", ["control_server.pid", "backend.pid", "release-instance.json", "release-stop"])
def test_runtime_records_migrate_without_content_changes(state_root, name):
    content = json.dumps({"pid": 123, "runtime_id": "test-instance"}).encode("utf-8")
    old = state_root / name
    old.write_bytes(content)
    path = runtime_paths.get_runtime_path(name)
    assert path == state_root / "runtime" / name
    assert path.read_bytes() == content and not old.exists()


def test_held_legacy_backend_lock_is_not_bypassed(state_root, monkeypatch):
    old = state_root / "backend.lock"
    old.write_bytes(b"occupied")
    held = managed.LogFileLock(old)
    errors = []
    monkeypatch.setattr(managed, "report_failure", errors.append)
    assert held.acquire()
    try:
        lock = BackendInstanceLock()
        assert lock.path == old
        assert not (state_root / "runtime" / "backend.lock").exists()
        assert not lock.acquire("127.0.0.1", 19870)
        assert errors and "旧运行锁仍被进程持有" in errors[0]
    finally:
        held.release()
    first = BackendInstanceLock()
    second = BackendInstanceLock()
    assert first.path == state_root / "runtime" / "backend.lock" and not old.exists()
    assert first.acquire("127.0.0.1", 19870)
    try:
        assert not second.acquire("127.0.0.1", 19870)
    finally:
        first.release()
    assert second.acquire("127.0.0.1", 19870)
    second.release()


def test_credential_migration_failure_keeps_old_token(state_root, monkeypatch):
    token = "do-not-replace-credential-" + "b" * 32
    legacy = state_root / "api-token"
    legacy.write_text(token, encoding="utf-8")
    errors = []
    monkeypatch.setattr(managed.LogFileLock, "acquire", lambda self, timeout=0: False)
    monkeypatch.setattr(managed, "report_failure", errors.append)
    path = runtime_paths.get_credential_path()
    assert path == legacy and load_or_create_api_token(path) == token
    assert errors and token not in errors[0]


def test_directory_overrides_do_not_migrate_project_files(state_root, tmp_path, monkeypatch):
    (state_root / "api-token").write_bytes(b"old-credential")
    (state_root / "backend.pid").write_bytes(b"123")
    monkeypatch.setenv("SATRAP_CREDENTIALS_ROOT", str(tmp_path / "secrets"))
    monkeypatch.setenv("SATRAP_RUNTIME_ROOT", str(tmp_path / "processes"))
    assert runtime_paths.get_credential_path() == tmp_path / "secrets" / "api-token"
    assert runtime_paths.get_runtime_path("backend.pid") == tmp_path / "processes" / "backend.pid"
    assert (state_root / "api-token").exists() and (state_root / "backend.pid").exists()
