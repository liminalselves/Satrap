"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
import pytest
from typing import Any
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from satrap.core.server_auth import BrowserSessionStore, ServerAuth, load_or_create_api_token
from typing import cast
from types import SimpleNamespace
import json

def _http_api_dispatch_make_server(tmp_path: Path) -> BackendHTTPServer:
    """构造数据目录隔离在 tmp_path 的真实后端服务器"""
    return BackendHTTPServer(BackendManager(BackendConfig(data_root=str(tmp_path))))

def _patch_manager(monkeypatch: pytest.MonkeyPatch, name: str, manager: Any) -> None:
    """以类级 property 替换注入子管理器 (实例属性无法遮蔽 data descriptor)"""
    monkeypatch.setattr(BackendManager, name, property(lambda self: manager))

async def _http_api_dispatch_route(
    server: BackendHTTPServer, method: str, path: str, body: bytes = b""
) -> tuple[int, Mapping[str, Any]]:
    return await server._route(method, path, body)

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/nope"),
        ("POST", "/api"),
        ("GET", "/api/sessionsxyz"),
        ("DELETE", "/api/health"),
        ("PUT", "/api/sessions"),
    ],
)
async def test_unknown_route_and_wrong_method_fall_through_to_404(
    tmp_path: Path, method: str, path: str
):
    """未命中任何区段的路径/方法组合落到最终 404 unknown route"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, method, path)
    assert status == 404
    assert data == {"error": f"unknown route: {method} {path}"}

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/api/config/session-classes"),
        ("GET", "/api/config/session-classes/foo"),
        ("GET", "/api/config/models"),
        ("GET", "/api/config/edictum/sessions/foo"),
    ],
)
async def test_manager_none_falls_through_to_404(tmp_path: Path, method: str, path: str):
    """管理器缺失时带管理器条件的分支不命中, 落到最终 404"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, method, path)
    assert status == 404
    assert data == {"error": f"unknown route: {method} {path}"}

@pytest.mark.asyncio
async def test_session_class_collection_responds_without_manager(tmp_path: Path):
    """GET 集合路由无管理器时也直接响应空表 (不 fallthrough)"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, "GET", "/api/config/session-classes")
    assert status == 200
    assert data == {}

@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["0", "1001", "invalid"])
async def test_users_list_and_limit_validation(tmp_path: Path, limit: str):
    """/api/users 命中用户列表 (不被 /api/user 前缀吞掉), limit 越界或非整数报 400"""
    server = _user_http_api_make_server(str(tmp_path / "users.db"))
    status, _ = await _user_http_api_route(server, "GET", "/api/users")
    assert status == 200

    status, data = await _user_http_api_route(server, "GET", f"/api/users?limit={limit}")
    assert status == 400
    assert "limit" in data["error"] or "invalid" in data["error"]

@pytest.mark.asyncio
async def test_user_routes_require_user_id(tmp_path: Path):
    """/api/user/sessions 与 /api/user/create 缺少 user_id 报 400 (命中 user 区段而非 404)"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, "GET", "/api/user/sessions")
    assert status == 400
    assert "user_id" in data["error"]

    status, data = await _http_api_dispatch_route(server, "POST", "/api/user/create", b"{}")
    assert status == 400
    assert "user_id" in data["error"]

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/checkpoints",
        "/api/checkpoint/branches",
    ],
)
async def test_checkpoint_get_routes_are_distinct(tmp_path: Path, path: str):
    """/api/checkpoints 与 /api/checkpoint/* 各自命中 (均非 404)"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, _ = await _http_api_dispatch_route(server, "GET", path)
    assert status == 200

@pytest.mark.asyncio
async def test_sessions_bulk_delete_exact_route(tmp_path: Path):
    """POST /api/sessions/bulk-delete 命中批量删除 (未知模式报 400)"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, "POST", "/api/sessions/bulk-delete", b'{"mode":"weird"}')
    assert status == 400
    assert "未知批量删除模式" in data["error"]

