"""
React SPA 静态文件托管

为控制服务和平台后端提供一致的静态资源, SPA 路由回退与路径安全检查
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit


DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent.parent.parent / "satrap-ui" / "dist"
"""Satrap React 前端默认构建产物目录"""


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

    async def serve(self, writer: asyncio.StreamWriter, path: str) -> bool:
        """
        服务静态文件或 SPA 入口

        参数:
        - writer: 流写入器
        - path: 请求路径

        返回:
        - bool: 是否已处理请求
        """
        route_path = unquote(urlsplit(path).path)
        if self._is_excluded(route_path):
            return False

        if route_path in {"", "/"}:
            self._send_index_html(writer)
            return True

        file_path = self._safe_file_path(route_path)
        if file_path is None:
            self._send_json_error(writer, 404, "file not found")
            return True
        if file_path.is_file():
            self._send_file(writer, file_path)
            return True

        self._send_index_html(writer)
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

    def _send_file(self, writer: asyncio.StreamWriter, file_path: Path) -> None:
        """
        发送静态文件

        参数:
        - writer: 流写入器
        - file_path: 文件路径
        """
        try:
            content = file_path.read_bytes()
        except FileNotFoundError:
            self._send_json_error(writer, 404, "file not found")
            return
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "application/octet-stream"
        header = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {mime_type}\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Cache-Control: public, max-age=31536000\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + content)

    def _send_index_html(self, writer: asyncio.StreamWriter) -> None:
        """
        发送 SPA 入口

        参数:
        - writer: 流写入器
        """
        index_path = self.static_dir / "index.html"
        if index_path.is_file():
            self._send_file(writer, index_path)
            return
        self._send_json_error(writer, 404, "frontend not built")

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
