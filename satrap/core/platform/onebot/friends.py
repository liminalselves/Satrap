"""OneBot 好友协议边界, 不读取任何插件配置"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING
from collections.abc import Awaitable
import json
import asyncio

from satrap.core.friends import FriendError
from satrap.core.platform.onebot.group_chat import _numeric_id
from satrap.core.platform.onebot.admin import UnsupportedAdminAction, AdminActionRejected, AdminActionUnconfirmed
from satrap.core.platform.onebot.request_registry import request_digest
from satrap.core.platform.onebot import request_registry
from satrap.core.config.group_action_origin import current_group_action_preflight, bind_group_action_preflight
from satrap.core.log import logger

SUSPICIOUS_REQUEST_LIMIT = 200
SUSPICIOUS_UNAVAILABLE_REASONS = {
    "invalid_item": "平台返回的申请条目格式无效",
    "invalid_credential": "平台返回的申请缺少有效处理凭据, 无法登记或处理",
    "invalid_applicant": "平台返回的申请人账号格式无效",
    "invalid_details": "平台返回的申请详情格式无效或超出长度限制",
    "ambiguous_identity": "申请缺少时间且与其他来源记录冲突, 无法确认是否为同一申请",
    "registration_unconfirmed": "申请未能完成登记或登记后的申请人身份不一致",
}

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

    async def refresh_suspicious(self, account: str) -> dict[str, Any]:
        """
        补取平台标记的可疑申请, 保留原始事件身份且不重置本地期限

        参数:
        - account: 固定机器人账号

        返回:
        - 查询覆盖证据及本轮确认的申请身份, 凭据不进入公开结果
        """
        generation = self.adapter.connection_generation()
        self.check(account, generation)
        async with self.adapter._message_lookup_slots:
            result = await self.protocol(self.adapter.admin._call("get_doubt_friends_add_request", count=SUSPICIOUS_REQUEST_LIMIT, timeout=5))
        self.check(account, generation)
        if not isinstance(result, list) or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 4 * 1024 * 1024:
            raise FriendError("invalid_response", "可疑申请列表格式无效或超出大小限制")
        identities: set[str] = set()
        unavailable_reasons: dict[str, int] = {}

        def record_unavailable(code: str) -> None:
            """
            统计条目失败原因, 仅记录固定错误码而不输出平台凭据或原始异常

            参数:
            - code: 固定的条目失败原因码
            """
            unavailable_reasons[code] = unavailable_reasons.get(code, 0) + 1
            logger.warning(f"[OneBotFriends] 可疑申请条目已跳过 code={code}; 未记录平台凭据")

        for raw in result[:SUSPICIOUS_REQUEST_LIMIT]:
            reason_code = "invalid_item"
            try:
                if not isinstance(raw, dict):
                    raise ValueError("可疑申请条目必须是对象")
                reason_code = "invalid_credential"
                flag = raw.get("flag") or raw.get("uid")
                if not isinstance(flag, str) or not flag or len(flag) > 4096 or any(char.isspace() for char in flag):
                    raise ValueError("可疑申请缺少有效处理凭据")
                reason_code = "invalid_applicant"
                raw_uid = raw.get("user_id")
                if isinstance(raw_uid, bool):
                    raise ValueError("可疑申请账号格式无效")
                uid = "" if raw_uid in (None, 0, "0", "") else _numeric_id(raw_uid)
                stamp = raw.get("reqTime", raw.get("time"))
                stamp = stamp if type(stamp) is int and stamp > 0 else None
                details = {"request_category": "suspicious", "nickname": raw.get("nick", raw.get("nickname", "")),
                           "request_source": raw.get("source", ""), "suspicious_reason": raw.get("reason", ""), "requested_at": stamp}
                reason_code = "invalid_details"
                comment = raw.get("msg", raw.get("comment", ""))
                if not isinstance(comment, str) or len(comment) > 2000 or any(not isinstance(value, str) or len(value) > 2000 for key, value in details.items() if key != "requested_at"):
                    raise ValueError("可疑申请展示字段无效")
                self.check(account, generation)
                identity = request_digest("friend", account, flag, stamp)
                if stamp is None:
                    existing = await asyncio.to_thread(self.adapter.request_flags.inbox.rows, self.adapter.config.id, account, "friend", request_registry.time())
                    if any(row["digest"] == identity and row["request_category"] != "suspicious" for row in existing):
                        record_unavailable("ambiguous_identity")
                        continue
                reason_code = "registration_unconfirmed"
                await self.adapter.request_flags.register("friend", flag, self_id=account, user_id=uid,
                                                          comment=comment, event_time=stamp, details=details)
                entry = await asyncio.to_thread(self.adapter.request_flags.ledger.lookup, self.adapter.config.id, account, "friend", flag, identity_digest=identity)
                if entry is None or entry["user_id"] != uid:
                    record_unavailable("registration_unconfirmed")
                    continue
                identities.add(identity)
            except ValueError:
                record_unavailable(reason_code)
        self.check(account, generation)
        return {"identities": identities, "coverage": {"state": "queried", "complete": False,
                "truncated": len(result) >= SUSPICIOUS_REQUEST_LIMIT, "unavailable_count": sum(unavailable_reasons.values()),
                "unavailable_reasons": [{"code": code, "message": SUSPICIOUS_UNAVAILABLE_REASONS[code], "count": count}
                                        for code, count in unavailable_reasons.items()],
                "reason": "查询覆盖范围未确认: 平台接口未提供总数或分页证据, 不能证明已覆盖全部申请"}}

    async def requests(self, account: str, limit: int, cursor: str | None, *, view: str, owner_user_id: str, request_category: str) -> dict[str, Any]:
        """
        合并本地普通申请与平台可疑申请, 分别报告查询覆盖范围

        参数:
        - account: 固定账号
        - limit: 每页条数
        - cursor: 固定筛选范围的下一页位置
        - view: 待处理, 归档或全部
        - owner_user_id: 宿主确认的本人范围, 空值为管理范围
        - request_category: all, normal 或 suspicious

        返回:
        - 不含凭据的申请列表和可疑来源查询证据
        """
        coverage: dict[str, Any] = {"state": "not_queried", "complete": False}
        if request_category != "normal" and cursor is None:
            try:
                coverage = (await self.refresh_suspicious(account))["coverage"]
            except FriendError as error:
                logger.warning(f"[OneBotFriends] 可疑申请查询失败 code={error.code}")
                if request_category == "suspicious":
                    raise
                coverage = {"state": error.code, "complete": False, "reason": str(error)}
        result = await self.adapter.request_flags.list_requests("friend", self_id=account, limit=limit, cursor=cursor,
                                                               view=view, owner_user_id=owner_user_id, request_category=request_category)
        if owner_user_id:
            coverage.pop("unavailable_count", None)
            coverage.pop("unavailable_reasons", None)
        return {**result, "request_category": request_category, "coverage": {"complete": False, "suspicious": coverage}}

    async def recheck(self, account: str, request_id: str) -> dict[str, Any]:
        """
        用平台查询补充可疑申请核验, 缺席不解释为已处理

        参数:
        - account: 固定账号
        - request_id: 查询返回的申请 ID

        返回:
        - 当前申请详情和平台核验证据
        """
        registry = self.adapter.request_flags
        row = await asyncio.to_thread(registry.inbox.resolve, self.adapter.config.id, account, "friend", request_id, request_registry.time())
        if row["request_category"] != "suspicious":
            return await registry.recheck_request("friend", request_id, self_id=account)
        proof = await self.refresh_suspicious(account)
        item = await registry.recheck_request("friend", request_id, self_id=account)
        return {**item, "verification": "platform_pending" if row["digest"] in proof["identities"] and item["can_handle"] else "not_confirmed",
                "platform_query_supported": True, "coverage": proof["coverage"]}

    async def handle(self, account: str, request_id: str, approve: bool, remark: str, *,
                     expected_revision: int | None = None, allow_archived: bool = False) -> None:
        """
        用宿主申请 ID 定位凭据并沿用不可重放账本

        参数:
        - account: 固定机器人账号
        - request_id: 宿主收件箱 ID
        - approve: 是否同意
        - remark: 同意后的备注
        - expected_revision: 可选的当前收件箱修订号, 归档处理必填
        - allow_archived: 是否允许已明确确认的归档申请, 默认 False
        """
        generation = self.adapter.connection_generation()
        row = await self.adapter.request_flags.resolve_request("friend", request_id, self_id=account,
                                                              expected_revision=expected_revision, allow_archived=allow_archived)
        self.check(account, generation)
        suspicious = row["request_category"] == "suspicious"
        if suspicious and remark:
            raise FriendError("invalid_parameters", "可疑好友申请不支持设置备注, 请省略 remark")
        if suspicious:
            proof = await self.refresh_suspicious(account)
            if row["digest"] not in proof["identities"]:
                raise FriendError("request_not_confirmed", "本次平台查询未确认这条可疑申请仍待处理, 未执行")
        previous = current_group_action_preflight()

        def verify_record() -> None:
            """发送前复核申请路由和修订号, 变更后拒绝旧审批"""
            if previous is not None:
                previous()
            self.check(account, generation)
            try:
                self.adapter.request_flags.inbox.resolve(self.adapter.config.id, account, "friend", request_id, request_registry.time(), row["revision"])
            except (LookupError, ValueError) as error:
                raise PermissionError("申请记录在发送前已变化, 请重新查询后处理") from error

        self.adapter.request_flags.inbox.resolve(self.adapter.config.id, account, "friend", request_id, request_registry.time(), row["revision"])
        with bind_group_action_preflight(verify_record):
            await self.protocol(self.adapter.admin.handle_friend_request(row["flag"], approve, remark,
                                allow_archived=allow_archived, identity_digest=row["digest"], suspicious=suspicious))
        self.check(account, generation)