@pytest.mark.asyncio
async def test_sessions_item_delete_captures_bulk_delete_literal(tmp_path: Path):
    """DELETE /api/sessions/bulk-delete 被项删除前缀捕获, 按名为 bulk-delete 的会话处理"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, "DELETE", "/api/sessions/bulk-delete")
    assert status == 400
    assert "platform_id" in data["error"]

    status, data = await _http_api_dispatch_route(server, "DELETE", "/api/sessions/bulk-delete?platform_id=local")
    assert status == 404
    assert "平台实例不存在" in data["error"]

@pytest.mark.asyncio
async def test_session_class_enable_disable_and_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """enable/disable 与普通详情互不吞并; GET .../enable 按名为 xxx/enable 的详情处理"""
    mgr = SessionClassConfigManager(storage_path=tmp_path / "sc.json")
    _patch_manager(monkeypatch, "session_class_mgr", mgr)
    server = _http_api_dispatch_make_server(tmp_path)

    status, _ = await _http_api_dispatch_route(
        server,
        "POST",
        "/api/config/session-classes",
        b'{"name":"demo","class_path":"satrap.core.framework.Base.Session"}',
    )
    assert status == 200

    status, data = await _http_api_dispatch_route(server, "POST", "/api/config/session-classes/demo/enable")
    assert status == 200
    assert data["ok"] is True

    status, data = await _http_api_dispatch_route(server, "GET", "/api/config/session-classes/demo")
    assert status == 200
    assert data["enabled"] is True

    status, data = await _http_api_dispatch_route(server, "POST", "/api/config/session-classes/demo/disable")
    assert status == 200
    assert data["config"]["enabled"] is False

    # GET 方法命中详情分支, enable 后缀被当作名称的一部分
    status, data = await _http_api_dispatch_route(server, "GET", "/api/config/session-classes/demo/enable")
    assert status == 404
    assert data == {"error": "not found"}

@pytest.mark.asyncio
async def test_edictum_enable_disable_and_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """edictum 前缀区段: 详情未命中报 404, enable 作用于未知名称报 400"""
    registry = create_default_edictum_type_registry()
    mgr = EdictumConfigManager(registry, storage_path=tmp_path / "ed.json")
    _patch_manager(monkeypatch, "edictum_type_registry", registry)
    _patch_manager(monkeypatch, "edictum_config_manager", mgr)
    server = _http_api_dispatch_make_server(tmp_path)

    status, data = await _http_api_dispatch_route(server, "GET", "/api/config/edictum/types")
    assert status == 200
    assert "types" in data

    status, _ = await _http_api_dispatch_route(server, "GET", "/api/config/edictum/sessions")
    assert status == 200

    status, data = await _http_api_dispatch_route(server, "GET", "/api/config/edictum/sessions/nope")
    assert status == 404
    assert data == {"error": "not found"}

    status, _ = await _http_api_dispatch_route(server, "POST", "/api/config/edictum/sessions/nope/enable")
    assert status == 400

@pytest.mark.asyncio
async def test_models_collection_and_prefix_quirk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GET /api/config/models 列表; startswith 前缀怪癖 (/api/config/modelsx 也命中)"""
    mgr = ModelConfigManager(storage_path=tmp_path / "models.json")
    _patch_manager(monkeypatch, "model_config_manager", mgr)
    server = _http_api_dispatch_make_server(tmp_path)

    status, _ = await _http_api_dispatch_route(server, "GET", "/api/config/models")
    assert status == 200

    status, _ = await _http_api_dispatch_route(server, "GET", "/api/config/models?type=llm")
    assert status == 200

    # 前缀怪癖: 无斜杠的 modelsx 同样被 startswith("/api/config/models") 捕获
    status, _ = await _http_api_dispatch_route(server, "GET", "/api/config/modelsx")
    assert status == 200

    # 项路由段数不足时在区段内直接 404 (与最终 404 同文)
    status, data = await _http_api_dispatch_route(server, "POST", "/api/config/models/llm", b"{}")
    assert status == 404
    assert data == {"error": "unknown route: POST /api/config/models/llm"}

@pytest.mark.asyncio
async def test_query_bearing_sessions_list(tmp_path: Path):
    """GET /api/sessions?x=1 经 partition("?") 剥离查询串后命中列表"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, "GET", "/api/sessions?x=1")
    assert status == 200
    assert data == {"sessions": []}

@pytest.mark.asyncio
async def test_query_bearing_storage_audit(tmp_path: Path):
    """GET /api/storage/audit?x=1 经 urlsplit 剥离查询串后命中审计"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, "GET", "/api/storage/audit?x=1")
    assert status == 200
    assert "items" in data
    assert "summary" in data

