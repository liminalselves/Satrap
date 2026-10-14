"""账号级入群邀请工具, 不依赖群管理或好友插件"""
from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import replace
import traceback
from typing import Any, cast
import copy

from satrap.core.config.model_tool_authorization import bind_tool_session, model_tool_authorization
from satrap.core.config.group_action_origin import bind_model_action_authorization
from satrap.core.platform.onebot.admin import OneBotAdmin, AdminActionRejected, AdminActionUnconfirmed, UnsupportedAdminAction
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop
from satrap.core.plugin_authorization import PluginEntryBinding, bind_plugin_factory_tools, require_plugin_entry_permission, permission_id_list
from satrap.edictum.plugin_resources import PluginResources
from satrap.core.utils.TCBuilder import Tool, AsyncTool, tool_error
from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.call_context import CallOrigin, require_call_origin, is_private_origin, is_group_origin
from satrap.core.platform import current_adapter_manager

from satrap.core.log import logger

DEFINITIONS: dict[str, tuple[str, dict[str, Any], list[str]]] = {
    "account_requests_list_group_invites": ("查看别人邀请机器人加入新群的邀请, 按当前机器人账号查询, 不要求先提供群号; 不包含别人申请加入已有群的申请. 归档不代表平台失效, can_handle 只表示本地处理资格", {
        "group_id": {"type": "string", "description": "只看这个群的邀请; 不填查询当前账号全部入群邀请"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "最多返回多少条, 默认 20"},
        "cursor": {"type": "string", "description": "上次查询返回的 next_cursor, 保持相同查询条件"},
        "view": {"type": "string", "enum": ["active", "archived", "all"], "description": "active 待处理, archived 归档, all 全部; 默认 active"},
    }, []),
    "account_requests_handle_group_invite": ("接受或拒绝查询结果中邀请机器人加入新群的邀请; 不处理成员入群申请. pending 表示待人工批准, succeeded 表示已执行, unknown 不要重复操作", {
        "request_id": {"type": "string", "description": "邀请查询返回的 request_id, 不能用群号代替"},
        "approve": {"type": "boolean", "description": "true 接受邀请, false 拒绝邀请"},
        "expected_revision": {"type": "integer", "minimum": 1, "description": "查询返回的 revision, 处理归档邀请必须填写"},
        "reason": {"type": "string", "maxLength": 120, "description": "拒绝邀请时的理由, 可省略"},
    }, ["request_id", "approve"]),
}


def _error(name: str | None, error: Exception) -> dict[str, Any]:
    """
    记录失败并转换为稳定工具结果

    参数:
    - name: 工具名称
    - error: 已捕获异常

    返回:
    - 不包含凭据或平台原始响应的失败结果
    """
    if isinstance(error, PermissionError):
        kind = "permission_denied"
    elif isinstance(error, (ValueError, TypeError)):
        kind = "invalid_arguments"
    elif isinstance(error, LookupError):
        kind = "not_found"
    elif isinstance(error, UnsupportedAdminAction):
        kind = "unsupported"
    elif isinstance(error, (AdminActionUnconfirmed, TimeoutError)):
        kind = "unconfirmed"
    elif isinstance(error, AdminActionRejected):
        kind = "rejected"
    elif isinstance(error, PlatformLoopUnavailable):
        kind = "unavailable"
    else:
        logger.error(f"[account_requests] 工具异常 tool={name}\n{traceback.format_exc()}")
        return tool_error(str(name), "入群邀请操作失败, 请查看后端日志", "execution_error")
    logger.warning(f"[account_requests] 工具失败 tool={name} reason={kind}")
    return tool_error(str(name), str(error), kind)


