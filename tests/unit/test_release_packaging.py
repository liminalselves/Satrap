"""发行包资源隔离与启动器进程所有权回归验证"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import os
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from satrap.core.backend.ui_config import build_ui_config


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, path: Path):
    """加载构建和启动脚本, 不执行命令行入口"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    monkeypatch.delenv("SATRAP_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("SATRAP_CREDENTIALS_ROOT", raising=False)
    module = load_script("release_launcher", ROOT / "scripts/release/launcher.py")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    data = tmp_path / ".satrap"
    data.mkdir()
    monkeypatch.setattr(module, "DATA", data)
    runtime = data / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(module, "STATE", runtime / "release-instance.json")
    monkeypatch.setattr(module, "STOP", runtime / "release-stop")
    return module


def test_release_lock_is_exclusive_and_reusable(launcher):
    """操作系统锁阻止重复启动, 释放后允许再次启动"""
    first = launcher.InstanceLock()
    second = launcher.InstanceLock()
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_launcher_rejects_occupied_port_without_stopping_owner(launcher, monkeypatch):
    """端口冲突报错后原监听器仍可接受连接"""
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        port = owner.getsockname()[1]
        monkeypatch.setattr(launcher, "port_description", lambda ports: f"端口 {port}: test-owner")
        with pytest.raises(RuntimeError, match="已被占用"):
            launcher.check_ports([port])
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) == 0


def test_start_failure_cleans_only_owned_processes(launcher, monkeypatch):
    """聊天服务启动失败时回收本次进程并释放目录锁"""
    from satrap.core.config.loader import ConfigLoader
    monkeypatch.setattr(ConfigLoader, "autodetect", lambda: SimpleNamespace(api_host="127.0.0.1", api_port=19870))
    monkeypatch.setattr(launcher, "check_ports", lambda ports, **kwargs: None)
    monkeypatch.setattr(launcher, "process_snapshot", lambda: [])
    monkeypatch.setattr(launcher, "request", lambda *args, **kwargs: {})
    monkeypatch.setattr(launcher, "WindowsProcessJob", lambda: SimpleNamespace(assign=lambda process: None, close=lambda: None))
    processes = []

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.returncode = None
            self.terminated = False
            processes.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(launcher.subprocess, "Popen", FakeProcess)

    def wait_health(url, process):
        if "chat" in url:
            raise RuntimeError("模拟聊天服务启动失败")
        return {}

    monkeypatch.setattr(launcher, "wait_health", wait_health)
    with pytest.raises(RuntimeError, match="模拟聊天"):
        launcher.start(True)
    assert len(processes) == 2
    assert all(process.terminated for process in processes)
    assert not launcher.STATE.exists()
    lock = launcher.InstanceLock()
    assert lock.acquire()
    lock.release()


def test_stop_does_not_use_stale_instance_record(launcher, monkeypatch):
    """没有持锁启动器时不操作过期状态指向的服务"""
    launcher.STATE.write_text('{"instance":"stale","pid":1}', encoding="utf-8")
    monkeypatch.setattr(launcher, "process_snapshot", lambda: [])
    monkeypatch.setattr(launcher, "check_ports", lambda ports, **kwargs: None)
    launcher.stop()
    assert not launcher.STOP.exists()


def test_archive_excludes_generated_state_and_caches(tmp_path):
    """发行 ZIP 排除凭据, 数据库和验证时产生的缓存"""
    builder = load_script("release_builder", ROOT / "scripts/build_release.py")
    stage = tmp_path / "Satrap-test"
    for name in ("satrap/main.py", ".satrap/api-token", ".satrap/data/private.db", "satrap/__pycache__/main.pyc", "runtime/Lib/test.pyc"):
        file = stage / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("test", encoding="utf-8")
    target = tmp_path / "release.zip"
    builder.archive(stage, target)
    with zipfile.ZipFile(target) as package:
        assert package.namelist() == ["Satrap-test/satrap/main.py"]


def test_release_ui_config_uses_chat_port_override(monkeypatch):
    """便携启动器自定义聊天端口在管理面板中生效"""
    monkeypatch.setenv("SATRAP_CHAT_PORT", "29872")
    monkeypatch.setenv("SATRAP_CONTROL_PORT", "29871")
    assert build_ui_config()["chat_api"] == "http://127.0.0.1:29872"
    assert build_ui_config()["control_api"] == "http://127.0.0.1:29871"
    assert build_ui_config(chat_port=39872)["chat_api"] == "http://127.0.0.1:39872"


def test_copy_sources_includes_builtin_resources(tmp_path):
    """直接从 Git 跟踪源码复制内置插件元数据和技能正文"""
    builder = load_script("release_builder", ROOT / "scripts/build_release.py")
    builder.copy_sources(tmp_path)
    assert (tmp_path / "satrap/expend/plugins/base_take/meta.yaml").is_file()
    assert (tmp_path / "satrap/expend/plugins/satrap_coding/skills/goal/skill.md").is_file()
    assert not (tmp_path / ".satrap").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 作业对象验证")
