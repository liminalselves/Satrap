"""OneBot 群管理同步和异步模型工具, 权限以来源身份为准, 写操作默认关闭"""
from __future__ import annotations

from collections.abc import Awaitable, Coroutine
from typing import Any, TypeVar, cast, overload

import traceback
from dataclasses import replace

from satrap.core.platform.onebot.admin import (OneBotAdmin, AdminActionRejected, AdminActionUnconfirmed,
                                                PlatformAdminError, UnsupportedAdminAction)
from satrap.core.platform.onebot.group_action_types import ACTION_FIELDS
from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop
from satrap.core.utils.TCBuilder import AsyncTool, Tool, strict_tool_definition, tool_error
from satrap.core.call_context import CallOrigin, require_call_origin, is_group_origin
from satrap.core.config.group_action_origin import ModelActionAuthorization, bind_model_action_authorization
from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.platform import current_adapter_manager
from satrap.edictum import AsyncSimpleSession
from satrap.core.log import logger
from satrap.core.config.model_tool_authorization import config_ids as _lines, bind_tool_session, model_tool_authorization
from satrap.core.config.group_approval import model_plugin_requires_approval
from satrap.core.plugin_authorization import PluginEntryBinding, authorize_plugin_entry, require_plugin_entry_permission, bind_plugin_factory_tools
from satrap.edictum.plugin_resources import PluginResources

_ACTION_RESULT_DESCRIPTION = "; 返回动作记录时, pending 表示等待批准, succeeded 表示已执行; 失败或结果未知时不要说操作成功"

_DEFINITIONS: dict[str, tuple[str, dict[str, tuple[str, str]], list[str], bool, bool]] = {
    # 工具名: (描述, 参数, 必填参数, 是否写操作, 是否需要群上下文)
    "group_admin_get_honors": ("查看群里的龙王, 群聊之火等荣誉及对应成员; 不指定荣誉类型时查询全部类型", {
        "group_id": ("string", "要查看的群号, 不填则查看当前群"),
        "honor_type": ("string", "荣誉类型: all 全部, talkative 龙王, performer 群聊之火, legend 群聊炽焰, strong_newbie 冒尖小春笋, emotion 快乐源泉; 不填默认 all"),
    }, [], False, True),
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
    "group_admin_list_group_requests": ("查看后端收到的加群申请或邀请; view=archived 查看归档历史, 本地归档不代表平台申请失效. 返回申请 ID, revision 和 can_handle; 管理者私聊查询时需指定群号", {
        "group_id": ("string", "申请所属群号, 群聊中不填则查询当前群"),
        "limit": ("integer", "最多查看多少条, 不填默认 20, 最大 100"),
        "cursor": ("string", "继续查看时填写上次返回的 next_cursor, 沿用相同群号"),
        "view": ("string", "active 待处理, archived 归档历史, all 全部; 默认 active"),
    }, [], False, True),
    "group_admin_handle_group_request": ("同意或拒绝查询结果中的加群申请或入群邀请; 类型和目标群由原申请确定, 不需要填写平台 flag" + _ACTION_RESULT_DESCRIPTION, {
        "request_id": ("string", "加群申请查询返回的申请 ID"),
        "expected_revision": ("integer", "当前申请的 revision; 处理归档申请必须填写, 并等待人工批准"),
        "approve": ("boolean", "true 同意该申请或邀请, false 拒绝"), "reason": ("string", "拒绝时填写的理由, 可不填"),
        "group_id": ("string", "申请或邀请对应的群号; 不填时从原请求记录中确定"),
    }, ["request_id", "approve"], True, True),
}

_ACTION_NAMES: dict[str, str] = {
    # 工具名: 统一审批服务的动作名
    "group_admin_recall_message": "recall_message", "group_admin_kick": "kick_group_member",
    "group_admin_ban": "ban_group_member", "group_admin_whole_ban": "set_group_whole_ban",
    "group_admin_ban_anonymous": "ban_anonymous", "group_admin_set_admin": "set_group_admin",
    "group_admin_set_anonymous": "set_group_anonymous", "group_admin_set_group_nickname": "set_group_card",
    "group_admin_set_name": "set_group_name", "group_admin_set_title": "set_group_special_title",
    "group_admin_leave": "leave_group", "group_admin_handle_group_request": "handle_group_request",
}


