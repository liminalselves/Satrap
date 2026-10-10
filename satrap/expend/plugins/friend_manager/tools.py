"""独立好友管理模型工具, 只调用通用好友宿主"""
from __future__ import annotations

from typing import Any
import copy
import secrets
from dataclasses import replace

from satrap.core.call_context import current_call_origin, is_private_origin, is_group_origin
from satrap.core.config.model_tool_authorization import bind_tool_session, model_tool_authorization
from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop
from satrap.core.platform import current_adapter_manager
from satrap.core.friends import FriendError
from satrap.core.friends.service import failure, text_id
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.plugin_authorization import PluginEntryBinding, authorize_management_permissions, require_plugin_entry_permission, bind_plugin_factory_tools, permission_id_list
from satrap.edictum.plugin_resources import PluginResources
from satrap.core.log import logger

LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "description": "最多返回多少条, 默认 20; 有下一页时用 next_cursor 继续查询"}
CURSOR = {"type": "string", "maxLength": 256, "description": "上次返回的 next_cursor, 沿用相同查询条件"}
PAGING = {"limit": LIMIT, "cursor": CURSOR}
DEFINITIONS: dict[str, tuple[str, dict[str, Any], list[str]]] = {
    "friend_manager_list_friends": ("查看机器人的好友列表, 返回好友 ID, 昵称和备注; 列表较长时可继续翻页", PAGING, []),
    "friend_manager_find_friends": ("按好友 ID, 昵称或备注查找好友; 重名时返回全部候选, 先确认目标 ID 再操作", {
        **PAGING, "query": {"type": "string", "minLength": 1, "maxLength": 256, "description": "要查找的好友 ID, 昵称或备注, 可以填写昵称或备注的一部分"}}, ["query"]),
    "friend_manager_list_requests": ("查看好友申请; 普通用户只返回本人申请, 管理员返回全部, 范围由后端确定. 默认查待处理, view=archived 查归档历史; 本地归档不代表平台失效", {
        **PAGING, "view": {"type": "string", "enum": ["active", "archived", "all"], "description": "active 待处理, archived 归档历史, all 全部本地记录; 默认 active"}}, []),
    "friend_manager_recheck_request": ("核验好友申请; 普通用户只能核验本人申请, 管理员可核验全部. verification=local_only 表示只核验本地记录, 不能证明平台申请仍有效", {
        "request_id": {"type": "string", "description": "申请列表返回的 request_id"}}, ["request_id"]),
    "friend_manager_handle_request": ("同意或拒绝好友申请; 普通用户只能处理本人申请, 获写授权的管理员可处理全部. 使用查询返回的 ID; 归档申请须填写当前 revision 并等待人工批准, 不要重复执行未知结果", {
        "request_id": {"type": "string", "description": "申请查询返回的 request_id, 不能用用户 ID 代替"},
        "approve": {"type": "boolean", "description": "true 同意, false 拒绝"},
        "expected_revision": {"type": "integer", "minimum": 1, "description": "申请查询或核验返回的 revision; 归档申请必须填写. pending 表示等待人工批准, 不是已处理"},
        "remark": {"type": "string", "maxLength": 60, "description": "同意后的好友备注, 可不填; 拒绝时不填写"}}, ["request_id", "approve"]),
    "friend_manager_delete_friend": ("申请删除好友; 省略 user_id 时删除当前请求者本人. 普通用户只能删除本人, 管理员可指定已确认目标; 必须人工批准, pending 不代表已删除", {
        "user_id": {"type": "string", "minLength": 1, "maxLength": 256, "description": "目标好友 ID, 省略为当前发送者; 普通用户只能指定本人"}}, []),
    "friend_manager_send_request": ("主动发送好友申请; 省略 user_id 时申请添加当前发送者. 普通用户只能添加本人, 获写授权的管理员可指定他人; submitted 仅表示申请已提交, 不代表已成为好友", {
        "user_id": {"type": "string", "minLength": 1, "maxLength": 256, "description": "目标账号 ID, 省略为当前发送者"},
        "message": {"type": "string", "maxLength": 200, "description": "好友申请验证文字, 可省略"}}, []),
}


def _failure_result(error: Exception) -> dict[str, Any]:
    """
    按好友工具契约构造嵌套失败结果

    参数:
    - error: 捕获的异常

    返回:
    - 带 code, message 与 retryable 的失败结果
    """
    code, message, retryable = failure(error)
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}


