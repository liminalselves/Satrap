from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import aiohttp

from satrap.core.log import logger
from satrap.core.platform.misskey.misskey_utils import FileIDExtractor


API_MAX_RETRIES = 5
"""Misskey API 最大重试次数"""
HTTP_OK = 200
"""Misskey API 成功状态码"""



class APIError(Exception):
    """Misskey API 基础异常"""


class APIConnectionError(APIError):
    """Misskey API 网络连接异常"""


class APIRateLimitError(APIError):
    """Misskey API 频率限制异常"""


class AuthenticationError(APIError):
    """Misskey API 认证异常"""


class WebSocketError(APIError):
    """Misskey WebSocket 异常"""


class StreamingClient:
    """Misskey WebSocket 流式客户端"""

    def __init__(self, instance_url: str, access_token: str) -> None:
        """初始化 StreamingClient
        
        参数:
        - instance_url: Misskey 实例 URL, 包含协议和端口
        - access_token: 访问令牌, 用于认证 WebSocket 连接
        """
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.websocket: Any | None = None
        self.is_connected = False
        self.message_handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        self.channels: dict[str, str] = {}
        self.desired_channels: dict[str, dict[str, Any] | None] = {}
        self._running = False

    async def connect(self) -> bool:
        """连接 Misskey streaming 端点"""
        try:
            import websockets  # type: ignore[import-not-found]

            ws_url = self.instance_url.replace("https://", "wss://").replace("http://", "ws://")
            ws_url += f"/streaming?i={self.access_token}"
            self.websocket = await websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
            )
            self.is_connected = True
            self._running = True
            for channel_type, params in list(self.desired_channels.items()):
                await self.subscribe_channel(channel_type, params)
            logger.info("[Misskey WebSocket] 已连接")
            return True
        except Exception as e:
            self.is_connected = False
            logger.error(f"[Misskey WebSocket] 连接失败: {e}")
            return False

    async def disconnect(self) -> None:
        """断开 WebSocket 连接"""
        self._running = False
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        self.is_connected = False
        logger.info("[Misskey WebSocket] 连接已断开")

    async def subscribe_channel(
        self,
        channel_type: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """订阅 Misskey streaming 频道
        
        参数:
        - channel_type: 频道类型, 可选值: "chat", "room", "note"
        - params: 频道参数, 可选
        """
        self.desired_channels[channel_type] = params
        if not self.is_connected or not self.websocket:
            raise WebSocketError("WebSocket 未连接")

        channel_id = uuid.uuid4().hex
        message = {
            "type": "connect",
            "body": {
                "channel": channel_type,
                "id": channel_id,
                "params": params or {},
            },
        }
        await self.websocket.send(json.dumps(message))
        self.channels[channel_id] = channel_type
        return channel_id

    async def unsubscribe_channel(self, channel_id: str) -> None:
        """取消订阅 Misskey streaming 频道
        
        参数:
        - channel_id: 要取消订阅的频道 ID
        """
        if not self.is_connected or not self.websocket or channel_id not in self.channels:
            return
        await self.websocket.send(json.dumps({"type": "disconnect", "body": {"id": channel_id}}))
        channel_type = self.channels.pop(channel_id, None)
        if channel_type and channel_type not in self.channels.values():
            self.desired_channels.pop(channel_type, None)

    def add_message_handler(
        self,
        event_type: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """注册 streaming 消息处理函数
        
        参数:
        - event_type: 事件类型, 可选值: "chat", "room", "note"
        - handler: 处理函数, 接收一个字典参数, 返回一个可等待对象
        """
        self.message_handlers[event_type] = handler

    async def listen(self) -> None:
        """持续监听 streaming 消息并分发给处理函数"""
        if not self.is_connected or not self.websocket:
            raise WebSocketError("WebSocket 未连接")

        try:
            async for message in self.websocket:
                if not self._running:
                    break
                try:
                    await self._handle_message(json.loads(message))
                except json.JSONDecodeError as e:
                    logger.warning(f"[Misskey WebSocket] 无法解析消息: {e}")
                except Exception as e:
                    logger.error(f"[Misskey WebSocket] 处理消息失败: {e}")
        except Exception as e:
            logger.warning(f"[Misskey WebSocket] 监听中断: {e}")
        finally:
            self.is_connected = False
            try:
                await self.disconnect()
            except Exception:
                pass

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """分发一条 streaming 消息
        
        参数:
        - data: 包含消息类型和体的字典
        """
        raw_message_type = data.get("type")
        message_type = raw_message_type if isinstance(raw_message_type, str) else ""
        body = data.get("body", {})

        if message_type == "channel" and isinstance(body, dict):
            raw_channel_id = body.get("id")
            raw_event_type = body.get("type")
            channel_id = raw_channel_id if isinstance(raw_channel_id, str) else ""
            event_type = raw_event_type if isinstance(raw_event_type, str) else ""
            event_body = body.get("body", {})
            channel_type = self.channels.get(channel_id, "")
            handler_key = f"{channel_type}:{event_type}" if channel_type and event_type else ""

            if handler_key in self.message_handlers:
                await self.message_handlers[handler_key](event_body)
                return
            if event_type in self.message_handlers:
                await self.message_handlers[event_type](event_body)
                return
            if "_debug" in self.message_handlers:
                await self.message_handlers["_debug"](
                    {"type": event_type, "body": event_body, "channel": channel_type}
                )
            return

        if message_type in self.message_handlers:
            await self.message_handlers[message_type](body)
        elif "_debug" in self.message_handlers:
            await self.message_handlers["_debug"](data)


def retry_async(
    max_retries: int = API_MAX_RETRIES,
    retryable_exceptions: tuple[type[Exception], ...] = (
        APIConnectionError,
        APIRateLimitError,
    ),
    backoff_base: float = 1.0,
    max_backoff: float = 30.0,
):
    """异步重试装饰器
    
    参数:
    - max_retries: 最大重试次数, 默认 5
    - retryable_exceptions: 可重试的异常类型, 默认 APIConnectionError 和 APIRateLimitError
    - backoff_base: 基础退避时间, 默认 1.0 秒
    - max_backoff: 最大退避时间, 默认 30.0 秒
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            func_name = getattr(func, "__name__", "unknown")
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exc = e
                    if attempt == max_retries:
                        logger.error(f"[Misskey API] {func_name} 重试 {max_retries} 次后仍失败: {e}")
                        break
                    if isinstance(e, APIRateLimitError):
                        backoff = min(backoff_base * (3**attempt), max_backoff)
                    else:
                        backoff = min(backoff_base * (2**attempt), max_backoff)
                    sleep_time = backoff + random.uniform(0.1, 0.5)
                    logger.warning(
                        f"[Misskey API] {func_name} 第 {attempt} 次失败: {e}, "
                        f"{sleep_time:.1f}s 后重试"
                    )
                    await asyncio.sleep(sleep_time)
                except Exception:
                    raise
            if last_exc:
                raise last_exc
            return None

        return wrapper

    return decorator


class MisskeyAPI:
    """Misskey HTTP API 与 streaming 客户端聚合"""

    def __init__(
        self,
        instance_url: str,
        access_token: str,
        *,
        allow_insecure_downloads: bool = False,
        download_timeout: int = 15,
        chunk_size: int = 64 * 1024,
        max_download_bytes: int | None = None,
    ) -> None:
        """初始化 Misskey API 客户端
        
        参数:
        - instance_url: Misskey 实例 URL, 以斜杠结尾
        - access_token: 访问令牌, 用于认证
        - allow_insecure_downloads: 是否允许不安全的下载, 默认 False
        - download_timeout: 下载超时时间, 默认 15 秒
        - chunk_size: 下载分块大小, 默认 64KB
        - max_download_bytes: 最大下载字节数, 可选
        """
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.allow_insecure_downloads = allow_insecure_downloads
        self.download_timeout = download_timeout
        self.chunk_size = chunk_size
        self.max_download_bytes = int(max_download_bytes) if max_download_bytes is not None else None
        self._session: aiohttp.ClientSession | None = None
        self.streaming: StreamingClient | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出时调用"""
        await self.close()
        return False

    @property
    def session(self) -> aiohttp.ClientSession:
        """获取或创建 aiohttp 会话"""
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self) -> None:
        """关闭 HTTP 与 WebSocket 资源"""
        if self.streaming:
            await self.streaming.disconnect()
            self.streaming = None
        if self._session:
            await self._session.close()
            self._session = None
        logger.debug("[Misskey API] 客户端已关闭")

    def get_streaming_client(self) -> StreamingClient:
        """获取 streaming 客户端"""
        if not self.streaming:
            self.streaming = StreamingClient(self.instance_url, self.access_token)
        return self.streaming

    def _handle_response_status(self, status: int, endpoint: str) -> NoReturn:
        """处理 Misskey API HTTP 状态码
        
        参数:
        - status: HTTP 状态码
        - endpoint: API 路径
        """
        if status in (401, 403):
            raise AuthenticationError(f"Unauthorized access for {endpoint}")
        if status == 429:
            raise APIRateLimitError(f"Rate limit exceeded for {endpoint}")
        if status in (500, 502, 503, 504):
            raise APIConnectionError(f"HTTP {status} for {endpoint}")
        raise APIError(f"HTTP {status} for {endpoint}")

    async def _process_response(self, response: aiohttp.ClientResponse, endpoint: str) -> Any:
        """处理 API 响应"""
        if response.status == HTTP_OK:
            try:
                return await response.json()
            except json.JSONDecodeError as e:
                raise APIConnectionError("Invalid JSON response") from e
        try:
            error_text = await response.text()
            logger.error(f"[Misskey API] 请求失败: {endpoint} - HTTP {response.status}, 响应: {error_text}")
        except Exception:
            logger.error(f"[Misskey API] 请求失败: {endpoint} - HTTP {response.status}")
        self._handle_response_status(response.status, endpoint)

    @retry_async()
    async def _make_request(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        """向 Misskey API 发起 POST 请求
        
        参数:
        - endpoint: API 路径
        - data: 请求体, 可选
        """
        url = f"{self.instance_url}/api/{endpoint}"
        payload = {"i": self.access_token}
        if data:
            payload.update(data)
        try:
            async with self.session.post(url, json=payload) as response:
                return await self._process_response(response, endpoint)
        except aiohttp.ClientError as e:
            raise APIConnectionError(f"HTTP request failed: {e}") from e

    async def create_note(
        self,
        text: str | None = None,
        visibility: str = "public",
        reply_id: str | None = None,
        visible_user_ids: list[str] | None = None,
        file_ids: list[str] | None = None,
        local_only: bool = False,
        cw: str | None = None,
        poll: dict[str, Any] | None = None,
        renote_id: str | None = None,
        channel_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """创建 Misskey note
        
        参数:
        - text: note 内容, 可选
        - visibility: 可见性, 默认 "public"
        - reply_id: 回复 note ID, 可选
        - visible_user_ids: 可见用户 ID 列表, 可选
        - file_ids: 文件 ID 列表, 可选
        - local_only: 是否仅显示本地 note, 默认 False
        - cw: 是否为敏感内容, 可选
        - poll: 投票配置, 可选
        - renote_id: 转发 note ID, 可选
        - channel_id: 通道 ID, 可选
        - extra: 其他参数, 可选
        """
        payload: dict[str, Any] = {"visibility": visibility, "localOnly": local_only}
        if text is not None:
            payload["text"] = text
        if reply_id:
            payload["replyId"] = reply_id
        if visible_user_ids and visibility == "specified":
            payload["visibleUserIds"] = visible_user_ids
        if file_ids:
            payload["fileIds"] = file_ids
        if cw is not None:
            payload["cw"] = cw
        if poll is not None:
            payload["poll"] = poll
        if renote_id is not None:
            payload["renoteId"] = renote_id
        if channel_id is not None:
            payload["channelId"] = channel_id
        payload.update({k: v for k, v in extra.items() if v is not None})
        return await self._make_request("notes/create", payload)

    async def get_current_user(self) -> dict[str, Any]:
        """获取当前账号信息"""
        return await self._make_request("i", {})

    async def send_message(self, user_id_or_payload: Any, text: str | None = None) -> dict[str, Any]:
        """发送 Misskey 私聊消息
        
        参数:
        - user_id_or_payload: 目标用户 ID 或消息 payload
        - text: 消息内容, 可选
        """
        if isinstance(user_id_or_payload, dict):
            payload = user_id_or_payload
        else:
            payload = {"toUserId": user_id_or_payload, "text": text}
        return await self._make_request("chat/messages/create-to-user", payload)

    async def send_room_message(self, room_id_or_payload: Any, text: str | None = None) -> dict[str, Any]:
        """发送 Misskey 房间消息
        
        参数:
        - room_id_or_payload: 目标房间 ID 或消息 payload
        - text: 消息内容, 可选
        """
        if isinstance(room_id_or_payload, dict):
            payload = room_id_or_payload
        else:
            payload = {"toRoomId": room_id_or_payload, "text": text}
        return await self._make_request("chat/messages/create-to-room", payload)

    async def get_mentions(self, limit: int = 10, since_id: str | None = None) -> list[dict[str, Any]]:
        """获取提及通知
        
        参数:
        - limit: 最大返回数量, 默认 10
        - since_id: 从指定 ID 开始返回, 可选
        """
        payload: dict[str, Any] = {"limit": limit, "includeTypes": ["mention", "reply", "quote"]}
        if since_id:
            payload["sinceId"] = since_id
        result = await self._make_request("i/notifications", payload)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("notifications", [])
        return []

    async def upload_file(
        self,
        file_path: str,
        name: str | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """上传本地文件到 Misskey Drive
        
        参数:
        - file_path: 本地文件路径
        - name: 文件名, 可选
        - folder_id: 目标文件夹 ID, 可选
        """
        if not file_path:
            raise APIError("No file path provided for upload")
        url = f"{self.instance_url}/api/drive/files/create"
        form = aiohttp.FormData()
        form.add_field("i", self.access_token)
        if folder_id:
            form.add_field("folderId", str(folder_id))
        filename = name or os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                form.add_field("file", f, filename=filename)
                async with self.session.post(url, data=form) as response:
                    result = await self._process_response(response, "drive/files/create")
        except FileNotFoundError as e:
            raise APIError(f"File not found: {file_path}") from e
        except aiohttp.ClientError as e:
            raise APIConnectionError(f"Upload failed: {e}") from e

        file_id = FileIDExtractor.extract_file_id(result)
        return {"id": file_id, "raw": result}

    async def find_files_by_hash(self, md5_hash: str) -> list[dict[str, Any]]:
        """按 MD5 查询 Drive 文件"""
        result = await self._make_request("drive/files/find-by-hash", {"md5": md5_hash})
        return result if isinstance(result, list) else []

    async def find_files_by_name(
        self,
        name: str,
        folder_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按文件名查询 Drive 文件
        
        参数:
        - name: 文件名
        - folder_id: 目标文件夹 ID, 可选
        """
        payload: dict[str, Any] = {"name": name}
        if folder_id:
            payload["folderId"] = folder_id
        result = await self._make_request("drive/files/find", payload)
        return result if isinstance(result, list) else []

    async def find_files(
        self,
        limit: int = 10,
        folder_id: str | None = None,
        type: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出 Drive 文件
        
        参数:
        - limit: 最大返回数量, 默认 10
        - folder_id: 目标文件夹 ID, 可选
        - type: 文件类型, 可选
        """
        payload: dict[str, Any] = {"limit": limit}
        if folder_id is not None:
            payload["folderId"] = folder_id
        if type is not None:
            payload["type"] = type
        result = await self._make_request("drive/files", payload)
        return result if isinstance(result, list) else []

    async def _download_bytes(self, url: str, ssl_verify: bool = True) -> bytes:
        """下载远程文件字节
        
        参数:
        - url: 远程文件 URL
        - ssl_verify: 是否验证 SSL 证书, 默认 True
        """
        timeout = aiohttp.ClientTimeout(total=self.download_timeout)
        connector = None if ssl_verify else aiohttp.TCPConnector(ssl=False)
        session_cm = aiohttp.ClientSession(connector=connector, timeout=timeout)
        async with session_cm as session:
            async with session.get(url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(self.chunk_size):
                    total += len(chunk)
                    if self.max_download_bytes is not None and total > self.max_download_bytes:
                        raise APIError("Downloaded file exceeds max_download_bytes")
                    chunks.append(chunk)
                return b"".join(chunks)

    async def upload_and_find_file(
        self,
        url: str,
        name: str | None = None,
        folder_id: str | None = None,
        *_,
        **__,
    ) -> dict[str, Any] | None:
        """下载远程 URL 后上传到 Misskey Drive
        
        参数:
        - url: 远程文件 URL
        - name: 上传后的文件名, 可选
        - folder_id: 上传到指定文件夹 ID, 可选
        """
        try:
            try:
                data = await self._download_bytes(url, ssl_verify=True)
            except Exception:
                if not self.allow_insecure_downloads:
                    raise
                data = await self._download_bytes(url, ssl_verify=False)

            suffix = os.path.splitext(name or url.split("?", 1)[0])[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                return await self.upload_file(tmp_path, name, folder_id)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            logger.error(f"[Misskey API] URL 文件上传失败: {e}")
            return None

    async def send_message_with_media(
        self,
        message_type: str,
        target_id: str,
        text: str | None = None,
        media_urls: list[str] | None = None,
        local_files: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """统一发送文本和媒体消息
        
        参数:
        - message_type: 消息类型, 可选值: "chat", "room", "note"
        - target_id: 目标用户 ID 或房间 ID
        - text: 文本内容, 可选
        - media_urls: 媒体 URL 列表, 可选
        - local_files: 本地文件路径列表, 可选
        - kwargs: 其他参数, 可选
        """
        file_ids: list[str] = []
        for url in media_urls or []:
            result = await self.upload_and_find_file(url)
            if result and result.get("id"):
                file_ids.append(str(result["id"]))
        for path in local_files or []:
            result = await self.upload_file(path)
            if result and result.get("id"):
                file_ids.append(str(result["id"]))
        return await self._dispatch_message(message_type, target_id, text, file_ids, **kwargs)

    async def _dispatch_message(
        self,
        message_type: str,
        target_id: str,
        text: str | None,
        file_ids: list[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """按目标类型分发消息
        
        参数:
        - message_type: 消息类型, 可选值: "chat", "room", "note"
        - target_id: 目标用户 ID 或房间 ID
        - text: 文本内容, 可选
        - file_ids: 文件 ID 列表, 可选
        - kwargs: 其他参数, 可选
        """
        if message_type == "chat":
            payload: dict[str, Any] = {"toUserId": target_id}
            if text:
                payload["text"] = text
            if file_ids:
                payload["fileId"] = file_ids[0]
            return await self.send_message(payload)
        if message_type == "room":
            payload = {"toRoomId": target_id}
            if text:
                payload["text"] = text
            if file_ids:
                payload["fileId"] = file_ids[0]
            return await self.send_room_message(payload)
        if message_type == "note":
            return await self.create_note(text=text, file_ids=file_ids or None, **kwargs)
        raise APIError(f"不支持的消息类型: {message_type}")


MisskeyClient = MisskeyAPI
