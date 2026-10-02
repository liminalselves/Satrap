from datetime import date
from pathlib import Path
import asyncio
import json
import logging
import subprocess
import sys
import time
import os

import pytest

from satrap.core.log.managed import ManagedDailyHandler
from satrap.core.log.policy import LoggingPolicy, LoggingPolicyStore, LoggingPolicyConflict, LogMaintenance


def store_for(tmp_path):
    return LoggingPolicyStore(tmp_path / "logging.json", tmp_path / "logs")


def expired(root, identity="123-aaaaaaaaaaaaaaaa"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"SATRAP-test-20200101-{identity}.log"
    path.write_text("过期", encoding="utf-8")
    return path


def test_default_policy_and_compare_and_swap(tmp_path):
    store = store_for(tmp_path)
    assert store.read() == (LoggingPolicy(True, 30), "missing")
    assert not store.path.exists()
    saved = store.save({"enabled": False, "retention_days": 90}, "missing")
    assert saved["policy"] == {"enabled": False, "retention_days": 90}
    before = store.path.read_bytes()
    with pytest.raises(LoggingPolicyConflict):
        store.save({"enabled": True, "retention_days": 1}, "missing")
    assert store.path.read_bytes() == before
    with pytest.raises(ValueError):
        store.save({"enabled": False, "retention_days": True}, saved["revision"])
    assert store.path.read_bytes() == before


def test_hourly_cleanup_policy_reload_disable_and_manual(tmp_path):
    store = store_for(tmp_path)
    old = expired(store.root)
    result = store.cleanup(automatic=True, now=10000)
    assert result["deleted"] == [old.name]
    old = expired(store.root)
    assert store.cleanup(automatic=True, now=13599) is None and old.exists()
    assert store.cleanup(automatic=True, now=13600)["deleted"] == [old.name]
    saved = store.save({"enabled": False, "retention_days": 30}, "missing")
    old = expired(store.root)
    assert store.cleanup(automatic=True, now=17200) is None and old.exists()
    assert store.cleanup(expected_revision=saved["revision"])["deleted"] == [old.name]
    with pytest.raises(LoggingPolicyConflict):
        store.cleanup(expected_revision="stale")
    saved = store.save({"enabled": True, "retention_days": 1}, saved["revision"])
    old = expired(store.root)
    assert store.cleanup(automatic=True, now=17201)["deleted"] == [old.name]


def test_maintenance_runs_while_idle_and_stops(tmp_path):
    store = store_for(tmp_path)
    current = [date(2020, 1, 1)]
    handler = ManagedDailyHandler(store.root, clock=lambda: current[0])
    handler.handle(logging.LogRecord("test", 30, "", 0, "旧日期", (), None))
    active = handler.current_file
    current[0] = date.today()
    maintenance = LogMaintenance(handler, store, interval=0.03)
    try:
        maintenance.start()
        deadline = time.monotonic() + 3
        while active.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not active.exists() and handler.stream is None
        new = expired(store.root)
        maintenance.clock = lambda: time.time() + 3601
        deadline = time.monotonic() + 3
        while new.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not new.exists()
    finally:
        maintenance.close()
        handler.close()
    assert not maintenance._thread.is_alive()


def test_corrupt_config_disables_deletion_until_repaired_and_reports(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    handler = ManagedDailyHandler(store.root)
    failures = []
    monkeypatch.setattr("satrap.core.log.policy.report_failure", failures.append)
    store.path.write_text("broken", encoding="utf-8")
    old = expired(store.root)
    maintenance = LogMaintenance(handler, store)
    try:
        maintenance.step()
        assert old.exists() and failures
        store.path.unlink()
        assert store.snapshot()["maintenance_error"]
        maintenance.step()
        assert not old.exists() and store.snapshot()["maintenance_error"] is None
    finally:
        maintenance.close()
        handler.close()


def test_save_failure_preserves_original_and_retry_succeeds(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    original_replace = os.replace
    def fail(source, destination):
        if Path(destination) == store.path:
            raise OSError("磁盘失败")
        return original_replace(source, destination)
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.save({"enabled": False, "retention_days": 60}, "missing")
    assert store.read() == (LoggingPolicy(), "missing")
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(os, "replace", original_replace)
    assert store.save({"enabled": False, "retention_days": 60}, "missing")["policy"]["retention_days"] == 60


def test_two_processes_coordinate_cleanup_and_keep_concurrent_records(tmp_path):
    store = store_for(tmp_path)
    old = expired(store.root)
    script = """
from pathlib import Path
import logging, json, sys
from satrap.core.log.managed import ManagedDailyHandler
from satrap.core.log.policy import LoggingPolicyStore
store = LoggingPolicyStore(Path(sys.argv[1]), Path(sys.argv[2]))
handler = ManagedDailyHandler(store.root, 'parallel')
for i in range(50):
    handler.handle(logging.LogRecord('probe', 30, '', 0, f'中文-{i}', (), None))
result = store.cleanup(automatic=True, now=10000)
print(json.dumps({'file': handler.current_file.name, 'cleaned': result is not None}), flush=True)
handler.close()
"""
    children = [subprocess.Popen([sys.executable, "-c", script, str(store.path), str(store.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}) for _ in range(2)]
    try:
        outputs = []
        for child in children:
            out, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            outputs.append(json.loads(out))
        assert sum(item["cleaned"] for item in outputs) == 1
        assert len({item["file"] for item in outputs}) == 2 and not old.exists()
        for item in outputs:
            assert (store.root / item["file"]).read_text(encoding="utf-8").splitlines() == [f"中文-{i}" for i in range(50)]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)


@pytest.mark.asyncio
async def test_control_api_read_save_cleanup_validation_and_failures(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control
    store = store_for(tmp_path)
    monkeypatch.setattr(control, "LoggingPolicyStore", lambda: store)
    logs = []
    monkeypatch.setattr(control.logger, "warning", logs.append)
    monkeypatch.setattr(control.logger, "error", logs.append)
    async def request(method, path="/config/logging", payload=None):
        content = json.dumps(payload).encode("utf-8") if payload is not None else b""
        reader = asyncio.StreamReader()
        reader.feed_data(content)
        reader.feed_eof()
        headers = f"{method} {path} HTTP/1.1\r\nContent-Length: {len(content)}\r\n\r\n".encode("utf-8")
        return await control._route_logging(control._RouteContext(method, path, path, reader, headers))
    status, data = await request("GET")
    assert status == 200 and data["policy"]["retention_days"] == 30
    status, saved = await request("PUT", payload={"policy": {"enabled": True, "retention_days": 2}, "expected_revision": data["revision"]})
    assert status == 200 and saved["policy"]["retention_days"] == 2
    assert (await request("PUT", payload={"policy": {"enabled": True, "retention_days": 2}, "expected_revision": "missing"}))[0] == 409
    assert (await request("PUT", payload={"policy": {"enabled": True, "retention_days": 0}, "expected_revision": saved["revision"]}))[0] == 400
    old = expired(store.root)
    status, result = await request("POST", "/config/logging/cleanup", {"expected_revision": saved["revision"]})
    assert status == 200 and result["ok"] and old.name in result["result"]["deleted"]
    old = expired(store.root)
    original = Path.unlink
    def deny(path, *args, **kwargs):
        if path == old:
            raise PermissionError("权限错误")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", deny)
    status, result = await request("POST", "/config/logging/cleanup", {"expected_revision": saved["revision"]})
    assert status == 200 and not result["ok"] and result["result"]["errors"]
    monkeypatch.setattr(store, "save", lambda *args: (_ for _ in ()).throw(OSError("故障")))
    assert (await request("PUT", payload={"policy": {}, "expected_revision": saved["revision"]}))[0] == 500
    assert (await request("DELETE"))[0] == 405
    assert logs


@pytest.mark.asyncio
async def test_http_dispatch_keeps_logging_api_authenticated(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control
    store = store_for(tmp_path)
    monkeypatch.setattr(control, "LoggingPolicyStore", lambda: store)
    server = await asyncio.start_server(control._handle_request, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        for authorized in (False, True):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            token = control._CONTROL_AUTH.token if authorized else "invalid"
            writer.write(f"GET /config/logging HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n\r\n".encode("utf-8"))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()
            await writer.wait_closed()
            assert response.startswith(b"HTTP/1.1 200" if authorized else b"HTTP/1.1 401")
    finally:
        server.close()
        await server.wait_closed()