class _FriendMixin:
    """声明保持稳定, 执行时核验可信平台来源和本人范围"""
    tool_name: str | None
    config: dict[str, Any]
    _plugin_entry_binding: PluginEntryBinding

    def _resolve(self, *, preview: bool = False) -> tuple[Any, Any]:
        """
        核验来源账号, 群聊或私聊及功能开关

        参数:
        - preview: 检查能否进入执行路径时跳过权限和写开关, 执行时每次复核

        返回:
        - 当前适配器和可信调用来源
        """
        origin = current_call_origin()
        if origin is None or origin.actor_kind != "platform_user" or not origin.actor_id or not (is_private_origin(origin) or is_group_origin(origin)):
            raise FriendError("permission_denied", "好友工具需要有效的平台发送者身份")
        if not preview:
            require_plugin_entry_permission(self._plugin_entry_binding, refresh=True)
        manager = current_adapter_manager()
        adapter = manager.get_adapter(origin.adapter_id) if manager else None
        if adapter is None or not adapter.friend_host or not origin.self_id or adapter.friend_account() != origin.self_id:
            raise FriendError("stale_account", "来源账号或好友宿主不可用")
        if not adapter.config.enable or not adapter.config.settings.get("enable_private" if is_private_origin(origin) else "enable_group", True):
            raise FriendError("permission_denied", "来源平台对话已停用")
        name = str(self.tool_name)
        if not preview and name in {"friend_manager_handle_request", "friend_manager_delete_friend", "friend_manager_send_request"}:
            switch = {"friend_manager_handle_request": "request_handling_enabled", "friend_manager_delete_friend": "delete_friend_enabled", "friend_manager_send_request": "send_request_enabled"}[name]
            if self.config.get(switch) is not True:
                raise FriendError("feature_disabled", "对应好友写功能未开启")
        return adapter, origin

    def _scope(self, *, write: bool = False) -> str:
        """
        根据当前管理授权选择全量或本人范围, 不使用模型提供的身份

        参数:
        - write: 写操作要求同时具有访问和写权限

        返回:
        - all 或 self, 授权系统错误不会降级放行
        """
        decision = authorize_management_permissions(self._plugin_entry_binding, ("access", "write") if write else ("access",))
        if decision.status != "allowed" and decision.reason_code != "permission_denied":
            raise FriendError("permission_denied", "无法确认当前好友权限")
        return "all" if decision.status == "allowed" else "self"

    def is_available_for_call(self) -> bool:
        """
        检查有效平台来源, 调用者权限和具体能力由执行路径返回明确错误

        返回:
        - 当前请求能否进入好友宿主执行路径
        """
        try:
            self._resolve(preview=True)
            return True
        except (FriendError, PermissionError, ValueError):
            return False

    def get_tool_defined(self) -> dict[str, Any]:
        """
        返回严格且可读的工具参数

        返回:
        - 函数 JSON schema
        """
        description, properties, required = DEFINITIONS[str(self.tool_name)]
        return {"type": "function", "function": {"name": self.tool_name, "description": description,
                "parameters": {"type": "object", "properties": copy.deepcopy(properties), "required": required, "additionalProperties": False}}}

    async def _run(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        将工具参数交给宿主并捕获失败

        参数:
        - kwargs: 模型提交的参数

        返回:
        - 宿主数据或带稳定原因的失败
        """
        try:
            name = str(self.tool_name)
            _, properties, required = DEFINITIONS[name]
            if set(kwargs) - set(properties) or not set(required) <= kwargs.keys():
                raise FriendError("invalid_parameters", "工具参数缺失或含未知字段")

            adapter, origin = self._resolve()

            # Step.1 验证当前会话范围, 本人服务和管理服务共享宿主边界
            await (adapter.group_chat_private_scope(origin) if is_private_origin(origin) else adapter.group_chat_scope(origin))
            host = adapter.friend_host
            scope = self._scope(write=name in {"friend_manager_handle_request", "friend_manager_delete_friend", "friend_manager_send_request"})
            owner = origin.actor_id if scope == "self" else ""

            if name in {"friend_manager_list_friends", "friend_manager_find_friends"}:
                query = kwargs.get("query", "")
                if name == "friend_manager_find_friends":
                    text_id(query, "搜索文字")
                result = await host.list_friends(origin.self_id, actor=origin.actor_id, query=query,
                                                limit=kwargs.get("limit", 20), cursor=kwargs.get("cursor"))
            elif name == "friend_manager_list_requests":
                result = await host.requests(origin.self_id, kwargs.get("limit", 20), kwargs.get("cursor"), actor=origin.actor_id,
                                             view=kwargs.get("view", "active"), owner_user_id=owner)
            elif name == "friend_manager_recheck_request":
                result = await host.recheck_request(origin.self_id, kwargs["request_id"], owner_user_id=owner)
            else:
                if name in {"friend_manager_delete_friend", "friend_manager_send_request"}:
                    kwargs = {**kwargs, "user_id": kwargs.get("user_id", origin.actor_id)}
                    if owner and kwargs["user_id"] != owner:
                        raise FriendError("permission_denied", "普通用户只能管理本人的好友关系")
                elif owner:
                    await host.recheck_request(origin.self_id, kwargs["request_id"], owner_user_id=owner)
                def permission(live: Any, target: str) -> None:
                    """
                    等待和审批后复核本插件权限及目标保护

                    参数:
                    - live: 当前工具
                    - target: 固定好友或申请 ID
                    """
                    current, _ = live._resolve()
                    if current is not adapter:
                        raise PermissionError("来源适配器已变化")
                    if scope == "all" and live._scope(write=True) != "all":
                        raise PermissionError("好友管理写权限已撤销")
                    if scope == "self" and name != "friend_manager_handle_request" and target != origin.actor_id:
                        raise PermissionError("目标不是当前请求者本人")
                    if name == "friend_manager_delete_friend" and target in permission_id_list(live.config.get("managers")) + permission_id_list(live.config.get("protected_friend_ids")):
                        raise PermissionError("目标好友受保护")
                source = model_tool_authorization(self, origin, "friend_manager", permission)
                source = replace(source, identity={**source.identity, "friend_scope": scope})
                action = {"friend_manager_delete_friend": "delete_friend", "friend_manager_send_request": "send_request", "friend_manager_handle_request": "handle_request"}[name]
                result = await host.submit(origin.self_id, secrets.token_hex(16), action, kwargs, actor="model", source=source)
            self._resolve()
            if name in {"friend_manager_list_requests", "friend_manager_recheck_request"} and self._scope() != scope:
                raise FriendError("permission_denied", "好友查询权限已变化, 请重新查询")
            return {"ok": True, "data": result}
        except Exception as error:
            code, message, _ = failure(error)
            logger.warning(f"[friend_manager] 工具失败 tool={self.tool_name} code={code} reason={message}")
            return _failure_result(error)


class FriendTool(_FriendMixin, Tool):
    """同步会话通过来源平台循环执行好友宿主"""

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """
        同步桥接且超时取消, 不自动重试写操作

        参数:
        - kwargs: 模型参数

        返回:
        - 宿主结果; 平台循环不可用返回 unavailable, 超时返回未确认结果, 其余失败按宿主错误码返回
        """
        try:
            adapter, _ = self._resolve()
            loop = getattr(adapter, "_loop", None)
            try:
                return run_on_platform_loop(self._run(kwargs), loop, 40)
            except PlatformLoopUnavailable as error:
                raise FriendError("unavailable", "来源平台事件循环不可用于同步工具") from error
        except Exception as error:
            code, message, _ = failure(error)
            logger.warning(f"[friend_manager] 同步调用失败 code={code} reason={message}")
            return _failure_result(error)


class AsyncFriendTool(_FriendMixin, AsyncTool):
    """异步会话直接等待好友宿主"""

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """
        异步执行工具

        参数:
        - kwargs: 模型参数

        返回:
        - 宿主结果或失败说明
        """
        return await self._run(kwargs)


def get_tools(session: Session | AsyncSession, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None) -> list[Any]:
    """
    建立独立好友工具并保留弱会话身份

    参数:
    - session: 当前会话
    - config: 已合成的插件配置
    - resources: 插件资源对象, 本插件从会话读取好友宿主

    返回:
    - 与会话执行方式一致的七个工具
    """
    tools = []
    kind = AsyncFriendTool if isinstance(session, AsyncSession) else FriendTool
    for name, (description, _, _) in DEFINITIONS.items():
        tool = kind(name, description, {})
        tool.config = dict(config or {})
        bind_tool_session(tool, session)
        tool.recovery_policy = "manual" if name in {"friend_manager_handle_request", "friend_manager_delete_friend", "friend_manager_send_request"} else "retry"
        tools.append(tool)
    bind_plugin_factory_tools(tools, __file__)
    return tools