@pytest.mark.asyncio
async def test_discovery_rejects_unconfigured_scan_path(tmp_path: Path):
    """GET /api/session/discovery?path=... 未配置目录报 400"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, "GET", "/api/session/discovery?path=/etc/forbidden")
    assert status == 400
    assert "只能扫描配置中的" in data["error"]

@pytest.mark.asyncio
async def test_ui_config_and_health(tmp_path: Path):
    """GET /ui-config.json 200; 未启动后端 GET /api/health 报 503"""
    server = _http_api_dispatch_make_server(tmp_path)
    status, data = await _http_api_dispatch_route(server, "GET", "/ui-config.json")
    assert status == 200
    assert data["backend_api"] == "http://127.0.0.1:19870"

    status, data = await _http_api_dispatch_route(server, "GET", "/api/health")
    assert status == 503
    assert data["healthy"] is False

@pytest.mark.asyncio
async def test_conversation_data_route_isolated_edit_conflict_and_failure_recovery(tmp_path: Path, monkeypatch):
    from satrap.core.backend import http_api
    from satrap.core.utils.context import ContextManager
    import json

    server = _http_api_dispatch_make_server(tmp_path)
    database = server.backend.platform_db_path("local")
    context = ContextManager("route-main", db_path=database)
    try:
        context.add_user_message("原始输入")
    finally:
        context.close()
    payload = {"platform_id": "local", "conversation_id": "route-main", "layer": "context"}
    async def request(extra=None):
        return await _http_api_dispatch_route(server, "POST", "/api/conversation-data", json.dumps({**payload, **(extra or {})}, ensure_ascii=False).encode("utf-8"))

    status, before = await request()
    assert status == 200
    status, edited = await request({"action": "edit", "index": 0, "content": "修改输入", "expected_revision": before["revision"]})
    assert status == 200 and edited["items"][0]["content"] == "修改输入"
    status, _ = await request({"action": "clear", "expected_revision": before["revision"]})
    assert status == 409
    status, _ = await request({"conversation_id": "missing"})
    assert status == 404
    status, _ = await request({"conversation_id": None})
    assert status == 400

    logs = []
    monkeypatch.setattr(http_api.logger, "error", logs.append)
    original = http_api.manage_platform_data
    async def broken(*args, **kwargs):
        raise RuntimeError("测试运行实例失败")
    monkeypatch.setattr(http_api, "manage_platform_data", broken)
    status, _ = await request()
    assert status == 500
    assert "local/route-main" in logs[0] and "Traceback" in logs[0]
    monkeypatch.setattr(http_api, "manage_platform_data", original)
    status, recovered = await request()
    assert status == 200 and recovered["revision"] == edited["revision"]

def test_token_is_generated_persisted_and_created_once_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置环境令牌时生成高熵令牌并持久化, 并发启动只创建一个共享值"""
    monkeypatch.delenv("SATRAP_API_TOKEN", raising=False)
    token_path = tmp_path / "api-token"
    first = load_or_create_api_token(token_path)
    second = load_or_create_api_token(token_path)
    assert first == second
    assert len(first) >= 32
    assert token_path.read_text(encoding="utf-8") == first

    def load_token(_: int) -> str:
        """读取或创建共享测试令牌"""
        return load_or_create_api_token(token_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(load_token, range(16)))
    assert len(set(tokens)) == 1 and tokens[0] == first

