"""可信插件图片产物登记接口, 不将文件路径或下载地址开放为模型工具参数"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import traceback

from satrap.core.call_context import current_call_origin, current_tool_workflow
from satrap.core.group_chat.assets import AssetStore, MAX_IMAGE_BYTES
from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.group_chat.service import group_chat_service
from satrap.core.group_chat.types import GroupChatError
from satrap.core.log import logger


async def register_group_chat_asset(payload: bytes | Path, mime_type: str | None, origin_ref: str) -> dict[str, Any]:
    """
    为当前主工具实际生成的图片登记同轮群权限

    参数:
    - payload: 可信插件提供的图片字节或受控产物 Path, 禁止直接透传模型路径
    - mime_type: 可选声明 MIME, 实际类型必须一致
    - origin_ref: 当前产物工具名, 必须在主工作流中启用

    返回:
    - 资产对象或已记录的明确失败, 不包含原始路径
    """
    try:
        turn = current_reply_turn()
        if turn is None or turn.origin is not current_call_origin() or current_tool_workflow() is not turn.workflow:
            raise GroupChatError("wrong_executor", "仅允许当前群主工具登记图片产物")
        turn.require_main_tool(origin_ref)
        context = await group_chat_service._resolve()
        if isinstance(payload, Path):
            if payload.is_symlink() or not payload.is_file() or payload.stat().st_size > MAX_IMAGE_BYTES:
                raise GroupChatError("invalid_media", "工具图片文件无效或超限")
            with payload.open("rb") as stream:
                payload = stream.read(MAX_IMAGE_BYTES + 1)
        result = await asyncio.to_thread(AssetStore(group_chat_service._store(context)).register,
                                        context.scope, payload, mime_type=mime_type, owner=turn.operation_owner)
        await group_chat_service._revalidate(context)
        turn.require_main_tool(origin_ref)
        return {"ok": True, "asset": result}
    except GroupChatError as exc:
        logger.warning(f"[群图片] 产物登记拒绝, 工具={origin_ref}, 原因={exc.code}")
        return {"ok": False, "error": {"code": exc.code, "message": str(exc), "retryable": False}}
    except Exception:
        logger.error(f"[群图片] 产物登记失败, 工具={origin_ref}: {traceback.format_exc()}")
        return {"ok": False, "error": {"code": "asset_unavailable", "message": "图片登记暂不可用", "retryable": True}}