def test_windows_job_reclaims_owned_child(launcher):
    """释放启动器作业句柄后所属子进程必须退出"""
    job = launcher.WindowsProcessJob()
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", "import time; time.sleep(30)"], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        job.assign(process)
        job.close()
        process.wait(timeout=5)
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("module,args,owned", [
    ("satrap.core.backend.control_server", "--port 29871", True),
    ("satrap.display.server", "", True),
    ("satrap.main", "--api-port 29870 run", True),
    ("satrap", "status", False),
    ("satrap.main", "model list", False),
    ("satrap.display.server_extra", "", False),
])
def test_service_identity_requires_exact_runtime_and_module(launcher, module, args, owned):
    """只有本目录运行时启动的精确服务模块可以被清理"""
    executable = launcher.ROOT / "runtime/python.exe"
    process = {"ExecutablePath": str(executable), "CommandLine": f'"{executable}" -X utf8 -m {module} {args}'}
    assert bool(launcher.service_module(process)) is owned
    process["ExecutablePath"] = str(launcher.ROOT / "another-instance/runtime/python.exe")
    assert launcher.service_module(process) is None


def test_recovery_cleans_owned_orphans_and_keeps_foreign_services(launcher, monkeypatch):
    """启动前清理本目录旧服务, 其他实例与普通 Python 程序保留"""
    executable = launcher.ROOT / "runtime/python.exe"
    owned = {"ProcessId": 10, "ExecutablePath": str(executable), "CommandLine": f'"{executable}" -m satrap.core.backend.control_server'}
    foreign = {"ProcessId": 20, "ExecutablePath": str(launcher.ROOT / "elsewhere/python.exe"), "CommandLine": "python -m satrap.display.server"}
    ordinary = {"ProcessId": 30, "ExecutablePath": str(executable), "CommandLine": f'"{executable}" -c "print(1)"'}
    monkeypatch.setattr(launcher, "process_snapshot", lambda: [owned, foreign, ordinary])
    monkeypatch.setattr(launcher, "listener_snapshot", lambda: [{"LocalPort": 29871, "OwningProcess": 10}])
    terminated = []
    requested = []
    verified = []
    monkeypatch.setattr(launcher, "terminate_service", lambda process: terminated.append(process["ProcessId"]))
    monkeypatch.setattr(launcher, "request", lambda url, *args, **kwargs: requested.append(url))
    monkeypatch.setattr(launcher, "check_ports", lambda ports, **kwargs: verified.append(ports))
    launcher.recover_services([29871, 29872, 29870], 29871)
    assert terminated == [10]
    assert requested == ["http://127.0.0.1:29871/stop"]
    assert verified == [[29871, 29872, 29870]]


def test_port_residue_reports_owner_without_exposing_command(launcher, monkeypatch):
    """停止后的残留检查输出进程名和 PID, 不泄漏命令行密钥"""
    monkeypatch.setattr(launcher, "busy_ports", lambda ports: [29870])
    monkeypatch.setattr(launcher, "process_snapshot", lambda: [{"ProcessId": 42, "Name": "python.exe", "CommandLine": "--token secret"}])
    monkeypatch.setattr(launcher, "listener_snapshot", lambda: [{"LocalPort": 29870, "OwningProcess": 42}])
    with pytest.raises(RuntimeError, match="python.exe, PID 42") as error:
        launcher.check_ports([29870])
    assert "secret" not in str(error.value)


def test_stopped_launcher_does_not_report_success_with_occupied_ports(launcher, monkeypatch):
    """目录锁已释放但端口仍被占用时, stop 必须报告失败"""
    launcher.STATE.write_text('{"ports":[29871,29872,29870]}', encoding="utf-8")
    monkeypatch.setattr(launcher, "process_snapshot", lambda: [])
    monkeypatch.setattr(launcher, "busy_ports", lambda ports: [29872])
    monkeypatch.setattr(launcher, "port_description", lambda ports: "端口 29872: unknown.exe, PID 42")
    with pytest.raises(RuntimeError, match="29872"):
        launcher.stop()
    assert launcher.STATE.exists()


def test_port_release_check_retries_before_success(launcher, monkeypatch):
    """允许子进程退出后的短暂端口释放延迟"""
    checks = iter([[29872], []])
    monkeypatch.setattr(launcher, "busy_ports", lambda ports: next(checks))
    monkeypatch.setattr(launcher.time, "sleep", lambda duration: None)
    launcher.check_ports([29872], timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows 进程身份复核验证")
def test_native_cleanup_rechecks_creation_time_before_termination(launcher, monkeypatch):
    """创建时间不匹配时保留进程, 身份一致后才终止测试创建的子进程"""
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        snapshot = next(item for item in launcher.process_snapshot() if int(item["ProcessId"]) == process.pid)
        monkeypatch.setattr(launcher, "runtime_executable", lambda: Path(sys.executable).resolve())
        monkeypatch.setattr(launcher, "service_module", lambda item: "satrap.display.server" if item["ProcessId"] == process.pid else None)
        changed = {**snapshot, "CreationDate": "20000101000000.000000+000"}
        with pytest.raises(RuntimeError, match="身份已变化"):
            launcher.terminate_service(changed)
        assert process.poll() is None
        launcher.terminate_service(snapshot)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
