"""健康与统计的跨服务整合场景, 全部使用本地假接口和临时存储"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


async def _get(port: int, path: str, token: str) -> tuple[int, dict[str, object]]:
    """
    通过真实 TCP 读取已鉴权 JSON 接口

    参数:
    - port: 临时监听端口
    - path: 请求路径
    - token: 当前服务测试凭据

    返回:
    - HTTP 状态码和公开 JSON 对象
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 10)
        head, body = raw.split(b"\r\n\r\n", 1)
        status = int(head.split()[1])
        return status, json.loads(body) if body.startswith(b"{") else {"error": body.decode("utf-8")}
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_service_health_is_independent_authenticated_and_stable_without_backend(tmp_path: Path, monkeypatch):
    """三服务通过真实 HTTP 保留原状态语义, Control 健康不依赖 Backend, 轮询不重复读取版本"""
    from satrap.core.backend import control_server as control
    from satrap.core.backend.BackendManager import BackendConfig, BackendManager
    from satrap.core.backend.http_api import BackendHTTPServer
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.server_auth import ServerAuth
    from satrap.core.storage import StorageLayout
    from satrap.core.telemetry.health import ServiceRuntime
    from satrap.display.plugins import ChatPluginRegistry
    from satrap.display.service import ChatService
    from satrap.display.server import ChatHTTPServer

    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    backend = BackendManager(BackendConfig(data_root=str(tmp_path / "data")))
    chat = ChatService(ModelConfigManager(storage_path=tmp_path / "models.json"),
                       ChatPluginRegistry(state_path=tmp_path / "plugins.json"),
                       storage_layout=StorageLayout(tmp_path / "data"), workspace_roots=[tmp_path])
    control_auth = ServerAuth.create("127.0.0.1", 0, token="control-test-token-at-least-32-characters")
    monkeypatch.setattr(control, "_CONTROL_AUTH", control_auth)
    monkeypatch.setattr(control, "_CONTROL_RUNTIME", ServiceRuntime("control"))
    monkeypatch.setattr(control, "_check_backend_health", lambda: {"running": False})
    monkeypatch.setattr(control, "_backend_process", None)
    servers = []
    try:
        server = await asyncio.start_server(control._handle_request, "127.0.0.1", 0)
        servers.append(server)
        endpoints = [("control", server.sockets[0].getsockname()[1], control_auth.token, "/api/health")]
        for name, handler, path in (("backend", BackendHTTPServer(backend), "/api/health"),
                                    ("chat", ChatHTTPServer(chat), "/api/chat/health")):
            handler.auth = ServerAuth.create("127.0.0.1", 0, token=name + "-test-token-at-least-32-characters")
            server = await asyncio.start_server(handler._handle_connection, "127.0.0.1", 0)
            servers.append(server)
            endpoints.append((name, server.sockets[0].getsockname()[1], handler.auth.token, path))
        for name, port, token, path in endpoints:
            assert (await _get(port, path, "invalid"))[0] == 401
            status, first = await _get(port, path, token)
            assert status == (503 if name == "backend" else 200)
            assert first["service"] == name and first["pid"] > 0
            assert first["runtime_id"] and first["python"] and first["platform"]
            assert first["healthy"] is (name != "backend")
            _, second = await _get(port, path, token)
            assert first["started_at"] == second["started_at"] and first["runtime_id"] == second["runtime_id"]
            assert second["uptime_seconds"] >= first["uptime_seconds"]
        status, body = await _get(endpoints[0][1], "/status", control_auth.token)
        assert status == 200 and body["health"] == {"running": False}
        assert body["running"] is False and body["probe"]["state"] == "offline"
        assert body["probe"]["latency_ms"] is None
        from satrap.core.telemetry.contracts import MetricScope, ModelCall
        from satrap.core.telemetry.store import TelemetryStore
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        import time

        layout = StorageLayout(tmp_path / "data")
        stamp = int(time.time()) - 60
        for name in ("control", "backend", "chat"):
            store = TelemetryStore(layout.root / "telemetry" / f"{name}.sqlite", name)
            store.begin_run(name, 1, stamp - 60)
            store.append(name, [ModelCall(name, MetricScope(model_config="named", session_id="session"), "physical", "provider", stamp, stamp + 1, 1000, "succeeded", 10, 2, None, 12)])
        monkeypatch.setattr(control, "_configured_storage_layout", lambda: layout)
        reads = []
        def fetch(service, path):
            _, port, token, _ = next(endpoint for endpoint in endpoints if endpoint[0] == service)
            reads.append(service)
            with urlopen(Request(f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {token}"}), timeout=5) as response:
                return json.load(response)
        monkeypatch.setattr(control, "_read_telemetry_source", fetch)
        query = urlencode({"from": stamp - 1, "to": stamp + 2, "tz": "UTC", "bucket": "minute"})
        assert (await _get(endpoints[1][1], "/api/stats/local?" + query, "invalid"))[0] == 401
        status, metrics = await _get(endpoints[0][1], "/api/stats/overview?" + query, control_auth.token)
        assert status == 200 and metrics["usage"]["calls"] == 3 and metrics["usage"]["total_tokens"] == 36
        assert len(reads) == 2 and len(metrics["sources"]) == 3
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


def test_service_runtime_uses_monotonic_uptime_and_frozen_build_identity(monkeypatch):
    """墙钟回拨和版本文件不可读不能改变已启动服务的身份"""
    from satrap.core.telemetry import health

    runtime = health.ServiceRuntime("backend", runtime_id="test-runtime", started_at=100, _started_monotonic=20)
    monkeypatch.setattr(health.time, "time", lambda: 5.0)
    monkeypatch.setattr(health.time, "monotonic", lambda: 25.0)
    monkeypatch.setattr(health, "_build_identity", lambda: (_ for _ in ()).throw(AssertionError("轮询不能读取版本")))
    first = runtime.snapshot(healthy=False)
    assert first["started_at"] == 100 and first["uptime_seconds"] == 5
    assert first["runtime_id"] == "test-runtime" and first["healthy"] is False


@pytest.mark.asyncio
async def test_adapter_connection_health_counts_only_confirmed_reconnections_and_caches_identity():
    """重复心跳不算重连, 初次连接不计, 未探测的往返延迟保留未知"""
    from satrap.core.platform import PlatformConfig
    from satrap.core.platform.misskey.adapter import MisskeyAdapter
    from satrap.core.platform.onebot.adapter import OneBotAdapter

    for adapter in (OneBotAdapter(PlatformConfig(id="qq", type="onebot", settings={})),
                    MisskeyAdapter(PlatformConfig(id="mk", type="misskey", settings={}))):
        first = adapter.get_stats()
        assert first["connected_at"] is None and first["reconnect_count"] == 0
        adapter.note_connection(True)
        adapter.note_connection(True)
        assert adapter.get_stats()["reconnect_count"] == 0
        adapter.note_connection(False)
        adapter.note_connection(True)
        last = adapter.get_stats()
        assert last["reconnect_count"] == 1 and last["disconnected_at"] is not None
        assert last["latency_ms"] is None and last["latency_kind"] is None


def test_metric_storage_deduplicates_calls_corrects_message_direction_and_retains_rollups(tmp_path):
    """调用去重和方向纠正与汇总同事务, 明细过期不抹去历史计数, 配置采用 CAS"""
    from dataclasses import replace
    from satrap.core.telemetry.contracts import MessageMetric, MetricScope, ModelCall, TelemetrySettings
    from satrap.core.telemetry.store import SettingsConflict, SettingsStore, TelemetryStore

    settings = SettingsStore(tmp_path / "config" / "telemetry.json")
    assert settings.read() == (TelemetrySettings(), "missing") and not settings.path.exists()
    saved = settings.save(TelemetrySettings().payload(), "missing")
    with pytest.raises(SettingsConflict):
        settings.save(TelemetrySettings().payload(), "missing")
    with pytest.raises(ValueError):
        settings.save({**TelemetrySettings().payload(), "usage_retention_days": True}, saved["revision"])
    store = TelemetryStore(tmp_path / "data" / "telemetry.sqlite", "backend")
    store.begin_run("runtime", 100, 100)
    scope = MetricScope(platform_id="qq", self_id="bot", conversation_kind="group", target_id="room", session_id="session")
    call = ModelCall("call", scope, "physical-model", "provider.test", 100, 101, 1000, "succeeded", 20, 5, 3, 25)
    unknown = replace(call, call_id="unknown", input_tokens=None, output_tokens=None, cached_tokens=None, total_tokens=None)
    message = MessageMetric("message", scope, 100, "inbound", True)
    store.append("runtime", [call, call, unknown, message, message])
    store.append("runtime", [replace(message, occurred_at=130, direction="outbound", command=False)])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 2
        assert db.execute("SELECT occurred_at,direction FROM message_events").fetchone()[:] == (100, "outbound")
        rows = [json.loads(row[0]) for row in db.execute("SELECT counters_json FROM metric_buckets WHERE granularity=60")]
        assert sum(item.get("calls", 0) for item in rows) == 2
        assert sum(item.get("usage_missing_calls", 0) for item in rows) == 1
        assert sum(item.get("inbound", 0) for item in rows) == 0
        assert sum(item.get("outbound", 0) for item in rows) == 1
        assert sum(item.get("commands", 0) for item in rows) == 0
        assert sum(item.get("total_tokens", 0) for item in rows) == 25
    result = store.cleanup(TelemetrySettings(usage_retention_days=1, aggregate_retention_days=180), now=3 * 86400)
    assert result["deleted"] >= 3
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM metric_buckets").fetchone()[0] > 0
    store.finish_run("runtime", clean=True)


def test_metric_windows_preserve_dst_boundaries_and_reject_unbounded_queries():
    """时区自然日允许 23/25 小时, 重复小时有独立 UTC 桶, 超长查询不分配大列表"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from satrap.core.telemetry.contracts import MetricWindow

    zone = ZoneInfo("America/New_York")
    for start, end, hours in (("2026-03-08", "2026-03-09", 23), ("2026-11-01", "2026-11-02", 25)):
        begin = datetime.fromisoformat(start).replace(tzinfo=zone).timestamp()
        finish = datetime.fromisoformat(end).replace(tzinfo=zone).timestamp()
        window = MetricWindow.parse({"from": str(begin), "to": str(finish), "tz": "America/New_York", "bucket": "day"})
        assert window.buckets() == [(begin, finish)]
        hourly = MetricWindow.parse({"from": str(begin), "to": str(finish), "tz": "America/New_York", "bucket": "hour"})
        assert len(hourly.buckets()) == hours
        assert len({bucket[0] for bucket in hourly.buckets()}) == hours
    for query in ({"from": "0"}, {"from": "nan", "to": "100"}, {"from": "0", "to": "1000000", "bucket": "minute"}, {"tz": "invalid-zone"}):
        with pytest.raises(ValueError):
            MetricWindow.parse(query)


def test_metric_writer_keeps_a_bounded_queue_and_records_missing_coverage(tmp_path, monkeypatch):
    """慢磁盘不会阻塞入队, 溢出有可持久化缺口, 退出排空且不持有业务库连接"""
    import threading
    from dataclasses import replace
    from satrap.core.telemetry.capture import TelemetryWriter
    from satrap.core.telemetry.contracts import MetricScope, ModelCall
    from satrap.core.telemetry.health import ServiceRuntime
    from satrap.core.telemetry.store import SettingsStore, TelemetryStore

    store = TelemetryStore(tmp_path / "data" / "backend.sqlite", "backend")
    entered, release = threading.Event(), threading.Event()
    append = store.append
    def slow_append(*args, **kwargs):
        if len(args) > 1 and args[1]:
            entered.set()
            assert release.wait(5)
        return append(*args, **kwargs)
    monkeypatch.setattr(store, "append", slow_append)
    runtime = ServiceRuntime("backend")
    writer = TelemetryWriter(store, runtime, settings_store=SettingsStore(tmp_path / "settings.json"), capacity=1)
    try:
        assert writer.ready.wait(3) and not writer.last_error
        call = ModelCall("first", MetricScope(), "m", "provider", runtime.started_at, runtime.started_at, 1, "succeeded")
        assert writer.submit(call)
        assert entered.wait(3)
        assert writer.submit(replace(call, call_id="second"))
        assert not writer.submit(replace(call, call_id="overflow"))
        assert writer.dropped_events == 1
    finally:
        release.set()
        writer.close()
    assert not writer._thread.is_alive()
    with store.connect() as db:
        run = db.execute("SELECT * FROM collection_runs").fetchone()
        assert run["closed"] == 1 and run["dropped_events"] == 1
        assert json.loads(run["gaps_json"])[0]["reason_code"] == "queue_full"
        assert db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_model_metering_records_real_usage_and_cancellation_with_debug_disabled(tmp_path, monkeypatch):
    """真实 LLM 边界返回原响应, 无 usage 不记为零, 异步取消保留已报告用量并关闭流"""
    from types import SimpleNamespace
    from satrap.core.telemetry import model
    from satrap.core.telemetry.capture import TelemetryWriter, bind_scope
    from satrap.core.telemetry.contracts import MetricScope
    from satrap.core.telemetry.health import ServiceRuntime
    from satrap.core.telemetry.store import SettingsStore, TelemetryStore

    store = TelemetryStore(tmp_path / "backend.sqlite", "backend")
    writer = TelemetryWriter(store, ServiceRuntime("backend"), settings_store=SettingsStore(tmp_path / "settings.json"))
    assert writer.ready.wait(3)
    monkeypatch.setattr(model, "current_writer", lambda: writer)
    monkeypatch.setattr(model, "current_trace", lambda: None)
    usage = SimpleNamespace(prompt_tokens=20, completion_tokens=5, total_tokens=25, prompt_tokens_details=SimpleNamespace(cached_tokens=3))
    response = SimpleNamespace(choices=[], usage=usage)
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return response
    class InterruptedStream:
        closed = False
        def __aiter__(self):
            self.first = True
            return self
        async def __anext__(self):
            if self.first:
                self.first = False
                return response
            raise asyncio.CancelledError()
        async def aclose(self):
            self.closed = True
    stream = InterruptedStream()
    async def interrupted(**kwargs):
        return stream
    try:
        with bind_scope(MetricScope(platform_id="qq", session_id="session")):
            assert model.complete_sync(complete, {"model": "physical"}, 60, provider="https://user:secret@provider.test/v1?api_key=secret", model_config="named", context_window=4096) is response
            result = await model.complete_async(interrupted, {"model": "physical", "stream": True}, 60)
            with pytest.raises(asyncio.CancelledError):
                async for item in result:
                    assert item is response
        assert stream.closed
        assert calls == [{"model": "physical"}]
        untouched = InterruptedStream()
        async def unstarted(**kwargs):
            return untouched
        result = await model.complete_async(unstarted, {"model": "unstarted", "stream": True}, 60)
        await result.aclose()
        assert untouched.closed
        class UnstartedSync:
            closed = False
            def __iter__(self):
                return self
            def __next__(self):
                raise AssertionError("关闭前不应消费流")
            def close(self):
                self.closed = True
        untouched_sync = UnstartedSync()
        result = model.complete_sync(lambda **kwargs: untouched_sync, {"model": "unstarted", "stream": True}, 60)
        result.close()
        assert untouched_sync.closed
    finally:
        writer.close()
    with store.connect() as db:
        records = db.execute("SELECT * FROM model_calls ORDER BY started_at").fetchall()
        assert len(records) == 4 and all(record["status"] == "cancelled" for record in records[1:])
        assert json.loads(records[0]["usage_json"])["cached_tokens"] == 3
        assert json.loads(records[0]["scope_json"])["model_config"] == "named"
        assert records[0]["provider"] == "provider.test"
        assert json.loads(records[1]["usage_json"])["total_tokens"] == 25
        context = db.execute("SELECT * FROM context_occupancy WHERE session_id='session'").fetchone()
        assert context["used"] == 20 and context["window"] is None


@pytest.mark.asyncio
async def test_aggregate_statistics_are_exact_at_edges_paginated_and_isolate_failed_sources(tmp_path, monkeypatch):
    """三服务聚合保持时间边界, 合并调用而非分位数, 分页绑定查询, 离线与权限拒绝分别处理"""
    from dataclasses import replace
    from urllib.parse import parse_qs, urlencode, urlsplit
    from satrap.core.storage import StorageLayout
    from satrap.core.telemetry import api
    from satrap.core.telemetry.api import SourceUnavailable, telemetry_response
    from satrap.core.telemetry.contracts import MessageMetric, MetricScope, ModelCall
    from satrap.core.telemetry.query import MetricQuery, local_metrics
    from satrap.core.telemetry.store import TelemetryStore

    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    layout = StorageLayout(tmp_path / "data")
    base = 1793030400
    stores = {name: TelemetryStore(layout.root / "telemetry" / f"{name}.sqlite", name) for name in ("control", "backend", "chat")}
    for name, store in stores.items():
        store.begin_run(name, 1, base - 2 * 86400)
        store.append(name, [])
    for name in ("backend", "chat"):
        scope = MetricScope(platform_id="qq" if name == "backend" else "chat", self_id="bot", conversation_kind="group", target_id="room",
                            model_config="named", session_id="session")
        call = ModelCall("valid", scope, "physical", "provider.test", base + 15, base + 16, 100, "succeeded", 20, 5, 3, 25)
        stores[name].append(name, [call, replace(call, call_id="outside", started_at=base + 5),
                                  MessageMetric("message", scope, base + 15, "inbound" if name == "backend" else "outbound")])
    stores["backend"].append("backend", [replace(call, call_id="second", scope=replace(scope, platform_id="qq", model_config="second"), started_at=base + 25)])
    incoming_scope = replace(scope, platform_id="qq")
    stores["backend"].append("backend", [MessageMetric(f"incoming-{i}", incoming_scope, base + 20, "inbound") for i in range(50)])
    stores["backend"].append("backend", [MessageMetric(f"previous-incoming-{i}", incoming_scope, base + 20 - 86400, "inbound") for i in range(100)])
    stores["chat"].append("chat", [MessageMetric(f"previous-output-{i}", scope, base + 20 - 86400, "outbound") for i in range(2)])
    mode, reads = "available", []
    def fetch(service, path):
        reads.append(service)
        if mode == "no_sources":
            raise SourceUnavailable("offline", "test_offline")
        if service == "backend" and mode in {"offline", "auth_required"}:
            raise SourceUnavailable(mode, "test_source_unavailable")
        if service == "backend" and mode == "invalid":
            return {"ok": True}
        params = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        return local_metrics(stores[service], MetricQuery.parse(params, params["view"]))
    query = {"from": str(base + 10), "to": str(base + 130), "tz": "UTC", "bucket": "minute"}
    path = "/api/stats/overview?" + urlencode(query)
    status, payload = await telemetry_response("GET", path, "control", layout, fetch=fetch)
    assert status == 200
    assert payload["messages"]["inbound"] == 51 and payload["messages"]["outbound"] == 1
    assert payload["usage"]["calls"] == 3 and payload["usage"]["total_tokens"] == 75
    assert payload["usage"]["cached_tokens"] == 9
    assert payload["usage"]["token_reported_calls"]["cached_tokens"] == 3
    assert payload["comparison"]["messages"] == {"current": 1, "previous": 2, "absolute_delta": -1, "percent_delta": -50}
    assert "counters" not in payload and all("counters" not in bucket for bucket in payload["series"])
    assert len(reads) == 2
    assert (await telemetry_response("GET", path, "control", layout, fetch=fetch))[1] == payload
    assert len(reads) == 2
    page_path = "/api/stats/models?" + urlencode({**query, "limit": "1"})
    _, first = await telemetry_response("GET", page_path, "control", layout, fetch=fetch)
    assert len(first["models"]) == 1 and first["models"][0]["model_config"] == "named"
    assert first["models"][0]["usage"]["calls"] == 2 and first["has_more"]
    _, second = await telemetry_response("GET", page_path + "&cursor=" + first["next_cursor"], "control", layout, fetch=fetch)
    assert second["models"][0]["model_config"] == "second" and not second["has_more"]
    assert (await telemetry_response("GET", page_path + "&platform_id=other&cursor=" + first["next_cursor"], "control", layout, fetch=fetch))[0] == 400
    for limit, state in enumerate(("auth_required", "offline", "invalid"), 2):
        mode = state
        status, result = await telemetry_response("GET", path + "&limit=" + str(limit), "control", layout, fetch=fetch)
        assert status == 200
        source = next(item for item in result["sources"] if item["service"] == "backend")
        assert source["state"] == ("stale" if state == "offline" else "error" if state == "invalid" else state)
        assert result["usage"]["calls"] == (3 if state == "offline" else 1)
        if state != "offline":
            assert result["complete"] is False
    saved = await telemetry_response("PUT", "/config/telemetry", "control", layout,
                                     payload={"settings": {"usage_enabled": False}, "expected_revision": "missing"})
    assert saved[0] == 400
    stores["control"].path.write_bytes(b"broken sqlite")
    status, partial = await telemetry_response("GET", path + "&limit=6", "control", layout, fetch=fetch)
    assert status == 200 and partial["complete"] is False
    assert next(source for source in partial["sources"] if source["service"] == "control")["state"] == "error"
    mode = "no_sources"
    for name in ("backend", "chat"):
        stores[name].path.unlink()
    assert (await telemetry_response("GET", path + "&limit=7", "control", layout, fetch=fetch))[0] == 503


def test_activity_only_storage_budget_and_context_occupancy_survive_later_writes(tmp_path):
    """仅有活动时仍执行空间预算, 后续心跳不抹去清理证据, 摘要不覆盖主对话占用"""
    from dataclasses import replace
    import time
    from satrap.core.telemetry.contracts import ActivityEvent, MetricScope, ModelCall, TelemetrySettings
    from satrap.core.telemetry.query import MetricQuery, local_metrics
    from satrap.core.telemetry.store import TelemetryStore

    store = TelemetryStore(tmp_path / "activity.sqlite", "backend")
    now = time.time()
    store.begin_run("runtime", 1, now - 120)
    scope = MetricScope(platform_id="qq", self_id="bot", session_id="session", agent="agent", model_config="named")
    template = ActivityEvent("base", now - 60, "config.applied", "info", "配置" * 120, scope)
    for start in range(0, 18000, 128):
        store.append("runtime", [replace(template, event_id=str(index), occurred_at=now - 60 + index / 1000)
                                 for index in range(start, min(18000, start + 128))])
    with store.connect() as db:
        before = (db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]) * db.execute("PRAGMA page_size").fetchone()[0]
        assert before > 16_000_000
    result = store.cleanup(TelemetrySettings(max_service_mb=16))
    assert result["deleted"] > 0
    store.append("runtime", [])
    with store.connect() as db:
        assert db.execute("SELECT reason_code FROM retention_bounds WHERE dataset='activity_events'").fetchone()[0] == "storage_budget"
        used = (db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]) * db.execute("PRAGMA page_size").fetchone()[0]
        assert used <= 16_000_000
    main = ModelCall("main", scope, "physical", "provider", now - 5, now - 4, 100, "succeeded", 20, 5, None, 25, 4096)
    summary = replace(main, call_id="summary", started_at=now - 2, input_tokens=999, scope=replace(scope, purpose="summary"))
    store.append("runtime", [main, summary])
    query = MetricQuery.parse({"from": str(now - 10), "to": str(now), "tz": "UTC", "bucket": "minute"}, "sessions")
    context = local_metrics(store, query)["sessions"][0]["context"]
    assert context["used"] == 20 and context["window"] == 4096 and context["source"] == "reported"
    from satrap.core.telemetry.query import batch_session_metrics

    summaries = batch_session_metrics(store, [("qq", "session"), ("another", "session")], {"usage", "context"})
    assert summaries[("qq", "session")]["context"]["used"] == 20
    assert summaries[("qq", "session")]["usage"]["calls"] == 2
    assert summaries[("qq", "session")]["usage"]["input_tokens"] == 1019
    assert summaries[("another", "session")]["context"] is None
    absent = TelemetryStore(tmp_path / "not-created.sqlite", "backend")
    missing = batch_session_metrics(absent, [("qq", "session")], {"usage", "context"})
    assert missing[("qq", "session")]["usage"] is None and not absent.path.exists()


@pytest.mark.asyncio
async def test_activity_aggregation_is_deduplicated_filtered_and_cursor_stable(tmp_path):
    """活动按服务稳定合并, 未知对象保持空值, 翻页不推进窗口, 被关闭的 usage 不隐藏活动覆盖"""
    from urllib.parse import parse_qs, urlencode, urlsplit
    from satrap.core.storage import StorageLayout
    from satrap.core.telemetry.api import telemetry_response
    from satrap.core.telemetry.contracts import ActivityEvent, MetricScope
    from satrap.core.telemetry.query import ActivityQuery, local_activity
    from satrap.core.telemetry.store import TelemetryStore
    import time

    layout = StorageLayout(tmp_path / "data")
    base = int(time.time()) - 60
    stores = {name: TelemetryStore(layout.root / "telemetry" / f"{name}.sqlite", name) for name in ("control", "backend", "chat")}
    for name, store in stores.items():
        store.begin_run(name, 1, base - 60)
        scope = MetricScope(platform_id="qq", self_id="bot", conversation_kind="group", target_id="room", request_id="request")
        event = ActivityEvent(name, base, "platform.disconnected", "warning", "平台连接断开", scope,
                              {"kind": "platform", "platform_id": "qq", "self_id": "bot"})
        store.append(name, [event, event], gaps=[{"from": base - 60, "to": base + 60, "reason_code": "usage_disabled"}])
    def fetch(service, path):
        assert urlsplit(path).path == "/api/activity/local"
        query = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        return local_activity(stores[service], ActivityQuery.parse(query))
    query = {"from": str(base - 1), "to": str(base + 1), "limit": "1", "tone": "warning"}
    path = "/api/activity?" + urlencode(query)
    status, first = await telemetry_response("GET", path, "control", layout, fetch=fetch)
    assert status == 200 and first["complete"] is True and first["window"]["bucket"] is None
    assert len(first["items"]) == 1 and first["has_more"]
    assert first["items"][0]["object"]["target_id"] == "room"
    identities = [first["items"][0]["service"]]
    cursor = first["next_cursor"]
    while cursor:
        status, page = await telemetry_response("GET", path + "&cursor=" + cursor, "control", layout, fetch=fetch)
        assert status == 200 and page["window"] == first["window"]
        identities.extend(item["service"] for item in page["items"])
        cursor = page["next_cursor"]
    assert len(identities) == 3 and set(identities) == {"control", "backend", "chat"}
    assert (await telemetry_response("GET", path + "&platform_id=other&cursor=" + first["next_cursor"], "control", layout, fetch=fetch))[0] == 400
    assert (await telemetry_response("GET", "/api/activity?from=1&to=99999999", "control", layout, fetch=fetch))[0] == 400
    defaults = ActivityQuery.parse({}, now=base)
    restored = ActivityQuery.parse({"cursor": defaults.cursor((-base, "id", "control"))}, now=base + 600)
    assert restored.window == defaults.window


@pytest.mark.asyncio
async def test_activity_records_committed_memory_plugin_and_coalesced_completions(tmp_path, monkeypatch):
    """Debug 关闭时仍记录已提交业务, 普通完成按对象合并计数, 事件不保存记忆正文或配置"""
    from satrap.core.telemetry import capture
    from satrap.core.telemetry.capture import TelemetryWriter, bind_scope, record_session_completed
    from satrap.core.telemetry.contracts import MetricScope
    from satrap.core.telemetry.store import SettingsStore, TelemetryStore
    from satrap.core.telemetry.health import ServiceRuntime
    from satrap.core.config.platform_messages import PlatformMessageStore, MessageScope, ArchiveMessage
    from satrap.core.memory.scoped import ScopedMemories
    from satrap.core.utils.application_progress import ApplicationOperations
    import time

    store = TelemetryStore(tmp_path / 'telemetry.sqlite', 'backend')
    writer = TelemetryWriter(store, ServiceRuntime('backend'), settings_store=SettingsStore(tmp_path / 'settings.json'))
    assert writer.ready.wait(3)
    monkeypatch.setattr(capture, 'current_writer', lambda: writer)
    archive = PlatformMessageStore(tmp_path / 'platform.db', 'qq')
    source = MessageScope('qq', '100', 'group', '456')
    archive.record(source, ArchiveMessage('source', 'member', time.time(), '资料'))
    repository = ScopedMemories(archive, source)
    proposal = repository.mutate('create', {'kind': 'group_rule', 'key': 'meeting', 'title': '会议', 'content': 'secret-memory-content', 'source_message_ids': ['source']},
                                 actor='member', current_message='source', operation_id='operation')
    repository.decide(proposal['proposal_id'], True, 0)
    scope = MetricScope(platform_id='qq', self_id='100', conversation_kind='group', target_id='456', session_id='session')
    now = int(time.time() // 60) * 60 + 1
    monkeypatch.setattr(capture.time, 'time', lambda: now)
    with bind_scope(scope):
        record_session_completed()
        record_session_completed()
    operations = ApplicationOperations()
    async def apply():
        return {'ok': True}
    operations.submit('plugin-operation-1234', {'route': '/api/plugins/apply', 'platform_id': 'qq'}, apply)
    while operations.get('plugin-operation-1234')['status'] == 'running':
        await asyncio.sleep(.005)
    writer.close()
    with store.connect() as db:
        events = db.execute('SELECT * FROM activity_events').fetchall()
        assert {'approval.pending','approval.decided','session.completed','plugin.applied'} <= {row['kind'] for row in events}
        completed = [row for row in events if row['kind'] == 'session.completed']
        assert len(completed) == 1 and completed[0]['event_count'] == 2
        assert all('secret-memory-content' not in str(tuple(row)) for row in events)
        assert all('api_key' not in str(tuple(row)) for row in events)


def test_telemetry_v1_upgrade_preserves_activity_without_initializing_readonly(tmp_path):
    """旧遥测库只读时不迁移, 写宿主升级后保留原活动且支持合并计数"""
    import sqlite3
    import time
    from satrap.core.telemetry.contracts import ActivityEvent, MetricScope
    from satrap.core.telemetry.store import TelemetryStore
    from satrap.core.telemetry.query import ActivityQuery, local_activity
    path = tmp_path / 'telemetry.sqlite'
    store = TelemetryStore(path, 'backend')
    now = time.time()
    store.begin_run('runtime', 1, now - 60)
    event = ActivityEvent('original', now - 1, 'config.applied', 'info', '配置已应用', MetricScope())
    store.append('runtime', [event])
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE activity_events DROP COLUMN event_count')
        db.execute('PRAGMA user_version=1')
    fresh = TelemetryStore(path, 'backend')
    query = ActivityQuery.parse({'from': str(now - 5), 'to': str(now)})
    assert local_activity(fresh, query)['items'][0]['count'] == 1
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    fresh.append('runtime', [event])
    with fresh.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('SELECT count(*),sum(event_count) FROM activity_events').fetchone()[:] == (1,1)
