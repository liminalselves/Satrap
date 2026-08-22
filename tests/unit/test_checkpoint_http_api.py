"""
checkpoint HTTP API 路由测试

通过 BackendHTTPServer._route 直接验证端点 (不启动真实端口):
列表 / 创建 / 审计 / 血缘 / fork / 分支 / 回滚 / 重试 / 异常路径
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.utils.context import ContextManager


def _make_server(db_path: str) -> BackendHTTPServer:
    fake = SimpleNamespace(checkpoint_db_path=db_path)
    return BackendHTTPServer(fake)   # type: ignore[arg-type]


async def _route(
    server: BackendHTTPServer, method: str, path: str, body: bytes = b""
) -> tuple[int, dict[str, Any]]:
    return await server._route(method, path, body)   # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_checkpoint_api_full_flow(tmp_path: Path):
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-1", db_path=db, enable_checkpoint=True)
    ctx.add_user_message("你好")
    cp = ctx.create_checkpoint(name="开场")
    ctx.close()

    server = _make_server(db)

    status, data = await _route(server, "GET", "/api/checkpoints?conversation=conv-1")
    # 列表: 自动 stable + manual
    assert status == 200
    assert len(data["checkpoints"]) >= 2
    assert data["branches"] == []

    status, data = await _route(
        server, "POST", "/api/checkpoint/create",
        json.dumps({"conversation": "conv-1", "name": "第二个"}).encode(),
    )
    # 创建
    assert status == 200
    assert data["checkpoint_id"]

    status, data = await _route(server, "GET", "/api/checkpoint/audit?conversation=conv-1")
    # 审计
    assert status == 200
    assert len(data["mutations"]) >= 3

    status, data = await _route(
        server, "GET", f"/api/checkpoint/lineage?checkpoint_id={cp.checkpoint_id}"
    )
    # 血缘
    assert status == 200
    assert len(data["lineage"]) == 1

    status, data = await _route(
        server, "POST", "/api/checkpoint/fork",
        json.dumps({"conversation": "conv-1", "branch_name": "v2"}).encode(),
    )
    # fork (不指定检查点, 用最新)
    assert status == 200
    new_id = data["conversation_id"]
    assert ":fork:" in new_id

    status, data = await _route(server, "GET", "/api/checkpoints?conversation=conv-1")
    # 分支列表 (聚合接口)
    assert status == 200
    assert len(data["branches"]) == 1
    assert data["branches"][0]["scope_id"] == new_id

    conn = sqlite3.connect(db)
    # 分支检查点已存在
    rows = conn.execute(
        "SELECT COUNT(*) FROM state_checkpoints WHERE scope_id = ?", (new_id,)
    ).fetchone()
    conn.close()
    assert rows is not None and rows[0] >= 1

    status, _ = await _route(
        server, "POST", "/api/checkpoint/rollback",
        json.dumps({"conversation": "conv-1", "checkpoint_id": cp.checkpoint_id}).encode(),
    )
    # 回滚 + 重试
    assert status == 200
    status, _ = await _route(
        server, "POST", "/api/checkpoint/retry",
        json.dumps({"conversation": "conv-1", "checkpoint_id": cp.checkpoint_id}).encode(),
    )
    assert status == 200

    status, data = await _route(
        server, "POST", "/api/checkpoint/rollback",
        json.dumps({"conversation": "conv-1", "checkpoint_id": "manual-nope"}).encode(),
    )
    # 异常: 不存在的检查点
    assert status == 400

    status, _ = await _route(server, "GET", "/api/checkpoint/nope")
    # 异常: 未知子路由
    assert status == 404


@pytest.mark.asyncio
async def test_checkpoint_api_requires_conversation(tmp_path: Path):
    db = str(tmp_path / "chat_history.db")
    server = _make_server(db)
    status, data = await _route(
        server, "POST", "/api/checkpoint/create",
        json.dumps({"name": "无对话"}).encode(),
    )
    assert status == 400
    assert "error" in data
