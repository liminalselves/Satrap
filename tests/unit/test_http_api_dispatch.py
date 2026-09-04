"""
http_api._route 路由分派特征测试

在路由函数拆解前固定分派语义:
- 前缀碰撞: /api/users↔/api/user, /api/checkpoints↔/api/checkpoint,
  /api/sessions/bulk-delete↔/api/sessions/{id},
  session-class 与 edictum 的 enable/disable↔普通详情
- 错误 HTTP 方法与未知子路径的回退
- query-bearing 路径的解析形态 (urlsplit / partition("?") / 原始 path)
- 子管理器为 None 时的 fallthrough (落到最终 404 unknown route)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry


def _make_server(tmp_path: Path) -> BackendHTTPServer:
    """构造数据目录隔离在 tmp_path 的真实后端服务器"""
    return BackendHTTPServer(BackendManager(BackendConfig(data_root=str(tmp_path))))


def _patch_manager(monkeypatch: pytest.MonkeyPatch, name: str, manager: Any) -> None:
    """以类级 property 替换注入子管理器 (实例属性无法遮蔽 data descriptor)"""
    monkeypatch.setattr(BackendManager, name, property(lambda self: manager))


async def _route(
    server: BackendHTTPServer, method: str, path: str, body: bytes = b""
) -> tuple[int, dict[str, Any]]:
    return await server._route(method, path, body)


# ================= 未知路由与错误方法 =================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/nope"),
        ("POST", "/api"),
        ("GET", "/api/sessionsxyz"),
        ("GET", "/api/checkpointxyz"),
        ("GET", "/api/userxyz"),
        ("DELETE", "/api/health"),
        ("GET", "/api/shutdown"),
        ("PUT", "/api/sessions"),
        ("GET", "/api/sessions/foo/restart"),
        ("PATCH", "/api/users"),
    ],
)
async def test_unknown_route_and_wrong_method_fall_through_to_404(
    tmp_path: Path, method: str, path: str
):
    """未命中任何区段的路径/方法组合落到最终 404 unknown route"""
    server = _make_server(tmp_path)
    status, data = await _route(server, method, path)
    assert status == 404
    assert data == {"error": f"unknown route: {method} {path}"}


# ================= 子管理器为 None 的 fallthrough =================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/api/config/session-classes"),
        ("GET", "/api/config/session-classes/foo"),
        ("DELETE", "/api/config/session-classes/foo"),
        ("GET", "/api/config/models"),
        ("POST", "/api/config/models/llm/x"),
        ("GET", "/api/config/edictum/types"),
        ("GET", "/api/config/edictum/sessions"),
        ("GET", "/api/config/edictum/sessions/foo"),
    ],
)
async def test_manager_none_falls_through_to_404(tmp_path: Path, method: str, path: str):
    """管理器缺失时带管理器条件的分支不命中, 落到最终 404"""
    server = _make_server(tmp_path)
    status, data = await _route(server, method, path)
    assert status == 404
    assert data == {"error": f"unknown route: {method} {path}"}


@pytest.mark.asyncio
async def test_session_class_collection_responds_without_manager(tmp_path: Path):
    """GET 集合路由无管理器时也直接响应空表 (不 fallthrough)"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "GET", "/api/config/session-classes")
    assert status == 200
    assert data == {}


# ================= 前缀碰撞: users↔user =================


@pytest.mark.asyncio
async def test_users_list_and_limit_validation(tmp_path: Path):
    """/api/users 命中用户列表 (不被 /api/user 前缀吞掉), limit 越界报 400"""
    server = _make_server(tmp_path)
    status, _ = await _route(server, "GET", "/api/users")
    assert status == 200

    status, data = await _route(server, "GET", "/api/users?limit=0")
    assert status == 400
    assert "limit" in data["error"]

    status, _ = await _route(server, "GET", "/api/users?limit=2000")
    assert status == 400


@pytest.mark.asyncio
async def test_user_sessions_requires_user_id(tmp_path: Path):
    """/api/user/sessions 缺少 user_id 报 400 (命中 user 区段而非 404)"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "GET", "/api/user/sessions")
    assert status == 400
    assert "user_id" in data["error"]


@pytest.mark.asyncio
async def test_user_create_requires_user_id(tmp_path: Path):
    """POST /api/user/create 空负载报 400 (命中 user 区段而非 404)"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "POST", "/api/user/create", b"{}")
    assert status == 400
    assert "user_id" in data["error"]


# ================= 前缀碰撞: checkpoints↔checkpoint =================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/checkpoints",
        "/api/checkpoint/branches",
        "/api/checkpoint/lineage",
        "/api/checkpoint/audit",
    ],
)
async def test_checkpoint_get_routes_are_distinct(tmp_path: Path, path: str):
    """/api/checkpoints 与 /api/checkpoint/* 各自命中 (均非 404)"""
    server = _make_server(tmp_path)
    status, _ = await _route(server, "GET", path)
    assert status == 200


# ================= 前缀碰撞: bulk-delete↔会话项删除 =================


