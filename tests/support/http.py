"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from typing import Any, cast
import json
from satrap.core.backend import control_server

class _BufferWriter:
    """记录控制服务响应的测试写入器"""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        """记录响应字节"""
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
    """直接调用控制服务连接处理器 (默认携带测试令牌)"""
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
    """解析响应 JSON 体"""
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert isinstance(payload, dict)
    return payload

def _use_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """写入覆盖全部冷配置服务的最小配置并停止后端健康检查"""
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
