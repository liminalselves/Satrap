"""控制应用任务的实际 HTTP 中继, 原保存事务和独立生命周期契约"""
from __future__ import annotations

import asyncio
from contextvars import copy_context
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import urllib.error
import urllib.request

import pytest

from satrap.core.backend import control_server
from satrap.core.backend import plugin_application as relay_module
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.plugin_service import PluginManagementService
from satrap.core.server_auth import ServerAuth
from satrap.core.utils.application_progress import ApplicationOperations, progress_external, progress_instance, progress_phase
from satrap.core.utils.async_worker import BoundedAsyncWorker
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.server import ChatHTTPServer
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager
from .test_control_dispatch import _request, _json_body
from .test_application_progress import _finished


_TOKEN = "control-runtime-test-only-token-0001"


@pytest.fixture
def operations(monkeypatch):
    """任务记录和认证均使用测试值, 不写入现有服务配置"""
    monkeypatch.setenv("SATRAP_API_TOKEN", _TOKEN)
    monkeypatch.setattr(control_server, "_CONTROL_AUTH", ServerAuth.create("127.0.0.1", 0))
    registry = ApplicationOperations()
    monkeypatch.setattr(control_server, "_PLUGIN_APPLICATIONS", registry)
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,location,targets", [(None, None, {"Chat", "Edictum"}), ("chat", "chat", {"Chat"}), ("edictum", "agent", {"Edictum"})])
async def test_actual_control_bridge_auth_progress_scope_and_one_dispatch(operations, monkeypatch, tmp_path: Path, kind, location, targets):
    """两个真实临时 HTTP 服务独立执行, 控制查询不重发, 单线程池在生命周期等待期间可处理其它工作"""
    release = asyncio.Event()
    calls = {"Chat": [], "Edictum": []}
    pool = BoundedAsyncWorker("test-plugin-relay", workers=1, capacity=2)
    monkeypatch.setattr(relay_module, "RUNTIME_REQUEST_WORKERS", pool)

    async def chat_apply(**scope):
        calls["Chat"].append(scope)
        with progress_instance("same-platform", "same-id"), progress_phase("install_ms"):
            await release.wait()
        return {"ok": True, "applied": False, "sessions": [{"ok": False, "session_id": "same-id", "error": "测试安装失败"}]}

    async def backend_apply(**scope):
        calls["Edictum"].append(scope)
        with progress_instance("same-platform", "same-id"), progress_phase("model_build_ms"):
            await release.wait()
        return [{"ok": True, "session_id": "same-id"}]

    chat = ChatHTTPServer(cast(Any, SimpleNamespace(reconcile_plugins=chat_apply)), port=0)
    backend = BackendHTTPServer(cast(Any, SimpleNamespace(reconcile_edictum_plugins_async=backend_apply)), port=0, static_dir=tmp_path)
    key = "control-bridge-operation-001"
    body = {"operation_id": key, "plugin_name": "插件", **({"kind": kind, "location_id": location} if kind else {})}
    try:
        await chat.start()
        await backend.start()
        if chat._server is None or backend._server is None:
            raise AssertionError("隔离 HTTP 服务未启动")
        monkeypatch.setenv("SATRAP_CHAT_PORT", str(chat._server.sockets[0].getsockname()[1]))
        backend_port = backend._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(control_server, "_configured_backend_address", lambda: ("127.0.0.1", backend_port))
        rejected = await _request("/config/plugins/reconcile", "POST", json.dumps(body).encode("utf-8"), authorized=False)
        assert rejected.startswith(b"HTTP/1.1 401") and not any(calls.values())
        response = await _request("/config/plugins/reconcile", "POST", json.dumps(body).encode("utf-8"))
        assert response.startswith(b"HTTP/1.1 202") and _json_body(response)["operation_id"] == key
        for _attempt in range(200):
            current = operations.get(key)
            service_phases = {item["platform_id"]: item["phase"] for item in current["instances"] if not item["session_id"]}
            if all(service_phases.get(target) == ("install" if target == "Chat" else "model_build") for target in targets):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("没有读到真实子服务阶段")
        assert await asyncio.wait_for(pool.run(lambda: "available"), 0.5) == "available"
        assert current["status"] == "running" and _TOKEN not in str(current)
        assert {item["platform_id"] for item in current["instances"] if item["session_id"]} == {f"{target}/same-platform" for target in targets}
        assert (await _request(f"/config/plugin-operations/{key}", authorized=False)).startswith(b"HTTP/1.1 401")
        assert _json_body(await _request(f"/config/plugin-operations/{key}"))["status"] == "running"
        assert (await _request("/config/plugin-operations/missing-operation-001")).startswith(b"HTTP/1.1 404")
        assert (await _request("/config/plugins/reconcile", "POST", json.dumps(body).encode("utf-8"))).startswith(b"HTTP/1.1 202")
        changed = {**body, "plugin_name": "different"}
        assert (await _request("/config/plugins/reconcile", "POST", json.dumps(changed).encode("utf-8"))).startswith(b"HTTP/1.1 409")
        assert {target for target, scopes in calls.items() if scopes} == targets
        assert all(len(calls[target]) == 1 for target in targets)
        assert calls["Chat"] == ([{"plugin_name": "插件"}] if "Chat" in targets else [])
        assert calls["Edictum"] == ([{"config_name": location if kind == "edictum" else None, "session_refs": None, "concurrency": 4, "plugin_name": "插件"}] if "Edictum" in targets else [])
        monkeypatch.setattr(control_server, "_configured_backend_address", lambda: (_ for _ in ()).throw(AssertionError("不能在查询期间重新路由")))
        release.set()
        for _attempt in range(300):
            final = operations.get(key)
            if final["status"] != "running":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("子服务完成后控制任务未结束")
        assert final["status"] == ("failed" if "Chat" in targets else "completed")
        assert {row["target"] for row in final["result"]["runtime"]} == targets
        assert all(len(calls[target]) == 1 for target in targets)
    finally:
        release.set()
        operation = operations._operations.get(key)
        if operation is not None and operation.task is not None:
            await asyncio.wait_for(asyncio.shield(operation.task), 5)
        await chat.stop()
        await backend.stop()
        pool.close()


