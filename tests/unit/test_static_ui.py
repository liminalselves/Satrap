"""React SPA 静态文件服务测试"""
from __future__ import annotations

import asyncio
import gzip
from pathlib import Path
from typing import cast

import pytest

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.backend.static_ui import SPAStaticService


class _BufferWriter:
    """记录响应字节的测试写入器"""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        """
        记录响应内容

        参数:
        - data: 响应字节
        """
        self.data.extend(data)


@pytest.mark.asyncio
async def test_spa_static_service_serves_index_assets_and_routes(tmp_path: Path):
    """
    静态服务应提供入口, 资源和 SPA 路由回退

    参数:
    - tmp_path: 临时目录
    """
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<main>Satrap</main>", encoding="utf-8")
    (assets_dir / "app.js").write_text("window.satrap = true", encoding="utf-8")
    service = SPAStaticService(static_dir)

    index_writer = _BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, index_writer), "/") is True
    assert b"200 OK" in index_writer.data
    assert b"Cache-Control: no-cache" in index_writer.data
    assert b"<main>Satrap</main>" in index_writer.data

    asset_writer = _BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, asset_writer), "/assets/app.js?v=1") is True
    assert b"Content-Type: text/javascript" in asset_writer.data
    assert b"Cache-Control: no-cache" in asset_writer.data
    assert b"window.satrap = true" in asset_writer.data

    route_writer = _BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, route_writer), "/settings") is True
    assert b"<main>Satrap</main>" in route_writer.data
    assert b"Cache-Control: no-cache" in route_writer.data


@pytest.mark.asyncio
async def test_spa_static_service_caches_only_hashed_assets(tmp_path: Path):
    """
    静态服务应仅长期缓存带哈希的 assets 文件

    参数:
    - tmp_path: 临时目录
    """
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    asset_path = assets_dir / "app-AbCd1234.js"
    asset_path.write_text("window.satrap = true", encoding="utf-8")
    service = SPAStaticService(static_dir)
    writer = _BufferWriter()

    assert await service.serve(
        cast(asyncio.StreamWriter, writer),
        "/assets/app-AbCd1234.js",
    ) is True

    assert b"Cache-Control: public, max-age=31536000, immutable" in writer.data


@pytest.mark.asyncio
async def test_spa_static_service_uses_stable_javascript_mime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    JavaScript MIME 不应受操作系统类型映射影响

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "app-AbCd1234.js").write_text("export {}", encoding="utf-8")
    monkeypatch.setattr("satrap.core.backend.static_ui.mimetypes.guess_type", lambda _: ("text/plain", None))
    writer = _BufferWriter()

    assert await SPAStaticService(static_dir).serve(
        cast(asyncio.StreamWriter, writer),
        "/assets/app-AbCd1234.js",
    ) is True

    assert b"Content-Type: text/javascript" in writer.data
    assert b"X-Content-Type-Options: nosniff" in writer.data


@pytest.mark.asyncio
async def test_spa_static_service_negotiates_precompressed_assets(tmp_path: Path):
    """
    静态服务应优先协商 Brotli 并支持 gzip 回退

    参数:
    - tmp_path: 临时目录
    """
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    asset_path = assets_dir / "app-AbCd1234.js"
    original = b"window.satrap = true"
    brotli_content = b"brotli-content"
    gzip_content = gzip.compress(original)
    asset_path.write_bytes(original)
    Path(f"{asset_path}.br").write_bytes(brotli_content)
    Path(f"{asset_path}.gz").write_bytes(gzip_content)
    service = SPAStaticService(static_dir)

    brotli_writer = _BufferWriter()
    assert await service.serve(
        cast(asyncio.StreamWriter, brotli_writer),
        "/assets/app-AbCd1234.js",
        {"accept-encoding": "gzip, br"},
    ) is True
    assert b"Content-Encoding: br" in brotli_writer.data
    assert b"Vary: Accept-Encoding" in brotli_writer.data
    assert bytes(brotli_writer.data).endswith(brotli_content)

    gzip_writer = _BufferWriter()
    assert await service.serve(
        cast(asyncio.StreamWriter, gzip_writer),
        "/assets/app-AbCd1234.js",
        {"accept-encoding": "br;q=0, gzip"},
    ) is True
    assert b"Content-Encoding: gzip" in gzip_writer.data
    assert bytes(gzip_writer.data).endswith(gzip_content)

    plain_writer = _BufferWriter()
    assert await service.serve(
        cast(asyncio.StreamWriter, plain_writer),
        "/assets/app-AbCd1234.js",
    ) is True
    assert b"Content-Encoding:" not in plain_writer.data
    assert bytes(plain_writer.data).endswith(original)


@pytest.mark.asyncio
async def test_spa_static_service_rejects_api_and_directory_traversal(tmp_path: Path):
    """
    静态服务应交还 API 请求并拒绝目录越界

    参数:
    - tmp_path: 临时目录
    """
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("index", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    service = SPAStaticService(static_dir)

    api_writer = _BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, api_writer), "/api/health") is False
    assert api_writer.data == b""

    traversal_writer = _BufferWriter()
    assert await service.serve(cast(asyncio.StreamWriter, traversal_writer), "/%2e%2e/secret.txt") is True
    assert b"404 Error" in traversal_writer.data
    assert b"secret" not in traversal_writer.data


@pytest.mark.asyncio
async def test_backend_http_server_uses_shared_static_service(tmp_path: Path):
    """
    平台后端应通过共享静态服务托管 React

    参数:
    - tmp_path: 临时目录
    """
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("backend-ui", encoding="utf-8")
    server = BackendHTTPServer(BackendManager(), static_dir=static_dir)
    writer = _BufferWriter()

    handled = await server._serve_static(cast(asyncio.StreamWriter, writer), "/dashboard")

    assert handled is True
    assert b"backend-ui" in writer.data


@pytest.mark.asyncio
async def test_backend_http_server_exposes_ui_runtime_config():
    """平台后端应提供前端运行时服务地址并避免被 SPA 捕获"""
    backend = BackendManager()
    backend.config.api_host = "0.0.0.0"
    backend.config.api_port = 29970
    server = BackendHTTPServer(backend)

    writer = _BufferWriter()
    handled = await server._serve_static(cast(asyncio.StreamWriter, writer), "/ui-config.json")
    status, data = await server._route("GET", "/ui-config.json", b"")

    assert handled is False
    assert status == 200
    assert data["backend_api"] == "http://127.0.0.1:29970"
    assert data["control_api"] == "http://127.0.0.1:19871"
