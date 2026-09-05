"""control_server._handle_request 路由分派特征测试 (B3b 拆解语义固定)

按连续源码区段拆解前固定路由匹配, 响应状态与副作用顺序语义:
- 未知路径与精确路径错误方法落到最终 404 {"error": "not found"}
- 前缀区段 (models/session-instances/edictum/session-classes) 在段内产生
  404 {"error": "not found: {method} {path}"}, 不落后续区段
- 精确集合与前缀详情边界 (bulk-delete 单项删除, enable/disable 普通详情)
- query 沿 raw_path 解析; 生命周期 start/stop/restart 与 /shutdown 副作用保持
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from typing import cast
import json

from satrap.core.backend import control_server


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
) -> bytes:
    """
    直接调用控制服务连接处理器 (默认携带测试令牌)

    参数:
    - path: 请求路径
    - method: HTTP 方法
    - body: 请求体

    返回:
    - bytes: 完整响应
    """
    reader = asyncio.StreamReader()
    header = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {control_server._CONTROL_AUTH.token}\r\n"
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


def _json_body(response: bytes) -> object:
    """
    解析响应 JSON 体

    参数:
    - response: 完整响应字节

    返回:
    - object: JSON 解析结果
    """
    return json.loads(response.split(b"\r\n\r\n", 1)[1])


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
