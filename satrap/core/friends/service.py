"""账号级好友管理宿主, 人工操作与模型授权共用执行边界"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
import asyncio
import secrets
import time
import traceback

from satrap.core.friends import FriendError
from satrap.core.friends.store import FriendStore
from satrap.core.config.group_action_origin import ModelActionAuthorization, bind_group_action_preflight
from satrap.core.log import logger


def text_id(value: Any, label: str = "ID") -> str:
    """
    校验通用平台 ID, 不假设平台使用数字

    参数:
    - value: 用户或模型输入
    - label: 参数名称

    返回:
    - 有界且不含控制字符的原始 ID
    """
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(ch) < 32 for ch in value) or value != value.strip():
        raise FriendError("invalid_parameters", f"{label} 必须是非空 ID 字符串")
    return value


def page_limit(value: Any) -> int:
    """
    校验分页上限

    参数:
    - value: 查询数量

    返回:
    - 1 到 100 的整数
    """
    if type(value) is not int or not 1 <= value <= 100:
        raise FriendError("invalid_parameters", "limit 必须为 1 到 100 的整数")
    return value


class FriendService:
    """按平台实例隔离的查询快照, 账号保护与写动作协调器"""

    def __init__(self, adapter_id: str, path: Path, adapter: Callable[[], Any],
                 source_check: Callable[[ModelActionAuthorization, str], str], protected: Callable[[], list[str]]) -> None:
        """
        注入宿主平台与当前授权复核

        参数:
        - adapter_id: 固定平台实例
        - path: 平台共用数据库
        - adapter: 读取最新适配器
        - source_check: 从持久配置复核模型写来源
        - protected: 从宿主配置读取受保护的管理账号
        """
        self.adapter_id, self.path = adapter_id, path
        self.adapter_provider, self.source_check, self.protected_provider = adapter, source_check, protected
        self._store: FriendStore | None = None
        self._store_lock = asyncio.Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._cursors: dict[str, tuple[str, int]] = {}
        self._request_cursors: dict[str, tuple[tuple[object, ...], str, float]] = {}
        self._sources: dict[tuple[str, str], tuple[ModelActionAuthorization, str, Any, object, float]] = {}

    async def store(self) -> FriendStore:
        """
        惰性打开动作存储, 每进程只恢复一次

        返回:
        - 当前实例的存储
        """
        async with self._store_lock:
            self._sources = {key: value for key, value in self._sources.items() if value[4] > time.time()}
            if self._store is None:
                self._store = await asyncio.to_thread(FriendStore, self.path)
            return self._store

    def context(self, account: str, capability: str | None = None) -> Any:
        """
        校验当前账号和能力

        参数:
        - account: 前端或宿主固定的账号
        - capability: 本次必须支持的能力

        返回:
        - 当前适配器
        """
        text_id(account, "机器人账号")
        adapter = self.adapter_provider()
        if adapter is None:
            raise FriendError("unavailable", "平台未启动")
        if adapter.friend_account() != account:
            raise FriendError("stale_account", "机器人账号已变化, 请刷新后重新操作")
        if not adapter.config.enable:
            raise FriendError("unavailable", "平台已停用")
        if capability:
            cap = adapter.friend_capabilities().get(capability, {})
            if cap.get("state") not in {"supported", "unknown"}:
                raise FriendError("unsupported" if cap.get("state") == "unsupported" else "unavailable",
                                  cap.get("reason") or "当前平台不支持此操作")
        return adapter

    def check_connection(self, account: str, adapter: Any, generation: object) -> None:
        """
        等待后拒绝旧适配器和旧连接

        参数:
        - account: 固定账号
        - adapter: 调用前的对象
        - generation: 调用前的代次
        """
        if self.context(account) is not adapter or adapter.friend_generation() != generation:
            raise FriendError("stale_account", "平台连接已变化, 请重新查询")

    async def info(self) -> dict[str, Any]:
        """
        返回前端当前账号和好友能力, 无需启动模型插件

        返回:
        - 当前账号, 能力和受保护名单
        """
        adapter = self.adapter_provider()
        account = adapter.friend_account() if adapter else ""
        protected = await self.policy(account) if account else {"protected_friend_ids": [], "manager_ids": []}
        return {"adapter_id": self.adapter_id, "current_account": account,
                "capabilities": adapter.friend_capabilities() if adapter else {}, **protected}

    async def policy(self, account: str, protected: list[str] | None = None) -> dict[str, Any]:
        """
        管理独立于模型工具开关的账号保护名单

        参数:
        - account: 固定机器人账号
        - protected: None 读取, 列表替换额外保护账号

        返回:
        - 人工保护名单与不可移除的管理者保护名单
        """
        self.context(account)
        if protected is not None:
            if not isinstance(protected, list) or len(protected) > 1000:
                raise FriendError("invalid_parameters", "保护名单最多 1000 个 ID")
            protected = [text_id(value, "受保护好友") for value in protected]
        store = await self.store()
        values = await asyncio.to_thread(store.policy, account, protected)
        self.context(account)
        return {"protected_friend_ids": values, "manager_ids": sorted(set(self.protected_provider()))}

    async def directory(self, account: str) -> dict[str, Any]:
        """
        读取经过归一化的真实目录

        参数:
        - account: 固定机器人账号

        返回:
        - 好友目录和完整性证据
        """
        adapter = self.context(account, "list_friends")
        generation = adapter.friend_generation()
        result = await asyncio.wait_for(adapter.friend_list(account), 8)
        self.check_connection(account, adapter, generation)
        if (not isinstance(result, dict) or not isinstance(result.get("items"), list)
                or type(result.get("complete")) is not bool or len(result["items"]) > 10000):
            raise FriendError("invalid_response", "适配器返回了无效的好友目录")
        items = []
        seen = set()
        for raw in result["items"]:
            if not isinstance(raw, dict):
                raise FriendError("invalid_response", "好友资料格式无效")
            uid = text_id(raw.get("user_id"), "好友 ID")
            if uid in seen or any(not isinstance(raw.get(key, ""), str) or len(raw.get(key, "")) > 256 for key in ("nickname", "remark")):
                raise FriendError("invalid_response", "好友资料格式无效或重复")
            seen.add(uid)
            items.append({"user_id": uid, "nickname": raw.get("nickname", ""), "remark": raw.get("remark", "")})
        return {"items": items, "complete": result["complete"]}

    async def list_friends(self, account: str, *, actor: str, query: str = "", limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        """
        在短期快照上分页搜索, 游标绑定账号和调用者

        参数:
        - account: 固定账号
        - actor: 宿主提供的真实调用者
        - query: ID, 昵称或备注, 空字符串列出全部
        - limit: 每页 1 到 100 条
        - cursor: 宿主返回的不透明游标

        返回:
        - 候选, 快照时间和覆盖信息
        """
        page_limit(limit)
        if not isinstance(query, str) or len(query) > 256:
            raise FriendError("invalid_parameters", "搜索文字最多 256 字符")
        adapter = self.context(account, "list_friends")
        generation = adapter.friend_generation()
        now = time.time()
        self._snapshots = {key: value for key, value in self._snapshots.items() if value["expires"] > now}
        self._cursors = {key: value for key, value in self._cursors.items() if value[0] in self._snapshots}
        query = query.strip()
        if cursor is not None:
            if not isinstance(cursor, str) or cursor not in self._cursors:
                raise FriendError("cursor_expired", "翻页位置已失效, 请重新查询")
            snapshot_id, offset = self._cursors[cursor]
            snapshot = self._snapshots[snapshot_id]
            if snapshot["scope"] != (account, actor, query, id(adapter), generation):
                raise FriendError("cursor_expired", "翻页位置不属于当前账号或查询")
        else:
            directory = await self.directory(account)
            self.check_connection(account, adapter, generation)
            candidates = []
            for item in directory["items"]:
                if not query:
                    candidates.append(item)
                    continue
                exact_fields = [field for field in ("user_id", "remark", "nickname") if item[field].casefold() == query.casefold()]
                fields = exact_fields or [field for field in ("remark", "nickname") if query.casefold() in item[field].casefold()]
                if fields:
                    candidates.append({**item, "matched_by": fields, "exact": bool(exact_fields)})
            candidates.sort(key=lambda item: (0 if item.get("exact") and "user_id" in item.get("matched_by", []) else
                                              1 if item.get("exact") else 2, item["user_id"]))
            if len(self._snapshots) >= 16:
                raise FriendError("busy", "查询快照数量已达上限, 请稍后刷新")
            snapshot_id, offset = secrets.token_urlsafe(24), 0
            snapshot = {"scope": (account, actor, query, id(adapter), generation), "items": candidates,
                        "complete": directory["complete"], "time": time.time(), "expires": time.time() + 120}
            self._snapshots[snapshot_id] = snapshot
        items = snapshot["items"][offset:offset + min(limit, 30)]
        next_cursor = None
        more = offset + len(items) < len(snapshot["items"])
        if more:
            next_cursor = secrets.token_urlsafe(24)
            self._cursors[next_cursor] = (snapshot_id, offset + len(items))
        self.check_connection(account, adapter, generation)
        return {"items": items, "has_more": more, "next_cursor": next_cursor, "total_loaded": len(snapshot["items"]),
                "snapshot_at": snapshot["time"], "coverage": {"complete": snapshot["complete"]},
                "ambiguous": bool(query) and len(snapshot["items"]) > 1}

    async def requests(self, account: str, limit: int = 20, cursor: str | None = None, *, actor: str = "panel") -> dict[str, Any]:
        """
        获取当前账号待处理好友申请

        参数:
        - account: 固定账号
        - limit: 每页条数
        - cursor: 收件箱分页位置
        - actor: 宿主提供的真实调用者

        返回:
        - 待处理申请
        """
        page_limit(limit)
        adapter = self.context(account, "list_requests")
        generation = adapter.friend_generation()
        scope = (account, actor, id(adapter), generation)
        self._request_cursors = {key: value for key, value in self._request_cursors.items() if value[2] > time.time()}
        raw_cursor = None
        if cursor is not None:
            entry = self._request_cursors.get(cursor) if isinstance(cursor, str) else None
            if entry is None or entry[0] != scope:
                raise FriendError("cursor_expired", "申请翻页位置已失效或不属于当前调用者")
            raw_cursor = entry[1]
        try:
            result = await adapter.friend_requests(account, limit, raw_cursor)
        except ValueError as error:
            raise FriendError("cursor_expired", "申请列表已变化, 请重新查询") from error
        self.check_connection(account, adapter, generation)
        next_cursor = result.get("next_cursor")
        if next_cursor:
            if len(self._request_cursors) >= 256:
                raise FriendError("busy", "申请查询数量已达上限, 请稍后查询")
            token = secrets.token_urlsafe(24)
            self._request_cursors[token] = (scope, next_cursor, time.time() + 120)
            result = {**result, "next_cursor": token}
        return result

    async def submit(self, account: str, action_id: str, action: str, params: dict[str, Any], *, actor: str,
                     source: ModelActionAuthorization | None = None) -> dict[str, Any]:
        """
        登记人工直接动作或模型待审批删除动作

        参数:
        - account: 固定账号
        - action_id: 幂等动作 ID
        - action: delete_friend 或 handle_request
        - params: 工具契约中的参数
        - actor: panel 或 model
        - source: 模型写工具的可信授权

        返回:
        - 持久动作记录, pending 不表示已执行
        """
        text_id(action_id, "动作 ID")
        if actor not in {"panel", "model"} or not isinstance(action, str) or action not in {"delete_friend", "handle_request"}:
            raise FriendError("invalid_parameters", "好友动作类型无效")
        if not isinstance(params, dict):
            raise FriendError("invalid_parameters", "动作参数必须为对象")
        if action == "delete_friend":
            if set(params) != {"user_id"}:
                raise FriendError("invalid_parameters", "删除好友只接受 user_id")
            target = text_id(params["user_id"], "好友 ID")
        else:
            if set(params) - {"request_id", "approve", "remark"} or not {"request_id", "approve"} <= params.keys():
                raise FriendError("invalid_parameters", "申请参数无效")
            target = text_id(params["request_id"], "申请 ID")
            remark = params.get("remark", "")
            if type(params["approve"]) is not bool or not isinstance(remark, str) or len(remark) > 60 or not params["approve"] and remark:
                raise FriendError("invalid_parameters", "approve 必须为布尔值, remark 最长 60 字符且仅用于同意申请")
            params = {**params, "remark": remark}
        adapter = self.context(account, action)
        generation = adapter.friend_generation()
        fingerprint = ""
        actor_id = "panel"
        if actor == "model":
            if source is None or source.identity.get("self_id") != account or source.identity.get("adapter_id") != self.adapter_id:
                raise FriendError("permission_denied", "模型写操作缺少可信来源")
            fingerprint = self.source_check(source, target)
            actor_id = source.identity.get("actor_id", "model")
        store = await self.store()
        try:
            existing = await asyncio.to_thread(store.get, account, action_id)
        except FriendError as error:
            if error.code != "action_not_found":
                raise
        else:
            if existing["action_type"] != action or existing["params"] != params or existing["actor_kind"] != actor or existing["actor_id"] != actor_id:
                raise FriendError("action_conflict", "相同动作 ID 已用于不同操作")
            return existing
        target_info = await self.check_delete(account, target) if action == "delete_friend" else None
        self.check_connection(account, adapter, generation)
        if source is not None:
            self.source_check(source, target)
        record, created = await asyncio.to_thread(store.register, account, action_id, action, params, actor, target_info, actor_id)
        if not created:
            return record
        if record["state"] == "pending":
            assert source is not None
            self._sources[(account, action_id)] = (source, fingerprint, adapter, generation, record["expires_at"])
            return record
        return await self.execute(account, action_id, "ready", adapter, generation, source, fingerprint)

    async def check_delete(self, account: str, target: str) -> dict[str, Any]:
        """
        复核目标真实好友关系和管理入口保护

        参数:
        - account: 固定账号
        - target: 确认的好友 ID

        返回:
        - 目标真实资料
        """
        policy = await self.policy(account)
        if target == account or target in policy["protected_friend_ids"] or target in policy["manager_ids"]:
            raise FriendError("protected_friend", "此账号受保护, 不允许删除")
        directory = await self.directory(account)
        row = next((item for item in directory["items"] if item["user_id"] == target), None)
        if row is None:
            raise FriendError("not_friend" if directory["complete"] else "incomplete_directory",
                              "目标不是当前机器人的好友" if directory["complete"] else "目录不完整, 无法确认目标好友")
        return row

    async def decide(self, account: str, action_id: str, approve: bool) -> dict[str, Any]:
        """
        人工批准或拒绝模型删除申请

        参数:
        - account: 固定账号
        - action_id: 待审批动作
        - approve: 是否批准

        返回:
        - 最新动作状态
        """
        if type(approve) is not bool:
            raise FriendError("invalid_parameters", "approve 必须为布尔值")
        self.context(account)
        store = await self.store()
        record = await asyncio.to_thread(store.get, account, action_id)
        if record["state"] != "pending":
            return record
        if not approve:
            await asyncio.to_thread(store.transition, account, action_id, "pending", "rejected")
            self._sources.pop((account, action_id), None)
            return await asyncio.to_thread(store.get, account, action_id)
        origin = self._sources.get((account, action_id))
        if origin is None:
            await asyncio.to_thread(store.transition, account, action_id, "pending", "expired")
            return await asyncio.to_thread(store.get, account, action_id)
        source, fingerprint, adapter, generation, _ = origin
        return await self.execute(account, action_id, "pending", adapter, generation, source, fingerprint)

    async def execute(self, account: str, action_id: str, expected: str, adapter: Any, generation: object,
                      source: ModelActionAuthorization | None, fingerprint: str) -> dict[str, Any]:
        """
        原子占用后执行一次, 超时和取消保守结算为未知

        参数:
        - account: 固定账号
        - action_id: 动作 ID
        - expected: 占用前状态
        - adapter: 提交时的平台对象
        - generation: 提交时的连接代次
        - source: 模型授权或人工 None
        - fingerprint: 提交时的权限指纹

        返回:
        - 已结算的动作记录
        """
        store = await self.store()
        record = await asyncio.to_thread(store.get, account, action_id)
        params = record["params"]
        target = params.get("user_id", params.get("request_id", ""))
        def preflight() -> None:
            """在协议发送的同一协程中最后复核账号和来源权限"""
            self.check_connection(account, adapter, generation)
            self.context(account, record["action_type"])
            if source is not None and self.source_check(source, target) != fingerprint:
                raise FriendError("permission_denied", "模型来源授权已变化, 请重新申请")
            if record["action_type"] == "delete_friend" and (target in self.protected_provider() or target in store.policy(account)):
                raise FriendError("protected_friend", "目标好友已受保护")
        occupied = False
        try:
            preflight()
            if record["action_type"] == "delete_friend":
                await self.check_delete(account, target)
            preflight()
            occupied = await asyncio.to_thread(store.transition, account, action_id, expected, "executing")
            if not occupied:
                return await asyncio.to_thread(store.get, account, action_id)
            with bind_group_action_preflight(preflight):
                if record["action_type"] == "delete_friend":
                    await adapter.friend_delete(account, target)
                else:
                    await adapter.friend_handle(account, target, params["approve"], params.get("remark", ""))
            result: dict[str, Any] = {"message": "平台返回成功", "verification": "not_verified"}
            if record["action_type"] == "delete_friend":
                self._snapshots.clear()
                self._cursors.clear()
                try:
                    directory = await self.directory(account)
                    if directory["complete"]:
                        result["verification"] = "confirmed" if not any(row["user_id"] == target for row in directory["items"]) else "still_present"
                except Exception as error:
                    logger.warning(f"[好友管理] 执行后核查失败 action_id={action_id} reason={type(error).__name__}")
            await asyncio.to_thread(store.transition, account, action_id, "executing", "succeeded", result)
            logger.info(f"[好友管理] 动作完成 adapter={self.adapter_id} account={account} action={record['action_type']} action_id={action_id}")
        except asyncio.CancelledError:
            if occupied:
                await asyncio.shield(asyncio.to_thread(store.transition, account, action_id, "executing", "unknown", {"message": "操作被中断, 结果未确认"}))
            raise
        except Exception as error:
            code, message, unknown = failure(error)
            logger.warning(f"[好友管理] 动作失败 action_id={action_id} code={code} reason={message}")
            await asyncio.to_thread(store.transition, account, action_id, "executing" if occupied else expected,
                                    "unknown" if occupied and unknown else "failed", {"code": code, "message": message})
        finally:
            self._sources.pop((account, action_id), None)
        return await asyncio.to_thread(store.get, account, action_id)

    async def actions(self, account: str, page: int = 1, limit: int = 20) -> dict[str, Any]:
        """
        查询账号动作记录

        参数:
        - account: 固定账号
        - page: 页码
        - limit: 每页条数

        返回:
        - 分页记录
        """
        self.context(account)
        page_limit(limit)
        if type(page) is not int or not 1 <= page <= 100000:
            raise FriendError("invalid_parameters", "页码无效")
        store = await self.store()
        result = await asyncio.to_thread(store.list, account, page, limit)
        self.context(account)
        return result


def failure(error: Exception) -> tuple[str, str, bool]:
    """
    将平台异常收窄为公共错误, 不泄露原始响应

    参数:
    - error: 操作失败

    返回:
    - 稳定错误码, 公开说明, 是否可能已经执行
    """
    if isinstance(error, FriendError):
        return error.code, str(error), error.code in {"stale_account", "unconfirmed"}
    if isinstance(error, PermissionError):
        return "permission_denied", "当前操作未获授权或权限已变化", False
    if isinstance(error, LookupError):
        return "request_expired", "申请已过期, 已处理或不属于当前账号", False
    if isinstance(error, ValueError):
        return "invalid_parameters", str(error), False
    if isinstance(error, TimeoutError):
        return "unconfirmed", "平台未确认结果, 请核查, 不要重复执行", True
    logger.error(f"[好友管理] 未预期异常: {traceback.format_exc()}")
    return "unavailable", "操作暂不可用, 请查看后端日志", True