def test_non_loopback_binding_requires_explicit_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非回环监听不能静默使用自动生成令牌"""
    monkeypatch.delenv("SATRAP_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="非回环地址"):
        ServerAuth.create("0.0.0.0", 19870, token_path=tmp_path / "api-token")

def test_bearer_random_cookie_and_exact_origin_validation() -> None:
    """Bearer, Cookie 和精确 Origin 白名单按预期校验"""
    token = "secure-test-token-that-is-longer-than-thirty-two"
    auth = ServerAuth.create("127.0.0.1", 19870, token=token)
    assert auth.authorized({"authorization": f"Bearer {token}"})
    cookie_header = auth.session_cookie()
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    session_id = cookie[auth.cookie_name].value
    assert session_id != token
    assert token not in cookie_header
    assert auth.authorized({"cookie": f"{auth.cookie_name}={session_id}"})
    assert not auth.authorized({"authorization": "Bearer wrong"})
    assert auth.origin_allowed("http://localhost:5173")
    assert not auth.origin_allowed("https://evil.example")
    assert auth.cors_headers("http://localhost:5173")["Access-Control-Allow-Origin"] == "http://localhost:5173"

def test_browser_session_expiry_capacity_and_revocation() -> None:
    """浏览器会话支持过期, 容量淘汰和显式撤销"""
    clock = {"now": 10.0}
    store = BrowserSessionStore(
        ttl_seconds=5,
        max_sessions=2,
        now_provider=lambda: clock["now"],
    )
    first = store.issue()
    clock["now"] = 11.0
    second = store.issue()
    assert store.validate(first) and store.validate(second)

    clock["now"] = 12.0
    third = store.issue()
    assert not store.validate(first)
    assert store.validate(second) and store.validate(third)
    assert store.revoke(second) is True
    assert store.validate(second) is False

    clock["now"] = 18.0
    assert store.validate(third) is False

def test_strict_mode_disables_loopback_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """严格模式下回环请求也必须提供 Bearer token"""
    monkeypatch.setenv("SATRAP_LOOPBACK_BOOTSTRAP", "false")
    auth = ServerAuth.create(
        "127.0.0.1",
        19870,
        token="secure-test-token-that-is-longer-than-thirty-two",
    )
    assert not auth.can_bootstrap("127.0.0.1", "http://localhost:5173", {})

def _user_http_api_make_server(db_path: str) -> BackendHTTPServer:
    fake = SimpleNamespace(
        checkpoint_db_path=db_path,
        config=SimpleNamespace(user_db_path=db_path),
    )
    # BackendManager 替身: 仅暴露 checkpoint_db_path 与 config.user_db_path, 边界断言集中在本工厂
    return BackendHTTPServer(cast(BackendManager, fake))

async def _user_http_api_route(
    server: BackendHTTPServer, method: str, path: str, body: bytes = b""
) -> tuple[int, Mapping[str, Any]]:
    return await server._route(method, path, body)

@pytest.mark.asyncio
async def test_user_api_full_flow_and_error_paths(tmp_path: Path):
    """用户端点全链路 (创建/列表/详情/绑定/会话/更新/解绑/删除) 与异常路径"""
    db = str(tmp_path / "users.db")
    server = _user_http_api_make_server(db)

    status, data = await _user_http_api_route(
        server, "POST", "/api/user/create",
        json.dumps({"user_id": "u1", "platform": "misskey", "nickname": "小美"}).encode(),
    )
    assert status == 200 and data["ok"] is True and data["created"] is True

    status, data = await _user_http_api_route(server, "GET", "/api/users")
    assert status == 200 and data["count"] == 1 and data["users"][0]["user_id"] == "u1"

    status, data = await _user_http_api_route(server, "GET", "/api/users?user_id=u1")
    assert status == 200 and data["user"]["user_nickname"] == "小美"

    status, data = await _user_http_api_route(
        server, "POST", "/api/user/bind",
        json.dumps({"user_id": "u1", "session_id": "sid-1"}).encode(),
    )
    assert status == 200 and data["session_ids"] == ["sid-1"]

    status, data = await _user_http_api_route(server, "GET", "/api/user/sessions?user_id=u1")
    assert status == 200 and data["session_ids"] == ["sid-1"]

    status, data = await _user_http_api_route(
        server, "POST", "/api/user/update",
        json.dumps({"user_id": "u1", "nickname": "新昵称"}).encode(),
    )
    assert status == 200 and data["user"]["user_nickname"] == "新昵称"

    status, data = await _user_http_api_route(
        server, "POST", "/api/user/unbind",
        json.dumps({"user_id": "u1", "session_id": "sid-1"}).encode(),
    )
    assert status == 200 and data["session_ids"] == []

    status, data = await _user_http_api_route(
        server, "POST", "/api/user/delete", json.dumps({"user_id": "u1"}).encode()
    )
    assert status == 200 and data["ok"] is True

    # 异常路径: 缺参数 / 用户不存在 / 未知路由
    status, data = await _user_http_api_route(server, "POST", "/api/user/create", json.dumps({}).encode())
    assert status == 400 and "user_id" in data["error"]

    status, data = await _user_http_api_route(server, "GET", "/api/users?user_id=missing")
    assert status == 200 and data["ok"] is False

    status, data = await _user_http_api_route(
        server, "POST", "/api/user/update", json.dumps({"user_id": "missing", "nickname": "x"}).encode())
    assert status == 200 and data["ok"] is False

    status, data = await _user_http_api_route(server, "POST", "/api/user/bind", json.dumps({"user_id": "u1"}).encode())
    assert status == 400 and "session_id" in data["error"]

    status, _ = await _user_http_api_route(server, "GET", "/api/user/unknown")
    assert status == 404
