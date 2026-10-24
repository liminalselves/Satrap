"""control_server._handle_request 路由分派特征测试 (B3b 拆解语义固定)

按连续源码区段拆解前固定路由匹配, 响应状态与副作用顺序语义:
- 未知路径与精确路径错误方法落到最终 404 {"error": "not found"}
- 前缀区段 (models/session-instances/edictum/session-classes) 在段内产生
  404 {"error": "not found: {method} {path}"}, 不落后续区段
- 精确集合与前缀详情边界 (bulk-delete 单项删除, enable/disable 普通详情)
- query 沿 raw_path 解析; 生命周期 start/stop/restart 与 /shutdown 副作用保持
"""
from __future__ import annotations

import threading
import asyncio
import sqlite3
from pathlib import Path
import pytest
from types import SimpleNamespace
import json
import time

from satrap.edictum.plugin_config import PluginConfigManager
from satrap.core.backend import control_server
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.type import SessionConfig, EmbeddingConfig
from satrap.edictum.plugin_archive import PluginArchiveInstaller
from satrap.edictum.plugin_catalog import PluginCatalog








@pytest.mark.asyncio
@pytest.mark.parametrize('model_type', ['llm', 'embedding', 'rerank', 'asr'])
async def test_model_get_keeps_config_version_and_file_unchanged(monkeypatch, tmp_path, model_type):
    """连续页面读取不能改写模型配置或触发版本重试"""
    models = tmp_path / 'models.json'
    ModelConfigManager(storage_path=models)
    main = tmp_path / 'main.json'
    main.write_text(json.dumps({'model_config_path': str(models)}), encoding='utf-8')
    monkeypatch.setattr(control_server, 'CONFIG_PATH', main)
    monkeypatch.setattr(control_server, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(control_server, '_CONFIG_VERSION_SERVICES', {})
    initial = (models.read_bytes(), models.stat().st_mtime_ns)
    for _ in range(3):
        before = _json_body(await _request('/config/versions?resources=models'))
        response = await _request('/config/models?type=' + model_type)
        after = _json_body(await _request('/config/versions?resources=models'))
        assert response.startswith(b'HTTP/1.1 200')
        assert 'default' in _json_body(response)
        assert before == after
        assert (models.read_bytes(), models.stat().st_mtime_ns) == initial


@pytest.mark.asyncio
async def test_model_versions_detect_external_edits_and_parallel_reads_are_stable(monkeypatch, tmp_path):
    """外部配置修改仍改变版本, 四种模型并发读取不再改写文件"""
    models = tmp_path / 'models.json'
    ModelConfigManager(storage_path=models)
    main = tmp_path / 'main.json'
    main.write_text(json.dumps({'model_config_path': str(models)}), encoding='utf-8')
    monkeypatch.setattr(control_server, 'CONFIG_PATH', main)
    monkeypatch.setattr(control_server, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(control_server, '_CONFIG_VERSION_SERVICES', {})
    before = _json_body(await _request('/config/versions?resources=models'))
    document = json.loads(models.read_text(encoding='utf-8'))
    document['llm']['default']['model'] = 'externally-updated-model'
    models.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    changed = _json_body(await _request('/config/versions?resources=models'))
    assert changed != before
    initial = (models.read_bytes(), models.stat().st_mtime_ns)
    responses = await asyncio.gather(*(
        _request('/config/models?type=' + model_type)
        for model_type in ('llm', 'embedding', 'rerank', 'asr')
    ))
    assert all(response.startswith(b'HTTP/1.1 200') for response in responses)
    assert _json_body(responses[0])['default']['model'] == 'externally-updated-model'
    assert _json_body(await _request('/config/versions?resources=models')) == changed
    assert (models.read_bytes(), models.stat().st_mtime_ns) == initial


@pytest.mark.asyncio
async def test_config_versions_auth_subset_and_parameter_validation(monkeypatch, tmp_path):
    main = tmp_path / 'main.yaml'
    main.write_text('api_key: secret-private\n', encoding='utf-8')
    monkeypatch.setattr(control_server, 'CONFIG_PATH', main)
    monkeypatch.setattr(control_server, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setenv('SATRAP_CONFIG_ROOT', str(tmp_path / 'config'))
    monkeypatch.setattr(control_server, '_CONFIG_VERSION_SERVICES', {})
    unauthorized = await _request('/config/versions', authorized=False)
    assert unauthorized.startswith(b'HTTP/1.1 401')
    response = await _request('/config/versions?resources=main_config')
    payload = _json_body(response)
    assert response.startswith(b'HTTP/1.1 200')
    assert set(payload['resources']) == {'main_config'}
    assert payload['resources']['main_config']['state'] == 'available'
    assert b'secret-private' not in response
    assert str(tmp_path) not in response.decode()
    for query in ('resources=unknown', 'force=yes', 'unrecognized=true'):
        assert (await _request('/config/versions?' + query)).startswith(b'HTTP/1.1 400')


@pytest.mark.asyncio
async def test_config_version_probe_runs_off_event_loop_and_contains_failures(monkeypatch):
    loop_thread = threading.get_ident()
    calls = []
    def probe(resources, force):
        calls.append((resources, force, threading.get_ident()))
        if resources == ('fail',):
            raise OSError('private-path')
        if resources == ('changed',):
            raise RuntimeError('changed')
        return {'ok': True, 'resources': {}}
    monkeypatch.setattr(control_server, '_config_versions', probe)
    response = await _request('/config/versions?resources=models,session_classes&force=true')
    assert response.startswith(b'HTTP/1.1 200')
    assert calls[0][:2] == (('models', 'session_classes'), True)
    assert calls[0][2] != loop_thread
    failed = await _request('/config/versions?resources=fail')
    assert failed.startswith(b'HTTP/1.1 500')
    assert b'private-path' not in failed
    changed = await _request('/config/versions?resources=changed')
    assert changed.startswith(b'HTTP/1.1 409')
    assert _json_body(changed)['code'] == 'config_version_changed'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/config/plugins', '/config/plugins/friend_manager/usages'])
async def test_slow_plugin_service_preparation_does_not_block_loop(monkeypatch, path):
    entered = threading.Event()
    release = threading.Event()

    def prepare(*args):
        entered.set()
        if not release.wait(2):
            raise RuntimeError('测试没有释放构造任务')
        return SimpleNamespace(list_plugins=lambda: [], get_config=lambda name: {'config': {}}, get_usages=lambda name: {'usages': []})

    monkeypatch.setattr(control_server, '_plugin_management_service', prepare)
    request = asyncio.create_task(_request(path))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        started = time.perf_counter()
        await asyncio.sleep(.01)
        assert time.perf_counter() - started < .1
        assert not request.done()
    finally:
        release.set()
    response = await request
    assert response.startswith(b'HTTP/1.1 200')


@pytest.mark.asyncio
async def test_single_plugin_read_does_not_construct_unrelated_services(monkeypatch, tmp_path):
    def forbidden(*args):
        raise AssertionError('单插件读取不应构造命名配置或模型服务')

    monkeypatch.setattr(control_server, '_plugin_management_service', forbidden)
    monkeypatch.setattr(control_server, '_edictum_config_service', forbidden)
    monkeypatch.setattr(control_server, '_model_config_service', forbidden)
    monkeypatch.setattr(control_server, 'PluginConfigManager', lambda: PluginConfigManager(tmp_path / 'plugin-config'))
    response = await _request('/config/plugins/friend_manager/config')
    assert response.startswith(b'HTTP/1.1 200')
    body = _json_body(response)
    assert body['revision'] == 'missing'
    assert body['schema']




@pytest.mark.asyncio
async def test_control_rag_and_session_overrides_share_scope_and_revision(tmp_path, monkeypatch):
    """冷管理接口共用真实数据库, 拒绝跨会话引用与过期覆盖请求"""
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugin-config"))
    instances = control_server._session_instance_config_service("local")
    for name in ("one", "two"):
        instances.store.upsert(SessionConfig(session_id=name, session_type_name="example"))
    models = control_server._model_config_service().manager
    models.set_embedding_config(EmbeddingConfig(model="embed", api_key="secret-test-key", dimensions=3), "embed")
    async def action(path, method="GET", payload=None):
        return await _request(path, method, json.dumps(payload).encode("utf-8") if payload is not None else b"")
    created = _json_body(await action("/config/rag?platform_id=local&session_id=one", "POST", {"action": "create", "name": "当前库", "scope": "session", "config": {"embed": "embed"}}))
    kb_id = created["knowledge_base"]["id"]
    assert not instances.storage_layout.session_root("local", "one").exists()
    assert _json_body(await action("/config/rag?platform_id=local&session_id=two"))["knowledge_bases"] == []
    url = "/config/session-plugin-config?platform_id=local&session_id=one&plugin=rag"
    record = _json_body(await action(url))
    assert record["revision"] == 0
    assert "secret-test-key" not in json.dumps(record)
    saved = _json_body(await action(url, "PUT", {"overrides": {"session_db_ids": [kb_id], "top_k": 3}, "expected_revision": 0}))
    assert saved["revision"] == 1 and saved["sources"]["top_k"] == "session"
    assert b"409" in await action(url, "PUT", {"overrides": {}, "expected_revision": 0})
    other = url.replace("session_id=one", "session_id=two")
    assert b"400" in await action(other, "PUT", {"overrides": {"session_db_ids": [kb_id]}, "expected_revision": 0})
    assert _json_body(await action(other))["revision"] == 0
    reset = _json_body(await action(url, "PUT", {"overrides": {}, "expected_revision": 1}))
    assert reset["config"]["top_k"] == 5 and reset["overrides"] == {}
    assert b"400" in await action(url.replace("session_id=one", "session_id=missing"))


@pytest.mark.asyncio
async def test_control_friend_instance_tool_overrides_share_atomic_revisions(tmp_path, monkeypatch):
    """好友实例工具覆盖可通过控制接口修改, 冲突时不写入部分参数"""
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugin-config"))
    instances = control_server._session_instance_config_service("local")
    instances.store.upsert(SessionConfig(session_id="one", session_type_name="example"))
    url = "/config/session-plugin-config?platform_id=local&session_id=one&plugin=friend_manager"
    async def action(payload=None):
        return await _request(url, "GET" if payload is None else "PUT", json.dumps(payload).encode("utf-8") if payload is not None else b"")
    initial = _json_body(await action())
    assert initial["tool_revision"] == 0 and initial["inherited_tools"]["friend_manager_handle_request"]
    changed = {"overrides": {"managers": "123"}, "expected_revision": initial["revision"],
               "tool_overrides": {"friend_manager_handle_request": False}, "expected_tool_revision": 0}
    saved = _json_body(await action(changed))
    assert saved["tool_revision"] == 1 and not saved["tool_overrides"]["friend_manager_handle_request"]
    conflict = await action({**changed, "overrides": {"managers": "456"}, "expected_revision": saved["revision"]})
    assert b"409" in conflict.split(b"\r\n", 1)[0]
    assert _json_body(await action())["overrides"] == {"managers": "123"}
    reset = _json_body(await action({"overrides": {}, "expected_revision": saved["revision"], "tool_overrides": {}, "expected_tool_revision": 1}))
    assert reset["tool_overrides"] == {} and reset["overrides"] == {}


@pytest.mark.asyncio
async def test_conversation_user_directory_dynamic_platforms_cold_reads_and_conflicts(tmp_path, monkeypatch):
    from satrap.core.framework.UserManager import UserInfoStore
    from satrap.core.type import UserInfo

    _use_config(monkeypatch, tmp_path)
    document = json.loads(control_server.CONFIG_PATH.read_text(encoding="utf-8"))
    document["platforms"] = [{"id": "new-instance", "type": "future"}]
    control_server.CONFIG_PATH.write_text(json.dumps(document), encoding="utf-8")
    layout = control_server._configured_storage_layout()
    layout.ensure_platform("removed-instance", platform_type="former")
    for platform in ("new-instance", "removed-instance"):
        UserInfoStore(layout.platform_db(platform)).upsert(UserInfo(user_id="same", user_nickname=platform))
    url = "/config/conversations/users"
    assert b"401" in (await _request(url, authorized=False)).split(b"\r\n", 1)[0]
    before = layout.platform_db("removed-instance").read_bytes()
    all_users = _json_body(await _request(url))
    assert {(item["platform_id"], item["user_id"]) for item in all_users["items"]} == {("new-instance", "same"), ("removed-instance", "same")}
    assert layout.platform_db("removed-instance").read_bytes() == before
    filtered = _json_body(await _request(url + "?platform_type=former&q=removed&offset=0&limit=1"))
    assert filtered["total"] == 1 and filtered["items"][0]["platform_id"] == "removed-instance"
    assert _json_body(await _request(url + "?offset=1&limit=1"))["items"][0] == all_users["items"][1]
    detail = _json_body(await _request(url + "?platform_id=new-instance&user_id=same"))["user"]
    payload = {"action": "update", "platform_id": "new-instance", "user_id": "same", "nickname": "修改", "expected_revision": detail["revision"]}
    saved = await _request(url, "POST", json.dumps(payload).encode())
    assert _json_body(saved)["user"]["user_nickname"] == "修改"
    assert b"409" in (await _request(url, "POST", json.dumps(payload).encode())).split(b"\r\n", 1)[0]
    assert b"400" in (await _request(url, "POST", json.dumps({**payload, "platform_id": "../../unknown"}).encode())).split(b"\r\n", 1)[0]
    other = _json_body(await _request(url + "?platform_id=removed-instance&user_id=same"))["user"]
    assert other["user_nickname"] == "removed-instance"
    created = _json_body(await _request(url, "POST", json.dumps({"action": "create", "platform_id": "new-instance", "user_id": "created", "nickname": "", "expected_revision": all_users["new_revision"]}).encode()))
    assert created["user"]["has_profile"]
    manifest = layout.platform_root("new-instance") / "platform.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["platform_type"] == "future"
    monkeypatch.setattr(control_server.UserDirectoryService, "records", lambda self: (_ for _ in ()).throw(OSError("读失败")) if self.platform["id"] == "removed-instance" else [])
    partial = _json_body(await _request(url))
    assert partial["warnings"] == ["removed-instance: 用户资料读取失败"]
    assert b"500" in (await _request(url + "?platform_id=removed-instance")).split(b"\r\n", 1)[0]
    assert b"200" in (await _request("/status")).split(b"\r\n", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/config/unknown"),
        ("POST", "/config/unknown"),
        ("GET", "/statusx"),
        ("GET", "/shutdownx"),
    ],
)
async def test_control_unknown_paths_fall_through_to_final_404(
    method: str,
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未被任何区段认领的路径落到最终 404 {"error": "not found"}"""
    _use_config(monkeypatch, tmp_path)
    response = await _request(path, method)
    assert b"404 Error" in response
    assert _json_body(response) == {"error": "not found"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/status"),
        ("POST", "/ui-config.json"),
        ("DELETE", "/config"),
        ("PUT", "/config/platforms"),
        ("DELETE", "/config/edictum/sessions"),
        ("GET", "/start"),
        ("GET", "/shutdown"),
    ],
)
async def test_control_wrong_method_on_exact_paths_falls_through(
    method: str,
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """精确路径方法错误时不属于任何区段, 落最终 404 (GET /shutdown 不得触发停机)"""
    _use_config(monkeypatch, tmp_path)
    response = await _request(path, method)
    assert b"404 Error" in response
    assert _json_body(response) == {"error": "not found"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/config/models/llm/foo"),
        ("POST", "/config/models/llm"),
        ("GET", "/config/session-instances/abc"),
        ("GET", "/config/edictum/sessions/foo/enable"),
        ("POST", "/config/edictum/sessions/"),
        ("GET", "/config/session-classes/foo/enable"),
        ("POST", "/config/session-classes/foo"),
    ],
)
async def test_control_prefix_segments_return_internal_404(
    method: str,
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前缀区段路径匹配但方法/形态错误时返回段内 404, 不落后续区段"""
    _use_config(monkeypatch, tmp_path)
    response = await _request(path, method)
    assert b"404 Error" in response
    assert _json_body(response) == {"error": f"not found: {method} {path}"}


@pytest.mark.asyncio
async def test_control_exact_vs_prefix_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """精确集合与前缀详情边界: modelsx 落最终 404, bulk-delete 仅精确 POST 命中"""
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        control_server,
        "_check_backend_health",
        lambda: {"running": False},
    )

    modelsx = await _request("/config/modelsx")
    assert b"404 Error" in modelsx
    assert _json_body(modelsx) == {"error": "not found"}

    empty_detail = await _request("/config/session-classes/")
    assert b"400 Error" in empty_detail
    assert _json_body(empty_detail) == {"error": "会话类配置名称不能为空"}

    bulk_get = await _request("/config/session-instances/bulk-delete")
    assert _json_body(bulk_get) == {
        "error": "not found: GET /config/session-instances/bulk-delete"
    }

    bulk_bad_mode = await _request(
        "/config/session-instances/bulk-delete",
        "POST",
        b'{"mode":"weird"}',
    )
    assert b"400 Error" in bulk_bad_mode
    assert _json_body(bulk_bad_mode) == {"error": "未知批量删除模式: weird"}


@pytest.mark.asyncio
async def test_control_query_bearing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query 沿 raw_path 解析: status 剥离 query, models/history/discovery/单项删除读取 query"""
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        control_server,
        "_check_backend_health",
        lambda: {"running": False},
    )

    status = await _request("/status?verbose=1")
    assert b"200 OK" in status
    assert _json_body(status) == {
        "running": False,
        "managed": False,
        "health": {"running": False},
    }

    models = await _request("/config/models?type=llm")
    assert b"200 OK" in models

    history = await _request("/chat/history?search=nothing&page=1")
    assert b"200 OK" in history

    discovery = await _request("/config/session/discovery?path=%2Fnot%2Fconfigured")
    assert b"400 Error" in discovery
    assert _json_body(discovery) == {"error": "只能扫描配置中的 Session 目录"}

    deleted = await _request("/config/session-instances/abc?platform_id=ghost", "DELETE")
    assert b"400 Error" in deleted
    assert _json_body(deleted) == {"error": "未知平台实例: ghost"}


class _FakeProcess:
    """subprocess.Popen 替身: 记录启动命令并提供存活进程外观"""

    def __init__(self, cmd: object, *args: object, **kwargs: object) -> None:
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 43210

    def poll(self) -> int | None:
        """模拟存活进程 (poll 返回 None)"""
        return None


async def _no_sleep(delay: float) -> None:
    """即时 sleep 替身: 跳过生命周期分支的等待"""

@pytest.mark.asyncio
async def test_control_lifecycle_start_stop_restart_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生命周期区段: /start 拉起进程并写运行时记录, /stop 清理, /restart 清理后重拉"""
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(control_server, "_backend_process", None)
    monkeypatch.setattr(control_server, "_read_backend_runtime", lambda: None)
    monkeypatch.setattr(control_server.asyncio, "sleep", _no_sleep)

    health_calls = {"count": 0}

    def _fake_health(*args: object, **kwargs: object) -> dict[str, object]:
        health_calls["count"] += 1
        return {"running": health_calls["count"] > 1}

    monkeypatch.setattr(control_server, "_check_backend_health", _fake_health)

    popen_calls: list[_FakeProcess] = []

    def _fake_popen(cmd: object, *args: object, **kwargs: object) -> _FakeProcess:
        proc = _FakeProcess(cmd, *args, **kwargs)
        popen_calls.append(proc)
        return proc

    monkeypatch.setattr(control_server.subprocess, "Popen", _fake_popen)

    written: list[control_server.BackendRuntimeRecord] = []

    def _record_runtime(record: control_server.BackendRuntimeRecord) -> None:
        written.append(record)

    monkeypatch.setattr(control_server, "_write_backend_runtime", _record_runtime)

    cleanup_calls: list[str] = []

    def _fake_cleanup() -> None:
        cleanup_calls.append("cleanup")

    monkeypatch.setattr(control_server, "_cleanup_backend", _fake_cleanup)

    started = await _request("/start", "POST")
    assert _json_body(started) == {"ok": True, "message": "后端已启动"}
    assert len(popen_calls) == 1
    assert control_server._backend_process is popen_calls[0]
    assert [record.pid for record in written] == [43210]

    stopped = await _request("/stop", "POST")
    assert _json_body(stopped) == {"ok": True, "message": "后端已停止"}
    assert cleanup_calls == ["cleanup"]

    restarted = await _request("/restart", "POST")
    assert _json_body(restarted) == {"ok": True, "message": "后端重启中"}
    assert cleanup_calls == ["cleanup", "cleanup"]
    assert len(popen_calls) == 2
    assert [record.pid for record in written] == [43210, 43210]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/start", "/restart"])
async def test_control_bad_config_does_not_spawn_or_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str):
    """配置预检失败返回具体路径, 重启前也不停止尚在运行的实例"""
    path = tmp_path / "invalid.yaml"
    path.write_text("platforms: wrong\n", encoding="utf-8")
    monkeypatch.setattr(control_server, "CONFIG_PATH", path)
    monkeypatch.setattr(control_server.subprocess, "Popen", lambda *a, **k: pytest.fail("坏配置不得启动"))
    monkeypatch.setattr(control_server, "_cleanup_backend", lambda: pytest.fail("坏配置不得停止现有后端"))
    response = await _request(endpoint, "POST")
    body = _json_body(response)
    assert response.startswith(b"HTTP/1.1 400")
    assert body["code"] == "config_invalid" and body["config_path"] == str(path.resolve())
    assert "platforms" in body["error"]


@pytest.mark.asyncio
async def test_concurrent_start_spawns_single_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个并发 /start 只拉起一个后端, 后到的请求看到已有进程并返回正在启动"""
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(control_server, "_backend_process", None)
    monkeypatch.setattr(control_server, "_read_backend_runtime", lambda: None)
    def _skip_runtime(record: control_server.BackendRuntimeRecord) -> None:
        """不写运行时记录"""

    monkeypatch.setattr(control_server, "_write_backend_runtime", _skip_runtime)

    def slow_health(*args: object, **kwargs: object) -> dict[str, object]:
        """模拟耗时的健康检查, 让两个请求都在检查阶段让出"""
        time.sleep(0.05)
        return {"running": False}

    monkeypatch.setattr(control_server, "_check_backend_health", slow_health)
    popen_calls: list[_FakeProcess] = []

    def _fake_popen(cmd: object, *args: object, **kwargs: object) -> _FakeProcess:
        proc = _FakeProcess(cmd, *args, **kwargs)
        popen_calls.append(proc)
        return proc

    monkeypatch.setattr(control_server.subprocess, "Popen", _fake_popen)
    real_sleep = asyncio.sleep

    async def short_sleep(delay: float) -> None:
        """把就绪轮询压缩到毫秒级, 仍保留让出点"""
        await real_sleep(0.001)

    monkeypatch.setattr(control_server.asyncio, "sleep", short_sleep)

    first, second = await asyncio.gather(_request("/start", "POST"), _request("/start", "POST"))

    assert len(popen_calls) == 1
    messages = sorted(_json_body(response)["message"] for response in (first, second))
    assert messages == ["后端启动中，请稍候...", "后端正在启动中"]


@pytest.mark.asyncio
async def test_blocking_health_check_runs_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /status 的阻塞健康检查在工作线程执行, 期间事件循环继续推进"""
    threads: list[threading.Thread] = []
    ticks = {"count": 0}

    def slow_health() -> dict[str, object]:
        """记录调用线程并模拟 200 毫秒的阻塞 IO"""
        threads.append(threading.current_thread())
        time.sleep(0.2)
        return {"running": False}

    monkeypatch.setattr(control_server, "_check_backend_health", slow_health)

    async def heartbeat() -> None:
        while True:
            ticks["count"] += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    try:
        response = await _request("/status")
    finally:
        beat.cancel()

    assert _json_body(response)["running"] is False
    assert threads and threads[0] is not threading.main_thread()
    assert ticks["count"] >= 5


@pytest.mark.asyncio
async def test_stop_cleans_backend_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /stop 的后端清理在工作线程执行, 期间事件循环继续推进"""
    threads: list[threading.Thread] = []
    ticks = {"count": 0}

    def slow_cleanup() -> None:
        """记录调用线程并模拟 200 毫秒的进程收尾"""
        threads.append(threading.current_thread())
        time.sleep(0.2)

    monkeypatch.setattr(control_server, "_cleanup_backend", slow_cleanup)
    monkeypatch.setattr(control_server, "_backend_process", None)

    async def heartbeat() -> None:
        while True:
            ticks["count"] += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    try:
        response = await _request("/stop", "POST")
    finally:
        beat.cancel()

    assert _json_body(response) == {"ok": True, "message": "后端已停止"}
    assert threads and threads[0] is not threading.main_thread()
    assert ticks["count"] >= 5


class _FakeOsModule:
    """os 模块替身: 记录 _exit 调用以避免测试进程退出"""

    def __init__(self) -> None:
        self.exit_codes: list[int] = []

    def _exit(self, code: int) -> None:
        """记录退出码而不终止进程"""
        self.exit_codes.append(code)


@pytest.mark.asyncio
async def test_control_shutdown_writes_response_and_schedules_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/shutdown 自写 200 响应, 关闭连接并延迟调度 os._exit(0)"""
    _use_config(monkeypatch, tmp_path)
    fake_os = _FakeOsModule()
    monkeypatch.setattr(control_server, "os", fake_os)

    response = await _request("/shutdown", "POST")
    assert b"200 OK" in response
    assert _json_body(response) == {"ok": True, "message": "控制服务即将停止"}

    await asyncio.sleep(1.0)
    assert fake_os.exit_codes == [0]


@pytest.mark.asyncio
async def test_plugin_zip_preview_install_and_json_limit(tmp_path, monkeypatch):
    """ZIP 上传独立限额, 两阶段安装保持现有 JSON 限制"""
    import io
    import zipfile

    installer = PluginArchiveInstaller(PluginCatalog(tmp_path / "builtin", tmp_path / "plugins"))
    monkeypatch.setattr(control_server, "PLUGIN_INSTALLER", installer)
    unauthenticated = await _request("/config/plugins/preview", "POST", b"corrupt", authorized=False)
    assert b"401" in unauthenticated.split(b"\r\n", 1)[0]
    assert not (tmp_path / ".plugin-install").exists()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("meta.yaml", "name: uploaded\nversion: '1'")
        archive.writestr("payload.txt", "x" * (1024 * 1024 + 100))
    response = await _request("/config/plugins/preview", "POST", buffer.getvalue())
    assert b"200 OK" in response
    preview = _json_body(response)
    assert preview["plugin"]["name"] == "uploaded"
    assert not installer.catalog.user_dir.exists()
    response = await _request("/config/plugins/install", "POST", json.dumps({"token": preview["token"]}).encode("utf-8"))
    assert b"200 OK" in response
    assert (installer.catalog.user_dir / "uploaded" / "payload.txt").stat().st_size > 1024 * 1024
    response = await _request("/config/plugins/install", "POST", b"x" * (1024 * 1024 + 1))
    assert b"413" in response.split(b"\r\n", 1)[0]
    response = await _request("/config/plugins/preview", "POST", b"corrupt")
    assert b"400" in response.split(b"\r\n", 1)[0]
    assert not list((tmp_path / ".plugin-install").iterdir())


@pytest.mark.asyncio
async def test_plugin_config_saved_independently_of_runtime_failure(tmp_path, monkeypatch):
    """运行应用失败不撤销已保存配置, 冲突请求不会再次应用"""
    from satrap.core.config.plugin_service import PluginManagementService
    from satrap.display.plugins import ChatPluginRegistry

    plugin = tmp_path / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("name: probe\nconfig_schema:\n  note:\n    default: old", encoding="utf-8")
    catalog = PluginCatalog(tmp_path / "builtin", tmp_path / "plugins")
    service = PluginManagementService(catalog, {}, ChatPluginRegistry(tmp_path / "chat.json"), manager=PluginConfigManager(tmp_path / "config"))
    monkeypatch.setattr(control_server, 'PluginCatalog', lambda: catalog)
    monkeypatch.setattr(control_server, "_plugin_management_service", lambda *args: service)
    monkeypatch.setattr(control_server, '_read_plugin_global_config', lambda name, catalog: service.get_config(name))
    applied = []
    async def apply_runtime(**scope):
        applied.append(scope)
        if scope.get("kind") == "chat":
            return [{"target": "Chat", "status": "error", "error": "测试失败", "sessions": []}]
        return [{"target": "Chat", "status": "applied", "sessions": [{"ok": True}]}, {"target": "Edictum", "status": "error", "error": "测试失败", "sessions": []}]
    monkeypatch.setattr(control_server, "_apply_plugin_runtime", apply_runtime)
    first = _json_body(await _request("/config/plugins/probe/config"))
    body = json.dumps({"config": {"note": "new"}, "expected_revision": first["revision"]}).encode("utf-8")
    response = await _request("/config/plugins/probe/config", "PUT", body)
    saved = _json_body(response)
    assert saved["saved"] is True
    assert saved["runtime"][1]["status"] == "error"
    assert service.get_config("probe")["config"]["note"] == "new"
    assert b"409" in (await _request("/config/plugins/probe/config", "PUT", body)).split(b"\r\n", 1)[0]
    assert len(applied) == 1
    assert applied[0] == {"plugin_name": "probe"}
    retry = _json_body(await _request("/config/plugins/reconcile", "POST", b"{}"))
    assert retry["runtime"][1]["status"] == "error"
    assert len(applied) == 2
    assert applied[1] == {}
    usage = _json_body(await _request("/config/plugins/probe/usages"))["locations"][0]
    response = await _request("/config/plugins/probe/usages", "PUT", json.dumps({"kind": "chat", "location_id": "chat", "state": {"present": True, "enabled": False, "capabilities": {}}, "expected_revision": usage["revision"]}).encode("utf-8"))
    assert b"200" in response.split(b"\r\n", 1)[0]
    assert _json_body(response)["locations"][0]["present"]
    assert _json_body(response)["runtime"][0]["status"] == "error"
    assert applied[2] == {"plugin_name": "probe", "kind": "chat", "location_id": "chat"}
    for invalid_scope in ({"location_id": "agent"}, {"kind": "chat", "location_id": "agent"}, {"plugin_name": []}):
        rejected = await _request("/config/plugins/reconcile", "POST", json.dumps(invalid_scope).encode("utf-8"))
        assert b"400" in rejected.split(b"\r\n", 1)[0]
    assert len(applied) == 3
    def snapshot(target, url, name):
        assert name == "probe"
        return {"target": target, "status": "stopped", "instances": []}
    monkeypatch.setattr(control_server, "_plugin_snapshot_request", snapshot)
    runtime = _json_body(await _request("/config/plugins/probe/runtime"))
    assert len(runtime["services"]) == 2
    assert all(item["status"] == "stopped" for item in runtime["services"])


@pytest.mark.parametrize("reason,status", [(ConnectionRefusedError(), "next_activation"), (TimeoutError(), "error")])
def test_plugin_runtime_distinguishes_stopped_service_and_timeout(monkeypatch, reason, status):
    def fail_request(request, **kwargs):
        assert request.get_header("Authorization").startswith("Bearer ")
        assert request.data == b"{}"
        raise control_server.urllib.error.URLError(reason)
    monkeypatch.setattr(control_server.urllib.request, "urlopen", fail_request)
    result = control_server._plugin_runtime_request("Chat", "http://127.0.0.1:1/api/chat/plugins/reconcile")
    assert result["status"] == status


@pytest.mark.asyncio
async def test_conversation_cold_data_routes_auth_and_timeout_never_falls_back(tmp_path, monkeypatch):
    from satrap.core.storage import StorageLayout
    from satrap.core.utils.context import ContextManager

    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("test-platform")
    database = layout.platform_db("test-platform")
    context = ContextManager("test-conversation", db_path=str(database))
    context.add_chat("输入", "回答")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    monkeypatch.setattr(control_server, "_conversation_data_request", lambda url, payload: None)
    try:
        assert b"401" in (await _request("/config/conversations", authorized=False)).split(b"\r\n", 1)[0]
        assert b"401" in (await _request("/config/models/llm/default/api-key", "POST", authorized=False)).split(b"\r\n", 1)[0]
        platforms = _json_body(await _request("/config/conversations/platforms"))
        assert "test-platform" in platforms["platforms"]
        catalog = _json_body(await _request("/config/conversations?platform_id=test-platform"))
        exact = _json_body(await _request("/config/conversations?platform_id=test-platform&conversation_id=test-conversation"))
        assert [item["conversation_id"] for item in exact["items"]] == ["test-conversation"]
        absent = _json_body(await _request("/config/conversations?platform_id=test-platform&conversation_id=missing"))
        assert absent["items"] == []
        from satrap.core.config.conversation_catalog import MAX_CONVERSATION_ID_LENGTH
        oversized = await _request("/config/conversations?platform_id=test-platform&conversation_id=" + "_" * (MAX_CONVERSATION_ID_LENGTH + 1))
        assert oversized.startswith(b"HTTP/1.1 400")
        assert "conversation_id" in _json_body(oversized)["error"]
        assert catalog["items"][0]["conversation_id"] == "test-conversation"
        request = {"platform_id": "test-platform", "conversation_id": "test-conversation", "layer": "context"}
        current = _json_body(await _request("/config/conversations/data", "POST", json.dumps(request).encode("utf-8")))
        edited = {**request, "action": "edit", "index": 0, "content": "修改输入", "expected_revision": current["revision"]}
        saved = _json_body(await _request("/config/conversations/data", "POST", json.dumps(edited).encode("utf-8")))
        assert saved["saved"] and saved["items"][0]["content"] == "修改输入"
        assert b"409" in (await _request("/config/conversations/data", "POST", json.dumps(edited).encode("utf-8"))).split(b"\r\n", 1)[0]
        def timeout(url, payload):
            raise TimeoutError("不能认定服务停止")
        monkeypatch.setattr(control_server, "_conversation_data_request", timeout)
        errors = []
        monkeypatch.setattr(control_server.logger, "error", errors.append)
        edited["expected_revision"] = saved["revision"]
        failed = await _request("/config/conversations/data", "POST", json.dumps({**edited, "content": "不应保存"}).encode("utf-8"))
        assert b"500" in failed.split(b"\r\n", 1)[0]
        context.load_context()
        assert context.get_context()[0]["content"] == "修改输入"
        assert "Traceback" in errors[0] and "test-conversation" in errors[0]
    finally:
        context.close()


@pytest.mark.parametrize("reason,stopped", [(ConnectionRefusedError(), True), (TimeoutError(), False)])
def test_conversation_proxy_requires_explicit_connection_refusal(monkeypatch, reason, stopped):
    def fail(request, **kwargs):
        assert request.get_header("Authorization").startswith("Bearer ")
        assert json.loads(request.data)["conversation_id"] == "conversation"
        raise control_server.urllib.error.URLError(reason)
    monkeypatch.setattr(control_server.urllib.request, "urlopen", fail)
    if stopped:
        assert control_server._conversation_data_request("http://127.0.0.1:1/api/conversation-data", {"conversation_id": "conversation"}) is None
    else:
        with pytest.raises(control_server.urllib.error.URLError):
            control_server._conversation_data_request("http://127.0.0.1:1/api/conversation-data", {"conversation_id": "conversation"})


@pytest.mark.asyncio
async def test_dynamic_conversation_platform_types_aggregate_same_ids_and_isolate_failures(tmp_path, monkeypatch):
    from satrap.core.config.conversation_data import ConversationDataService
    from satrap.core.utils.context import ContextManager
    from satrap.core.storage import StorageLayout

    layout = StorageLayout(tmp_path / "data")
    contexts = []
    for identity, platform_type in (("instance-a", "custom-a"), ("instance-b", "custom-b")):
        layout.ensure_platform(identity, platform_type=platform_type)
        context = ContextManager("same-id", db_path=str(layout.platform_db(identity)))
        context.add_user_message("消息")
        contexts.append(context)
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    monkeypatch.setattr(control_server, "load_config_document", lambda path: {})
    try:
        platforms = _json_body(await _request("/config/conversations/platforms"))
        assert {row["type"] for row in platforms["items"]} >= {"custom-a", "custom-b"}
        result = _json_body(await _request("/config/conversations?scope=all"))
        assert {row["platform_id"] for row in result["items"]} == {"instance-a", "instance-b"}
        assert result["total"] == 2
        filtered = _json_body(await _request("/config/conversations?scope=all&platform_type=custom-b&filter.source=unknown"))
        assert [row["platform_id"] for row in filtered["items"]] == ["instance-b"]
        read = ConversationDataService.catalog_records
        def fail_one(self, platform=None, requested=''):
            if self.database == layout.platform_db("instance-b"):
                raise sqlite3.OperationalError("平台数据库不可读")
            return read(self, platform, requested)
        monkeypatch.setattr(ConversationDataService, "catalog_records", fail_one)
        partial = _json_body(await _request("/config/conversations?scope=all"))
        assert [row["platform_id"] for row in partial["items"]] == ["instance-a"]
        assert partial["warnings"] == ["instance-b: 读取失败"]
    finally:
        for context in contexts:
            context.close()
from support.http import _json_body, _request, _use_config
