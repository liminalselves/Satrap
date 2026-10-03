"""
OneBot 群管理同步与异步工具

依据冻结的调用来源校验成员和群范围, 写操作默认关闭,
按插件配置向当前会话请求高危动作审批后调用平台管理接口
"""
from __future__ import annotations

from collections.abc import Coroutine
import asyncio
import inspect
from typing import Any, Callable, TypeVar, cast, overload
import json

from satrap.core.platform.onebot.admin import OneBotAdmin, PlatformAdminError, UnsupportedAdminAction
from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.call_context import CallOrigin, require_call_origin
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.platform import current_adapter_manager
from satrap.core.type import safe_getattr_callable
from satrap.edictum import AsyncSimpleSession

from satrap.core.log import logger

_DEFINITIONS: dict[str, tuple[str, dict[str, tuple[str, str]], list[str], bool, bool]] = {
    # 工具名: (描述, 参数, 必填参数, 是否写操作, 是否需要群上下文)
    "group_admin_list_groups": ("列出机器人所在群, 返回群号和群名", {}, [], False, False),
    "group_admin_get_group_info": ("查看群信息, 不含群号时默认当前群", {
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, [], False, True),
    "group_admin_list_members": ("列出群成员, 不含群号时默认当前群", {
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, [], False, True),
    "group_admin_get_member": ("查看群成员信息", {
        "user_id": ("string", "目标成员 QQ"), "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["user_id"], False, True),
    "group_admin_get_honors": ("查看群荣誉信息", {
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
        "honor_type": ("string", "talkative/performer/legend/strong_newbie/emotion 之一, 默认 all"),
    }, [], False, True),
    "group_admin_get_message": ("回源读取一条群消息的原文与发送者", {
        "message_id": ("string", "平台消息 ID"), "group_id": ("string", "消息所在群号, 可选, 默认当前群"),
    }, ["message_id"], False, True),
    "group_admin_get_forward": ("回源读取合并转发内容, 不展开嵌套转发; 必须提供包含该转发的来源消息", {
        "forward_id": ("string", "合并转发消息 ID"),
        "source_message_id": ("string", "包含该转发的群消息 ID, 必须是当前群内的消息 (用于核验来源归属)"),
        "group_id": ("string", "所在群号, 可选, 默认当前群"),
    }, ["forward_id", "source_message_id"], False, True),
    "group_admin_send_forward": ("向群发送合并转发消息, 经统一发送通道按序投递", {
        "nodes": ("array", "节点列表, 每项为 {content: 1 到 2000 字符文本, name: 可选昵称}, 共 1 到 30 项"),
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["nodes"], True, True),
    "group_admin_recall_message": ("撤回当前群的一条消息", {
        "message_id": ("string", "平台消息 ID"), "group_id": ("string", "消息所在群号, 可选, 默认当前群"),
    }, ["message_id"], True, True),
    "group_admin_kick": ("将成员移出群聊", {
        "user_id": ("string", "目标成员 QQ"), "group_id": ("string", "目标群号, 可选, 默认当前群"),
        "reject_add_request": ("boolean", "是否拒绝其后续加群请求, 默认 false"),
    }, ["user_id"], True, True),
    "group_admin_ban": ("禁言群成员, 时长 0 秒表示解除禁言", {
        "user_id": ("string", "目标成员 QQ"), "duration": ("number", "禁言秒数, 0 到 2592000, 默认 1800"),
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["user_id"], True, True),
    "group_admin_whole_ban": ("开启或解除全员禁言", {
        "enable": ("boolean", "true 开启, false 解除"), "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["enable"], True, True),
    "group_admin_ban_anonymous": ("禁言匿名成员", {
        "flag": ("string", "匿名消息上报的 anonymous flag"), "duration": ("number", "禁言秒数, 默认 1800"),
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["flag"], True, True),
    "group_admin_set_admin": ("设置或取消群管理员", {
        "user_id": ("string", "目标成员 QQ"), "enable": ("boolean", "true 设置, false 取消"),
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["user_id", "enable"], True, True),
    "group_admin_set_anonymous": ("开启或关闭群匿名聊天", {
        "enable": ("boolean", "true 开启, false 关闭"), "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["enable"], True, True),
    "group_admin_set_card": ("设置群成员名片, 空字符串表示删除", {
        "user_id": ("string", "目标成员 QQ"), "card": ("string", "新名片, 不超过 60 字符"),
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["user_id"], True, True),
    "group_admin_set_name": ("修改群名", {
        "name": ("string", "新群名, 1 到 60 字符"), "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["name"], True, True),
    "group_admin_set_title": ("设置群成员专属头衔, 空字符串表示删除", {
        "user_id": ("string", "目标成员 QQ"), "title": ("string", "头衔文本, 不超过 18 字符"),
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
    }, ["user_id"], True, True),
    "group_admin_leave": ("退出群聊, 群主可选择解散", {
        "group_id": ("string", "目标群号, 可选, 默认当前群"),
        "dismiss": ("boolean", "true 解散群, 默认 false 退群"),
    }, [], True, True),
    "group_admin_handle_friend_request": ("批准或拒绝好友添加请求, 不自动审批", {
        "flag": ("string", "好友请求事件上报的标识"), "approve": ("boolean", "true 同意, false 拒绝"),
        "remark": ("string", "同意后的好友备注, 可选"),
    }, ["flag", "approve"], True, False),
    "group_admin_handle_group_request": ("批准或拒绝加群请求或邀请, 不自动审批", {
        "flag": ("string", "请求事件上报的标识"), "sub_type": ("string", "add 或 invite, 必须与事件一致"),
        "approve": ("boolean", "true 同意, false 拒绝"), "reason": ("string", "拒绝理由, 可选"),
        "group_id": ("string", "请求所属群号, 可选, 默认当前群"),
    }, ["flag", "sub_type", "approve"], True, True),
}


def _lines(value: Any) -> list[str]:
    """将逐行配置拆为非空字符串列表"""
    if isinstance(value, list):
        items = cast(list[Any], value)
        return [str(item).strip() for item in items if str(item).strip()]
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


_HIGH_RISK_TOOLS = frozenset({
    "group_admin_kick",
    "group_admin_ban",
    "group_admin_whole_ban",
    "group_admin_ban_anonymous",
    "group_admin_set_admin",
    "group_admin_set_name",
    "group_admin_leave",
    "group_admin_handle_friend_request",
    "group_admin_handle_group_request",
})
"""影响成员资格, 管理员权限或不可逆的写动作, 可经 high_risk_approval 配置逐次审批"""


def _needs_high_risk_approval(name: str, config: dict[str, Any]) -> bool:
    """
    判断动作是否需要逐次审批

    参数:
    - name: 工具名
    - config: 当前会话插件配置

    返回:
    - bool: 高危分组且显式开启审批时为 True
    """
    return name in _HIGH_RISK_TOOLS and config.get("high_risk_approval") is True


def _approval_question(name: str, origin: CallOrigin, kwargs: dict[str, Any]) -> str:
    """
    构造包含实际目标与触发者的审批提问

    参数:
    - name: 高危工具名
    - origin: 调用来源
    - kwargs: 工具参数

    返回:
    - str: 实际群目标或好友请求, 触发者与有界参数摘要
    """
    summary = json.dumps(kwargs, ensure_ascii=False, default=str)
    if len(summary) > 200:
        summary = summary[:200] + "..."
    chat = str(kwargs.get("group_id") or origin.chat_id) if _DEFINITIONS[name][4] else "好友请求"
    return f"高危群管理动作 {name} 目标 {chat} 由 {origin.actor_id} 触发, 参数 {summary}. 是否允许执行?"


def _approval_granted(answer: object) -> bool:
    """
    判断审批回答是否明确同意

    参数:
    - answer: 用户输入通道的回答

    返回:
    - bool: y, yes, 允许或批准时为 True, 其他回答拒绝
    """
    return str(answer).strip().lower() in ("y", "yes", "允许", "批准")


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

    写操作在 write_tools_enabled 开启的基础上还要求 allowed_callers 非空 (留空即
    拒绝所有写操作); 只读工具默认不限制调用者, 配置 allowed_read_callers 后仅允许
    列出的成员

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
        raise PermissionError(
            "写操作要求在插件配置 allowed_callers 中显式列出允许的调用者 (留空即拒绝所有写操作)"
        )
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


def _build_call(name: str, admin: OneBotAdmin, origin: CallOrigin, allowed: list[str], kwargs: dict[str, Any]) -> Coroutine[Any, Any, Any]:
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
    gid = _group_id(origin, allowed, kwargs) if needs_group else ""
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
    session: Session | AsyncSession

    @property
    def user_input_provider(self) -> Callable[..., Any] | None:
        """
        读取当前会话的审批通道, 支持工具安装后注入或替换

        返回:
        - Callable[..., Any] | None: 当前可调用通道, 未注入时为 None
        """
        return safe_getattr_callable(self.session, "user_input_provider")

    def _complete_definition(self, definition: dict[str, Any]) -> dict[str, Any]:
        if not definition or self.tool_name is None:
            return definition
        definition["function"]["parameters"]["required"] = _DEFINITIONS[self.tool_name][2]
        definition["function"]["parameters"]["additionalProperties"] = False
        return definition

    def _ask_approval(self, question: str) -> object:
        """
        调用当前会话的输入通道, 按签名传递选项

        参数:
        - question: 审批提问

        返回:
        - object: 同步回答或待等待的异步回答, 无通道时拒绝
        """
        provider = self.user_input_provider
        if provider is None:
            raise PermissionError("高危动作已开启逐次审批, 当前会话无审批通道, 已拒绝")
        try:
            inspect.signature(cast(Callable[..., object], provider)).bind(question, ["允许", "拒绝"])
        except (TypeError, ValueError):
            return provider(f"{question} 可选: 1. 允许  2. 拒绝")
        return provider(question, ["允许", "拒绝"])

    async def _authorize_high_risk(self, name: str, origin: CallOrigin, kwargs: dict[str, Any]) -> None:
        """
        异步请求高危动作审批, 无通道或未批准即拒绝

        参数:
        - name: 工具名
        - origin: 已校验的调用来源
        - kwargs: 已校验群范围的工具参数
        """
        if not _needs_high_risk_approval(name, self.config):
            return
        if not callable(self.user_input_provider):
            raise PermissionError("高危动作已开启逐次审批, 当前会话无审批通道, 已拒绝")
        answer = self._ask_approval(_approval_question(name, origin, kwargs))
        if inspect.isawaitable(answer):
            answer = await answer
        if not _approval_granted(answer):
            raise PermissionError("高危动作未获人工批准")

    def _authorize_high_risk_sync(self, name: str, origin: CallOrigin, kwargs: dict[str, Any]) -> None:
        """
        同步请求高危动作审批, 无法等待异步回答时拒绝

        参数:
        - name: 工具名
        - origin: 已校验的调用来源
        - kwargs: 已校验群范围的工具参数
        """
        if not _needs_high_risk_approval(name, self.config):
            return
        if not callable(self.user_input_provider):
            raise PermissionError("高危动作已开启逐次审批, 当前会话无审批通道, 已拒绝")
        answer = self._ask_approval(_approval_question(name, origin, kwargs))
        if inspect.isawaitable(answer):
            if inspect.iscoroutine(answer):
                answer.close()
            raise PermissionError("同步会话的审批通道返回了异步结果, 无法等待, 已拒绝")
        if not _approval_granted(answer):
            raise PermissionError("高危动作未获人工批准")

    async def _run(self, **kwargs: Any) -> dict[str, Any]:
        """
        异步校验来源与审批后执行管理动作

        参数:
        - kwargs: 定义表中的工具参数

        返回:
        - dict[str, Any]: 动作完成的状态及可选结果, 权限或平台错误向调用方传播
        """
        name = str(self.tool_name)
        write = _DEFINITIONS[name][3]
        adapter, origin, allowed = _resolve(self.config, write)
        if _DEFINITIONS[name][4]:
            _group_id(origin, allowed, kwargs)
        await self._authorize_high_risk(name, origin, kwargs)
        result = await _build_call(name, adapter.admin, origin, allowed, kwargs)
        if write:
            logger.info(f"[group_admin] 写动作完成 tool={name} actor={origin.actor_id} chat={origin.chat_id}")
        return {"status": "ok", "data": result} if result is not None else {"status": "ok"}

    def _execute(self, **kwargs: Any) -> dict[str, Any]:
        """
        同步校验来源与审批, 在平台循环执行管理动作

        参数:
        - kwargs: 定义表中的工具参数

        返回:
        - dict[str, Any]: 完成, 不支持或错误状态; 超时返回 unconfirmed, 不宣称动作失败
        """
        try:
            name = str(self.tool_name)
            write = _DEFINITIONS[name][3]
            adapter, origin, allowed = _resolve(self.config, write)
            if _DEFINITIONS[name][4]:
                _group_id(origin, allowed, kwargs)
            self._authorize_high_risk_sync(name, origin, kwargs)
            loop = getattr(adapter, "_loop", None)
            if loop is None or loop.is_closed():
                raise ValueError("平台事件循环不可用")
            coro = _build_call(name, adapter.admin, origin, allowed, kwargs)
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


def _build_tools(
    kind: type[_AnyGroupAdminTool], config: dict[str, Any], session: Session | AsyncSession
) -> list[_AnyGroupAdminTool]:
    """
    构造管理工具并绑定会话, 审批通道在执行时读取

    参数:
    - kind: 同步或异步工具类型
    - config: 当前会话的插件配置
    - session: 当前会话

    返回:
    - list: 定义表中的管理工具实例
    """
    result: list[_AnyGroupAdminTool] = []
    for name, (description, params, _, _, _) in _DEFINITIONS.items():
        tool = kind(name, description, params)
        tool.recovery_policy = "retry" if not _DEFINITIONS[name][3] else "manual"
        tool.config = config
        tool.session = session
        result.append(tool)
    return result


@overload
def get_tools(session: AsyncSimpleSession, config: dict[str, Any], resources: Any = None) -> list[AsyncGroupAdminTool]: ...
@overload
def get_tools(session: Session | AsyncSession, config: dict[str, Any], resources: Any = None) -> list[GroupAdminTool] | list[AsyncGroupAdminTool]: ...
def get_tools(session: Session | AsyncSession, config: dict[str, Any], resources: Any = None) -> list[GroupAdminTool] | list[AsyncGroupAdminTool]:
    """
    按会话形态构造管理工具, 执行时解析来源权限与审批通道

    参数:
    - session: 绑定的当前会话
    - config: 当前会话插件配置
    - resources: 插件资源, 当前不使用

    返回:
    - list: AsyncSimpleSession 使用异步工具, 其他会话使用同步工具
    """
    if isinstance(session, AsyncSimpleSession):
        return _build_tools(AsyncGroupAdminTool, config, session)
    return _build_tools(GroupAdminTool, config, session)
