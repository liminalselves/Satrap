"""
React SPA 静态文件托管

为控制服务和平台后端提供一致的静态资源, SPA 路由回退与路径安全检查
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit


DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent.parent.parent / "satrap-ui" / "dist"
"""Satrap React 前端默认构建产物目录"""

_HASHED_ASSET_PATTERN = re.compile(r"^.+-[A-Za-z0-9_-]{8,}\.[^.]+$")
_MIME_TYPE_OVERRIDES = {
    ".css": "text/css",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".json": "application/json",
    ".map": "application/json",
    ".mjs": "text/javascript",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


class SPAStaticService:
    """React SPA 静态文件服务"""

    def __init__(
        self,
        static_dir: str | Path,
        excluded_prefixes: tuple[str, ...] = ("/api/",),
    ) -> None:
        """
        初始化 React SPA 静态文件服务

        参数:
        - static_dir: 前端构建产物目录
        - excluded_prefixes: 不由静态服务处理的路径前缀
        """
        self.static_dir = Path(static_dir).resolve()
        self.excluded_prefixes = tuple(excluded_prefixes)

    async def serve(
        self,
        writer: asyncio.StreamWriter,
        path: str,
        request_headers: Mapping[str, str] | None = None,
    ) -> bool:
        """
        服务静态文件或 SPA 入口

        参数:
        - writer: 流写入器
        - path: 请求路径
        - request_headers: 小写键名的 HTTP 请求头

        返回:
        - bool: 是否已处理请求
        """
        route_path = unquote(urlsplit(path).path)
        if self._is_excluded(route_path):
            return False

        if route_path in {"", "/"}:
            self._send_index_html(writer, request_headers)
            return True

        file_path = self._safe_file_path(route_path)
        if file_path is None:
            self._send_json_error(writer, 404, "file not found")
            return True
        if file_path.is_file():
            self._send_file(writer, file_path, request_headers)
            return True

        self._send_index_html(writer, request_headers)
        return True

    def _is_excluded(self, route_path: str) -> bool:
        """
        判断路径是否应交还 API 路由

        参数:
        - route_path: 已解码的请求路径

        返回:
        - bool: 是否排除静态处理
        """
        return any(
            route_path == prefix.rstrip("/") or route_path.startswith(prefix)
            for prefix in self.excluded_prefixes
        )

    def _safe_file_path(self, route_path: str) -> Path | None:
        """
        解析并校验静态文件路径

        参数:
        - route_path: 已解码的请求路径

        返回:
        - Path | None: 位于静态目录内的路径, 越界时返回 None
        """
        candidate = (self.static_dir / route_path.lstrip("/")).resolve()
        try:
            candidate.relative_to(self.static_dir)
        except ValueError:
            return None
        return candidate

    def _send_file(
        self,
        writer: asyncio.StreamWriter,
        file_path: Path,
        request_headers: Mapping[str, str] | None = None,
    ) -> None:
        """
        发送静态文件

        参数:
        - writer: 流写入器
        - file_path: 文件路径
        - request_headers: 小写键名的 HTTP 请求头
        """
        content_path, content_encoding = self._select_content_path(
            file_path,
            (request_headers or {}).get("accept-encoding", ""),
        )
        try:
            content = content_path.read_bytes()
        except FileNotFoundError:
            self._send_json_error(writer, 404, "file not found")
            return
        mime_type = self._content_type(file_path)
        encoding_header = (
            f"Content-Encoding: {content_encoding}\r\n" if content_encoding else ""
        )
        vary_header = (
            "Vary: Accept-Encoding\r\n"
            if self._has_compressed_variant(file_path)
            else ""
        )
        header = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {mime_type}\r\n"
            f"Content-Length: {len(content)}\r\n"
            f"Cache-Control: {self._cache_control(file_path)}\r\n"
            f"{encoding_header}"
            f"{vary_header}"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(header + content)

    def _send_index_html(
        self,
        writer: asyncio.StreamWriter,
        request_headers: Mapping[str, str] | None = None,
    ) -> None:
        """
        发送 SPA 入口

        参数:
        - writer: 流写入器
        - request_headers: 小写键名的 HTTP 请求头
        """
        index_path = self.static_dir / "index.html"
        if index_path.is_file():
            self._send_file(writer, index_path, request_headers)
            return
        self._send_json_error(writer, 404, "frontend not built")

    def _cache_control(self, file_path: Path) -> str:
        """
        根据文件路径生成缓存策略

        参数:
        - file_path: 原始静态文件路径

        返回:
        - str: Cache-Control 响应头值
        """
        try:
            relative_path = file_path.relative_to(self.static_dir)
        except ValueError:
            return "no-cache"
        if (
            len(relative_path.parts) > 1
            and relative_path.parts[0] == "assets"
            and _HASHED_ASSET_PATTERN.fullmatch(file_path.name)
        ):
            return "public, max-age=31536000, immutable"
        return "no-cache"

    @staticmethod
    def _content_type(file_path: Path) -> str:
        """
        获取稳定的静态文件 MIME 类型

        参数:
        - file_path: 原始静态文件路径

        返回:
        - str: MIME 类型
        """
        suffix = file_path.suffix.lower()
        if suffix in _MIME_TYPE_OVERRIDES:
            return _MIME_TYPE_OVERRIDES[suffix]
        mime_type, _ = mimetypes.guess_type(str(file_path))
        return mime_type or "application/octet-stream"

    @staticmethod
    def _accepted_encodings(header_value: str) -> set[str]:
        """
        解析可用且质量大于零的内容编码

        参数:
        - header_value: Accept-Encoding 请求头值

        返回:
        - set[str]: 可接受的编码名称
        """
        accepted: set[str] = set()
        for item in header_value.lower().split(","):
            parts = [part.strip() for part in item.split(";") if part.strip()]
            if not parts:
                continue
            quality = 1.0
            for parameter in parts[1:]:
                if parameter.startswith("q="):
                    try:
                        quality = float(parameter[2:])
                    except ValueError:
                        quality = 0.0
            if quality > 0:
                accepted.add(parts[0])
        return accepted

    @classmethod
    def _select_content_path(cls, file_path: Path, accept_encoding: str) -> tuple[Path, str | None]:
        """
        选择客户端支持的预压缩文件

        参数:
        - file_path: 原始静态文件路径
        - accept_encoding: Accept-Encoding 请求头值

        返回:
        - tuple[Path, str | None]: 实际文件路径与内容编码
        """
        accepted = cls._accepted_encodings(accept_encoding)
        for encoding in ("br", "gzip"):
            accepted_name = "gzip" if encoding == "gzip" else encoding
            sidecar_suffix = "gz" if encoding == "gzip" else encoding
            compressed_path = Path(f"{file_path}.{sidecar_suffix}")
            if accepted_name in accepted and compressed_path.is_file():
                return compressed_path, accepted_name
        return file_path, None

    @staticmethod
    def _has_compressed_variant(file_path: Path) -> bool:
        """
        判断文件是否存在预压缩版本

        参数:
        - file_path: 原始静态文件路径

        返回:
        - bool: 是否存在 gzip 或 Brotli 文件
        """
        return Path(f"{file_path}.gz").is_file() or Path(f"{file_path}.br").is_file()

    @staticmethod
    def _send_json_error(
        writer: asyncio.StreamWriter,
        status: int,
        message: str,
    ) -> None:
        """
        发送 JSON 错误响应

        参数:
        - writer: 流写入器
        - status: HTTP 状态码
        - message: 错误消息
        """
        content = json.dumps({"error": message}, ensure_ascii=False).encode()
        header = (
            f"HTTP/1.1 {status} Error\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + content)
