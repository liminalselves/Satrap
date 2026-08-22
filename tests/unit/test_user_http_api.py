"""
用户 HTTP API 路由测试

通过 BackendHTTPServer._route 直接验证端点 (不启动真实端口):
列表 / 详情 / 创建 / 更新 / 删除 / 绑定 / 解绑 / 会话列表 / 异常路径
"""
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from satrap.core.backend.http_api import BackendHTTPServer


def _make_server(db_path: str) -> BackendHTTPServer:
    fake = SimpleNamespace(
        checkpoint_db_path=db_path,
        config=SimpleNamespace(user_db_path=db_path),
    )
    return BackendHTTPServer(fake)   # type: ignore[arg-type]


async def _route(
    server: BackendHTTPServer, method: str, path: str, body: bytes = b""
) -> tuple[int, dict[str, Any]]:
    return await server._route(method, path, body)   # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_user_api_full_flow(tmp_path: Path):
    """
    用户端点全链路: 创建 -> 列表 -> 详情 -> 绑定 -> 会话 -> 解绑 -> 删除

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "users.db")
    server = _make_server(db)

    status, data = await _route(
        server, "POST", "/api/user/create",
        json.dumps({"user_id": "u1", "platform": "misskey", "nickname": "小美"}).encode(),
    )
    # 创建
    assert status == 200
    assert data["ok"] is True
    assert data["created"] is True

    status, data = await _route(server, "GET", "/api/users")
    # 列表
    assert status == 200
    assert data["count"] == 1
    assert data["users"][0]["user_id"] == "u1"

    status, data = await _route(server, "GET", "/api/users?user_id=u1")
    # 详情
    assert status == 200
    assert data["user"]["user_nickname"] == "小美"

    status, data = await _route(
        server, "POST", "/api/user/bind",
        json.dumps({"user_id": "u1", "session_id": "sid-1"}).encode(),
    )
    # 绑定会话
    assert status == 200
    assert data["session_ids"] == ["sid-1"]

    status, data = await _route(server, "GET", "/api/user/sessions?user_id=u1")
    # 会话列表
    assert status == 200
    assert data["session_ids"] == ["sid-1"]

    status, data = await _route(
        server, "POST", "/api/user/update",
        json.dumps({"user_id": "u1", "nickname": "新昵称"}).encode(),
    )
    # 更新
    assert status == 200
    assert data["user"]["user_nickname"] == "新昵称"

    status, data = await _route(
        server, "POST", "/api/user/unbind",
        json.dumps({"user_id": "u1", "session_id": "sid-1"}).encode(),
    )
    # 解绑
    assert status == 200
    assert data["session_ids"] == []

    status, data = await _route(
        server, "POST", "/api/user/delete", json.dumps({"user_id": "u1"}).encode()
    )
    # 删除
    assert status == 200
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_user_api_error_paths(tmp_path: Path):
    """
    异常路径: 缺参数 / 用户不存在

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "users.db")
    server = _make_server(db)

    status, data = await _route(
        server, "POST", "/api/user/create", json.dumps({}).encode()
    )
    # 缺 user_id
    assert status == 400
    assert "user_id" in data["error"]

    status, data = await _route(server, "GET", "/api/users?user_id=missing")
    # 不存在的用户
    assert status == 200
    assert data["ok"] is False

    status, data = await _route(
        server, "POST", "/api/user/update",
        json.dumps({"user_id": "missing", "nickname": "x"}).encode(),
    )
    assert status == 200
    assert data["ok"] is False

    status, data = await _route(
        server, "POST", "/api/user/bind", json.dumps({"user_id": "u1"}).encode()
    )
    # 绑定缺 session_id
    assert status == 400
    assert "session_id" in data["error"]

    status, data = await _route(server, "GET", "/api/user/unknown")
    # 未知路由仍 404
    assert status == 404
