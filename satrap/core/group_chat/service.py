"""
当前群的消息读取与结构化回复宿主

固定可信轮次来源, 在等待后核验平台, 账号和路由;
验证全部回复组件后暂存草稿, 提交前再次核验引用与成员
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar
import asyncio
import base64
import hashlib
import json
import math
import time
import uuid
import traceback

from satrap.core.call_context import CallOrigin, current_call_origin, require_call_origin
from satrap.core.config.platform_messages import MessageArchiveError, MessageScope, PlatformMessageStore
from satrap.core.group_chat.types import GroupChatError, GroupChatLimits, MemberRecord, MemberSnapshot, VerifiedMember, VerifiedMessage, GroupRecord, GroupSnapshot, VerifiedGroup
from satrap.core.group_chat.summaries import SummaryStore, SummaryLimits
from satrap.core.platform import PlatformAdapter, current_adapter_manager
from satrap.core.components import At, Plain, Reply, Image, BaseMessageComponent
from satrap.core.group_chat.assets import AssetStore, AssetLease, MAX_REPLY_BYTES, MAX_IMAGE_BYTES
from satrap.core.group_chat.stickers import library_for
from satrap.core.platform.event import MessageChain
from satrap.core.log import logger


T = TypeVar("T")
_ARGUMENTS = {
    "group_chat_list_groups": frozenset(),
    "group_chat_get_group_info": frozenset(),
    "group_chat_list_members": frozenset({"limit", "cursor"}),
    "group_chat_reply": frozenset({"components"}),
    "group_chat_find_members": frozenset({"query", "limit", "cursor"}),
    "group_chat_get_member": frozenset({"user_id"}),
    "group_chat_get_message": frozenset({"message_id"}),
    "group_chat_recent_messages": frozenset({"limit", "before_message_id", "cursor"}),
    "group_chat_search_messages": frozenset({"keyword", "sender_id", "start_time", "end_time", "limit", "cursor"}),
    "group_chat_prepare_summary": frozenset({"start_time", "end_time", "keyword", "include_bot"}),
    "group_chat_read_summary_sources": frozenset({"snapshot_id", "cursor"}),
    "group_chat_save_summary": frozenset({"snapshot_id", "title", "points"}),
    "group_chat_get_summary": frozenset({"summary_id"}),
    "group_chat_list_summaries": frozenset({"keyword", "limit", "cursor"}),
    "group_chat_get_message_assets": frozenset({"message_id"}),
    "group_chat_list_stickers": frozenset({"keyword", "limit", "cursor"}),
}


def _id(value: object) -> str:
    """
    校验平台无关的字符串 ID, 不将数字或布尔值隐式转换为身份

    参数:
    - value: 模型参数或适配器返回的身份字段

    返回:
    - 长度有限的原始 ID, 无效值抛出 ValueError
    """
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("ID 必须是长度不超过 256 的非空字符串")
    return value


def _limit(value: object, maximum: int) -> int:
    """
    严格校验模型请求的条数

    参数:
    - value: 请求条数
    - maximum: 宿主或插件配置的上限

    返回:
    - 有效条数, 非整数或超限时抛出 ValueError
    """
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"查询条数必须是 1 到 {maximum} 的整数")
    return value


def _query_time(value: object) -> str | None:
    """
    为未指定时区的模型查询时间补上后端本地时区

    参数:
    - value: 日期时间字符串, 可带时区; None 表示不限制该时间边界

    返回:
    - 带时区的 ISO 8601 字符串或 None, 已指定时区时保留其偏移; 非法时间抛出 ValueError
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("查询时间必须是日期时间字符串, 例如 2026-10-04T09:00:00")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.astimezone()
        return parsed.isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("查询时间无效, 请填写日期和时间, 例如 2026-10-04T09:00:00") from exc


def _same_connection(left: tuple[object, int], right: tuple[object, int]) -> bool:
    """
    按对象身份比较连接, 缓存持有对象引用以避免 ID 被回收后复用

    参数:
    - left: 等待前或缓存保存的客户端和代次
    - right: 当前客户端和代次

    返回:
    - 客户端是同一对象且代次相同时为 True
    """
    return left[0] is right[0] and left[1] == right[1]


@dataclass(frozen=True)
class _ReadContext:
    """只在本次操作中保留来源, 不存入可复用会话"""

    origin: CallOrigin
    adapter: PlatformAdapter
    scope: MessageScope
    store: PlatformMessageStore | None
    authorize: Callable[[str], None] | None = None


@dataclass(frozen=True)
class _MemberCache:
    """有期限且带实例和连接身份的成员分页快照"""

    adapter: PlatformAdapter
    connection: tuple[object, int]
    snapshot: MemberSnapshot
    created_at: float
    expires_at: float


