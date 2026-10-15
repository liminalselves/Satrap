"""
可信插件图片产物登记与沙箱图片读取

限定当前主工作流的群与轮次, 把图片复制到受管理缓存并返回不透明资产 ID
沙箱导入只读取宿主绑定目录内的相对路径, 不开放任意服务器文件或下载地址
"""
from __future__ import annotations

import traceback
from pathlib import PureWindowsPath, Path
import asyncio
from typing import Any
import stat
import os

from satrap.core.group_chat.service import group_chat_service
from satrap.core.group_chat.assets import AssetStore, MAX_IMAGE_BYTES
from satrap.core.group_chat.reply import current_reply_turn
from satrap.core.group_chat.types import GroupChatError
from satrap.core.call_context import current_call_origin, current_tool_workflow

from satrap.core.log import logger


def read_sandbox_image(root: Path, path: str) -> bytes:
    """
    有界读取当前会话沙箱内的普通图片文件, 不登记或发送

    参数:
    - root: 宿主绑定的会话沙箱根, 不接受模型配置或全局目录回退
    - path: 相对沙箱根的文件路径, 不允许绝对路径, 设备路径或上级目录

    返回:
    - 待交给资产登记器校验格式的原始字节; 路径越界或文件不可用时抛出 GroupChatError
    """
    # Step.1 拒绝远程地址, 绝对路径与平台相关的路径歧义
    relative = Path(path.replace("\\", "/"))
    if (not path or len(path) > 4096 or "\0" in path or ":" in path or relative.is_absolute()
            or path.startswith(("/", "\\")) or ".." in relative.parts):
        raise GroupChatError("invalid_argument", "请填写当前会话沙箱内的相对图片路径")
    if os.name == "nt":
        reserved = getattr(os.path, "isreserved", lambda part: PureWindowsPath(part).is_reserved())
        if any(reserved(part) or part.endswith((".", " ")) for part in relative.parts):
            raise GroupChatError("invalid_argument", "图片路径不能包含 Windows 设备名或含歧义的路径段")
    try:
        # Step.2 逐段拒绝链接和 Windows 重解析点, 不借用其它会话文件
        for target in (root, *[root.joinpath(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)]):
            metadata = target.lstat()
            if (stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise GroupChatError("invalid_argument", "图片路径不能包含符号链接或重解析点")
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root.resolve(strict=True)):
            raise GroupChatError("invalid_argument", "图片路径越出当前会话沙箱")
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_IMAGE_BYTES:
            raise GroupChatError("invalid_media", "图片必须是非空普通文件且不超过 10 MiB")

        # Step.3 只打开普通文件并限制实际读取大小, 不把模型路径直接交给登记 SDK
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(target, flags), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_IMAGE_BYTES:
                raise GroupChatError("invalid_media", "图片必须是非空普通文件且不超过 10 MiB")
            payload = stream.read(MAX_IMAGE_BYTES + 1)
        if not 0 < len(payload) <= MAX_IMAGE_BYTES:
            raise GroupChatError("invalid_media", "图片内容为空或超过 10 MiB")
        return payload
    except (OSError, ValueError) as exc:
        raise GroupChatError("asset_unavailable", "沙箱图片不存在或无法读取") from exc


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
        group_chat_service._capability(context, "image")
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
