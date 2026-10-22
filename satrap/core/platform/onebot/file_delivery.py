"""通过实现端分块上传传输普通文件, 避免跨容器使用 Satrap 本机路径"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import TYPE_CHECKING
import uuid
import traceback
from satrap.core.log import logger

from satrap.core.call_context import CallOrigin
from satrap.core.file_access.artifacts import CHUNK_BYTES, FileArtifact, open_artifact
from satrap.core.platform.onebot.onebot_utils import group_session_id, private_session_id
from satrap.core.platform.onebot.admin import is_missing_action_error
from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable

if TYPE_CHECKING:
    from satrap.core.platform.onebot.adapter import OneBotAdapter


async def deliver(adapter: OneBotAdapter, origin: CallOrigin, artifact: FileArtifact, check: Callable[[], None]) -> dict[str, object]:
    """
    分块上传后复用群或私聊文件接口, 只有平台确认才返回 uploaded

    参数:
    - adapter: 当前 OneBot 实例
    - origin: 真实来源
    - artifact: 已冻结文件
    - check: 插件执行及环境的复核回调

    返回:
    - 上传成功, 明确失败或无法确认的结果, 未知结果不重试
    """
    scope = await adapter.file_delivery_scope(origin)
    bot = adapter._bot
    if bot is None:
        return {"ok": False, "status": "failed", "reason": "client_unavailable"}
    connection = adapter.group_chat_connection_token()
    stream_id = uuid.uuid4().hex
    submitted = False

    async def valid() -> None:
        """每块及正式发送前重新核验目标和连接"""
        check()
        if adapter._bot is not bot or adapter.group_chat_connection_token() != connection:
            raise PermissionError("文件传输期间平台连接已经变化")
        if await adapter.file_delivery_scope(origin) != scope:
            raise PermissionError("文件传输期间目标已经变化")

    try:
        async with asyncio.timeout(150):
            stream = await open_artifact(artifact)
            try:
                total = max(1, (artifact.size + CHUNK_BYTES - 1) // CHUNK_BYTES)
                for index in range(total):
                    await valid()
                    data = await asyncio.to_thread(stream.read, CHUNK_BYTES)
                    encoded = base64.b64encode(data).decode("ascii") or "\n"
                    # 实现端以字符串真值判断是否有分块, 空文件用合法 Base64 空白表示零字节块
                    result = await bot.upload_file_stream(stream_id=stream_id, chunk_data=encoded,
                                                          chunk_index=index, total_chunks=total, file_size=artifact.size,
                                                          expected_sha256=artifact.sha256, filename=artifact.file_id, file_retention=300000)
                    if not isinstance(result, dict) or result.get("status") != "chunk_received" or result.get("received_chunks") != index + 1:
                        return {"ok": False, "status": "failed", "reason": "file_stream_unconfirmed"}
                await valid()
                complete = await bot.upload_file_stream(stream_id=stream_id, is_complete=True)
            finally:
                stream.close()
            if (not isinstance(complete, dict) or complete.get("status") != "file_complete"
                    or complete.get("file_size") != artifact.size or complete.get("sha256") != artifact.sha256
                    or not isinstance(complete.get("file_path"), str) or not complete["file_path"]):
                return {"ok": False, "status": "failed", "reason": "file_stream_verification_failed"}
            await valid()
            target = group_session_id(scope.chat_id) if scope.conversation_kind == "group" else private_session_id(scope.chat_id)
            submitted = True
            receipt = await adapter._send_uploaded_file(target, complete["file_path"], artifact.name)
            if receipt.status != "success":
                return {"ok": False, "status": receipt.status, "reason": receipt.reason}
            return {"ok": True, "status": "uploaded", "file": artifact.public(), "platform_file_id": receipt.message_ids[0]}
    except (ActionFailed, ApiNotAvailable) as error:
        return {"ok": False, "status": "failed", "reason": "file_upload_unsupported" if is_missing_action_error(error) else "file_upload_rejected"}
    except asyncio.CancelledError:
        raise
    except PermissionError:
        return {"ok": False, "status": "failed", "reason": "file_source_stale"}
    except Exception:
        logger.error(f"[OneBot 文件交付] 传输异常, 已提交={submitted}: {traceback.format_exc()}")
        return {"ok": False, "status": "unknown" if submitted else "failed", "reason": "file_upload_unconfirmed" if submitted else "file_transfer_failed"}
