"""OneBot 好友协议边界, 不读取任何插件配置"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING
from collections.abc import Awaitable
import json

from satrap.core.friends import FriendError
from satrap.core.platform.onebot.group_chat import _numeric_id
from satrap.core.platform.onebot.admin import UnsupportedAdminAction, AdminActionRejected, AdminActionUnconfirmed

if TYPE_CHECKING:
    from satrap.core.platform.onebot.adapter import OneBotAdapter


class OneBotFriends:
    """有界好友目录与原有申请账本的协议接缝"""

    def __init__(self, adapter: OneBotAdapter) -> None:
        """
        固定适配器

        参数:
        - adapter: 当前平台实例
        """
        self.adapter = adapter

    def check(self, account: str, generation: int) -> None:
        """
        拒绝账号或连接切换后的迟到结果

        参数:
        - account: 请求账号
        - generation: 请求连接代次
        """
        if account != self.adapter.bot_self_id or generation != self.adapter.connection_generation():
            raise FriendError("stale_account", "机器人账号或平台连接已变化, 请刷新")

    async def protocol(self, operation: Awaitable[Any]) -> Any:
        """
        在适配器边界将协议错误转换为通用好友错误

        参数:
        - operation: 平台协议调用

        返回:
        - 平台结果, 失败时抛通用错误
        """
        try:
            return await operation
        except UnsupportedAdminAction as error:
            raise FriendError("unsupported", "当前平台实现不支持此接口") from error
        except AdminActionRejected as error:
            raise FriendError("platform_rejected", str(error)) from error
        except AdminActionUnconfirmed as error:
            raise FriendError("unconfirmed", "平台未确认结果, 请核查, 不要重复执行") from error

    async def list(self, account: str) -> dict[str, Any]:
        """
        读取真实好友列表并返回完整性证据

        参数:
        - account: 固定机器人账号

        返回:
        - items 与 complete, 不合法或超限条目不会证明某人不是好友
        """
        generation = self.adapter.connection_generation()
        self.check(account, generation)
        async with self.adapter._message_lookup_slots:
            self.check(account, generation)
            result = await self.protocol(self.adapter.admin._call("get_friend_list", timeout=5))
        self.check(account, generation)
        if not isinstance(result, list) or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 4 * 1024 * 1024:
            raise FriendError("invalid_response", "好友列表格式无效或超出大小限制")
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        complete = len(result) <= 10000
        for raw in result[:10000]:
            try:
                if not isinstance(raw, dict):
                    raise ValueError()
                uid = _numeric_id(raw.get("user_id"))
                if uid in seen or any(not isinstance(raw.get(key, ""), str) or len(raw.get(key, "")) > 256 for key in ("nickname", "remark")):
                    raise ValueError()
                seen.add(uid)
                items.append({"user_id": uid, "nickname": raw.get("nickname", ""), "remark": raw.get("remark", "")})
            except ValueError:
                complete = False
        return {"items": items, "complete": complete}

    async def delete(self, account: str, user_id: str) -> None:
        """
        删除单个好友, 不拉黑或修改本地历史

        参数:
        - account: 固定机器人账号
        - user_id: 已确认好友 ID
        """
        generation = self.adapter.connection_generation()
        self.check(account, generation)
        uid = _numeric_id(user_id)
        await self.protocol(self.adapter.admin._call("delete_friend", user_id=int(uid)))
        self.check(account, generation)

    async def handle(self, account: str, request_id: str, approve: bool, remark: str) -> None:
        """
        用宿主申请 ID 定位凭据并沿用不可重放账本

        参数:
        - account: 固定机器人账号
        - request_id: 宿主收件箱 ID
        - approve: 是否同意
        - remark: 同意后的备注
        """
        generation = self.adapter.connection_generation()
        row = await self.adapter.request_flags.resolve_request("friend", request_id, self_id=account)
        self.check(account, generation)
        await self.protocol(self.adapter.admin.handle_friend_request(row["flag"], approve, remark))
        self.check(account, generation)