@pytest.mark.asyncio
async def test_sessions_bulk_delete_exact_route(tmp_path: Path):
    """POST /api/sessions/bulk-delete 命中批量删除 (未知模式报 400)"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "POST", "/api/sessions/bulk-delete", b'{"mode":"weird"}')
    assert status == 400
    assert "未知批量删除模式" in data["error"]


@pytest.mark.asyncio
async def test_sessions_item_delete_captures_bulk_delete_literal(tmp_path: Path):
    """DELETE /api/sessions/bulk-delete 被项删除前缀捕获, 按名为 bulk-delete 的会话处理"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "DELETE", "/api/sessions/bulk-delete")
    assert status == 400
    assert "platform_id" in data["error"]

    status, data = await _route(server, "DELETE", "/api/sessions/bulk-delete?platform_id=local")
    assert status == 404
    assert "平台实例不存在" in data["error"]


# ================= session-class: enable/disable↔普通详情 =================


@pytest.mark.asyncio
async def test_session_class_enable_disable_and_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """enable/disable 与普通详情互不吞并; GET .../enable 按名为 xxx/enable 的详情处理"""
    mgr = SessionClassConfigManager(storage_path=tmp_path / "sc.json")
    _patch_manager(monkeypatch, "session_class_mgr", mgr)
    server = _make_server(tmp_path)

    status, _ = await _route(
        server,
        "POST",
        "/api/config/session-classes",
        b'{"name":"demo","class_path":"satrap.core.framework.Base.Session"}',
    )
    assert status == 200

    status, data = await _route(server, "POST", "/api/config/session-classes/demo/enable")
    assert status == 200
    assert data["ok"] is True

    status, data = await _route(server, "GET", "/api/config/session-classes/demo")
    assert status == 200
    assert data["enabled"] is True

    status, data = await _route(server, "POST", "/api/config/session-classes/demo/disable")
    assert status == 200
    assert data["config"]["enabled"] is False

    # GET 方法命中详情分支, enable 后缀被当作名称的一部分
    status, data = await _route(server, "GET", "/api/config/session-classes/demo/enable")
    assert status == 404
    assert data == {"error": "not found"}


# ================= edictum: enable/disable↔普通详情 =================


@pytest.mark.asyncio
async def test_edictum_enable_disable_and_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """edictum 前缀区段: 详情未命中报 404, enable 作用于未知名称报 400"""
    registry = create_default_edictum_type_registry()
    mgr = EdictumConfigManager(registry, storage_path=tmp_path / "ed.json")
    _patch_manager(monkeypatch, "edictum_type_registry", registry)
    _patch_manager(monkeypatch, "edictum_config_manager", mgr)
    server = _make_server(tmp_path)

    status, data = await _route(server, "GET", "/api/config/edictum/types")
    assert status == 200
    assert "types" in data

    status, _ = await _route(server, "GET", "/api/config/edictum/sessions")
    assert status == 200

    status, data = await _route(server, "GET", "/api/config/edictum/sessions/nope")
    assert status == 404
    assert data == {"error": "not found"}

    status, _ = await _route(server, "POST", "/api/config/edictum/sessions/nope/enable")
    assert status == 400


# ================= models 区段 =================


@pytest.mark.asyncio
async def test_models_collection_and_prefix_quirk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GET /api/config/models 列表; startswith 前缀怪癖 (/api/config/modelsx 也命中)"""
    mgr = ModelConfigManager(storage_path=tmp_path / "models.json")
    _patch_manager(monkeypatch, "model_config_manager", mgr)
    server = _make_server(tmp_path)

    status, _ = await _route(server, "GET", "/api/config/models")
    assert status == 200

    status, _ = await _route(server, "GET", "/api/config/models?type=llm")
    assert status == 200

    # 前缀怪癖: 无斜杠的 modelsx 同样被 startswith("/api/config/models") 捕获
    status, _ = await _route(server, "GET", "/api/config/modelsx")
    assert status == 200

    # 项路由段数不足时在区段内直接 404 (与最终 404 同文)
    status, data = await _route(server, "POST", "/api/config/models/llm", b"{}")
    assert status == 404
    assert data == {"error": "unknown route: POST /api/config/models/llm"}


# ================= query-bearing 路径 =================


@pytest.mark.asyncio
async def test_query_bearing_sessions_list(tmp_path: Path):
    """GET /api/sessions?x=1 经 partition("?") 剥离查询串后命中列表"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "GET", "/api/sessions?x=1")
    assert status == 200
    assert data == {"sessions": []}


@pytest.mark.asyncio
async def test_query_bearing_storage_audit(tmp_path: Path):
    """GET /api/storage/audit?x=1 经 urlsplit 剥离查询串后命中审计"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "GET", "/api/storage/audit?x=1")
    assert status == 200
    assert "items" in data
    assert "summary" in data


@pytest.mark.asyncio
async def test_discovery_rejects_unconfigured_scan_path(tmp_path: Path):
    """GET /api/session/discovery?path=... 未配置目录报 400"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "GET", "/api/session/discovery?path=/etc/forbidden")
    assert status == 400
    assert "只能扫描配置中的" in data["error"]


# ================= ui / health =================


@pytest.mark.asyncio
async def test_ui_config_and_health(tmp_path: Path):
    """GET /ui-config.json 200; 未启动后端 GET /api/health 报 503"""
    server = _make_server(tmp_path)
    status, data = await _route(server, "GET", "/ui-config.json")
    assert status == 200
    assert data["backend_api"] == "http://127.0.0.1:19870"

    status, data = await _route(server, "GET", "/api/health")
    assert status == 503
    assert data["healthy"] is False
