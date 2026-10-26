"""Misskey 普通文件必须上传并发布到原始对话, 不以文字兜底冒充文件送达"""
from __future__ import annotations

import asyncio
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING

from satrap.core.call_context import CallOrigin
from satrap.core.file_access.artifacts import FileArtifact, open_artifact
from satrap.core.platform.misskey.client import APIConnectionError, APIError
from satrap.core.platform.misskey.misskey_utils import resolve_message_visibility
from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.platform.misskey.adapter import MisskeyAdapter


async def deliver(adapter: MisskeyAdapter, origin: CallOrigin, artifact: FileArtifact, check: Callable[[], None]) -> dict[str, object]:
    """
    单次上传 Drive 后发布附件, 分开记录上传与对话发布结果

    参数:
    - adapter: 当前平台实例
    - origin: 宿主固定来源
    - artifact: 受控文件
    - check: 外部副作用前的宿主复核

    返回:
    - published 才表示文件已经发布到对话, unknown 不可自动重试
    """
    scope = await adapter.file_delivery_scope(origin)
    client = adapter._client
    if client is None or not adapter.enable_file_upload:
        return {"ok": False, "status": "failed", "reason": "file_upload_disabled"}
    if scope.conversation_kind == "group" and not adapter.enable_room or scope.conversation_kind == "private" and not adapter.enable_chat:
        return {"ok": False, "status": "failed", "reason": "conversation_disabled"}
    if artifact.size > 10 * 1024 * 1024:
        return {"ok": False, "status": "failed", "reason": "file_exceeds_platform_limit"}
    uploaded: str | None = None
    submitted = False

    async def valid() -> None:
        """上传和发布之间复核账号及真实对话"""
        check()
        if adapter._client is not client or await adapter.file_delivery_scope(origin) != scope:
            raise PermissionError("文件交付期间平台或对话已经变化")

    try:
        async with asyncio.timeout(150):
            await valid()
            stream = await open_artifact(artifact)
            try:
                submitted = True
                result = await client.upload_stream(stream, artifact.name, adapter.upload_folder)
            finally:
                stream.close()
            if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
                return {"ok": False, "status": "unknown", "reason": "file_upload_unconfirmed"}
            uploaded = result["id"]
            await valid()
            if scope.conversation_kind in {"group", "private"}:
                endpoint = "chat/messages/create-to-room" if scope.conversation_kind == "group" else "chat/messages/create-to-user"
                field = "toRoomId" if scope.conversation_kind == "group" else "toUserId"
                payload = {field: scope.chat_id, "text": "", "fileId": uploaded}
            else:
                endpoint = "notes/create"
                visibility, users = resolve_message_visibility(user_id=origin.actor_id, user_cache=adapter._user_cache,
                                                               self_id=origin.self_id, default_visibility=adapter.default_visibility)
                payload = {"fileIds": [uploaded], "replyId": origin.source_message_id, "visibility": visibility, "localOnly": adapter.local_only}
                if visibility == "specified":
                    payload["visibleUserIds"] = users
            result = await client.request_once(endpoint, payload)
            note = result.get("createdNote", result) if isinstance(result, dict) else {}
            message_id = note.get("id") if isinstance(note, dict) else None
            if not isinstance(message_id, str) or not message_id:
                return {"ok": False, "status": "unknown", "reason": "file_publish_unconfirmed", "platform_file_id": uploaded}
            return {"ok": True, "status": "published", "file": artifact.public(), "platform_file_id": uploaded, "message_id": message_id}
    except (APIConnectionError, TimeoutError):
        return {"ok": False, "status": "unknown" if submitted else "failed", "reason": "file_delivery_unconfirmed", "platform_file_id": uploaded}
    except APIError as error:
        reason = "file_upload_rejected" if uploaded is None else "file_publish_rejected"
        return {"ok": False, "status": "failed", "reason": reason, "platform_file_id": uploaded,
                "error": {"code": reason, "message": str(error), "platform_code": error.code,
                          "stage": "upload" if uploaded is None else "publish", "retryable": False}}
    except PermissionError:
        return {"ok": False, "status": "failed", "reason": "file_source_stale", "platform_file_id": uploaded}
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error(f"[Misskey 文件交付] 非预期传输异常: {traceback.format_exc()}")
        return {"ok": False, "status": "unknown" if submitted else "failed", "reason": "file_delivery_unconfirmed", "platform_file_id": uploaded}
