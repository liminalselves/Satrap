"""三个独立进程的插件保存/应用与慢读取性能验收, 不连接用户平台或模型"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest


pytestmark = pytest.mark.integration


def test_plugin_application_across_actual_processes(tmp_path: Path) -> None:
    """
    验证实际跨进程生命周期, 定向/无变化应用及慢读取期间状态响应

    参数:
    - tmp_path: 所有进程共享的隔离配置与数据根目录
    """
    plugin = tmp_path / "plugins" / "application_probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("name: application_probe\nversion: 1\ncommands:\n  about: 查询当前参数\nconfig_schema:\n  about_text:\n    type: string\n    default: old\n", encoding="utf-8")
    (plugin / "commands.py").write_text("def build_commands(session, config=None):\n    async def about():\n        return config['about_text']\n    return {}, {'about': about}\n", encoding="utf-8")
    (tmp_path / "main.json").write_text(json.dumps({"edictum_config_path": str(tmp_path / "backend" / "named.json"),
        "model_config_path": str(tmp_path / "config" / "models.json"), "platforms": []}), encoding="utf-8")
    token = "isolated-application-test-" + uuid4().hex
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "SATRAP_API_TOKEN": token,
        "SATRAP_CONFIG_ROOT": str(tmp_path / "config"), "SATRAP_DATA_ROOT": str(tmp_path / "data"),
        "SATRAP_RUNTIME_ROOT": str(tmp_path / "runtime"), "SATRAP_CREDENTIALS_ROOT": str(tmp_path / "credentials"),
        "SATRAP_LOG_ROOT": str(tmp_path / "logs")}
    worker = Path(__file__).resolve().parents[1] / "fixtures" / "plugin_application_worker.py"
    processes = []
    logs = []
    report = {}

    def until(predicate, timeout=20):
        """
        等待当前进程可验证的状态, 进程异常时立即报告测试日志

        参数:
        - predicate: 当前状态查询
        - timeout: 秒数截止, 默认 20

        返回:
        - 非空状态
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for kind, process in processes:
                if process.poll() is not None:
                    raise AssertionError(f"{kind} 提前退出: " + (tmp_path / f"{kind}.log").read_text(encoding="utf-8")[-5000:])
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        raise AssertionError("等待跨进程状态超时")

    def start(kind, *ports):
        """
        启动隐藏的隔离进程并等待真实监听端口

        参数:
        - kind: control, chat 或 backend
        - ports: 控制服务所需的子服务端口

        返回:
        - 实际监听信息
        """
        ready = tmp_path / f"{kind}-ready.json"
        log = (tmp_path / f"{kind}.log").open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen([sys.executable, str(worker), kind, str(tmp_path), str(ready), *map(str, ports)],
            cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append((kind, process))
        return until(lambda: json.loads(ready.read_text(encoding="utf-8")) if ready.exists() else None)

    def request(port, path, body=None, *, method=None, status=200, authorized=True):
        """
        经真实 HTTP 接口发送测试请求

        参数:
        - port: 已确认的隔离进程端口
        - path: 接口路径
        - body: 可选请求正文
        - method: 默认由正文决定 GET/POST
        - status: 预期 HTTP 状态
        - authorized: 是否使用测试令牌

        返回:
        - 原接口 JSON 结果
        """
        wire = Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method or ("POST" if body is not None else "GET"), headers={"Authorization": "Bearer " + (token if authorized else "invalid"), "Content-Type": "application/json"})
        try:
            with urlopen(wire, timeout=8) as response:
                code, payload = response.status, json.load(response)
        except HTTPError as error:
            code, payload = error.code, json.load(error)
        assert code == status, payload
        return payload

    def state(kind) -> dict[str, Any]:
        """
        读取实际运行实例的脱敏状态

        参数:
        - kind: 子服务名

        返回:
        - 当前实例及真实生命周期计数, 首次发布前返回空字典
        """
        path = tmp_path / kind / "state.json"
        deadline = time.monotonic() + 1
        while True:
            try:
                return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.01)  # Windows 原子替换期间可能短暂占用读取句柄

    try:
        chat = start("chat")
        backend = start("backend")
        control = start("control", chat["port"], backend["port"])
        port = control["port"]
        assert len({chat["pid"], backend["pid"], control["pid"], os.getpid()}) == 4
        until(lambda: (tmp_path / "chat" / "model_waiting").exists())
        before = {kind: until(lambda kind=kind: state(kind)) for kind in ("chat", "backend")}
        assert before["chat"]["values"] == ["old"] * 2 and before["backend"]["values"] == ["old"] * 3
        assert before["chat"]["model_tasks"] == [{"done": False, "cancelled": False}]
        health = request(backend["port"], "/api/health")
        assert health["healthy"] and health["pid"] == backend["pid"]
        administrator_status = request(backend["port"], "/api/administrators/status")
        assert administrator_status["ok"] and administrator_status["pid"] == backend["pid"]
        request(port, "/config/administrator-groups/snapshot", status=401, authorized=False)

        read_samples = {}
        cold_parses = None
        for path in ("/config/plugins", "/config/plugins/application_probe/config", "/config/administrator-groups", "/config/administrator-groups/snapshot"):
            samples = []
            for _ in range(6):
                began = time.perf_counter(); request(port, path); samples.append((time.perf_counter() - began) * 1000)
                if cold_parses is None:
                    cold_parses = until(lambda: state("control").get("catalog_parses"))
            read_samples[path] = {"cold_ms": round(samples[0], 3), "warm_median_ms": round(statistics.median(samples[1:]), 3)}
        report["reads"] = read_samples
        assert cold_parses and all(count == 1 for count in cold_parses.values())
        assert state("control")["catalog_parses"] == cold_parses
        report["metadata"] = {"cold_parses": sum(cold_parses.values()), "warm_additional_parses": 0}
        administrator_snapshot = request(port, "/config/administrator-groups/snapshot")
        assert administrator_snapshot["runtime"]["status"] == "unconfirmed"
        runtime = request(port, "/config/administrator-groups/runtime")
        assert runtime["section_revision"] == administrator_snapshot["section_revision"] and runtime["runtime"]["status"] == "applied"

        (tmp_path / "slow_reads").touch()
        with ThreadPoolExecutor(max_workers=8) as pool:
            reads = [pool.submit(request, port, "/config/plugins/application_probe/config") for _ in range(6)]
            status_samples = []
            for _ in range(30):
                began = time.perf_counter(); request(port, "/status"); status_samples.append((time.perf_counter() - began) * 1000)
                time.sleep(.01)
            for read in reads:
                read.result()
        (tmp_path / "slow_reads").unlink()
        p95 = statistics.quantiles(status_samples, n=100)[94]
        report["status_during_slow_reads"] = {"samples": len(status_samples), "p95_ms": round(p95, 3), "max_ms": round(max(status_samples), 3)}
        assert p95 < 100, report

        path = "/config/plugins/application_probe/config"
        original = request(port, path)
        saved = request(port, path, {"config": {"about_text": "new"}, "expected_revision": original["revision"], "apply_runtime": False}, method="PUT")
        assert saved["saved"] and "runtime" not in saved
        assert state("chat")["values"] == ["old"] * 2 and state("backend")["values"] == ["old"] * 3
        body = {"operation_id": "process-application-test-0001", "plugin_name": "application_probe"}
        request(port, "/config/plugins/reconcile", body, status=401, authorized=False)
        started = time.perf_counter()
        first = request(port, "/config/plugins/reconcile", body, status=202)
        request(port, "/config/plugins/reconcile", body, status=202)
        request(port, "/config/plugins/reconcile", {**body, "plugin_name": "different"}, status=409)
        query = "/config/plugin-operations/" + body["operation_id"]

        def phases():
            """查询实际持锁/安装阶段"""
            snapshot = request(port, query)
            kinds = {item["phase"] for item in snapshot["instances"] if item["session_id"]}
            services = {item["platform_id"].split("/")[0] for item in snapshot["instances"] if item["session_id"]}
            return snapshot if {"waiting_lock", "install"} <= kinds and services == {"Chat", "Edictum"} else None

        waiting = until(phases)
        assert waiting["status"] == "running" and token not in json.dumps(waiting)
        assert {row["platform_id"].split("/")[0] for row in waiting["instances"] if row["session_id"]} == {"Chat", "Edictum"}
        assert state("chat")["model_tasks"] == [{"done": False, "cancelled": False}]
        assert state("backend")["same_handles"][0]
        (tmp_path / "release_locks").touch()
        until(lambda: state("chat")["model_tasks"] == [{"done": True, "cancelled": False}])
        until(lambda: state("chat")["install"] == 2 and state("backend")["install"] == 3)
        installing = request(port, query)
        assert installing["status"] == "running"
        (tmp_path / "release_install").touch()
        final = until(lambda: (current := request(port, query))["status"] != "running" and current)
        assert final["status"] == "completed" and final["result"]["ok"], final
        assert {row["target"] for row in final["result"]["runtime"]} == {"Chat", "Edictum"}
        assert sum(len(row["sessions"]) for row in final["result"]["runtime"]) == 5
        after = {kind: until(lambda kind=kind: (current := state(kind))["values"] == ["new"] * before[kind]["instances"] and current) for kind in before}
        assert after["chat"]["model_build"] == before["chat"]["model_build"]
        report["first_application"] = {"wall_ms": round((time.perf_counter() - started) * 1000, 3), "instances": 5,
            "changed": sum(after[kind]["install"] for kind in after), "timings": [{"platform_id": row["platform_id"], "session_id": row["session_id"], "timings_ms": row["timings_ms"]} for row in final["instances"] if row["session_id"]]}

        repeat = {**body, "operation_id": "process-application-test-0002"}
        request(port, "/config/plugins/reconcile", repeat, status=202)
        repeated = until(lambda: (current := request(port, "/config/plugin-operations/" + repeat["operation_id"]))["status"] != "running" and current)
        assert repeated["status"] == "completed" and repeated["result"]["ok"], repeated
        for kind in after:
            assert state(kind)["install"] == after[kind]["install"] and state(kind)["uninstall"] == after[kind]["uninstall"]
        report["unchanged_application"] = {"instances": 5, "additional_installs": 0, "additional_uninstalls": 0}

        saved = request(port, path, {"config": {"about_text": "targeted"}, "expected_revision": saved["revision"], "apply_runtime": False}, method="PUT")
        scoped = {**body, "operation_id": "process-application-test-0003", "kind": "edictum", "location_id": "agent-a"}
        request(port, "/config/plugins/reconcile", scoped, status=202)
        scoped_final = until(lambda: (current := request(port, "/config/plugin-operations/" + scoped["operation_id"]))["status"] != "running" and current)
        assert scoped_final["status"] == "completed" and scoped_final["result"]["ok"], scoped_final
        assert [row["target"] for row in scoped_final["result"]["runtime"]] == ["Edictum"]
        until(lambda: state("backend")["values"] == ["targeted", "targeted", "new"])
        assert state("chat")["values"] == ["new"] * 2
        assert state("chat")["install"] == 2 and state("backend")["install"] == 5
        assert state("control")["catalog_parses"] == cold_parses
        report["targeted_application"] = {"instances": 2, "changed": 2, "untouched_instances": 3,
            "timings": [{"platform_id": row["platform_id"], "session_id": row["session_id"], "timings_ms": row["timings_ms"]} for row in scoped_final["instances"] if row["session_id"]]}
        (tmp_path / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        for flag in ("release_locks", "release_install", "stop"):
            (tmp_path / flag).touch()
        for _kind, process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=8)
        for log in logs:
            log.close()