@pytest.mark.asyncio
async def test_cold_save_flags_preserve_revision_and_do_not_dispatch(operations, monkeypatch, tmp_path: Path):
    """仅保存仍执行原校验/版本事务, 错误开关与冲突不写入或应用, 配置正文不进入任务记录"""
    plugin = tmp_path / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("name: probe\nconfig_schema:\n  note:\n    default: old", encoding="utf-8")
    catalog = PluginCatalog(tmp_path / "builtin", tmp_path / "plugins")
    service = PluginManagementService(catalog, {}, ChatPluginRegistry(tmp_path / "chat.json"), manager=PluginConfigManager(tmp_path / "config"))
    monkeypatch.setattr(control_server, "PluginCatalog", lambda: catalog)
    monkeypatch.setattr(control_server, "_plugin_management_service", lambda *args: service)
    monkeypatch.setattr(control_server, "_read_plugin_global_config", lambda name, catalog: service.get_config(name))
    async def forbidden(**scope):
        raise AssertionError("仅保存不能协调运行实例")
    monkeypatch.setattr(control_server, "_apply_plugin_runtime", forbidden)
    original = service.get_config("probe")
    payload = {"config": {"note": "不得进入任务记录"}, "expected_revision": original["revision"], "apply_runtime": False}
    for invalid in (None, 0, "false"):
        assert (await _request("/config/plugins/probe/config", "PUT", json.dumps({**payload, "apply_runtime": invalid}).encode("utf-8"))).startswith(b"HTTP/1.1 400")
    assert service.get_config("probe")["revision"] == original["revision"]
    response = await _request("/config/plugins/probe/config", "PUT", json.dumps(payload).encode("utf-8"))
    assert response.startswith(b"HTTP/1.1 200")
    assert _json_body(response)["saved"] is True and "runtime" not in _json_body(response)
    assert service.get_config("probe")["overrides"] == payload["config"]
    assert (await _request("/config/plugins/probe/config", "PUT", json.dumps(payload).encode("utf-8"))).startswith(b"HTTP/1.1 409")
    usage = service.get_usages("probe")["locations"][0]
    state = {"present": True, "enabled": False, "capabilities": {}}
    body = {"kind": "chat", "location_id": "chat", "state": state, "expected_revision": usage["revision"], "apply_runtime": False}
    assert (await _request("/config/plugins/probe/usages", "PUT", json.dumps({**body, "apply_runtime": []}).encode("utf-8"))).startswith(b"HTTP/1.1 400")
    saved = _json_body(await _request("/config/plugins/probe/usages", "PUT", json.dumps(body).encode("utf-8")))
    assert saved["saved"] is True and "runtime" not in saved
    assert saved["locations"][0]["present"] is True
    assert not operations._operations


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [("post_timeout", "applied"), ("post_refused", "next_activation"), ("post_503", "error"), ("lost_record", "error"), ("get_timeout", "applied"), ("wrong_id", "error")])
async def test_relay_ambiguity_never_replays_post_and_recovers_reads(monkeypatch, failure, expected):
    """接受响应丢失/查询暂时失败只读取原 ID, 接受前连接拒绝与丢失记录明确区分"""
    requests = []
    pool = BoundedAsyncWorker("test-relay-errors", workers=1, capacity=2)
    def request(url, method, body, factory):
        requests.append((method, url, body))
        child_id = body["operation_id"] if body else requests[0][2]["operation_id"]
        if method == "POST" and failure in {"post_timeout", "lost_record"}:
            raise relay_module.RuntimeRelayError("timeout")
        if method == "POST" and failure == "post_refused":
            raise relay_module.RuntimeRelayError("offline", refused=True)
        if method == "POST" and failure == "post_503":
            raise relay_module.RuntimeRelayError("queue full", status=503)
        if method == "GET" and failure == "lost_record":
            raise relay_module.RuntimeRelayError("lost", status=404)
        if method == "GET" and failure == "get_timeout" and len(requests) == 2:
            raise relay_module.RuntimeRelayError("temporary timeout")
        return 202 if method == "POST" else 200, {"operation_id": "wrong" if failure == "wrong_id" else child_id,
                "status": "running" if method == "POST" else "completed", "result": None if method == "POST" else {"ok": True, "edictum_sessions": [{"ok": True}]}}
    monkeypatch.setattr(relay_module, "_request", request)
    try:
        result = await relay_module.relay_plugin_application("Edictum", "http://test/api/apply", {}, "test-operation-id", urllib.request.Request, worker=pool, poll_interval=0.01)
        assert result["status"] == expected
        assert [method for method, _url, _body in requests].count("POST") == 1
        assert all(url.startswith("http://test/") for _method, url, _body in requests)
        if failure == "lost_record":
            assert result["confirmation"] == "unconfirmed"
        if failure == "post_503":
            assert result["confirmation"] == "not_accepted"
    finally:
        pool.close()