class GroupChatService:
    """向工具提供当前群的查询, 不接受跨群参数或任意数据库路径"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        """
        初始化有限成员分页缓存

        参数:
        - clock: 缓存过期使用的单调时钟
        """
        self._clock = clock
        self._members: OrderedDict[str, _MemberCache] = OrderedDict()
        self._summary_lock = asyncio.Lock()

    async def _resolve(self, *, private: bool = False, authorize: Callable[[str], None] | None = None) -> _ReadContext:
        """
        从仍有效的轮次来源取得当前适配器与群身份

        参数:
        - private: 是否读取获授权私聊中的账号群列表
        - authorize: 等待后重新读取插件权限的回调

        返回:
        - 本次操作的可信读取上下文
        """
        try:
            origin = require_call_origin()
        except PermissionError as exc:
            raise GroupChatError("stale_call", "当前调用没有有效的平台来源") from exc
        manager = current_adapter_manager()
        adapter = manager.get_adapter(origin.adapter_id) if manager else None
        if adapter is None:
            raise GroupChatError("unavailable", "来源平台实例不可用", retryable=True)
        scope = await (adapter.group_chat_private_scope(origin) if private else adapter.group_chat_scope(origin))
        context = _ReadContext(origin, adapter, scope, adapter.message_archive, authorize)
        await self._revalidate(context)
        return context

    async def _revalidate(self, context: _ReadContext) -> None:
        """
        等待之后重新核验轮次, 平台实例, 账号, 群与有效路由

        参数:
        - context: 等待之前固定的操作来源
        """
        manager = current_adapter_manager()
        if (current_call_origin() is not context.origin or manager is None
                or manager.get_adapter(context.origin.adapter_id) is not context.adapter):
            raise GroupChatError("stale_call", "轮次结束或平台实例已替换, 丢弃旧查询结果")
        scope = await (context.adapter.group_chat_private_scope(context.origin) if context.scope.conversation_kind == "private"
                       else context.adapter.group_chat_scope(context.origin))
        if (scope != context.scope or current_call_origin() is not context.origin
                or manager is not current_adapter_manager()
                or manager.get_adapter(context.origin.adapter_id) is not context.adapter
                or context.adapter.message_archive is not context.store):
            raise GroupChatError("stale_call", "查询来源已经变化")
        if context.authorize is not None:
            context.authorize(scope.chat_id)

    @staticmethod
    def _capability(context: _ReadContext, name: str) -> None:
        """
        检查当前实例的声明, 不把缺失能力当成空结果

        参数:
        - context: 当前可信操作来源
        - name: 通用能力名称
        """
        capability = context.adapter.group_chat_capabilities().get(name, {})
        state = capability.get("state", "unsupported")
        if state != "supported":
            raise GroupChatError("unsupported" if state == "unsupported" else "unavailable",
                                 "当前平台不支持该能力" if state == "unsupported" else "当前平台能力暂不可用",
                                 retryable=state == "unavailable")

    async def _archive(self, context: _ReadContext, callback: Callable[..., T], *args: Any,
                       argument_errors: bool = False, **kwargs: Any) -> T:
        """
        在工作线程读取平台档案, 将存储失败与无匹配结果区分

        参数:
        - context: 当前可信操作来源
        - callback: 当前平台档案的方法
        - args: 已校验的位置参数
        - argument_errors: 是否允许查询筛选校验失败返回 invalid_argument
        - kwargs: 已校验的查询参数

        返回:
        - 档案结果, 读取失败抛出 archive_unavailable
        """
        if context.store is None:
            raise GroupChatError("archive_unavailable", "平台消息档案尚未装配", retryable=True)
        try:
            result = await asyncio.to_thread(callback, *args, **kwargs)
        except MessageArchiveError as exc:
            raise GroupChatError(exc.code, str(exc)) from exc
        except GroupChatError:
            raise
        except ValueError as exc:
            if argument_errors and not isinstance(exc, json.JSONDecodeError):
                raise
            logger.error(f"[群聊档案] 读取数据格式损坏, 平台={context.scope.adapter_id}, 原因={type(exc).__name__}")
            raise GroupChatError("archive_unavailable", "平台消息档案数据暂不可用", retryable=True) from exc
        except Exception as exc:
            logger.error(f"[群聊档案] 存储操作失败, 平台={context.scope.adapter_id}, 原因={type(exc).__name__}")
            raise GroupChatError("archive_unavailable", "平台消息档案暂不可用", retryable=True) from exc
        await self._revalidate(context)
        return result

    @staticmethod
    def _store(context: _ReadContext) -> PlatformMessageStore:
        """
        取得已装配的档案对象, 不临时创建无归属的存储

        参数:
        - context: 当前可信操作来源

        返回:
        - 当前平台档案, 未装配时抛出 archive_unavailable
        """
        if context.store is None:
            raise GroupChatError("archive_unavailable", "平台消息档案尚未装配", retryable=True)
        return context.store

    async def execute(self, operation: str, arguments: Mapping[str, object], *,
                      limits: GroupChatLimits | None = None, authorize: Callable[[str], None] | None = None) -> dict[str, Any]:
        """
        执行当前群查询或准备回复, 在工具边界捕获并记录失败

        参数:
        - operation: 注册的 group_chat 工具名称
        - arguments: 模型参数, 不允许平台, 账号或目标群字段
        - limits: 宿主查询上限, 缺省使用首批默认值
        - authorize: 可选的当前插件权限复核回调

        返回:
        - 明确成功结果或包含错误码和可重试状态的失败结果
        """
        context: _ReadContext | None = None
        try:
            if not isinstance(operation, str) or operation not in _ARGUMENTS or not isinstance(arguments, Mapping):
                raise ValueError("群聊读取操作或参数格式不符")
            if set(arguments) - _ARGUMENTS[operation]:
                raise ValueError("群聊工具含有未知参数, 只能操作当前群")
            bounds = limits or GroupChatLimits()
            private = operation == "group_chat_list_groups"
            context = await self._resolve(private=private, authorize=authorize)
            if private:
                if context.origin.actor_id not in bounds.cross_group_query_callers:
                    raise GroupChatError("forbidden", "未配置为跨群查询管理者")
                return await self._groups(context, bounds)
            if bounds.allowed_groups and context.scope.chat_id not in bounds.allowed_groups:
                raise GroupChatError("forbidden", "当前群不在插件允许范围内")
            if operation == "group_chat_get_group_info":
                self._capability(context, "group_info")
                connection = context.adapter.group_chat_connection_token()
                info = await context.adapter.group_chat_group(context.scope)
                await self._revalidate(context)
                if (not isinstance(info, VerifiedGroup) or info.scope != context.scope or info.group.group_id != context.scope.chat_id
                        or not math.isfinite(info.fetched_at) or not _same_connection(connection, context.adapter.group_chat_connection_token())):
                    raise GroupChatError("unverified_target", "群资料来源已经变化或不属于当前群")
                return {"ok": True, "item": self._group_item(info.group), "source": "adapter", "fetched_at": info.fetched_at}
            if operation in {"group_chat_get_message_assets", "group_chat_list_stickers"}:
                if not bounds.media_reply_enabled:
                    raise GroupChatError("unsupported", "当前 Agent 未启用图片与表情回复")
                if operation == "group_chat_get_message_assets":
                    self._capability(context, "image")
                    return await self._message_assets(context, _id(arguments.get("message_id")), bounds)
                capabilities = context.adapter.group_chat_capabilities()
                native = capabilities.get("sticker", {}).get("state") == "supported"
                if not native:
                    self._capability(context, "image")
                return await self._archive(context, library_for(self._store(context)).list, scope=context.scope,
                                           adapter_type=context.adapter.config.type, formats=context.adapter.group_chat_media_formats(), native=native,
                                           **dict(arguments), argument_errors=True)
            if operation in {"group_chat_prepare_summary", "group_chat_read_summary_sources", "group_chat_save_summary",
                             "group_chat_get_summary", "group_chat_list_summaries"}:
                return await self._summary(context, operation, arguments, bounds)
            if operation == "group_chat_reply":
                from copy import deepcopy
                from satrap.core.group_chat.reply import current_reply_turn

                turn = current_reply_turn()
                if turn is None or turn.origin is not context.origin:
                    raise GroupChatError("stale_call", "当前没有可提交的群聊回复轮次")
                raw_components = arguments.get("components")
                if not isinstance(raw_components, list) or not 1 <= len(raw_components) <= 64:
                    raise ValueError("components 必须包含 1 到 64 个组件")
                components = deepcopy(raw_components)
                connection = context.adapter.group_chat_connection_token()

                async def validate_reply() -> MessageChain:
                    """
                    提交前再次核验全部目标, 防止准备后的撤回或本地删除

                    返回:
                    - 来源仍有效的完整组件链, 任一目标失效时抛出明确错误
                    """
                    if not _same_connection(connection, context.adapter.group_chat_connection_token()):
                        raise GroupChatError("stale_call", "准备回复后平台连接已经变化")
                    return await self._reply_components(context, components, bounds)

                return await turn.prepare(validate_reply)
            if operation in {"group_chat_find_members", "group_chat_list_members"}:
                return await self._find(context, arguments, bounds, list_all=operation == "group_chat_list_members")
            if operation == "group_chat_get_member":
                return await self._member(context, _id(arguments.get("user_id")))
            if operation == "group_chat_get_message":
                return await self._message(context, _id(arguments.get("message_id")), bounds)
            store = self._store(context)
            params = dict(arguments)
            params["limit"] = _limit(params.get("limit", min(20, bounds.message_limit)), bounds.message_limit)
            params["text_budget"] = bounds.text_budget
            if operation == "group_chat_search_messages":
                for key in ("start_time", "end_time"):
                    if key in params:
                        params[key] = _query_time(params[key])
            return await self._archive(context, store.query, context.scope, argument_errors=True, **params)
        except GroupChatError as exc:
            code, message, retryable = exc.code, str(exc), exc.retryable
        except ValueError as exc:
            code, message, retryable = "invalid_argument", str(exc), False
        except Exception as exc:
            code, message, retryable = "unavailable", "群聊查询暂不可用", True
            logger.error(f"[群聊工具] 未预期执行失败, 操作={operation}: {traceback.format_exc()}")
        origin = context.origin if context else current_call_origin()
        logger.warning(f"[群聊工具] 执行失败, 操作={operation}, 错误={code}, "
                       f"平台={origin.adapter_id if origin else ''}, 轮次={origin.request_id if origin else ''}")
        return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}

    @staticmethod
    def _group_item(group: GroupRecord) -> dict[str, Any]:
        """
        校验通用群资料并输出有限字段

        参数:
        - group: 适配器核验后的群资料

        返回:
        - 不含平台额外字段的群条目
        """
        if (not isinstance(group, GroupRecord) or not isinstance(group.name, str) or len(group.name) > 512
                or any(value is not None and (type(value) is not int or value < 0)
                       for value in (group.member_count, group.max_member_count))):
            raise GroupChatError("unavailable", "适配器群资料不符合契约")
        return {"group_id": _id(group.group_id), "group_name": group.name, "member_count": group.member_count,
                "max_member_count": group.max_member_count, "verified": True}

    async def _groups(self, context: _ReadContext, bounds: GroupChatLimits) -> dict[str, Any]:
        """
        读取管理者私聊中的账号群列表, 不超过上下文预算

        参数:
        - context: 可信私聊来源
        - bounds: 允许群与正文预算

        返回:
        - 当前账号的群列表及完整性声明
        """
        self._capability(context, "group_list")
        connection = context.adapter.group_chat_connection_token()
        snapshot = await context.adapter.group_chat_groups(context.scope)
        await self._revalidate(context)
        if (not isinstance(snapshot, GroupSnapshot) or snapshot.scope != context.scope or len(snapshot.groups) > 512
                or type(snapshot.complete) is not bool or type(snapshot.truncated) is not bool
                or snapshot.complete and snapshot.truncated or not math.isfinite(snapshot.fetched_at)
                or not _same_connection(connection, context.adapter.group_chat_connection_token())):
            raise GroupChatError("unverified_target", "群列表身份或完整性声明无效")
        items, seen, budget = [], set(), 0
        truncated = snapshot.truncated
        for group in snapshot.groups:
            item = self._group_item(group)
            if group.group_id in seen:
                raise GroupChatError("unavailable", "群列表包含重复身份")
            seen.add(group.group_id)
            if (not context.adapter.group_chat_group_visible(group.group_id)
                    or bounds.allowed_groups and group.group_id not in bounds.allowed_groups):
                continue
            budget += len(json.dumps(item, ensure_ascii=False))
            if budget > bounds.text_budget:
                truncated = True
                continue
            items.append(item)
        return {"ok": True, "items": items, "source": "adapter", "fetched_at": snapshot.fetched_at,
                "coverage": {"complete": snapshot.complete and not truncated}, "truncated": truncated}

    async def _message_assets(self, context: _ReadContext, message_id: str, bounds: GroupChatLimits) -> dict[str, Any]:
        """
        按需读取已核验消息的图片, 下载失败不返回可发送 ID

        参数:
        - context: 当前可信群身份
        - message_id: 当前群原消息
        - bounds: 当前插件上限

        返回:
        - 按原媒体顺序的目录, 逐项说明可用状态与失败原因
        """
        from pathlib import Path
        from satrap.core.group_chat.reply import current_reply_turn
        from satrap.core.pipeline.attachments import _download

        item = (await self._message(context, message_id, bounds))["item"]
        if not item["verified"]:
            raise GroupChatError("unverified_target", "图片来源消息未核验")
        turn = current_reply_turn()
        settings = turn.event.policy_settings if turn else context.adapter.config.settings
        hosts = settings.get("media_trusted_hosts", []) or []
        if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
            raise GroupChatError("invalid_configuration", "图片下载可信主机配置无效")
        trusted = tuple(hosts)
        assets = AssetStore(self._store(context))
        deadline = time.monotonic() + 20
        used = 0
        result = []
        event_media = [component for component in turn.event.get_messages() if isinstance(component, Image)] if turn and message_id == context.origin.source_message_id else []
        image_index = 0
        for index, reference in enumerate(item["media"]):
            entry: dict[str, Any] = {"index": index, "type": reference.get("type"), "available": False}
            result.append(entry)
            if reference.get("type") != "Image":
                entry["reason"] = "unsupported_media"
                continue
            local = event_media[image_index] if image_index < len(event_media) else None
            image_index += 1
            if image_index > 8 or used >= MAX_REPLY_BYTES or time.monotonic() >= deadline:
                entry["reason"] = "quota_exceeded"
                continue
            try:
                cached = assets.find(context.scope, message_id, index)
                if cached is not None:
                    used += cached["size_bytes"]
                    if used > MAX_REPLY_BYTES:
                        raise GroupChatError("quota_exceeded", "本次图片读取总大小超过 20 MiB")
                    if cached["mime_type"] not in context.adapter.group_chat_media_formats():
                        raise GroupChatError("unsupported", "当前平台不支持该图片格式")
                    entry.update(cached)
                    continue
                data: bytes | None = None
                if local is not None and local.resolved_path:
                    path = Path(local.resolved_path)
                    if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_IMAGE_BYTES:
                        with path.open("rb") as stream:
                            data = stream.read(MAX_IMAGE_BYTES + 1)
                async def fetch() -> bytes:
                    """先用已核验地址, 过期时仅经适配器刷新后再次下载"""
                    address = reference.get("url")
                    if isinstance(address, str) and address:
                        try:
                            return await _download(address, MAX_IMAGE_BYTES, trusted, settings.get("media_insecure_tls", False) is not True,
                                                   settings.get("media_plaintext_http", False) is True)
                        except Exception as exc:
                            logger.warning(f"[群图片] 原地址读取失败, 平台={context.scope.adapter_id}, 原因={type(exc).__name__}")
                    fresh = await context.adapter.group_chat_refresh_image(context.scope, reference)
                    if not fresh:
                        raise GroupChatError("asset_unavailable", "图片地址已失效且平台无法刷新")
                    await self._revalidate(context)
                    return await _download(fresh, MAX_IMAGE_BYTES, trusted, settings.get("media_insecure_tls", False) is not True,
                                           settings.get("media_plaintext_http", False) is True)
                if data is None:
                    data = await asyncio.wait_for(fetch(), max(0.01, deadline - time.monotonic()))
                used += len(data)
                if used > MAX_REPLY_BYTES:
                    raise GroupChatError("quota_exceeded", "本次图片读取总大小超过 20 MiB")
                await self._revalidate(context)
                registered = await self._archive(context, assets.register, context.scope, data, source_message_id=message_id, media_index=index)
                if registered["mime_type"] not in context.adapter.group_chat_media_formats():
                    raise GroupChatError("unsupported", "当前平台不支持该图片格式")
                entry.update(registered)
            except GroupChatError as exc:
                if exc.code == "stale_call":
                    raise
                logger.warning(f"[群图片] 读取拒绝, 平台={context.scope.adapter_id}, 原因={exc.code}")
                entry["reason"] = exc.code
            except (asyncio.TimeoutError, OSError) as exc:
                logger.warning(f"[群图片] 读取失败, 平台={context.scope.adapter_id}, 原因={type(exc).__name__}")
                entry["reason"] = "asset_unavailable"
            except Exception:
                logger.error(f"[群图片] 读取异常, 平台={context.scope.adapter_id}: {traceback.format_exc()}")
                entry["reason"] = "asset_unavailable"
        await self._revalidate(context)
        if (await self._archive(context, self._store(context).get, context.scope, message_id) or {}).get("status") != "active":
            raise GroupChatError("asset_unavailable", "返回前图片来源已失效")
        return {"ok": True, "message_id": message_id, "items": result}

    async def _summary(self, context: _ReadContext, operation: str, arguments: Mapping[str, object],
                       bounds: GroupChatLimits) -> dict[str, Any]:
        """
        执行当前群摘要契约, 写入与快照仅允许有效主工作流

        参数:
        - context: 已核验的当前群身份
        - operation: 摘要工具名称
        - arguments: 严格限定的工具参数
        - bounds: 当前插件和模型预算

        返回:
        - 明确的保存状态, 快照或查询结果
        """
        if not bounds.summary_enabled:
            raise GroupChatError("unsupported", "当前 Agent 未启用群摘要")
        store = SummaryStore(self._store(context))
        if operation == "group_chat_get_summary":
            return await self._archive(context, store.get, context.scope, _id(arguments.get("summary_id")), argument_errors=True)
        if operation == "group_chat_list_summaries":
            return await self._archive(context, store.list, context.scope, **dict(arguments), argument_errors=True)
        from satrap.core.group_chat.reply import current_reply_turn

        turn = current_reply_turn()
        if turn is None or turn.origin is not context.origin:
            raise GroupChatError("stale_call", "当前没有有效的群聊主工作流")
        turn.require_main_tool(operation)
        if bounds.summary_input_budget < 1000:
            raise GroupChatError("quota_exceeded", "当前模型剩余上下文不足, 请缩短讨论范围或清理上下文")
        limits = SummaryLimits(bounds.summary_message_limit, bounds.summary_text_budget, bounds.message_limit,
                               bounds.text_budget, bounds.summary_retention_days, bounds.summary_input_budget)
        if operation == "group_chat_prepare_summary":
            start, end = _query_time(arguments.get("start_time")), _query_time(arguments.get("end_time"))
            if start is None or end is None:
                raise ValueError("群摘要需要指定开始和结束时间")
            async with self._summary_lock:
                manager = current_adapter_manager()
                active = 0
                for adapter_id in manager.list_adapters() if manager else []:
                    adapter = manager.get_adapter(adapter_id) if manager else None
                    if adapter is not None and adapter.message_archive is not None:
                        active += await asyncio.to_thread(SummaryStore(adapter.message_archive).active_count)
                await self._revalidate(context)
                turn.require_main_tool(operation)
                return await self._archive(context, store.prepare, context.scope, turn.operation_owner, start_time=start,
                                           end_time=end, keyword=arguments.get("keyword"),
                                           include_bot=arguments.get("include_bot", False), limits=limits, allow_new=active < 20, argument_errors=True)
        if operation == "group_chat_read_summary_sources":
            return await self._archive(context, store.read_sources, context.scope, turn.operation_owner,
                                       _id(arguments.get("snapshot_id")), arguments.get("cursor"), limits=limits, argument_errors=True)
        await self._revalidate(context)
        turn.require_main_tool(operation)
        result = await self._archive(context, store.save, context.scope, turn.operation_owner,
                                     _id(arguments.get("snapshot_id")), arguments.get("title"), arguments.get("points"),
                                     limits=limits, argument_errors=True)
        logger.info(f"[群摘要] 保存完成, 平台={context.scope.adapter_id}, 摘要={result['summary']['summary_id']}, 轮次={context.origin.request_id}")
        return result

    async def _reply_components(self, context: _ReadContext, components: object, bounds: GroupChatLimits) -> MessageChain:
        """
        整体验证草稿, 失败时释放全部媒体租约

        参数:
        - context: 可信群来源
        - components: 模型组件
        - bounds: 插件上限

        返回:
        - 可提交的完整组件链
        """
        leases: list[AssetLease] = []
        try:
            return await self._reply_components_inner(context, components, bounds, leases)
        except BaseException:
            for lease in leases:
                lease.release()
            raise

    async def _reply_components_inner(self, context: _ReadContext, components: object, bounds: GroupChatLimits,
                                      leases: list[AssetLease]) -> MessageChain:
        """
        整体验证首批组件, 提及消息发送者而非正文中的被提及者

        参数:
        - context: 当前群的可信身份和适配器
        - components: 模型提供的文本, 引用与提及组件列表
        - bounds: 当前插件读取预算
        - leases: 本次校验取得的文件引用, 由外层失败边界和轮次宿主管理

        返回:
        - 引用位于首位的完整消息链, 任一目标失败时不返回部分草稿
        """
        if not isinstance(components, list) or not 1 <= len(components) <= 64:
            raise ValueError("components 必须包含 1 到 64 个组件")
        connection = context.adapter.group_chat_connection_token()
        output: list[BaseMessageComponent] = []
        quote: Reply | None = None
        text_size = 0
        image_count = 0
        sticker_count = 0
        file_count = 0
        media_bytes = 0
        for raw in components:
            if not isinstance(raw, dict):
                raise ValueError("每个组件必须是对象")
            kind = raw.get("type")
            if kind == "text":
                if set(raw) != {"type", "text"} or not isinstance(raw["text"], str) or not raw["text"]:
                    raise ValueError("text 组件必须包含非空 text 字符串")
                text_size += len(raw["text"])
                if text_size > 100000:
                    raise ValueError("回复正文超过 100000 字符")
                if text_size > context.adapter.group_chat_media_limits().get("max_text", 100000):
                    raise GroupChatError("quota_exceeded", "回复正文超过当前平台单条消息上限, 请缩短正文")
                self._capability(context, "text")
                output.append(Plain(raw["text"]))
            elif kind == "quote":
                if set(raw) != {"type", "message_id"} or quote is not None:
                    raise ValueError("quote 只接受 message_id, 且每条回复至多一个引用")
                self._capability(context, "quote")
                item = (await self._message(context, _id(raw["message_id"]), bounds))["item"]
                if item.get("verified") is not True:
                    raise GroupChatError("unverified_target", "引用消息未通过当前群归属核验")
                quote = Reply(id=item["message_id"])
            elif kind == "mention":
                self._capability(context, "mention")
                if set(raw) == {"type", "source_message_id"}:
                    item = (await self._message(context, _id(raw["source_message_id"]), bounds))["item"]
                    if item.get("verified") is not True:
                        raise GroupChatError("unverified_target", "提及来源消息未通过当前群核验")
                    user_id = _id(item.get("sender_id"))
                elif set(raw) == {"type", "user_id"}:
                    user_id = _id(raw["user_id"])
                    if context.adapter.group_chat_capabilities().get("member_info", {}).get("state") == "supported":
                        await self._member(context, user_id)
                    else:
                        store = self._store(context)
                        result = await self._archive(context, store.query, context.scope, sender_id=user_id, limit=100,
                                                     text_budget=128)
                        if not any(item.get("verified") is True and item.get("sender_id") == user_id for item in result["items"]):
                            raise GroupChatError("unverified_target", "成员 ID 缺少当前群资料或已核验消息依据")
                else:
                    raise ValueError("mention 的 source_message_id 与 user_id 必须二选一")
                output.append(At(qq=user_id))
            elif kind == "image":
                if set(raw) != {"type", "asset_id"}:
                    raise ValueError("image 只接受 asset_id, 不接受路径或 URL")
                if not bounds.media_reply_enabled:
                    raise GroupChatError("unsupported", "当前 Agent 未启用图片与表情回复")
                self._capability(context, "image")
                image_count += 1
                file_count += 1
                maximum = min(bounds.max_reply_images, context.adapter.group_chat_media_limits()["max_images"])
                if image_count > maximum:
                    raise GroupChatError("quota_exceeded", f"当前回复最多支持 {maximum} 张图片")
                from satrap.core.group_chat.reply import current_reply_turn

                turn = current_reply_turn()
                if turn is None:
                    raise GroupChatError("stale_call", "当前图片回复轮次已失效")
                lease, metadata = AssetStore(self._store(context)).acquire(context.scope, _id(raw["asset_id"]), turn.operation_owner)
                leases.append(lease)
                await self._revalidate(context)
                media_bytes += metadata["size_bytes"]
                if media_bytes > min(MAX_REPLY_BYTES, context.adapter.group_chat_media_limits()["max_bytes"]):
                    raise GroupChatError("quota_exceeded", "回复图片总大小不能超过 20 MiB")
                if metadata["mime_type"] not in context.adapter.group_chat_media_formats():
                    raise GroupChatError("unsupported", "当前平台不支持该图片格式")
                component = Image.fromFileSystem(str(lease.path))
                component.asset_lease = lease
                output.append(component)
            elif kind == "sticker":
                if set(raw) != {"type", "sticker_id"}:
                    raise ValueError("sticker 只接受表情目录中的 sticker_id")
                if not bounds.media_reply_enabled:
                    raise GroupChatError("unsupported", "当前 Agent 未启用图片与表情回复")
                sticker_count += 1
                maximum = min(bounds.max_reply_stickers, context.adapter.group_chat_media_limits()["max_stickers"])
                if sticker_count > maximum:
                    raise GroupChatError("quota_exceeded", f"当前回复最多支持 {maximum} 个表情")
                native = context.adapter.group_chat_capabilities().get("sticker", {}).get("state") == "supported"
                row, lease = library_for(self._store(context)).acquire(context.scope, _id(raw["sticker_id"]),
                                                                     context.adapter.config.type, context.adapter.group_chat_media_formats(), native)
                if lease is not None:
                    leases.append(lease)
                    self._capability(context, "image")
                    file_count += 1
                    media_bytes += row["size_bytes"]
                    component = Image.fromFileSystem(str(lease.path))
                    component.asset_lease = lease
                    output.append(component)
                else:
                    self._capability(context, "sticker")
                    output.append(context.adapter.group_chat_native_sticker_component(row["native_key"]))
            else:
                raise ValueError("不支持该回复组件类型")
            media_limits = context.adapter.group_chat_media_limits()
            if file_count > media_limits.get("max_attachments", media_limits["max_images"]) or media_bytes > min(MAX_REPLY_BYTES, media_limits["max_bytes"]):
                raise GroupChatError("quota_exceeded", "图片与图片表情的合计超过当前平台发送限制")
        if not output:
            raise ValueError("回复不能只有引用")
        await self._revalidate(context)
        if not _same_connection(connection, context.adapter.group_chat_connection_token()):
            raise GroupChatError("stale_call", "回复核验期间平台连接已经变化")
        return MessageChain(([quote] if quote else []) + output)

    @staticmethod
    def _member_item(member: MemberRecord) -> dict[str, object]:
        """
        校验并收窄适配器返回的成员资料

        参数:
        - member: 适配器核验的成员

        返回:
        - 有限长度的 ID, 昵称和名片
        """
        if (not isinstance(member, MemberRecord) or not isinstance(member.nickname, str) or not isinstance(member.card, str)
                or len(member.nickname) > 512 or len(member.card) > 512):
            raise GroupChatError("unavailable", "适配器成员资料不符合读取契约")
        try:
            user_id = _id(member.user_id)
        except ValueError as exc:
            raise GroupChatError("unverified_target", "适配器未返回有效成员身份") from exc
        if not isinstance(member.role, str) or len(member.role) > 64:
            raise GroupChatError("unavailable", "成员角色资料无效")
        return {"user_id": user_id, "nickname": member.nickname, "card": member.card, "role": member.role, "verified": True}

    async def _member(self, context: _ReadContext, user_id: str) -> dict[str, Any]:
        """
        核验单个当前群成员, 不猜测或跨群复用成员资料

        参数:
        - context: 当前可信操作来源
        - user_id: 待核验的成员 ID

        返回:
        - 带来源和读取时间的成员资料
        """
        self._capability(context, "member_info")
        connection = context.adapter.group_chat_connection_token()
        result = await context.adapter.group_chat_member(context.scope, user_id)
        await self._revalidate(context)
        if not _same_connection(context.adapter.group_chat_connection_token(), connection):
            raise GroupChatError("stale_call", "成员读取期间平台连接已变化")
        if (not isinstance(result, VerifiedMember) or result.scope != context.scope or result.member.user_id != user_id
                or not math.isfinite(result.fetched_at)):
            raise GroupChatError("unverified_target", "成员读取结果不属于当前群或请求身份")
        return {"ok": True, "item": self._member_item(result.member), "source": "adapter",
                "fetched_at": result.fetched_at, "truncated": False}

    def _prune(self) -> None:
        """清理过期快照并限制总成员数, 不随查询次数无限积累"""
        now = self._clock()
        for token in list(self._members):
            if self._members[token].expires_at <= now:
                self._members.pop(token)
        while len(self._members) > 32 or sum(len(entry.snapshot.members) for entry in self._members.values()) > 20000:
            self._members.popitem(last=False)

    async def _find(self, context: _ReadContext, arguments: Mapping[str, object], bounds: GroupChatLimits, *, list_all: bool = False) -> dict[str, Any]:
        """
        按昵称与名片查找, 重名返回候选, 分页保持同一短期快照

        参数:
        - context: 当前可信操作来源
        - arguments: query, limit 和 cursor 参数
        - bounds: 宿主查询和缓存上限
        - list_all: 是否分页列出全部成员, 默认按名称匹配

        返回:
        - 候选, 匹配方式, 完整性与分页信息
        """
        query = "" if list_all else arguments.get("query")
        if not isinstance(query, str) or not list_all and not query.strip() or len(query) > 128:
            raise ValueError("成员查询必须是 1 到 128 字符的昵称或名片")
        query = query.strip().casefold()
        limit = _limit(arguments.get("limit", min(10, bounds.member_limit)), bounds.member_limit)
        fingerprint = hashlib.sha256((context.scope.key + "\0" + ("list:" if list_all else "find:") + query).encode("utf-8")).hexdigest()
        self._capability(context, "member_list")
        self._prune()
        token, offset = "", 0
        cursor = arguments.get("cursor")
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError
                value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                if (not isinstance(value, list) or len(value) != 4 or type(value[0]) is not int or value[0] != 1 or value[2] != fingerprint
                        or type(value[3]) is not int or value[3] < 0):
                    raise ValueError
                token, offset = _id(value[1]), value[3]
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("成员游标无效或不属于当前群和查询条件") from exc
            cached = self._members.get(token)
            if cached is None:
                raise GroupChatError("cursor_expired", "成员分页快照已过期, 请重新查询", retryable=True)
        else:
            cached = None
            if bounds.member_cache_ttl:
                for key, entry in reversed(self._members.items()):
                    if (entry.adapter is context.adapter and _same_connection(entry.connection, context.adapter.group_chat_connection_token())
                            and entry.snapshot.scope == context.scope
                            and entry.created_at + bounds.member_cache_ttl > self._clock()):
                        token, cached = key, entry
                        break
            if cached is None:
                connection = context.adapter.group_chat_connection_token()
                snapshot = await context.adapter.group_chat_members(context.scope)
                await self._revalidate(context)
                if not _same_connection(context.adapter.group_chat_connection_token(), connection):
                    raise GroupChatError("stale_call", "成员列表读取期间平台连接已变化")
                if (not isinstance(snapshot, MemberSnapshot) or snapshot.scope != context.scope
                        or not math.isfinite(snapshot.fetched_at) or len(snapshot.members) > 10000
                        or type(snapshot.complete) is not bool or type(snapshot.truncated) is not bool
                        or (snapshot.complete and snapshot.truncated)):
                    raise GroupChatError("unverified_target", "成员快照不属于当前群或完整性声明无效")
                seen: set[str] = set()
                for member in snapshot.members:
                    item = self._member_item(member)
                    if str(item["user_id"]) in seen:
                        raise GroupChatError("unavailable", "成员快照包含重复身份")
                    seen.add(str(item["user_id"]))
                token, now = uuid.uuid4().hex, self._clock()
                cached = _MemberCache(context.adapter, connection, snapshot, now,
                                      now + max(15, bounds.member_cache_ttl))
                self._members[token] = cached
                self._prune()
        if (cached.adapter is not context.adapter or not _same_connection(cached.connection, context.adapter.group_chat_connection_token())
                or cached.snapshot.scope != context.scope):
            raise GroupChatError("stale_call", "成员快照来源实例或连接已经变化")
        self._members.move_to_end(token)
        matches: list[dict[str, Any]] = []
        for member in cached.snapshot.members:
            if list_all:
                matches.append(self._member_item(member))
                continue
            by = [name for name, text in (("nickname", member.nickname), ("card", member.card)) if query in text.casefold()]
            if by:
                exact = any(query == text.casefold() for text in (member.nickname, member.card))
                matches.append({**self._member_item(member), "matched_by": by, "match": "exact" if exact else "contains"})
        matches.sort(key=lambda item: (item.get("match") != "exact", item["user_id"]))
        if offset > len(matches):
            raise ValueError("成员游标超出匹配范围")
        items = matches[offset:offset + limit]
        if list_all:
            page, used = [], 0
            for item in items:
                used += len(json.dumps(item, ensure_ascii=False))
                if used > bounds.text_budget:
                    break
                page.append(item)
            if items and not page:
                raise ValueError("成员资料超出单次查询预算, 请提高插件 text_budget")
            items = page
        next_offset = offset + len(items)
        more = next_offset < len(matches)
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [1, token, fingerprint, next_offset], separators=(",", ":"),
        ).encode("utf-8")).decode("ascii").rstrip("=") if more else None
        await self._revalidate(context)
        result = {"ok": True, "items": items, "source": "adapter_snapshot", "fetched_at": cached.snapshot.fetched_at,
                "coverage": {"complete": cached.snapshot.complete, "reason": cached.snapshot.reason},
                "truncated": cached.snapshot.truncated, "has_more": more, "next_cursor": next_cursor}
        if not list_all:
            result.update(ambiguous=len(matches) > 1, unique=len(matches) == 1 and cached.snapshot.complete)
        return result

    async def _message(self, context: _ReadContext, message_id: str, bounds: GroupChatLimits) -> dict[str, Any]:
        """
        优先读取本地档案, 缺失时回源核验并尊重删除与保留范围

        参数:
        - context: 当前可信操作来源
        - message_id: 当前群消息 ID
        - bounds: 宿主正文预算

        返回:
        - 已核验单条消息, 明确区分档案与适配器回源
        """
        store = self._store(context)
        item = await self._archive(context, store.get, context.scope, message_id)
        source = "local_archive"
        if item is None:
            allowed = await self._archive(context, store.backfill_allowed, context.scope, message_id)
            if not allowed:
                raise GroupChatError("message_deleted", "消息已在本地档案删除, 不自动回源恢复")
            self._capability(context, "message_lookup")
            connection = context.adapter.group_chat_connection_token()
            result = await context.adapter.group_chat_message(context.scope, message_id)
            await self._revalidate(context)
            if not _same_connection(context.adapter.group_chat_connection_token(), connection):
                raise GroupChatError("stale_call", "消息回源期间平台连接已变化")
            if (not isinstance(result, VerifiedMessage) or result.scope != context.scope
                    or result.message.message_id != message_id or result.message.verified is not True
                    or result.message.time_source != "platform"):
                raise GroupChatError("unverified_target", "回源消息缺少当前群归属或原始时间依据")
            allowed = await self._archive(context, store.backfill_allowed, context.scope, message_id, result.message.message_time)
            if not allowed:
                raise GroupChatError("message_deleted_or_expired", "回源消息位于已删除或过期范围, 不恢复正文")
            await self._archive(context, store.record, context.scope, result.message)
            item = await self._archive(context, store.get, context.scope, message_id)
            source = "adapter_verified"
        if item is None:
            raise GroupChatError("message_deleted", "读取期间档案已被清理, 不返回旧正文")
        if item["status"] != "active":
            raise GroupChatError("message_" + str(item["status"]), "消息正文已删除, 撤回或超过保留期")
        if len(str(item["text"])) > bounds.text_budget:
            item["text"] = str(item["text"])[:bounds.text_budget]
            item["truncated"] = True
        return {"ok": True, "item": item, "source": source, "truncated": item["truncated"]}


group_chat_service = GroupChatService()
