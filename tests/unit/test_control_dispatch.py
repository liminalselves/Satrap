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
from typing import Any, cast
import json
import time

from satrap.edictum.plugin_config import PluginConfigManager
from satrap.core.backend import control_server
from satrap.core.type import SessionConfig, EmbeddingConfig
from satrap.edictum.plugin_archive import PluginArchiveInstaller
from satrap.edictum.plugin_catalog import PluginCatalog


class _BufferWriter:
    """记录控制服务响应的测试写入器"""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        """
        记录响应字节

        参数:
        - data: 响应字节
        """
        self.data.extend(data)

    async def drain(self) -> None:
        """模拟等待响应写出"""

    def close(self) -> None:
        """记录连接关闭状态"""
        self.closed = True


async def _request(
    path: str,
    method: str = "GET",
    body: bytes = b"",
    *, authorized: bool = True,
) -> bytes:
    """
    直接调用控制服务连接处理器 (默认携带测试令牌)

    参数:
    - path: 请求路径
    - method: HTTP 方法
    - body: 请求体
    - authorized: 是否携带测试管理令牌

    返回:
    - bytes: 完整响应
    """
    reader = asyncio.StreamReader()
    header = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {control_server._CONTROL_AUTH.token if authorized else 'invalid'}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    reader.feed_data(header + body)
    reader.feed_eof()
    writer = _BufferWriter()
    await control_server._handle_request(
        reader,
        cast(asyncio.StreamWriter, writer),
    )
    assert writer.closed is True
    return bytes(writer.data)


def _json_body(response: bytes) -> dict[str, Any]:
    """
    解析响应 JSON 体

    参数:
    - response: 完整响应字节

    返回:
    - dict[str, Any]: JSON 对象
    """
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert isinstance(payload, dict)
    return payload


def _use_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    写入覆盖全部冷配置服务的最小配置并停止后端健康检查

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    - tmp_path: 临时目录
    """
    scan_dir = tmp_path / "sessions"
    scan_dir.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "data_root": str(tmp_path / "data"),
            "model_config_path": str(tmp_path / "models.json"),
            "session_class_config_path": str(tmp_path / "session-classes.json"),
            "session_scan_paths": [str(scan_dir)],
            "edictum_config_path": str(tmp_path / "edictum.json"),
            "platforms": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_server, "CONFIG_PATH", config_path)


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
        ("GET", "/storage/unknown"),
        ("GET", "/chat/history/unknown"),
        ("GET", "/statusx"),
        ("GET", "/configx"),
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
        ("PUT", "/chat/history"),
        ("POST", "/chat/history/trash"),
        ("DELETE", "/config"),
        ("DELETE", "/config/models"),
        ("PUT", "/config/platforms"),
        ("PUT", "/config/session-classes"),
        ("PATCH", "/config/session-instances"),
        ("DELETE", "/config/edictum/sessions"),
        ("GET", "/start"),
        ("GET", "/stop"),
        ("GET", "/restart"),
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
        ("PUT", "/config/models/llm/foo"),
        ("GET", "/config/models/llm"),
        ("POST", "/config/models/llm"),
        ("GET", "/config/session-instances/abc"),
        ("POST", "/config/session-instances/abc"),
        ("GET", "/config/edictum/sessions/foo/enable"),
        ("PATCH", "/config/edictum/sessions/foo/enable"),
        ("DELETE", "/config/edictum/sessions/foo/enable"),
        ("POST", "/config/edictum/sessions/"),
        ("GET", "/config/session-classes/foo/enable"),
        ("DELETE", "/config/session-classes/foo/disable"),
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
async def test_control_start_already_running_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端健康时 /start 直接返回已在运行, 不得再次拉起进程"""
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        control_server,
        "_check_backend_health",
        lambda: {"running": True},
    )
    popen_calls: list[_FakeProcess] = []

    def _fake_popen(cmd: object, *args: object, **kwargs: object) -> _FakeProcess:
        proc = _FakeProcess(cmd, *args, **kwargs)
        popen_calls.append(proc)
        return proc

    monkeypatch.setattr(control_server.subprocess, "Popen", _fake_popen)
    response = await _request("/start", "POST")
    assert b"200 OK" in response
    assert _json_body(response) == {"ok": True, "message": "后端已在运行中"}
    assert popen_calls == []


@pytest.mark.asyncio
async def test_control_start_in_progress_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已拉起但未就绪时 /start 返回正在启动, 不得重复拉起进程"""
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        control_server,
        "_check_backend_health",
        lambda: {"running": False},
    )
    monkeypatch.setattr(control_server, "_backend_process", _FakeProcess("existing"))
    popen_calls: list[_FakeProcess] = []

    def _fake_popen(cmd: object, *args: object, **kwargs: object) -> _FakeProcess:
        proc = _FakeProcess(cmd, *args, **kwargs)
        popen_calls.append(proc)
        return proc

    monkeypatch.setattr(control_server.subprocess, "Popen", _fake_popen)
    response = await _request("/start", "POST")
    assert _json_body(response) == {"ok": True, "message": "后端正在启动中"}
    assert popen_calls == []


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
        """
        模拟耗时的健康检查, 让两个请求都在检查阶段让出

        返回:
        - 始终未运行的健康结果
        """
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
        """
        记录调用线程并模拟 200 毫秒的阻塞 IO

        返回:
        - 停止状态的健康结果
        """
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
        """
        记录退出码而不终止进程

        参数:
        - code: 退出码
        """
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
    monkeypatch.setattr(control_server, "_plugin_management_service", lambda: service)
    applied = []
    async def apply_runtime():
        applied.append(True)
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
    retry = _json_body(await _request("/config/plugins/reconcile", "POST", b"{}"))
    assert retry["runtime"][1]["status"] == "error"
    assert len(applied) == 2
    usage = _json_body(await _request("/config/plugins/probe/usages"))["locations"][0]
    response = await _request("/config/plugins/probe/usages", "PUT", json.dumps({"kind": "chat", "location_id": "chat", "state": {"present": True, "enabled": False, "capabilities": {}}, "expected_revision": usage["revision"]}).encode("utf-8"))
    assert b"200" in response.split(b"\r\n", 1)[0]
    assert _json_body(response)["locations"][0]["present"]
    assert _json_body(response)["runtime"][1]["status"] == "error"
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
        platforms = _json_body(await _request("/config/conversations/platforms"))
        assert "test-platform" in platforms["platforms"]
        catalog = _json_body(await _request("/config/conversations?platform_id=test-platform"))
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
        def fail_one(self, platform=None):
            if self.database == layout.platform_db("instance-b"):
                raise sqlite3.OperationalError("平台数据库不可读")
            return read(self, platform)
        monkeypatch.setattr(ConversationDataService, "catalog_records", fail_one)
        partial = _json_body(await _request("/config/conversations?scope=all"))
        assert [row["platform_id"] for row in partial["items"]] == ["instance-a"]
        assert partial["warnings"] == ["instance-b: 读取失败"]
    finally:
        for context in contexts:
            context.close()
