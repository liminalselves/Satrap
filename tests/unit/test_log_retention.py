from datetime import date
from pathlib import Path
import logging
import subprocess
import os
import sys

import pytest

from satrap.core.log.managed import ManagedDailyHandler, cleanup_logs, active_runtimes, management_lock


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
