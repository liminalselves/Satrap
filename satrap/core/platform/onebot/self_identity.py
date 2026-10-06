"""OneBot 自身昵称及本群昵称的有界回源缓存"""
from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, cast
from time import monotonic
import asyncio

from satrap.core.platform.identity import BotIdentity
from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.platform.onebot.adapter import OneBotAdapter


IDENTITY_TTL = 60.0
"""自身资料的最长缓存秒数, 失败结果同样缓存以避免每轮重复回源"""
IDENTITY_TIMEOUT = 3.0
"""一次身份补全的总等待上限, 包含并发请求等待"""
IDENTITY_ACTION_TIMEOUT = 1.0
"""每次只读身份请求的等待上限"""


class OneBotSelfIdentity:
    """按当前连接隔离账号昵称与本群昵称, 查询结果不可跨连接复用"""

    def __init__(self, adapter: OneBotAdapter) -> None:
        """
        初始化自身资料缓存

        参数:
        - adapter: 提供当前机器人账号和连接的适配器
        """
        self.adapter = adapter
        self._lock = asyncio.Lock()
        self._client: object = None
        self._account = ""
        self._generation = -1
        self._cache: OrderedDict[str, tuple[float, BotIdentity | None]] = OrderedDict()

    def clear(self) -> None:
        """丢弃当前连接的资料及失败缓存"""
        self._cache.clear()
        self._client = None
        self._account = ""
        self._generation = -1

    def _current(self, client: object, account: str, generation: int) -> bool:
        """
        核验等待期间连接和账号是否仍相同

        参数:
        - client: 发起查询时的客户端
        - account: 发起查询时的账号
        - generation: 发起查询时的连接代次

        返回:
        - bool: 来源仍有效时为 True
        """
        return (self.adapter.get_client() is client and self.adapter.bot_self_id == account
                and self.adapter.connection_generation() == generation)

    def _remember(self, key: str, identity: BotIdentity | None) -> None:
        """
        保存有界的成功或失败结果

        参数:
        - key: 群 ID, 空字符串表示账号昵称
        - identity: 已核验资料或失败标记
        """
        self._cache[key] = (monotonic() + IDENTITY_TTL, identity)
        self._cache.move_to_end(key)
        while len(self._cache) > 257:
            self._cache.popitem(last=False)

    async def _read(self, action: str, **params: object) -> dict[str, Any]:
        """
        有界读取只读身份动作, 异常降级为空资料

        参数:
        - action: get_login_info 或 get_group_member_info
        - params: 已由适配器账号及当前路由固定的动作参数

        返回:
        - dict[str, Any]: 平台返回的资料, 格式错误或查询失败时为空
        """
        try:
            result = await self.adapter.admin._call(action, timeout=IDENTITY_ACTION_TIMEOUT, **params)
        except Exception as error:
            logger.debug(f"[OneBotIdentity] 自身资料查询失败 action={action}: {type(error).__name__}")
            return {}
        return cast(dict[str, Any], result) if isinstance(result, dict) else {}

    async def resolve(self, self_id: str, group_id: str = "") -> BotIdentity | None:
        """
        查询当前账号昵称及机器人在可访问群中的本群昵称

        参数:
        - self_id: 当前事件固定的机器人账号
        - group_id: 当前群 ID, 私聊为空

        返回:
        - BotIdentity | None: 确认的自身资料, 无法确认昵称时为 None
        """
        if (not self_id.isdecimal() or self_id != self.adapter.bot_self_id
                or (group_id and (not group_id.isdecimal() or not self.adapter.allows_group(group_id)))):
            return None
        client = self.adapter.get_client()
        generation = self.adapter.connection_generation()
        if client is None:
            return None
        try:
            identity = await asyncio.wait_for(self._resolve(client, self_id, group_id, generation), IDENTITY_TIMEOUT)
            return identity if self._current(client, self_id, generation) and (not group_id or self.adapter.allows_group(group_id)) else None
        except asyncio.TimeoutError:
            logger.debug("[OneBotIdentity] 自身资料补全达到总等待上限")
            return None

    async def _resolve(self, client: object, account: str, group_id: str, generation: int) -> BotIdentity | None:
        """
        在缓存锁内合并账号昵称与当前群资料

        参数:
        - client: 查询前固定的客户端
        - account: 查询前固定的机器人账号
        - group_id: 当前群 ID, 私聊为空
        - generation: 查询前固定的连接代次

        返回:
        - BotIdentity | None: 同一连接确认的资料, 连接变化或全部失败时为空
        """
        async with self._lock:
            if not self._current(client, account, generation):
                return None
            if group_id and not self.adapter.allows_group(group_id):
                return None
            if self._client is not client or self._account != account or self._generation != generation:
                self.clear()
                self._client, self._account, self._generation = client, account, generation
            now = monotonic()
            cached = self._cache.get("")
            if cached is None or cached[0] <= now:
                data = await self._read("get_login_info", self_id=account)
                if not self._current(client, account, generation):
                    return None
                nickname = data.get("nickname")
                info = BotIdentity(account, nickname[:128]) if str(data.get("user_id", "")) == account and isinstance(nickname, str) and nickname.strip() else None
                self._remember("", info)
            info = self._cache[""][1]
            if not group_id:
                return info
            cached = self._cache.get(group_id)
            if cached is None or cached[0] <= now:
                data = await self._read("get_group_member_info", self_id=account, group_id=int(group_id), user_id=int(account), no_cache=True)
                if not self._current(client, account, generation) or not self.adapter.allows_group(group_id):
                    return None
                card, nickname = data.get("card"), data.get("nickname")
                group_info = None
                if str(data.get("user_id", "")) == account and str(data.get("group_id", "")) == group_id:
                    group_info = BotIdentity(account, nickname[:128] if isinstance(nickname, str) else "", group_id,
                                             card[:128] if isinstance(card, str) else "")
                self._remember(group_id, group_info)
            group_info = self._cache[group_id][1]
            nickname = info.nickname if info is not None else group_info.nickname if group_info is not None else ""
            card = group_info.group_card if group_info is not None else ""
            return BotIdentity(account, nickname, group_id, card) if nickname.strip() or card.strip() else None
