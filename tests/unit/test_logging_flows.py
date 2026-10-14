"""业务链路场景与集中边界契约"""
from __future__ import annotations

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
from satrap.core.log.managed import cleanup_logs, active_runtimes, management_lock
from typing import Any, cast
import io
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.log.stream import StandardLogStream, StandardStreamCapture, standard_log_stream
from satrap.core.log import logger

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

@pytest.mark.parametrize("stage", ["path-resolution", "file-open"])
def test_logger_initialization_failure_keeps_console_usable(tmp_path, monkeypatch, capsys, stage):
    from satrap.core.log import Logger
    failures = []
    monkeypatch.setattr("satrap.core.log.report_failure", failures.append)
    monkeypatch.setattr("satrap.core.log.managed.report_failure", failures.append)
    if stage == "path-resolution":
        monkeypatch.setattr("satrap.core.log.log_root", lambda: (_ for _ in ()).throw(OSError("日志目录不可解析")))
    logger = Logger("invalid-root", std_out=True, file_name=str(tmp_path / "invalid\0path") if stage == "file-open" else None)
    try:
        logger.warning("业务仍可运行")
        assert failures and logger.log_file is None
        assert "业务仍可运行" in capsys.readouterr().err
    finally:
        logger.close()

def test_corrupt_cleanup_result_is_visible_without_breaking_config(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    store.root.mkdir()
    (store.root / ".cleanup-result.json").write_text('{"created_at": 1, "deleted": [], "skipped": [null], "errors": []}', encoding="utf-8")
    failures = []
    monkeypatch.setattr("satrap.core.log.policy.report_failure", failures.append)
    snapshot = store.snapshot()
    assert snapshot["status_error"] and snapshot["last_cleanup"] is None and failures
    assert snapshot["policy"]["retention_days"] == 30

def test_manual_global_failure_is_saved_and_retry_clears_it(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    from satrap.core.log import policy
    original_cleanup = policy.cleanup_logs
    monkeypatch.setattr(policy, "cleanup_logs", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("维护目录不可写")))
    with pytest.raises(PermissionError):
        store.cleanup(expected_revision="missing")
    assert store.snapshot()["maintenance_error"]["error"] == "维护目录不可写"
    monkeypatch.setattr(policy, "cleanup_logs", original_cleanup)
    assert store.cleanup(expected_revision="missing") is not None
    assert store.snapshot()["maintenance_error"] is None

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

def _record(text):
    return logging.LogRecord("probe", logging.WARNING, __file__, 1, text, (), None)

def test_utf8_daily_rotation_and_idle_close(tmp_path):
    now = [date(2026, 10, 3)]
    handler = ManagedDailyHandler(tmp_path, "backend", clock=lambda: now[0])
    try:
        handler.handle(_record("中文消息"))
        first = handler.current_file
        assert first.read_text(encoding="utf-8") == "中文消息\n"
        now[0] = date(2026, 10, 4)
        handler.tick()
        assert handler.current_file is None and handler.stream is None
        handler.handle(_record("次日消息"))
        assert handler.current_file != first
        assert "20261004" in handler.current_file.name
        assert first.read_text(encoding="utf-8") == "中文消息\n"
    finally:
        handler.close()

def test_date_boundary_and_active_file_protection(tmp_path):
    handler = ManagedDailyHandler(tmp_path, "chat", clock=lambda: date(2026, 9, 1))
    try:
        handler.handle(_record("还在写入"))
        with management_lock(tmp_path):
            assert active_runtimes(tmp_path)[0]["current_file"] == handler.current_file.name
        old = tmp_path / "SATRAP-test-20260903-123-aaaaaaaaaaaaaaaa.log"
        boundary = tmp_path / "SATRAP-test-20260904-123-aaaaaaaaaaaaaaaa.log"
        old.write_text("过期", encoding="utf-8")
        boundary.write_text("边界", encoding="utf-8")
        foreign = tmp_path / "foreign-20200101.log"
        foreign.write_text("其它程序", encoding="utf-8")
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert result["deleted"] == [old.name]
        assert handler.current_file.exists() and boundary.exists() and foreign.exists()
        assert result["skipped"][0]["file"] == handler.current_file.name
        saved = handler.current_file
    finally:
        handler.close()
    result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
    assert saved.name in result["deleted"]

def test_multiprocess_lease_and_crash_release(tmp_path):
    script = """
from datetime import date
from pathlib import Path
import logging, sys
from satrap.core.log.managed import ManagedDailyHandler
handler = ManagedDailyHandler(Path(sys.argv[1]), 'worker', clock=lambda: date(2020, 1, 1))
handler.handle(logging.LogRecord('probe', 30, '', 0, '子进程日志', (), None))
print(handler.current_file.name, flush=True)
sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    try:
        filename = child.stdout.readline().strip()
        assert filename.startswith("SATRAP-worker-")
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert (tmp_path / filename).exists() and result["skipped"]
        child.kill()
        child.communicate(timeout=10)
        assert filename in cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))["deleted"]
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)

def test_legacy_compatibility(tmp_path):
    old = tmp_path / "SATRAP-20200101.log"
    old.write_text("旧日志", encoding="utf-8")
    with old.open("a", encoding="utf-8"):
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert old.exists() and result["skipped"]
    result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
    if os.name == "nt":
        assert result["deleted"] == [old.name] and not result["errors"]
    else:
        assert result["skipped"] and old.exists()

def test_write_and_cleanup_failures_are_isolated_and_reported(tmp_path, monkeypatch):
    failures = []
    monkeypatch.setattr("satrap.core.log.managed.report_failure", failures.append)
    handler = ManagedDailyHandler(tmp_path)
    monkeypatch.setattr(handler, "tick", lambda **kwargs: (_ for _ in ()).throw(OSError("磁盘失败")))
    handler.handle(_record("不能写入"))
    assert "磁盘失败" in failures[0]
    handler.close()
    old = tmp_path / "SATRAP-test-20200101-123-aaaaaaaaaaaaaaaa.log"
    old.write_text("旧内容", encoding="utf-8")
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path == old:
            raise PermissionError("删除失败")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
    assert result["errors"][0]["file"] == old.name and old.exists()
    assert any("删除失败" in message for message in failures)

def test_write_failure_recovers_on_next_record(tmp_path, monkeypatch):
    failures = []
    monkeypatch.setattr("satrap.core.log.managed.report_failure", failures.append)
    handler = ManagedDailyHandler(tmp_path)
    try:
        handler.handle(_record("首条"))
        filename = handler.current_file
        handler.stream.close()
        handler.handle(_record("故障条"))
        assert failures and handler.stream is None
        handler.handle(_record("恢复条"))
        assert filename.read_text(encoding="utf-8") == "首条\n恢复条\n"
    finally:
        handler.close()

@pytest.mark.parametrize("content", ["broken", "missing_current_file", "wrong_process"])
def test_corrupt_live_state_protects_all_process_files(tmp_path, monkeypatch, content):
    failures = []
    monkeypatch.setattr("satrap.core.log.managed.report_failure", failures.append)
    handler = ManagedDailyHandler(tmp_path, clock=lambda: date(2020, 1, 1))
    try:
        handler.handle(_record("保留"))
        import json
        if content == "missing_current_file":
            content = json.dumps({"id": handler.identity})
        elif content == "wrong_process":
            content = json.dumps({"id": handler.identity, "current_file": "SATRAP-process-20200101-123-aaaaaaaaaaaaaaaa.log"})
        (tmp_path / ".runtime" / f"{handler.identity}.json").write_text(content, encoding="utf-8")
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert handler.current_file.exists() and result["skipped"] and failures
    finally:
        handler.close()

@pytest.mark.parametrize("days", [0, -1, 3651, True, 1.5, "30"])
def test_invalid_retention_cannot_delete_files(tmp_path, days):
    old = tmp_path / "SATRAP-test-20200101-123-aaaaaaaaaaaaaaaa.log"
    old.write_text("保留", encoding="utf-8")
    with pytest.raises(ValueError):
        cleanup_logs(tmp_path, days)
    assert old.exists()

def test_logger_integration_uses_independent_handlers_and_levels(tmp_path):
    from satrap.core.log import Logger
    first = Logger("same", std_out=False, output_dir=str(tmp_path), file_level=logging.WARNING)
    second = Logger("same", std_out=False, output_dir=str(tmp_path), file_level=logging.WARNING)
    try:
        first.set_service("custom-service")
        first.info("过滤")
        first.warning("第一实例")
        second.warning("第二实例")
        assert first.log_file != second.log_file
        content = Path(first.log_file).read_text(encoding="utf-8")
        assert "第一实例" in content and "第二实例" not in content and "过滤" not in content
        assert "custom-service" in first.log_file
    finally:
        first.close()
        second.close()

def test_symlink_and_foreign_file_are_preserved(tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}.log"
    outside.write_text("外部数据", encoding="utf-8")
    link = tmp_path / "SATRAP-test-20200101-123-aaaaaaaaaaaaaaaa.log"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("系统没有创建符号链接权限")
    result = cleanup_logs(tmp_path, 30)
    assert outside.read_text(encoding="utf-8") == "外部数据"
    assert link.is_symlink() and result["skipped"]

@pytest.mark.parametrize("name", [".runtime", ".locks"])
def test_redirected_management_directory_cannot_touch_outside_files(tmp_path, name):
    root = tmp_path / "logs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "123-aaaaaaaaaaaaaaaa.json"
    sentinel.write_text("不能删除", encoding="utf-8")
    (outside / "runtime-123-aaaaaaaaaaaaaaaa.lock").write_bytes(b"\0")
    target = root / name
    if os.name == "nt":
        command = "$taskUtf8 = [System.Text.UTF8Encoding]::new($false); [Console]::InputEncoding = $taskUtf8; [Console]::OutputEncoding = $taskUtf8; $OutputEncoding = $taskUtf8; chcp 65001 > $null; New-Item -ItemType Junction -Path $env:SATRAP_TEST_LINK -Target $env:SATRAP_TEST_TARGET | Out-Null"
        completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], env={**os.environ, "SATRAP_TEST_LINK": str(target), "SATRAP_TEST_TARGET": str(outside)}, text=True, encoding="utf-8", capture_output=True, timeout=15)
        assert completed.returncode == 0, completed.stderr
    else:
        target.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="重定向"):
            cleanup_logs(root, 30)
        assert sentinel.read_text(encoding="utf-8") == "不能删除"
        assert (outside / "runtime-123-aaaaaaaaaaaaaaaa.lock").exists()
    finally:
        if os.name == "nt":
            assert target.absolute().parent == root.resolve()
            os.rmdir(target)
        else:
            target.unlink()

def test_failure_diagnostics_cannot_raise_even_when_both_channels_fail(monkeypatch):
    from satrap.core.log import managed
    class BrokenStream:
        def write(self, content):
            raise OSError("stderr 故障")
    monkeypatch.setattr(sys, "__stderr__", BrokenStream())
    monkeypatch.setattr(managed.standard_log_stream, "publish", lambda *args: (_ for _ in ()).throw(RuntimeError("实时流故障")))
    messages = []
    monkeypatch.setattr(os, "write", lambda descriptor, content: messages.append(content))
    managed.report_failure("文件故障")
    assert "文件故障" in messages[0].decode("utf-8") and "实时流故障" in messages[0].decode("utf-8")
    monkeypatch.setattr(os, "write", lambda *args: (_ for _ in ()).throw(OSError("输出不可用")))
    managed.report_failure("全部通道故障")

@pytest.mark.asyncio
@pytest.mark.parametrize("history_limit", [2, 0])
async def test_standard_log_stream_history_and_live_entries(history_limit: int) -> None:
    """订阅原子获取历史日志 (limit=0 时为空) 并收到后续实时日志"""
    stream = StandardLogStream(history_size=3)
    stream.publish("first", "INFO")
    stream.publish("second", "WARNING")
    stream.publish("third", "ERROR")

    subscription = stream.subscribe(history_limit=history_limit, queue_size=2)
    try:
        assert [entry.content for entry in subscription.history] == (["second", "third"] if history_limit else [])
        stream.publish("live", "DEBUG")
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == ("live", "DEBUG")
    finally:
        stream.unsubscribe(subscription.subscription_id)
    stream.publish("after unsubscribe", "INFO")
    assert subscription.queue.empty()

@pytest.mark.asyncio
async def test_satrap_standard_logger_publishes_to_live_stream() -> None:
    """Satrap 控制台日志应同步到标准日志实时流"""
    standard_log_stream.clear()
    subscription = standard_log_stream.subscribe(history_limit=0)
    try:
        logger.info("standard stream probe", save_to_file=False)
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert entry.level == "INFO"
        assert "standard stream probe" in entry.content
    finally:
        standard_log_stream.unsubscribe(subscription.subscription_id)

@pytest.mark.asyncio
async def test_standard_stream_capture_preserves_output_and_publishes_lines() -> None:
    """标准流代理应保留原输出并按完整文本行发布"""
    wrapped = io.StringIO()
    stream = StandardLogStream()
    capture = StandardStreamCapture(wrapped, stream, "INFO")
    subscription = stream.subscribe(history_limit=0)
    try:
        assert capture.write("plain stdout") == len("plain stdout")
        assert subscription.queue.empty()
        capture.write("\n[ERROR]: failed\n")

        first = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        second = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (first.content, first.level) == ("plain stdout", "INFO")
        assert (second.content, second.level) == ("[ERROR]: failed", "ERROR")
        assert wrapped.getvalue() == "plain stdout\n[ERROR]: failed\n"
    finally:
        stream.unsubscribe(subscription.subscription_id)

@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
@pytest.mark.parametrize("template", [
    "[2026-09-28 21:27:17.222] [{level}]: 配置已刷新",
    "[2026-09-28 21:27:17,222] {level} in __init__: 配置已刷新",
    "[{level}]: 配置已刷新",
])
async def test_capture_uses_header_level_on_stderr(level: str, template: str) -> None:
    """标准流中的日志头级别应覆盖 stderr 默认级别"""
    wrapped = io.StringIO()
    stream = StandardLogStream()
    capture = StandardStreamCapture(wrapped, stream, "ERROR")
    subscription = stream.subscribe(history_limit=0)
    content = template.format(level=level)
    try:
        capture.write(content + "\n")
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == (content, level)
        assert wrapped.getvalue() == content + "\n"
    finally:
        stream.unsubscribe(subscription.subscription_id)

@pytest.mark.asyncio
@pytest.mark.parametrize(("content", "default_level", "expected_level"), [
    ("[INFO]: 正文包含 [ERROR]", "ERROR", "INFO"),
    ("[2026-09-28 21:27:17.222] [INFO]: 正文包含 [CRITICAL]", "ERROR", "INFO"),
    ("[2026-09-28 21:27:17,222] WARNING in __init__: 正文包含 [ERROR]", "ERROR", "WARNING"),
    ("普通输出包含 [ERROR]", "INFO", "INFO"),
    ("普通输出包含 [INFO]", "ERROR", "ERROR"),
    ("[2026-09-28 21:27:17.222] 正文包含 [INFO]", "ERROR", "ERROR"),
    ("[UNKNOWN]: 正文包含 [INFO]", "ERROR", "ERROR"),
    ("plain stdout", "INFO", "INFO"),
    ("plain stderr", "ERROR", "ERROR"),
    ('  File "[INFO]", line 1, in execute', "ERROR", "ERROR"),
    ("ConnectionResetError", "ERROR", "ERROR"),
])
async def test_capture_ignores_levels_in_body(
    content: str, default_level: str, expected_level: str,
) -> None:
    """正文和异常堆栈中的级别标记不应覆盖日志头或默认级别"""
    stream = StandardLogStream()
    capture = StandardStreamCapture(io.StringIO(), stream, default_level)
    subscription = stream.subscribe(history_limit=0)
    try:
        capture.write(content + "\n")
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == (content, expected_level)
    finally:
        stream.unsubscribe(subscription.subscription_id)

@pytest.mark.asyncio
async def test_capture_parses_colored_split_header_and_flushes_tail() -> None:
    """分段写入和 flush 均应在清理颜色后解析完整日志头"""
    wrapped = io.StringIO()
    stream = StandardLogStream()
    capture = StandardStreamCapture(wrapped, stream, "ERROR")
    subscription = stream.subscribe(history_limit=0)
    first = "\x1b[32m[2026-09-28 21:27:17.222] [INFO]: 配置已刷新\x1b[0m"
    tail = "[2026-09-28 21:27:18,568] WARNING in __init__: 应用失败"
    try:
        capture.write(first[:30])
        assert subscription.queue.empty()
        capture.write(first[30:] + "\r\n" + tail[:40])
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == (
            "[2026-09-28 21:27:17.222] [INFO]: 配置已刷新", "INFO",
        )
        capture.write(tail[40:])
        capture.flush()
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == (tail, "WARNING")
        capture.flush()
        await asyncio.sleep(0)
        assert subscription.queue.empty()
        assert wrapped.getvalue() == first + "\r\n" + tail
    finally:
        stream.unsubscribe(subscription.subscription_id)

class _Reader:
    """可控制 EOF 状态的 WebSocket 测试读取器"""

    def __init__(self) -> None:
        self.eof = False

    def at_eof(self) -> bool:
        """
        返回:
        - bool: 当前是否已结束
        """
        return self.eof

@pytest.mark.asyncio
async def test_websocket_logs_use_standard_stream_without_log_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日志 WebSocket 应发送内存标准流的历史与实时输出"""
    standard_log_stream.clear()
    capture = StandardStreamCapture(io.StringIO(), standard_log_stream, "ERROR")
    history = "[2026-09-28 21:27:17,222] INFO in __init__: 配置已刷新"
    live = "[WARNING]: 应用失败, 正文包含 [ERROR]"
    capture.write(history + "\n")
    server = BackendHTTPServer(BackendManager())
    reader = _Reader()
    sent: list[dict[str, Any]] = []

    async def capture_send(_writer: object, message: dict[str, Any]) -> None:
        """
        参数:
        - _writer: 未使用的测试写入器
        - message: WebSocket 消息
        """
        sent.append(message)

    monkeypatch.setattr(server, "_ws_send", capture_send)
    task = asyncio.create_task(server._ws_log_handler(
        cast(asyncio.StreamReader, reader),
        cast(asyncio.StreamWriter, object()),
        history_lines=10,
    ))
    await asyncio.sleep(0)

    capture.write(live + "\n")
    for _ in range(10):
        if len(sent) >= 2:
            break
        await asyncio.sleep(0.01)

    reader.eof = True
    standard_log_stream.publish("wake", "DEBUG")
    await asyncio.wait_for(task, timeout=1)

    assert sent[0]["data"] == {"content": history, "level": "INFO"}
    assert sent[1]["data"] == {"content": live, "level": "WARNING"}