@pytest.mark.asyncio
async def test_remote_progress_is_bounded_atomic_and_ignores_obsolete_context():
    """只投影阶段白名单, 超限不丢实际结果, 终态或 ID 新代次均拒绝迟到观测"""
    registry = ApplicationOperations(max_instances=2, max_completed=1)
    captured = []
    entered, release = asyncio.Event(), asyncio.Event()
    root = {"phase": "applying", "status": "running", "phase_elapsed_ms": 1, "timings_ms": {}, "credential": "must-not-appear"}
    rows = [{**root, "phase": "install", "platform_id": "p", "session_id": str(index)} for index in range(3)]
    remote = {"stage": root, "instances": rows, "instances_limited": False, "secret": "must-not-appear"}
    async def action():
        captured.append(copy_context())
        progress_external("Edictum", remote)
        before = registry.get("remote-observe-task-001")
        with pytest.raises(ValueError, match="未知进度状态"):
            progress_external("Edictum", {"stage": {**root, "phase": "uninstall"}, "instances": [{**rows[0], "phase": "unknown"}]})
        after = registry.get("remote-observe-task-001")
        assert [(item["platform_id"], item["phase"]) for item in before["instances"]] == [(item["platform_id"], item["phase"]) for item in after["instances"]]
        entered.set()
        await release.wait()
        return {"ok": True, "runtime": [{"sessions": [{"ok": True}] * 3}]}
    key = "remote-observe-task-001"
    registry.submit(key, {}, action)
    await asyncio.wait_for(entered.wait(), 1)
    current = registry.get(key)
    assert len(current["instances"]) == 2 and current["instances_limited"] is True
    assert "must-not-appear" not in str(current)
    release.set()
    final = await _finished(registry, key)
    assert len(final["result"]["runtime"][0]["sessions"]) == 3
    captured[0].run(progress_external, "Edictum", remote)
    assert registry.get(key) == final
    async def done():
        return {"ok": True}
    registry.submit("evict-remote-task-002", {}, done)
    await _finished(registry, "evict-remote-task-002")
    entered.clear()
    release.clear()
    async def replacement():
        entered.set()
        await release.wait()
        return {"ok": True}
    registry.submit(key, {}, replacement)
    await asyncio.wait_for(entered.wait(), 1)
    captured[0].run(progress_external, "Edictum", remote)
    assert registry.get(key)["instances"] == [] and not registry.get(key)["instances_limited"]
    release.set()
    await _finished(registry, key)


