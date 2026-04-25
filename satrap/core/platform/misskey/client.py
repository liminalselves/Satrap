# satrap/core/platform/misskey/client.py
import asyncio
import json
import uuid
from typing import Any

import aiohttp

from satrap.core.log import logger


class MisskeyClient:
    """极简 Misskey API 客户端, 仅保留核心接口"""

    def __init__(self, instance_url: str, token: str):
        self.base_url = instance_url.rstrip("/")
        self.token = token
        self._session: aiohttp.ClientSession | None = None

    def _is_init(self) -> bool:
        """检查会话是否已初始化"""
        return True if self._session is not None else False

    async def __aenter__(self):
        """初始化会话"""
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        """关闭会话"""
        if self._session:
            await self._session.close()

    async def _post(self, endpoint: str, data: dict) -> dict | None:
        """通用请求: 自动附加 token + 基础重试"""
        url = f"{self.base_url}/api/{endpoint}"
        payload = {"i": self.token, **(data or {})}

        if not self._is_init():
            logger.error("[MisskeyClient] 会话未初始化, 无法发送请求")
            return None

        for attempt in range(3):
            try:
                async with self._session.post(url, json=payload) as resp:   # type: ignore
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)  # 指数退避

    # ── 核心接口 ──

    async def create_note(self, text: str, reply_id: str | None = None) -> dict | None:
        """发帖 / 回复"""
        payload = {"text": text}
        if reply_id:
            payload["replyId"] = reply_id
        return await self._post("notes/create", payload)

    async def get_user_by_username(self, username: str) -> dict | None:
        """通过 @用户名 获取用户信息"""
        try:
            return await self._post("users/show", {"username": username})
        except Exception:
            return None

    async def stream_connect(self, channel: str, callback):
        """WebSocket 订阅 + 事件回调 (简化版, 无重连)"""
        import websockets
        ws_url = self.base_url.replace("http", "ws") + f"/streaming?i={self.token}"

        async with websockets.connect(ws_url) as ws:
            # 订阅频道
            await ws.send(json.dumps({
                "type": "connect",
                "body": {"channel": channel, "id": uuid.uuid4().hex},
            }))

            async for raw in ws:
                try:
                    data = json.loads(raw)
                    if data.get("type") == "channel":
                        body = data.get("body", {})
                        body_body = body.get("body", {})
                        await callback(body_body)
                except Exception as e:
                    logger.error(f"[MisskeyClient] 解析消息失败: {e}")
