"""OneBot 群管理同步和异步模型工具, 权限以来源身份为准, 写操作默认关闭"""
from __future__ import annotations

from collections.abc import Awaitable, Coroutine
from typing import Any, TypeVar, cast, overload

import asyncio
import traceback
from dataclasses import replace

from satrap.core.platform.onebot.admin import OneBotAdmin, PlatformAdminError, UnsupportedAdminAction
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.call_context import CallOrigin, require_call_origin
from satrap.core.config.group_action_origin import ModelActionAuthorization, bind_model_action_authorization
from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.platform import current_adapter_manager
from satrap.edictum import AsyncSimpleSession
from satrap.core.log import logger
from satrap.core.config.model_tool_authorization import config_ids as _lines, bind_tool_session, model_tool_authorization
from satrap.core.config.group_approval import model_plugin_requires_approval

_ACTION_RESULT_DESCRIPTION = "; 返回动作记录时, pending 表示等待批准, succeeded 表示已执行; 失败或结果未知时不要说操作成功"

_DEFINITIONS: dict[str, tuple[str, dict[str, tuple[str, str]], list[str], bool, bool]] = {
    # 工具名: (描述, 参数, 必填参数, 是否写操作, 是否需要群上下文)
    "group_admin_get_honors": ("查看群里的龙王, 群聊之火等荣誉及对应成员; 不指定荣誉类型时查询全部类型", {
        "group_id": ("string", "要查看的群号, 不填则查看当前群"),
        "honor_type": ("string", "荣誉类型: all 全部, talkative 龙王, performer 群聊之火, legend 群聊炽焰, strong_newbie 冒尖小春笋, emotion 快乐源泉; 不填默认 all"),
    }, [], False, True),
    "group_admin_get_forward": ("查看群里一条合并转发消息的内容; 同时提供转发 ID 和群里包含它的消息 ID. 转发中的其他合并转发不会自动展开", {
        "forward_id": ("string", "合并转发内容的 ID, 从包含转发的群消息中取得"),
        "source_message_id": ("string", "群里包含这条合并转发的消息 ID, 用来确认转发属于目标群"),
        "group_id": ("string", "消息所在的群号, 不填则使用当前群"),
    }, ["forward_id", "source_message_id"], False, True),
    "group_admin_send_forward": ("把多段文字作为一条合并转发消息发到群里; 需要把长内容分段展示时使用. 发送是否成功以实际返回结果为准, 结果未知时不要重复发送", {
        "nodes": ("array", "按显示顺序填写 1 到 30 段内容, 每项为 {content: 文字, name: 可选显示名称}; 每段文字 1 到 2000 字符"),
        "group_id": ("string", "接收消息的群号, 不填则发送到当前群"),
    }, ["nodes"], True, True),
    "group_admin_recall_message": ("撤回群里的一条指定消息, 需要填写该消息的 ID; 能否撤回取决于机器人权限和平台限制" + _ACTION_RESULT_DESCRIPTION, {
        "message_id": ("string", "要撤回的消息 ID, 从聊天上下文或消息查询结果中取得"), "group_id": ("string", "消息所在的群号, 不填则使用当前群"),
    }, ["message_id"], True, True),
    "group_admin_kick": ("将指定成员移出群聊; 操作前确认对方的 QQ 号. 默认允许对方重新申请入群" + _ACTION_RESULT_DESCRIPTION, {
        "user_id": ("string", "要移出群聊的成员 QQ 号, 从已确认的成员资料中取得"), "group_id": ("string", "要执行操作的群号, 不填则使用当前群"),
        "reject_add_request": ("boolean", "是否拒绝对方之后的加群申请; true 拒绝, false 允许重新申请, 不填默认 false"),
    }, ["user_id"], True, True),
    "group_admin_ban": ("禁言指定群成员, 或解除该成员的禁言; duration 填 0 表示解除禁言" + _ACTION_RESULT_DESCRIPTION, {
        "user_id": ("string", "要禁言或解除禁言的成员 QQ 号, 从已确认的成员资料中取得"), "duration": ("number", "禁言时长, 单位为秒; 600 表示十分钟, 0 表示解除禁言. 不填默认 1800, 最长 2592000 秒 (30 天)"),
        "group_id": ("string", "要执行操作的群号, 不填则使用当前群"),
    }, ["user_id"], True, True),
    "group_admin_whole_ban": ("开启或关闭整个群的全员禁言, 与对单个成员禁言不同" + _ACTION_RESULT_DESCRIPTION, {
        "enable": ("boolean", "true 开启全员禁言, false 关闭全员禁言"), "group_id": ("string", "要执行操作的群号, 不填则使用当前群"),
    }, ["enable"], True, True),
    "group_admin_ban_anonymous": ("禁言群里某条匿名消息的发送者; 需要消息中提供的匿名身份标识, 不能用昵称代替" + _ACTION_RESULT_DESCRIPTION, {
        "flag": ("string", "匿名消息里的 anonymous flag, 必须使用平台提供的原值"), "duration": ("number", "禁言时长, 单位为秒; 不填默认 1800, 最长 2592000 秒 (30 天)"),
        "group_id": ("string", "匿名消息所在的群号, 不填则使用当前群"),
    }, ["flag"], True, True),
    "group_admin_set_admin": ("将指定成员设为群管理员, 或取消其管理员身份; 需要机器人具有群主权限" + _ACTION_RESULT_DESCRIPTION, {
        "user_id": ("string", "要设置或取消管理员身份的成员 QQ 号"), "enable": ("boolean", "true 设为管理员, false 取消管理员身份"),
        "group_id": ("string", "要执行操作的群号, 不填则使用当前群"),
    }, ["user_id", "enable"], True, True),
    "group_admin_set_anonymous": ("开启或关闭群内的匿名聊天; 是否支持取决于平台" + _ACTION_RESULT_DESCRIPTION, {
        "enable": ("boolean", "true 允许匿名聊天, false 关闭匿名聊天"), "group_id": ("string", "要执行操作的群号, 不填则使用当前群"),
    }, ["enable"], True, True),
    "group_admin_set_group_nickname": ("修改指定成员在群内显示的群昵称; nickname 填空字符串表示清空群昵称" + _ACTION_RESULT_DESCRIPTION, {
        "user_id": ("string", "要修改群昵称的成员 ID"), "nickname": ("string", "新的群昵称, 不超过 60 字符; 填写空字符串可清空"),
        "group_id": ("string", "成员所在的群号, 不填则使用当前群"),
    }, ["user_id", "nickname"], True, True),
    "group_admin_set_name": ("修改整个群的名称; 修改某位成员的群昵称请使用 group_admin_set_group_nickname" + _ACTION_RESULT_DESCRIPTION, {
        "name": ("string", "新的群名称, 1 到 60 字符, 不能只填空格"), "group_id": ("string", "要改名的群号, 不填则使用当前群"),
    }, ["name"], True, True),
    "group_admin_set_title": ("设置指定群成员的专属头衔; title 填空字符串表示清除头衔, 需要机器人具有相应权限" + _ACTION_RESULT_DESCRIPTION, {
        "user_id": ("string", "要设置头衔的成员 QQ 号"), "title": ("string", "新的专属头衔, 不超过 18 字符; 填写空字符串可清除"),
        "group_id": ("string", "成员所在的群号, 不填则使用当前群"),
    }, ["user_id"], True, True),
    "group_admin_leave": ("让机器人退出指定群; 机器人是群主时可选择解散整个群, 解散会影响所有成员" + _ACTION_RESULT_DESCRIPTION, {
        "group_id": ("string", "机器人要退出或解散的群号, 不填则使用当前群"),
        "dismiss": ("boolean", "false 只退出群聊, true 解散整个群; 不填默认 false"),
    }, [], True, True),
    "group_admin_list_group_requests": ("查看当前群尚可处理的加群申请或邀请, 返回申请 ID, 申请人, 验证信息和有效期限; 管理者私聊查询时需指定群号", {
        "group_id": ("string", "申请所属群号, 群聊中不填则查询当前群"),
        "limit": ("integer", "最多查看多少条, 不填默认 20, 最大 100"),
        "cursor": ("string", "继续查看时填写上次返回的 next_cursor, 沿用相同群号"),
    }, [], False, True),
    "group_admin_handle_group_request": ("同意或拒绝查询结果中的加群申请或入群邀请; 类型和目标群由原申请确定, 不需要填写平台 flag" + _ACTION_RESULT_DESCRIPTION, {
        "request_id": ("string", "加群申请查询返回的申请 ID"),
        "approve": ("boolean", "true 同意该申请或邀请, false 拒绝"), "reason": ("string", "拒绝时填写的理由, 可不填"),
        "group_id": ("string", "申请或邀请对应的群号; 不填时从原请求记录中确定"),
    }, ["request_id", "approve"], True, True),
}