def _as_bool(value: Any, name: str) -> bool:
    """严格解析布尔参数, 拒绝真值语义"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{name} 必须为布尔值")


def _resolve(tool: Any, write: bool, *, preview: bool = False) -> tuple[Any, CallOrigin, list[str]]:
    """
    校验来源身份, 写操作开关, 调用者与群范围, 返回来源适配器

    参数:
    - tool: 实际来源工具及配置
    - write: 是否为写操作
    - preview: 声明过滤时不重复记录预期权限拒绝

    返回:
    - tuple: (适配器, 调用来源, 允许的群列表)
    """
    origin = require_call_origin()
    config = tool.config
    if write and config.get("write_tools_enabled") is not True:
        raise PermissionError("管理写操作未在插件配置中开启")
    if preview:
        if authorize_plugin_entry(tool._plugin_entry_binding).status == "denied":
            raise PermissionError("当前调用者未获得管理权限")
    else:
        require_plugin_entry_permission(tool._plugin_entry_binding)
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
    group_id = raw or (origin.chat_id if is_group_origin(origin) else "")
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
    current_adapter, _, _ = _resolve(tool, _DEFINITIONS[str(tool.tool_name)][3])
    if current_adapter is not adapter or not tool.is_enabled():
        raise PermissionError("申请工具已停用或平台实例已变化")
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
        current_adapter, _, groups = _resolve(live, True)
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
                                                                  limit=kwargs.get("limit", 20), cursor=kwargs.get("cursor"), view=kwargs.get("view", "active"))
                _request_access(source_tool, origin, adapter)
                if gid:
                    _, _, current_groups = _resolve(source_tool, False)
                    _group_id(origin, current_groups, {"group_id": gid})
                    if not adapter.allows_group(gid):
                        raise PermissionError("申请目标群不在平台可管理范围内")
                return result
            revision = kwargs.get("expected_revision")
            row = await adapter.request_flags.resolve_request("group", kwargs["request_id"], self_id=origin.self_id,
                                                              allow_archived=revision is not None, expected_revision=revision)
            _request_access(source_tool, origin, adapter)
            values = {key: value for key, value in kwargs.items() if key != "request_id"}
            if revision is not None:
                values["request_id"] = kwargs["request_id"]
            values["flag"] = row["flag"]
            if kwargs.get("group_id") and str(kwargs["group_id"]) != row["group_id"]:
                raise PermissionError("目标群与原申请不符")
            if is_group_origin(origin) and origin.chat_id != row["group_id"]:
                raise PermissionError("群申请不属于当前群")
            values.update(group_id=row["group_id"], sub_type=row["sub_type"])
            _, _, current_groups = _resolve(source_tool, True)
            # 上一行与这里的等待后复核刚刚覆盖权限, 开关与账号, 直接组装动作而不重复授权
            return await _dispatch_action(name, admin, origin, _group_id(origin, current_groups, values), values, source_tool)
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
    return _dispatch_action(name, admin, origin, gid, kwargs, source_tool)


def _dispatch_action(name: str, admin: OneBotAdmin, origin: CallOrigin, gid: str, kwargs: dict[str, Any],
                     source_tool: Any) -> Coroutine[Any, Any, Any]:
    """
    把已授权且已解析群目标的动作组装成待执行协程

    参数:
    - name: 工具名
    - admin: 当前平台动作集
    - origin: 宿主冻结的调用来源
    - gid: 已解析并校验的目标群, 不需要群上下文时为空
    - kwargs: 工具参数
    - source_tool: 当前来源工具, 用于等待和审批时复核权限

    返回:
    - Coroutine: 待执行的管理动作
    """
    handler = getattr(admin._adapter, "group_action_handler", None)
    if model_plugin_requires_approval(name, source_tool.config) and not callable(handler):
        raise PermissionError("高危动作需要持久审批, 当前审批服务尚未装配")
    if name in _ACTION_NAMES and callable(handler):
        action = _ACTION_NAMES[name]
        if name == "group_admin_set_group_nickname":
            kwargs = {**kwargs, "card": kwargs["nickname"]}
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


def _error_result(tool_name: str | None, error: Exception) -> dict[str, Any]:
    """
    把同步与异步入口的异常映射为同一工具结果, 须在 except 块内调用以保留堆栈

    参数:
    - tool_name: 当前工具名
    - error: 捕获的异常

    返回:
    - 框架扁平失败结果, 稳定类型区分不支持, 拒绝, 未确认, 权限, 参数与执行故障
    """
    name = tool_name or "unknown_tool"
    if isinstance(error, UnsupportedAdminAction):
        return tool_error(name, str(error), "unsupported")
    if isinstance(error, PermissionError):
        logger.debug(f"[group_admin] 权限拒绝 tool={name}: {error}")
        return tool_error(name, str(error), "permission_denied")
    if isinstance(error, AdminActionRejected):
        logger.warning(f"[group_admin] 动作被拒绝 tool={name}: {error}")
        return tool_error(name, str(error), "rejected")
    if isinstance(error, AdminActionUnconfirmed):
        logger.warning(f"[group_admin] 动作结果未知 tool={name}: {error}")
        return tool_error(name, str(error), "unconfirmed")
    if isinstance(error, PlatformAdminError):
        logger.warning(f"[group_admin] 动作失败 tool={name}: {type(error).__name__}: {error}")
        return tool_error(name, str(error), "unavailable")
    if isinstance(error, LookupError):
        logger.warning(f"[group_admin] 申请已失效 tool={name}: {error}")
        return tool_error(name, str(error), "not_found")
    if isinstance(error, ValueError):
        logger.warning(f"[group_admin] 动作参数错误 tool={name}: {error}")
        return tool_error(name, str(error), "invalid_arguments")
    logger.error(f"[group_admin] 动作异常 tool={name}: {traceback.format_exc()}")
    return tool_error(name, "管理操作暂不可用, 请查看后端日志", "execution_error")


class _GroupAdminMixin:
    """权限解析与执行归一, 同步入口把协程桥接到平台事件循环"""

    tool_name: str | None
    config: dict[str, Any]
    _plugin_entry_binding: PluginEntryBinding

    def is_available_for_call(self) -> bool:
        """
        仅向指定申请管理者展示加群申请工具

        返回:
        - 当前请求可用时为 True
        """
        name = str(self.tool_name)
        try:
            adapter, origin, _ = _resolve(self, _DEFINITIONS[name][3], preview=True)
            if name in {"group_admin_list_group_requests", "group_admin_handle_group_request"} and (
                    not origin.self_id or adapter.bot_self_id != origin.self_id):
                return False
            return True
        except (PermissionError, ValueError, PlatformAdminError):
            return False

    def _complete_definition(self, definition: dict[str, Any]) -> dict[str, Any]:
        if self.tool_name is None:
            return definition
        return strict_tool_definition(definition, _DEFINITIONS[self.tool_name][2])

    async def _run(self, **kwargs: Any) -> dict[str, Any]:
        """在当前上下文完成身份校验后执行管理动作"""
        name = str(self.tool_name)
        write = _DEFINITIONS[name][3]
        adapter, origin, allowed = _resolve(self, write)
        result = await _build_call(name, adapter.admin, origin, allowed, kwargs, self)
        if write:
            logger.info(f"[group_admin] 写动作完成 tool={name} actor={origin.actor_id} chat={origin.chat_id}")
        return {"ok": True, "data": result} if result is not None else {"ok": True}

    def _execute(self, **kwargs: Any) -> dict[str, Any]:
        try:
            name = str(self.tool_name)
            write = _DEFINITIONS[name][3]
            adapter, origin, allowed = _resolve(self, write)
            loop = getattr(adapter, "_loop", None)
            coro = _build_call(name, adapter.admin, origin, allowed, kwargs, self)
            try:
                result = run_on_platform_loop(coro, loop, 15)
            except TimeoutError:
                logger.warning(f"[group_admin] 动作超时已取消 tool={name} actor={origin.actor_id}")
                return tool_error(name, "动作超时, 结果未知", "unconfirmed")
            except PlatformLoopUnavailable as error:
                # 桥接已关闭未提交的协程, 这里只把原因还原成同步入口原有的参数错误结果
                raise ValueError(str(error)) from error
            if write:
                logger.info(f"[group_admin] 写动作完成 tool={name} actor={origin.actor_id} chat={origin.chat_id}")
            return {"ok": True, "data": result} if result is not None else {"ok": True}
        except Exception as error:
            return _error_result(self.tool_name, error)


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
        try:
            return await self._run(**kwargs)
        except Exception as error:
            return _error_result(self.tool_name, error)


_AnyGroupAdminTool = TypeVar("_AnyGroupAdminTool", GroupAdminTool, AsyncGroupAdminTool)


def _build_tools(kind: type[_AnyGroupAdminTool], config: dict[str, Any]) -> list[_AnyGroupAdminTool]:
    """按工具基类批量构造定义表中的工具"""
    result: list[_AnyGroupAdminTool] = []
    for name, (description, params, _, _, _) in _DEFINITIONS.items():
        tool = kind(name, description, params)
        tool.recovery_policy = "retry" if not _DEFINITIONS[name][3] else "manual"
        tool.config = config
        result.append(tool)
    bind_plugin_factory_tools(result, __file__)
    return result


@overload
def get_tools(session: AsyncSimpleSession, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None) -> list[AsyncGroupAdminTool]: ...
@overload
def get_tools(session: Session, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None) -> list[GroupAdminTool]: ...
@overload
def get_tools(session: AsyncSession, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None) -> list[GroupAdminTool] | list[AsyncGroupAdminTool]: ...
def get_tools(session: Session | AsyncSession, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None) -> list[GroupAdminTool] | list[AsyncGroupAdminTool]:
    """
    创建平台管理工具并保留审批时可复核的来源会话引用

    参数:
    - session: 会话
    - config: 插件配置
    - resources: 插件资源对象, 本插件从会话读取平台适配器

    返回:
    - 与会话执行方式一致的群管理工具
    """
    settings = config or {}
    tools = (_build_tools(AsyncGroupAdminTool, settings) if isinstance(session, AsyncSimpleSession)
             else _build_tools(GroupAdminTool, settings))
    for tool in tools:
        bind_tool_session(tool, session)
    return tools