class _AccountRequestMixin(_ToolBase):
    """执行时检查真实发言者与账号权限, 声明不随调用者授权改变"""

    config: dict[str, Any]
    _plugin_entry_binding: PluginEntryBinding

    def _resolve(self, *, preview: bool = False) -> tuple[OneBotAdapter, CallOrigin, tuple[str, ...]]:
        """
        核验来源机器人账号与独立插件权限

        参数:
        - preview: 仅确认环境, 默认执行时复核当前权限

        返回:
        - 当前适配器, 可信来源和邀请目标范围
        """
        origin = require_call_origin()
        if origin.actor_kind != "platform_user" or not (is_private_origin(origin) or is_group_origin(origin)):
            raise PermissionError("入群邀请工具需要有效的平台发送者身份")
        manager = current_adapter_manager()
        adapter = manager.get_adapter(origin.adapter_id) if manager else None
        if adapter is None or not isinstance(adapter, OneBotAdapter):
            raise UnsupportedAdminAction("当前平台未提供账号入群邀请能力")
        if not origin.self_id or adapter.bot_self_id != origin.self_id:
            raise PermissionError("机器人账号未确认或已变化")
        if not adapter.config.enable or not adapter.config.settings.get("enable_group" if is_group_origin(origin) else "enable_private", True):
            raise PermissionError("来源平台对话已停用")
        if not preview:
            require_plugin_entry_permission(self._plugin_entry_binding, refresh=True)
            if not self.is_enabled():
                raise PermissionError("入群邀请工具已停用")
        return adapter, origin, tuple(permission_id_list(self.config.get("allowed_invitation_groups")))

    def is_available_for_call(self) -> bool:
        """
        检查平台支持, 权限拒绝由执行路径明确返回

        返回:
        - 当前环境是否能够提供邀请工具
        """
        try:
            self._resolve(preview=True)
            return True
        except (PermissionError, ValueError, UnsupportedAdminAction):
            return False

    def get_tool_defined(self) -> dict[str, Any]:
        """
        从单一声明表生成模型工具描述

        返回:
        - 严格工具参数 schema
        """
        description, properties, required = DEFINITIONS[str(self.tool_name)]
        return {"type": "function", "function": {"name": self.tool_name, "description": description,
                "parameters": {"type": "object", "properties": copy.deepcopy(properties), "required": required, "additionalProperties": False}}}

    async def _run(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        查询邀请或提交宿主账号动作, 等待后复核权限

        参数:
        - kwargs: 模型参数

        返回:
        - 查询或动作记录, 失败返回明确原因
        """
        try:
            name = str(self.tool_name)
            _, properties, required = DEFINITIONS[name]
            if set(kwargs) - set(properties) or not set(required) <= kwargs.keys():
                raise ValueError("邀请工具缺少必填参数或含未知参数")
            adapter, origin, groups = self._resolve()
            generation = adapter.connection_generation()
            if name == "account_requests_list_group_invites":
                gid = kwargs.get("group_id", "")
                if not isinstance(gid, str) or gid and (not gid.isdecimal() or groups and gid not in groups):
                    raise PermissionError("邀请目标不在允许范围内或群号无效")
                result = await adapter.request_flags.list_requests("group", self_id=origin.self_id, group_id=gid,
                    sub_type="invite", group_ids=groups, limit=kwargs.get("limit", 20), cursor=kwargs.get("cursor"), view=kwargs.get("view", "active"))
                current, _, allowed = self._resolve()
                if current is not adapter or current.connection_generation() != generation or allowed != groups:
                    raise PermissionError("邀请查询范围已变化, 请重新查询")
            else:
                handler = getattr(adapter, "account_request_handler", None)
                if not callable(handler):
                    raise PlatformLoopUnavailable("unavailable", "账号邀请审批服务未装配")
                approval_policy = (self.config.get("approval_required", True) is not False,
                                   self.config.get("administrator_invites_auto_approve") is True)

                def permission(live: _AccountRequestMixin, target: str) -> None:
                    """
                    执行和人工审批后重验来源及邀请目标

                    参数:
                    - live: 当前来源工具
                    - target: 原邀请的目标群
                    """
                    current, _, allowed = live._resolve()
                    if current is not adapter or current.connection_generation() != generation or allowed and target not in allowed:
                        raise PermissionError("账号邀请来源或目标授权已变化")
                    if (live.config.get("approval_required", True) is not False,
                            live.config.get("administrator_invites_auto_approve") is True) != approval_policy:
                        raise PermissionError("账号邀请审批要求已变化")

                source = model_tool_authorization(self, origin, "account_requests", permission)
                source = replace(source, approval_required=approval_policy[0], administrator_invites_auto_approve=approval_policy[1])
                with bind_model_action_authorization(source):
                    result = await cast(Awaitable[dict[str, Any]], handler(kwargs))
            self._resolve()
            return {"ok": True, "data": result}
        except Exception as error:
            return _error(self.tool_name, error)


class AccountRequestTool(_AccountRequestMixin, Tool):
    """同步平台会话的账号邀请工具"""

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """
        在来源平台循环执行, 超时不重放

        参数:
        - kwargs: 模型参数

        返回:
        - 宿主结果或明确失败
        """
        try:
            adapter, _, _ = self._resolve()
            return run_on_platform_loop(self._run(kwargs), getattr(adapter, "_loop", None), 30)
        except Exception as error:
            return _error(self.tool_name, error)


class AsyncAccountRequestTool(_AccountRequestMixin, AsyncTool):
    """异步平台会话的账号邀请工具"""

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """
        异步执行账号邀请工具

        参数:
        - kwargs: 模型参数

        返回:
        - 宿主结果或明确失败
        """
        return await self._run(kwargs)


def get_tools(session: Session | AsyncSession, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None) -> list[Any]:
    """
    构造独立账号邀请工具并绑定来源会话

    参数:
    - session: 当前会话
    - config: 已解析插件配置
    - resources: 宿主资源, 当前能力使用来源适配器

    返回:
    - 与会话执行方式一致的两个工具
    """
    kind = AsyncAccountRequestTool if isinstance(session, AsyncSession) else AccountRequestTool
    tools = []
    for name, (description, _, _) in DEFINITIONS.items():
        tool = kind(name, description, {})
        tool.config = config or {}
        tool.recovery_policy = "retry" if name.endswith("list_group_invites") else "manual"
        bind_tool_session(tool, session)
        tools.append(tool)
    bind_plugin_factory_tools(tools, __file__)
    return tools