def test_selected_runtime_target_does_not_read_other_service_config(monkeypatch):
    """Chat 应用不读取后端地址, 平台应用不依赖 Chat 端口有效"""
    monkeypatch.setattr(control_server, "_configured_backend_address", lambda: (_ for _ in ()).throw(AssertionError("无关后端地址不应读取")))
    monkeypatch.setenv("SATRAP_CHAT_PORT", "12345")
    assert control_server._plugin_runtime_targets("chat", "chat") == [("Chat", "http://127.0.0.1:12345/api/chat/plugins/reconcile")]
    monkeypatch.setattr(control_server, "_configured_backend_address", lambda: ("127.0.0.1", 12346))
    monkeypatch.setenv("SATRAP_CHAT_PORT", "invalid")
    assert control_server._plugin_runtime_targets("edictum", "agent") == [("Edictum", "http://127.0.0.1:12346/api/edictum/plugins/reconcile")]


@pytest.mark.parametrize("kind", ["refused", "http", "malformed", "oversized"])
def test_internal_request_classifies_failure_and_logs_stack(monkeypatch, kind):
    """内部鉴权和短截止保留, 拒绝/无效/过大响应记录堆栈而不打印请求凭据"""
    logs = []
    monkeypatch.setattr(relay_module.logger, "warning", logs.append)
    monkeypatch.setattr(relay_module.logger, "error", logs.append)
    def open_request(request, *, timeout):
        assert request.get_header("Authorization") == "Bearer " + _TOKEN
        assert timeout == 3 and json.loads(request.data.decode("utf-8"))["plugin_name"] == "插件"
        if kind == "refused":
            raise urllib.error.URLError(ConnectionRefusedError("offline"))
        if kind == "http":
            raise urllib.error.HTTPError("http://test", 503, "queue full", cast(Any, {}), None)
        raw = b"[]" if kind == "malformed" else b"x" * (relay_module._MAX_RESPONSE_BYTES + 1)
        return nullcontext(SimpleNamespace(status=200, read=lambda limit: raw))
    monkeypatch.setattr(relay_module.urllib.request, "urlopen", open_request)
    def factory(url, method):
        return urllib.request.Request(url, method=method, headers={"Authorization": "Bearer " + _TOKEN})
    with pytest.raises(relay_module.RuntimeRelayError) as captured:
        relay_module._request("http://test", "POST", {"plugin_name": "插件"}, factory)
    assert captured.value.refused is (kind == "refused")
    assert captured.value.status == (503 if kind == "http" else None)
    assert logs and all("Traceback" in line and _TOKEN not in line for line in logs)
