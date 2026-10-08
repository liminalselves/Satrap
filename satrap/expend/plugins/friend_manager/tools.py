"""独立好友管理模型工具, 只调用通用好友宿主"""
from __future__ import annotations

from typing import Any
import copy
import secrets

from satrap.core.call_context import current_call_origin, is_private_origin
from satrap.core.config.model_tool_authorization import bind_tool_session, model_tool_authorization
from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop
from satrap.core.platform import current_adapter_manager
from satrap.core.friends import FriendError
from satrap.core.friends.service import failure, text_id
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.plugin_authorization import PluginEntryBinding, authorize_plugin_entry, require_plugin_entry_permission, bind_plugin_factory_tools, permission_id_list
from satrap.edictum.plugin_resources import PluginResources
from satrap.core.log import logger

LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "description": "最多返回多少条, 默认 20; 有下一页时用 next_cursor 继续查询"}
CURSOR = {"type": "string", "maxLength": 256, "description": "上次返回的 next_cursor, 沿用相同查询条件"}
PAGING = {"limit": LIMIT, "cursor": CURSOR}
DEFINITIONS: dict[str, tuple[str, dict[str, Any], list[str]]] = {
    "friend_manager_list_friends": ("查看机器人的好友列表, 返回好友 ID, 昵称和备注; 列表较长时可继续翻页", PAGING, []),
    "friend_manager_find_friends": ("按好友 ID, 昵称或备注查找好友; 重名时返回全部候选, 先确认目标 ID 再操作", {
        **PAGING, "query": {"type": "string", "minLength": 1, "maxLength": 256, "description": "要查找的好友 ID, 昵称或备注, 可以填写昵称或备注的一部分"}}, ["query"]),
    "friend_manager_list_requests": ("查看机器人收到的待处理好友申请, 返回申请 ID, 申请人, 验证信息和有效期限", PAGING, []),
    "friend_manager_handle_request": ("同意或拒绝一条好友申请; 使用申请查询返回的 ID, 同意时可设置备注. 过期, 已处理或结果未知的申请不能重复执行", {
        "request_id": {"type": "string", "description": "申请查询返回的 request_id, 不能用用户 ID 代替"},
        "approve": {"type": "boolean", "description": "true 同意, false 拒绝"},
        "remark": {"type": "string", "maxLength": 60, "description": "同意后的好友备注, 可不填; 拒绝时不填写"}}, ["request_id", "approve"]),
    "friend_manager_delete_friend": ("申请删除机器人的一位好友; 填写已确认的好友 ID, 人工批准后才执行. pending 表示等待审批, 不代表已删除; 结果未知时不要重复提交", {
        "user_id": {"type": "string", "minLength": 1, "maxLength": 256, "description": "好友查询结果中已确认的目标 ID, 不能填写昵称"}}, ["user_id"]),
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
    """注入和执行时分别检查可信私聊来源"""
    tool_name: str | None
    config: dict[str, Any]
    _plugin_entry_binding: PluginEntryBinding

    def _resolve(self, *, preview: bool = False, recheck_entry: bool = True) -> tuple[Any, Any]:
        """
        核验来源账号, 私聊和配置名单

        参数:
        - preview: 声明过滤时不记录预期权限拒绝
        - recheck_entry: 执行入口已核验管理权限时可跳过重复判定, 等待之后的复检不受影响

        返回:
        - 当前适配器和可信调用来源
        """
        origin = current_call_origin()
        if origin is None or not is_private_origin(origin):
            raise FriendError("permission_denied", "好友工具仅限管理者私聊")
        if preview:
            if authorize_plugin_entry(self._plugin_entry_binding).status == "denied":
                raise FriendError("permission_denied", "当前调用者未获得好友管理权限")
        elif recheck_entry:
            require_plugin_entry_permission(self._plugin_entry_binding)
        manager = current_adapter_manager()
        adapter = manager.get_adapter(origin.adapter_id) if manager else None
        if adapter is None or not adapter.friend_host or not origin.self_id or adapter.friend_account() != origin.self_id:
            raise FriendError("stale_account", "来源账号或好友宿主不可用")
        if not adapter.config.enable or not adapter.config.settings.get("enable_private", True):
            raise FriendError("permission_denied", "来源平台私聊已停用")
        name = str(self.tool_name)
        if name in {"friend_manager_handle_request", "friend_manager_delete_friend"}:
            switch = "request_handling_enabled" if name.endswith("handle_request") else "delete_friend_enabled"
            if self.config.get(switch) is not True:
                raise FriendError("permission_denied", "好友模型写操作未开启或调用者未获授权")
        return adapter, origin

    def is_available_for_call(self) -> bool:
        """
        普通用户与群聊不注入好友工具

        返回:
        - 当前调用是否有对应能力和权限
        """
        try:
            adapter, _ = self._resolve(preview=True)
            cap = {"friend_manager_list_friends": "list_friends", "friend_manager_find_friends": "list_friends",
                   "friend_manager_list_requests": "list_requests", "friend_manager_handle_request": "handle_request",
                   "friend_manager_delete_friend": "delete_friend"}[str(self.tool_name)]
            return adapter.friend_capabilities().get(cap, {}).get("state") in {"supported", "unknown"}
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

            adapter, origin = self._resolve(recheck_entry=False)

            # 管理入口权限已由框架在派发前核验, 这里只核验本地来源; 等待之后的复检保持不变
            await adapter.group_chat_private_scope(origin)
            host = adapter.friend_host

            if name in {"friend_manager_list_friends", "friend_manager_find_friends"}:
                query = kwargs.get("query", "")
                if name == "friend_manager_find_friends":
                    text_id(query, "搜索文字")
                result = await host.list_friends(origin.self_id, actor=origin.actor_id, query=query,
                                                limit=kwargs.get("limit", 20), cursor=kwargs.get("cursor"))
            elif name == "friend_manager_list_requests":
                result = await host.requests(origin.self_id, kwargs.get("limit", 20), kwargs.get("cursor"), actor=origin.actor_id)
            else:
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
                    if name == "friend_manager_delete_friend" and target in permission_id_list(live.config.get("managers")) + permission_id_list(live.config.get("protected_friend_ids")):
                        raise PermissionError("目标好友受保护")
                source = model_tool_authorization(self, origin, "friend_manager", permission)
                action = "delete_friend" if name == "friend_manager_delete_friend" else "handle_request"
                result = await host.submit(origin.self_id, secrets.token_hex(16), action, kwargs, actor="model", source=source)
            self._resolve()
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
    - 与会话执行方式一致的五个工具
    """
    tools = []
    kind = AsyncFriendTool if isinstance(session, AsyncSession) else FriendTool
    for name, (description, _, _) in DEFINITIONS.items():
        tool = kind(name, description, {})
        tool.config = dict(config or {})
        bind_tool_session(tool, session)
        tool.recovery_policy = "manual" if name in {"friend_manager_handle_request", "friend_manager_delete_friend"} else "retry"
        tools.append(tool)
    bind_plugin_factory_tools(tools, __file__)
    return tools
