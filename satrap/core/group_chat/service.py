"""当前群的只读工具宿主, 固定来源身份并在每次等待后重新核验"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar
import asyncio
import base64
import hashlib
import json
import math
import time
import uuid

from satrap.core.call_context import CallOrigin, current_call_origin, require_call_origin
from satrap.core.config.platform_messages import MessageArchiveError, MessageScope, PlatformMessageStore
from satrap.core.group_chat.types import GroupChatError, GroupChatLimits, MemberRecord, MemberSnapshot, VerifiedMember, VerifiedMessage
from satrap.core.platform import PlatformAdapter, current_adapter_manager
from satrap.core.log import logger


T = TypeVar("T")
_ARGUMENTS = {
    "group_chat_find_members": frozenset({"query", "limit", "cursor"}),
    "group_chat_get_member": frozenset({"user_id"}),
    "group_chat_get_message": frozenset({"message_id"}),
    "group_chat_recent_messages": frozenset({"limit", "before_message_id", "cursor"}),
    "group_chat_search_messages": frozenset({"keyword", "sender_id", "start_time", "end_time", "limit", "cursor"}),
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

    async def _resolve(self) -> _ReadContext:
        """
        从仍有效的轮次来源取得当前适配器与群身份

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
        scope = await adapter.group_chat_scope(origin)
        context = _ReadContext(origin, adapter, scope, adapter.message_archive)
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
        scope = await context.adapter.group_chat_scope(context.origin)
        if (scope != context.scope or current_call_origin() is not context.origin
                or manager is not current_adapter_manager()
                or manager.get_adapter(context.origin.adapter_id) is not context.adapter
                or context.adapter.message_archive is not context.store):
            raise GroupChatError("stale_call", "查询来源已经变化")

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
                      limits: GroupChatLimits | None = None) -> dict[str, Any]:
        """
        执行五个当前群只读工具, 在工具边界捕获并记录失败

        参数:
        - operation: 注册的 group_chat 只读工具名称
        - arguments: 模型参数, 不允许平台, 账号或目标群字段
        - limits: 宿主查询上限, 缺省使用首批默认值

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
            context = await self._resolve()
            if operation == "group_chat_find_members":
                return await self._find(context, arguments, bounds)
            if operation == "group_chat_get_member":
                return await self._member(context, _id(arguments.get("user_id")))
            if operation == "group_chat_get_message":
                return await self._message(context, _id(arguments.get("message_id")), bounds)
            store = self._store(context)
            params = dict(arguments)
            params["limit"] = _limit(params.get("limit", min(20, bounds.message_limit)), bounds.message_limit)
            params["text_budget"] = bounds.text_budget
            return await self._archive(context, store.query, context.scope, argument_errors=True, **params)
        except GroupChatError as exc:
            code, message, retryable = exc.code, str(exc), exc.retryable
        except ValueError as exc:
            code, message, retryable = "invalid_argument", str(exc), False
        except Exception as exc:
            code, message, retryable = "unavailable", "群聊查询暂不可用", True
            logger.error(f"[群聊工具] 未预期读取失败, 操作={operation}, 原因={type(exc).__name__}")
        origin = context.origin if context else current_call_origin()
        logger.warning(f"[群聊工具] 读取失败, 操作={operation}, 错误={code}, "
                       f"平台={origin.adapter_id if origin else ''}, 轮次={origin.request_id if origin else ''}")
        return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}

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
        return {"user_id": user_id, "nickname": member.nickname, "card": member.card, "verified": True}

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

    async def _find(self, context: _ReadContext, arguments: Mapping[str, object], bounds: GroupChatLimits) -> dict[str, Any]:
        """
        按昵称与名片查找, 重名返回候选, 分页保持同一短期快照

        参数:
        - context: 当前可信操作来源
        - arguments: query, limit 和 cursor 参数
        - bounds: 宿主查询和缓存上限

        返回:
        - 候选, 匹配方式, 完整性与分页信息
        """
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            raise ValueError("成员查询必须是 1 到 128 字符的昵称或名片")
        query = query.strip().casefold()
        limit = _limit(arguments.get("limit", min(10, bounds.member_limit)), bounds.member_limit)
        fingerprint = hashlib.sha256((context.scope.key + "\0" + query).encode("utf-8")).hexdigest()
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
            by = [name for name, text in (("nickname", member.nickname), ("card", member.card)) if query in text.casefold()]
            if by:
                exact = any(query == text.casefold() for text in (member.nickname, member.card))
                matches.append({**self._member_item(member), "matched_by": by, "match": "exact" if exact else "contains"})
        matches.sort(key=lambda item: (item["match"] != "exact", item["user_id"]))
        if offset > len(matches):
            raise ValueError("成员游标超出匹配范围")
        items = matches[offset:offset + limit]
        more = offset + limit < len(matches)
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [1, token, fingerprint, offset + limit], separators=(",", ":"),
        ).encode("utf-8")).decode("ascii").rstrip("=") if more else None
        await self._revalidate(context)
        return {"ok": True, "items": items, "source": "adapter_snapshot", "fetched_at": cached.snapshot.fetched_at,
                "coverage": {"complete": cached.snapshot.complete, "reason": cached.snapshot.reason},
                "truncated": cached.snapshot.truncated, "has_more": more, "next_cursor": next_cursor,
                "ambiguous": len(matches) > 1, "unique": len(matches) == 1 and cached.snapshot.complete}

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