def _as_bool(value: Any, name: str) -> bool:
    """严格解析布尔参数, 拒绝真值语义"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{name} 必须为布尔值")


def _resolve(config: dict[str, Any], write: bool) -> tuple[Any, CallOrigin, list[str]]:
    """
    校验来源身份, 写操作开关, 调用者与群范围, 返回来源适配器

    参数:
    - config: 插件配置
    - write: 是否为写操作

    返回:
    - tuple: (适配器, 调用来源, 允许的群列表)
    """
    origin = require_call_origin()
    if write and config.get("write_tools_enabled") is not True:
        raise PermissionError("管理写操作未在插件配置中开启")
    callers = _lines(config.get("allowed_callers" if write else "allowed_read_callers"))
    if write and not callers:
        raise PermissionError("管理写操作要求 allowed_callers 显式列出调用者, 留空拒绝写操作")
    if callers and origin.actor_id not in callers:
        raise PermissionError("当前调用者不在管理动作允许范围内")
    manager = current_adapter_manager()
    adapter = manager.get_adapter(origin.adapter_id) if manager is not None else None
    if adapter is None:
        raise ValueError("来源平台实例不存在或未运行")
    if not isinstance(getattr(adapter, "admin", None), OneBotAdmin):
        raise UnsupportedAdminAction("当前平台不支持 OneBot 群管理动作")
    return adapter, origin, _lines(config.get("allowed_groups"))


def _group_id(origin: CallOrigin, allowed: list[str], kwargs: dict[str, Any]) -> str:
    """
    解析目标群, 默认当前群聊上下文, 私聊必须显式指定

    参数:
    - origin: 调用来源
    - allowed: 插件允许的群列表, 空列表不额外限制
    - kwargs: 工具参数

    返回:
    - str: 目标群号
    """
    raw = str(kwargs.get("group_id") or "").strip()
    group_id = raw or (origin.chat_id if origin.chat_type == "GroupMessage" else "")
    if not group_id:
        raise ValueError("当前不是群聊上下文, 必须显式指定 group_id")
    if allowed and group_id not in allowed:
        raise PermissionError("目标群不在插件允许范围内")
    return group_id


def _request_access(tool: Any, origin: CallOrigin, adapter: Any) -> None:
    """
    对申请查询和处理单独授权, 不跨目标群读取申请

    参数:
    - tool: 当前管理工具及配置
    - origin: 宿主提供的真实发言者
    - adapter: 当前来源平台实例
    """
    current_adapter, _, _ = _resolve(tool.config, _DEFINITIONS[str(tool.tool_name)][3])
    if current_adapter is not adapter or not tool.is_enabled():
        raise PermissionError("申请工具已停用或平台实例已变化")
    managers = _lines(tool.config.get("request_managers"))
    if not managers or origin.actor_id not in managers:
        raise PermissionError("当前调用者未配置为申请管理者")
    if not origin.self_id or adapter.bot_self_id != origin.self_id:
        raise PermissionError("申请来源机器人账号已变化或未确认")


def _write_authorization(tool: Any, admin: OneBotAdmin, origin: CallOrigin) -> ModelActionAuthorization:
    """
    用本插件配置复核管理写授权, 来源存活校验由宿主完成

    参数:
    - tool: 当前管理工具
    - admin: 当前平台动作集
    - origin: 宿主冻结的调用来源

    返回:
    - 可交给宿主审批的写授权
    """
    def permission(live: Any, target_group: str) -> None:
        """
        从管理插件当前配置复核目标与调用者

        参数:
        - live: 仍有效的管理工具
        - target_group: 固定目标群
        """
        current_adapter, _, groups = _resolve(live.config, True)
        if live.tool_name == "group_admin_handle_group_request":
            _request_access(live, origin, current_adapter)
        if current_adapter.admin is not admin or current_adapter.bot_self_id != origin.self_id:
            raise PermissionError("模型管理工具的机器人账号或平台已变化")
        _group_id(origin, groups, {"group_id": target_group})
    source = model_tool_authorization(tool, origin, "group_admin", permission)
    return replace(source, approval_required=model_plugin_requires_approval(str(tool.tool_name), tool.config))


def _build_call(name: str, admin: OneBotAdmin, origin: CallOrigin, allowed: list[str],
                kwargs: dict[str, Any], source_tool: Any) -> Coroutine[Any, Any, Any]:
    """
    按工具名组装管理动作协程, 群目标默认取当前群

    参数:
    - name: 工具名
    - admin: OneBot 管理动作集
    - origin: 调用来源
    - allowed: 允许的群列表
    - kwargs: 工具参数
    - source_tool: 当前来源工具, 用于等待后和审批时复核权限

    返回:
    - Coroutine: 待执行的管理动作
    """
    if name == "group_admin_set_group_nickname":
        if set(kwargs) - set(_DEFINITIONS[name][1]) or "nickname" not in kwargs:
            raise ValueError("群昵称修改必须使用 nickname, 不接受旧参数或未知参数")
        if not isinstance(kwargs["nickname"], str) or len(kwargs["nickname"]) > 60:
            raise ValueError("nickname 必须是最多 60 字符的文本")
    if name == "group_admin_list_group_requests" or (
            name == "group_admin_handle_group_request" and "request_id" in kwargs):
        async def request_call() -> Any:
            """
            先解析申请身份, 等待后重验权限并沿用原有审批执行路径

            返回:
            - 有界申请列表或管理动作结果
            """
            if set(kwargs) - set(_DEFINITIONS[name][1]):
                raise ValueError("申请工具含有未知参数")
            adapter = admin._adapter
            _request_access(source_tool, origin, adapter)
            if name.startswith("group_admin_list_"):
                gid = _group_id(origin, allowed, kwargs)
                if gid and not adapter.allows_group(gid):
                    raise PermissionError("申请目标群不在平台可管理范围内")
                result = await adapter.request_flags.list_requests("group", self_id=origin.self_id, group_id=gid,
                                                                  limit=kwargs.get("limit", 20), cursor=kwargs.get("cursor"))
                _request_access(source_tool, origin, adapter)
                if gid:
                    _, _, current_groups = _resolve(source_tool.config, False)
                    _group_id(origin, current_groups, {"group_id": gid})
                    if not adapter.allows_group(gid):
                        raise PermissionError("申请目标群不在平台可管理范围内")
                return result
            row = await adapter.request_flags.resolve_request("group", kwargs["request_id"], self_id=origin.self_id)
            _request_access(source_tool, origin, adapter)
            values = {key: value for key, value in kwargs.items() if key != "request_id"}
            values["flag"] = row["flag"]
            if kwargs.get("group_id") and str(kwargs["group_id"]) != row["group_id"]:
                raise PermissionError("目标群与原申请不符")
            if origin.chat_type == "GroupMessage" and origin.chat_id != row["group_id"]:
                raise PermissionError("群申请不属于当前群")
            values.update(group_id=row["group_id"], sub_type=row["sub_type"])
            _, _, current_groups = _resolve(source_tool.config, True)
            return await _build_call(name, admin, origin, current_groups, values, source_tool)
        return request_call()
    if name == "group_admin_handle_group_request":
        _request_access(source_tool, origin, admin._adapter)
    needs_group = _DEFINITIONS[name][4]
    if name == "group_admin_handle_group_request" and not str(kwargs.get("group_id") or "").strip():
        flag = str(kwargs.get("flag") or "")
        adapter = admin._adapter
        entry = adapter.request_flags.ledger.lookup(adapter.config.id, adapter.bot_self_id, "group", flag)
        if entry is None:
            raise ValueError("群请求目标无法从原请求账本确认, 请显式指定 group_id")
        target = str(entry["group_id"])
        gid = _group_id(origin, allowed, {"group_id": target})
    else:
        gid = _group_id(origin, allowed, kwargs) if needs_group else ""
    action_names = {
        "group_admin_recall_message": "recall_message", "group_admin_kick": "kick_group_member",
        "group_admin_ban": "ban_group_member", "group_admin_whole_ban": "set_group_whole_ban",
        "group_admin_ban_anonymous": "ban_anonymous", "group_admin_set_admin": "set_group_admin",
        "group_admin_set_anonymous": "set_group_anonymous", "group_admin_set_group_nickname": "set_group_card",
        "group_admin_set_name": "set_group_name", "group_admin_set_title": "set_group_special_title",
        "group_admin_leave": "leave_group", "group_admin_handle_group_request": "handle_group_request",
    }
    handler = getattr(admin._adapter, "group_action_handler", None)
    if model_plugin_requires_approval(name, source_tool.config) and not callable(handler):
        raise PermissionError("高危动作需要持久审批, 当前审批服务尚未装配")
    if name in action_names and callable(handler):
        action = action_names[name]
        if name == "group_admin_set_group_nickname":
            kwargs = {**kwargs, "card": kwargs["nickname"]}
        from satrap.core.platform.onebot.group_action_types import ACTION_FIELDS

        params: dict[str, object] = {
            key: value for key, value in kwargs.items() if key in ACTION_FIELDS[action]
        }
        for key in ("enable", "approve", "dismiss", "reject_add_request"):
            if key in params:
                params[key] = _as_bool(params[key], key)

        async def submit() -> dict[str, Any]:
            """把已授权的模型群管理请求交给统一审批与执行服务"""
            source = _write_authorization(source_tool, admin, origin)
            with bind_model_action_authorization(source):
                return await cast(Awaitable[dict[str, Any]], handler(gid, action, params))

        return submit()
    if name == "group_admin_get_honors":
        return admin.get_group_honor_info(gid, str(kwargs.get("honor_type") or "all"))
    if name == "group_admin_get_forward":
        source_id = str(kwargs.get("source_message_id") or "").strip()
        if not source_id:
            # 缺少来源消息 ID 时无法证明转发对象归属, 不提供不安全兼容放行
            raise ValueError("必须提供 source_message_id: 该转发所在群消息的 ID")
        return admin.get_forward_message(gid, kwargs.get("forward_id", ""), source_id)
    if name == "group_admin_send_forward":
        # request_id 取自调用上下文而不是模型参数: 工具发送要归并进同一请求的发送结论
        return admin.send_group_forward(gid, kwargs.get("nodes"), origin.request_id)
    if name == "group_admin_recall_message":
        return admin.recall_message(gid, kwargs.get("message_id", ""))
    if name == "group_admin_kick":
        return admin.kick_group_member(gid, kwargs.get("user_id", ""), _as_bool(kwargs.get("reject_add_request", False), "reject_add_request"))
    if name == "group_admin_ban":
        return admin.ban_group_member(gid, kwargs.get("user_id", ""), kwargs.get("duration", 1800))
    if name == "group_admin_whole_ban":
        return admin.set_group_whole_ban(gid, _as_bool(kwargs.get("enable"), "enable"))
    if name == "group_admin_ban_anonymous":
        return admin.ban_anonymous(gid, kwargs.get("flag", ""), kwargs.get("duration", 1800))
    if name == "group_admin_set_admin":
        return admin.set_group_admin(gid, kwargs.get("user_id", ""), _as_bool(kwargs.get("enable"), "enable"))
    if name == "group_admin_set_anonymous":
        return admin.set_group_anonymous(gid, _as_bool(kwargs.get("enable"), "enable"))
    if name == "group_admin_set_group_nickname":
        return admin.set_group_card(gid, kwargs.get("user_id", ""), kwargs.get("nickname"))
    if name == "group_admin_set_name":
        return admin.set_group_name(gid, kwargs.get("name", ""))
    if name == "group_admin_set_title":
        return admin.set_group_special_title(gid, kwargs.get("user_id", ""), str(kwargs.get("title", "")))
    if name == "group_admin_leave":
        return admin.leave_group(gid, _as_bool(kwargs.get("dismiss", False), "dismiss"))
    return admin.handle_group_request(gid, kwargs.get("flag", ""), str(kwargs.get("sub_type", "")), _as_bool(kwargs.get("approve"), "approve"), str(kwargs.get("reason", "")))


class _GroupAdminMixin:
    """权限解析与执行归一, 同步入口把协程桥接到平台事件循环"""

    tool_name: str | None
    config: dict[str, Any]

    def is_available_for_call(self) -> bool:
        """
        仅向指定申请管理者展示加群申请工具

        返回:
        - 当前请求可用时为 True
        """
        name = str(self.tool_name)
        if name not in {"group_admin_list_group_requests", "group_admin_handle_group_request"}:
            return True
        try:
            adapter, origin, _ = _resolve(self.config, _DEFINITIONS[name][3])
            _request_access(self, origin, adapter)
            return True
        except (PermissionError, ValueError, PlatformAdminError):
            return False

    def _complete_definition(self, definition: dict[str, Any]) -> dict[str, Any]:
        if not definition or self.tool_name is None:
            return definition
        definition["function"]["parameters"]["required"] = _DEFINITIONS[self.tool_name][2]
        definition["function"]["parameters"]["additionalProperties"] = False
        return definition

    async def _run(self, **kwargs: Any) -> dict[str, Any]:
        """在当前上下文完成身份校验后执行管理动作"""
        name = str(self.tool_name)
        write = _DEFINITIONS[name][3]
        adapter, origin, allowed = _resolve(self.config, write)
        result = await _build_call(name, adapter.admin, origin, allowed, kwargs, self)
        if write:
            logger.info(f"[group_admin] 写动作完成 tool={name} actor={origin.actor_id} chat={origin.chat_id}")
        return {"status": "ok", "data": result} if result is not None else {"status": "ok"}

    def _execute(self, **kwargs: Any) -> dict[str, Any]:
        try:
            name = str(self.tool_name)
            write = _DEFINITIONS[name][3]
            adapter, origin, allowed = _resolve(self.config, write)
            loop = getattr(adapter, "_loop", None)
            if loop is None or loop.is_closed():
                raise ValueError("平台事件循环不可用")
            coro = _build_call(name, adapter.admin, origin, allowed, kwargs, self)
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                result = future.result(timeout=15)
            except TimeoutError:
                # 超时后取消协程, 避免写动作在平台循环里继续生效却被报为失败
                future.cancel()
                logger.warning(f"[group_admin] 动作超时已取消 tool={name} actor={origin.actor_id}")
                return {"status": "unconfirmed", "error": "动作超时, 结果未知"}
            if write:
                logger.info(f"[group_admin] 写动作完成 tool={name} actor={origin.actor_id} chat={origin.chat_id}")
            return {"status": "ok", "data": result} if result is not None else {"status": "ok"}
        except UnsupportedAdminAction as error:
            return {"status": "unsupported", "error": str(error)}
        except PermissionError as error:
            logger.debug(f"[group_admin] 权限拒绝 tool={self.tool_name}: {error}")
            return {"status": "error", "error": str(error)}
        except PlatformAdminError as error:
            logger.warning(f"[group_admin] 动作失败 tool={self.tool_name}: {type(error).__name__}: {error}")
            return {"status": "error", "error": str(error)}
        except (ValueError, LookupError) as error:
            logger.warning(f"[group_admin] 动作参数或申请已失效 tool={self.tool_name}: {error}")
            return {"status": "error", "error": str(error)}
        except Exception:
            logger.error(f"[group_admin] 动作异常 tool={self.tool_name}: {traceback.format_exc()}")
            return {"status": "error", "error": "管理操作暂不可用, 请查看后端日志"}


class GroupAdminTool(_GroupAdminMixin, Tool):
    """同步群管理工具"""

    def get_tool_defined(self) -> dict[str, Any]:
        return self._complete_definition(super().get_tool_defined())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self._execute(**kwargs)


class AsyncGroupAdminTool(_GroupAdminMixin, AsyncTool):
    """异步群管理工具"""

    def get_tool_defined(self) -> dict[str, Any]:
        return self._complete_definition(super().get_tool_defined())

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        name = str(self.tool_name)
        try:
            return await self._run(**kwargs)
        except UnsupportedAdminAction as error:
            return {"status": "unsupported", "error": str(error)}
        except PermissionError as error:
            logger.debug(f"[group_admin] 权限拒绝 tool={name}: {error}")
            return {"status": "error", "error": str(error)}
        except PlatformAdminError as error:
            logger.warning(f"[group_admin] 动作失败 tool={name}: {type(error).__name__}: {error}")
            return {"status": "error", "error": str(error)}
        except (ValueError, LookupError) as error:
            logger.warning(f"[group_admin] 动作参数或申请已失效 tool={name}: {error}")
            return {"status": "error", "error": str(error)}
        except Exception:
            logger.error(f"[group_admin] 动作异常 tool={name}: {traceback.format_exc()}")
            return {"status": "error", "error": "管理操作暂不可用, 请查看后端日志"}


_AnyGroupAdminTool = TypeVar("_AnyGroupAdminTool", GroupAdminTool, AsyncGroupAdminTool)


def _build_tools(kind: type[_AnyGroupAdminTool], config: dict[str, Any]) -> list[_AnyGroupAdminTool]:
    """按工具基类批量构造定义表中的工具"""
    result: list[_AnyGroupAdminTool] = []
    for name, (description, params, _, _, _) in _DEFINITIONS.items():
        tool = kind(name, description, params)
        tool.recovery_policy = "retry" if not _DEFINITIONS[name][3] else "manual"
        tool.config = config
        result.append(tool)
    return result


@overload
def get_tools(session: AsyncSimpleSession, config: dict[str, Any], resources: Any = None) -> list[AsyncGroupAdminTool]: ...
@overload
def get_tools(session: Session, config: dict[str, Any], resources: Any = None) -> list[GroupAdminTool]: ...
@overload
def get_tools(session: AsyncSession, config: dict[str, Any], resources: Any = None) -> list[GroupAdminTool] | list[AsyncGroupAdminTool]: ...
def get_tools(session: Session | AsyncSession, config: dict[str, Any], resources: Any = None) -> list[GroupAdminTool] | list[AsyncGroupAdminTool]:
    """创建平台管理工具并保留审批时可复核的来源会话引用"""
    tools = (_build_tools(AsyncGroupAdminTool, config) if isinstance(session, AsyncSimpleSession)
             else _build_tools(GroupAdminTool, config))
    for tool in tools:
        bind_tool_session(tool, session)
    return tools
