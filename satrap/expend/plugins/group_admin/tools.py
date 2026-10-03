"""OneBot 群管理同步和异步模型工具, 权限以来源身份为准, 写操作默认关闭"""
from __future__ import annotations

from collections.abc import Awaitable, Coroutine
from typing import Any, TypeVar, cast, overload

import asyncio
import weakref

from satrap.core.platform.onebot.admin import OneBotAdmin, PlatformAdminError, UnsupportedAdminAction
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.call_context import CallOrigin, bind_call_origin, require_call_origin
from satrap.core.config.group_action_origin import ModelActionAuthorization, bind_model_action_authorization
from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.platform import current_adapter_manager
from satrap.edictum import AsyncSimpleSession
from satrap.core.log import logger

_ACTION_RESULT_DESCRIPTION = "; 返回动作记录时, pending 表示等待批准, succeeded 表示已执行; 失败或结果未知时不要说操作成功"

_DEFINITIONS: dict[str, tuple[str, dict[str, tuple[str, str]], list[str], bool, bool]] = {
    # 工具名: (描述, 参数, 必填参数, 是否写操作, 是否需要群上下文)
    "group_admin_list_groups": ("查看机器人加入了哪些群, 返回群号和群名", {}, [], False, False),
    "group_admin_get_group_info": ("查看群名, 群号和成员人数等基本信息; 不填 group_id 时查看当前群", {
        "group_id": ("string", "要查看的群号, 不填则查看当前群"),
    }, [], False, True),
    "group_admin_list_members": ("查看群成员列表, 包括成员 QQ 号, 昵称, 群名片和角色; 需要确认群里有哪些人时使用", {
        "group_id": ("string", "要查看的群号, 不填则查看当前群"),
    }, [], False, True),
    "group_admin_get_member": ("查看某位群成员的资料, 包括昵称, 群名片和群主/管理员/普通成员身份; 需要确认操作对象时使用", {
        "user_id": ("string", "要查看的成员 QQ 号, 填写数字字符串; 可从群成员查询结果中取得"), "group_id": ("string", "成员所在的群号, 不填则使用当前群"),
    }, ["user_id"], False, True),
    "group_admin_get_honors": ("查看群里的龙王, 群聊之火等荣誉及对应成员; 不指定荣誉类型时查询全部类型", {
        "group_id": ("string", "要查看的群号, 不填则查看当前群"),
        "honor_type": ("string", "荣誉类型: all 全部, talkative 龙王, performer 群聊之火, legend 群聊炽焰, strong_newbie 冒尖小春笋, emotion 快乐源泉; 不填默认 all"),
    }, [], False, True),
    "group_admin_get_message": ("向平台查询一条群消息, 返回原文和发送者; 需要查看引用消息或确认某条原话时使用", {
        "message_id": ("string", "要查看的消息 ID, 从聊天上下文或消息查询结果中取得"), "group_id": ("string", "消息所在的群号, 不填则使用当前群"),
    }, ["message_id"], False, True),
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
    "group_admin_set_card": ("修改指定成员在群内显示的群名片; card 填空字符串表示清空群名片" + _ACTION_RESULT_DESCRIPTION, {
        "user_id": ("string", "要修改群名片的成员 QQ 号"), "card": ("string", "新的群名片, 不超过 60 字符; 填写空字符串可清空"),
        "group_id": ("string", "成员所在的群号, 不填则使用当前群"),
    }, ["user_id"], True, True),
    "group_admin_set_name": ("修改整个群的名称; 修改某位成员的群名片请使用 group_admin_set_card" + _ACTION_RESULT_DESCRIPTION, {
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
    "group_admin_handle_friend_request": ("同意或拒绝机器人收到的一条好友申请; 必须使用这条申请提供的 flag, 操作是否成功以实际返回结果为准", {
        "flag": ("string", "好友申请事件提供的请求标识, 使用原值, 不能填写 QQ 号代替"), "approve": ("boolean", "true 同意添加好友, false 拒绝申请"),
        "remark": ("string", "同意申请后给对方设置的好友备注, 可不填"),
    }, ["flag", "approve"], True, False),
    "group_admin_handle_group_request": ("同意或拒绝一条加群申请, 或一条邀请机器人入群的请求; 必须使用原请求的标识和类型" + _ACTION_RESULT_DESCRIPTION, {
        "flag": ("string", "加群申请或入群邀请事件提供的请求标识, 必须使用原值"), "sub_type": ("string", "原请求的类型: add 表示加群申请, invite 表示入群邀请; 按事件提供的类型填写"),
        "approve": ("boolean", "true 同意该申请或邀请, false 拒绝"), "reason": ("string", "拒绝时填写的理由, 可不填"),
        "group_id": ("string", "申请或邀请对应的群号; 不填时从原请求记录中确定"),
    }, ["flag", "sub_type", "approve"], True, True),
}


def _lines(value: Any) -> list[str]:
    """将逐行配置拆为非空字符串列表"""
    if isinstance(value, list):
        items = cast(list[Any], value)
        return [str(item).strip() for item in items if str(item).strip()]
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


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
    callers = _lines(config.get("allowed_callers"))
    if write and callers and origin.actor_id not in callers:
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


def _authorization_source(tool: Any, admin: OneBotAdmin, origin: CallOrigin) -> ModelActionAuthorization:
    """固定可信来源并在审批时从仍有效的工具读取当前权限"""
    tool_ref = weakref.ref(tool)
    session_ref = getattr(tool, "_group_admin_session_ref", None)
    if hasattr(tool, "_group_admin_session_ref") and session_ref is None:
        raise PermissionError("模型管理工具无法复核来源会话")
    identity = {"adapter_id": origin.adapter_id, "self_id": origin.self_id,
                "chat_type": origin.chat_type, "chat_id": origin.chat_id,
                "actor_id": origin.actor_id, "session_id": str(getattr(tool, "_group_admin_session_id", "")),
                "tool_name": str(tool.tool_name)}

    def verify(target_group: str) -> None:
        """重验工具存活、插件启用状态及当前调用者和目标群限制"""
        live_tool = tool_ref()
        if live_tool is None or not live_tool.is_enabled():
            raise PermissionError("模型管理工具已停用或来源已失效")
        if session_ref is not None:
            session = session_ref()
            workflow = getattr(session, "_wf", None) if session is not None else None
            tools_manager = getattr(workflow, "tools_manager", None)
            plugins = session.list_plugins() if session is not None else []
            plugin = next((item for item in plugins if item.name == "group_admin"), None)
            if (tools_manager is None or tools_manager.tools.get(live_tool.tool_name) is not live_tool
                    or not tools_manager.is_tool_enabled(live_tool.tool_name)
                    or plugin is None or not plugin.enabled or not plugin.tools.get(live_tool.tool_name, False)):
                raise PermissionError("模型管理工具已从来源会话移除或停用")
        with bind_call_origin(origin):
            current_adapter, _, current_groups = _resolve(live_tool.config, True)
        if current_adapter.admin is not admin or current_adapter.bot_self_id != origin.self_id:
            raise PermissionError("模型管理工具的机器人账号或平台已变化")
        _group_id(origin, current_groups, {"group_id": target_group})

    return ModelActionAuthorization(identity, verify, session_ref)


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

    返回:
    - Coroutine: 待执行的管理动作
    """
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
        "group_admin_set_anonymous": "set_group_anonymous", "group_admin_set_card": "set_group_card",
        "group_admin_set_name": "set_group_name", "group_admin_set_title": "set_group_special_title",
        "group_admin_leave": "leave_group", "group_admin_handle_group_request": "handle_group_request",
    }
    handler = getattr(admin._adapter, "group_action_handler", None)
    if name in action_names and callable(handler):
        action = action_names[name]
        from satrap.core.platform.onebot.group_action_types import ACTION_FIELDS

        params: dict[str, object] = {
            key: value for key, value in kwargs.items() if key in ACTION_FIELDS[action]
        }
        for key in ("enable", "approve", "dismiss", "reject_add_request"):
            if key in params:
                params[key] = _as_bool(params[key], key)

        async def submit() -> dict[str, Any]:
            """把已授权的模型群管理请求交给统一审批与执行服务"""
            source = _authorization_source(source_tool, admin, origin)
            with bind_model_action_authorization(source):
                return await cast(Awaitable[dict[str, Any]], handler(gid, action, params))

        return submit()
    if name == "group_admin_list_groups":
        return admin.get_group_list()
    if name == "group_admin_get_group_info":
        return admin.get_group_info(gid)
    if name == "group_admin_list_members":
        return admin.get_group_member_list(gid)
    if name == "group_admin_get_member":
        return admin.get_group_member_info(gid, kwargs.get("user_id", ""))
    if name == "group_admin_get_honors":
        return admin.get_group_honor_info(gid, str(kwargs.get("honor_type") or "all"))
    if name == "group_admin_get_message":
        return admin.get_message(gid, kwargs.get("message_id", ""))
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
    if name == "group_admin_set_card":
        return admin.set_group_card(gid, kwargs.get("user_id", ""), str(kwargs.get("card", "")))
    if name == "group_admin_set_name":
        return admin.set_group_name(gid, kwargs.get("name", ""))
    if name == "group_admin_set_title":
        return admin.set_group_special_title(gid, kwargs.get("user_id", ""), str(kwargs.get("title", "")))
    if name == "group_admin_leave":
        return admin.leave_group(gid, _as_bool(kwargs.get("dismiss", False), "dismiss"))
    if name == "group_admin_handle_friend_request":
        return admin.handle_friend_request(kwargs.get("flag", ""), _as_bool(kwargs.get("approve"), "approve"), str(kwargs.get("remark", "")))
    return admin.handle_group_request(gid, kwargs.get("flag", ""), str(kwargs.get("sub_type", "")), _as_bool(kwargs.get("approve"), "approve"), str(kwargs.get("reason", "")))


class _GroupAdminMixin:
    """权限解析与执行归一, 同步入口把协程桥接到平台事件循环"""

    tool_name: str | None
    config: dict[str, Any]
    _group_admin_session_ref: weakref.ReferenceType[Session | AsyncSession] | None
    _group_admin_session_id: str

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
        except Exception as error:
            logger.warning(f"[group_admin] 动作异常 tool={self.tool_name}: {type(error).__name__}: {error}")
            return {"status": "error", "error": str(error)}


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
        except Exception as error:
            logger.warning(f"[group_admin] 动作异常 tool={name}: {type(error).__name__}: {error}")
            return {"status": "error", "error": str(error)}


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
    try:
        session_ref = weakref.ref(session)
    except TypeError:
        session_ref = None
    for tool in tools:
        tool._group_admin_session_ref = session_ref
        tool._group_admin_session_id = str(getattr(session, "session_id", ""))
    return tools
