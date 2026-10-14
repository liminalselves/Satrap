"""原消息转发工具, 同步与异步共用平台执行路径"""
from __future__ import annotations

from typing import Any
from copy import deepcopy
import traceback

from satrap.core.call_context import require_call_origin, is_group_origin, is_private_origin
from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.log import logger
from satrap.core.message_forward import ForwardError
from satrap.core.platform import current_adapter_manager
from satrap.core.platform.loop_bridge import PlatformLoopUnavailable, run_on_platform_loop
from satrap.core.plugin_authorization import PluginEntryBinding, PluginPermissionDenied, authorize_management_permissions, authorize_plugin_entry, require_plugin_entry_permission, bind_plugin_factory_tools
from satrap.core.utils.TCBuilder import Tool, AsyncTool, strict_tool_definition
from satrap.edictum.plugin_resources import PluginResources

ADDRESS = {"type": "object", "properties": {
    "conversation_kind": {"type": "string", "enum": ["group", "private"]},
    "conversation_id": {"type": "string", "minLength": 1, "maxLength": 256}},
    "required": ["conversation_kind", "conversation_id"], "additionalProperties": False,
    "description": "群聊或私聊的对话地址, 省略时使用当前对话; 跨对话由后端检查当前发送者的跨对话权限"}
DEFINITIONS = {
    "message_forward_read": ("查看一条合并转发的内容预览, 默认当前对话; 指定其他对话需跨对话授权. 预览可能截断; 转发原文请使用 send, 不要把预览复述后当成原文", {
        "source_message_id": {"type": "string", "description": "包含合并转发卡片的原消息 ID"}, "source": ADDRESS}, ["source_message_id"]),
    "message_forward_send": ("按原消息 ID 真正转发原文, 支持群聊和私聊. mode=merge 合并多条原消息, existing_forward 转发已有合并转发卡片; 无法保留原文时明确失败, 不用复述代替. unknown 时不要重复发送", {
        "message_ids": {"type": "array", "minItems": 1, "maxItems": 30, "uniqueItems": True, "items": {"type": "string"}, "description": "来源对话的原消息 ID, 不填写正文或发送者"},
        "mode": {"type": "string", "enum": ["merge", "existing_forward"], "description": "默认 merge; existing_forward 只能指定一个卡片消息 ID"},
        "source": ADDRESS, "target": ADDRESS}, ["message_ids"]),
    "message_forward_compose": ("将你写的多段文字创建为机器人署名的文字合集; 这是新内容, 不能称为原文转发. 原文请用 send 工具", {
        "nodes": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "object", "properties": {
            "content": {"type": "string", "minLength": 1, "maxLength": 2000}, "name": {"type": "string", "maxLength": 30}},
            "required": ["content"], "additionalProperties": False}}, "target": ADDRESS}, ["nodes"]),
}


def _failure(error: Exception) -> dict[str, Any]:
    """
    同步与异步共用错误形状, 只记录不含原消息正文的诊断

    参数:
    - error: 执行失败

    返回:
    - 公开错误码与说明, 未确认结果不允许自动重试
    """
    if isinstance(error, (ForwardError, PermissionError, ValueError, PlatformLoopUnavailable)):
        logger.warning(f"[message_forward] 转发未执行或失败, 原因={type(error).__name__}")
        fallback = "permission_denied" if isinstance(error, PermissionError) else "unavailable" if isinstance(error, PlatformLoopUnavailable) else "invalid_parameters"
        return {"ok": False, "error": {"code": getattr(error, "code", fallback), "message": str(error), "retryable": False}}
    logger.error(f"[message_forward] 转发异常: {traceback.format_exc()}")
    return {"ok": False, "error": {"code": "unconfirmed", "message": "转发结果无法确认, 请核查后再操作", "retryable": False}}


def resolve(config: dict[str, Any], check: Any, params: dict[str, Any], *, command: bool = False,
            binding: PluginEntryBinding | None = None) -> tuple[Any, Any]:
    """
    核验当前可信身份, 对话范围与独立发送开关

    参数:
    - config: 已合成的插件配置
    - check: 本入口的实时权限检查
    - params: 模型或命令输入的对话地址
    - command: 是否经过真实命令入口授权, 默认 False
    - binding: 模型工具入口, 跨对话工具调用时必须存在

    返回:
    - 当前适配器和可信来源, 权限或对话范围不符合时抛出错误
    """
    check()
    origin = require_call_origin()
    if origin.actor_kind != "platform_user" or not (is_group_origin(origin) or is_private_origin(origin)):
        raise ForwardError("permission_denied", "转发需要有效的群聊或私聊来源")
    manager = current_adapter_manager()
    adapter = manager.get_adapter(origin.adapter_id) if manager else None
    if adapter is None or not adapter.config.enable or not adapter.supports_message_forward() or adapter.message_forward_account() != origin.self_id:
        raise ForwardError("unavailable", "当前账号或平台转发能力不可用")
    current = {"conversation_kind": "group" if is_group_origin(origin) else "private",
               "conversation_id": origin.conversation_id or origin.chat_id}
    cross = False
    for address in (params.get("source", current), params.get("target", current)):
        if not isinstance(address, dict) or set(address) != set(current) or address["conversation_kind"] not in {"group", "private"} or not isinstance(address["conversation_id"], str) or not address["conversation_id"]:
            raise ForwardError("invalid_parameters", "对话地址无效")
        cross = cross or address != current
        if not adapter.message_forward_conversation_allowed(address["conversation_kind"], address["conversation_id"]):
            raise ForwardError("permission_denied", "来源或目标对话超出平台允许范围")
    if cross:
        if config.get("cross_conversation_enabled") is not True:
            raise ForwardError("feature_disabled", "跨对话功能未开启")
        if not command:
            if binding is None:
                raise ForwardError("permission_denied", "跨对话调用缺少有效工具入口")
            required = binding.permissions.required("tools", binding.name)
            decision = authorize_management_permissions(binding, tuple(dict.fromkeys((*required, "cross"))))
            if decision.status != "allowed":
                raise PluginPermissionDenied(decision)
    return adapter, origin


class _ForwardMixin:
    """共享声明, 权限复查和实际执行"""
    tool_name: str | None
    config: dict[str, Any]
    _plugin_entry_binding: PluginEntryBinding

    def get_tool_defined(self) -> dict[str, Any]:
        """
        返回稳定的工具声明

        返回:
        - 严格参数 schema, 调用者权限由执行路径核验
        """
        description, properties, required = DEFINITIONS[str(self.tool_name)]
        definition = {"type": "function", "function": {"name": self.tool_name, "description": description,
                      "parameters": {"type": "object", "properties": deepcopy(properties)}}}
        return strict_tool_definition(definition, required)

    def _check(self) -> None:
        """实时复核入口权限与发送开关"""
        require_plugin_entry_permission(self._plugin_entry_binding, refresh=True)
        if self.tool_name != "message_forward_read" and self.config.get("send_enabled") is not True:
            raise ForwardError("permission_denied", "本插件的发送功能未开启")

    def is_available_for_call(self) -> bool:
        """
        判断当前来源是否可进入执行路径

        返回:
        - 来源和入口可用时为 True, 无效或无权限时为 False
        """
        try:
            if authorize_plugin_entry(self._plugin_entry_binding).status == "denied":
                return False
            resolve(self.config, self._check, {}, binding=self._plugin_entry_binding)
            return True
        except (ForwardError, PermissionError, ValueError):
            return False

    async def _run(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        校验参数并在共享平台路径执行

        参数:
        - params: 模型提交的参数

        返回:
        - 平台数据或明确失败原因, 不自动重试发送
        """
        try:
            _, properties, required = DEFINITIONS[str(self.tool_name)]
            if set(params) - set(properties) or not set(required) <= params.keys():
                raise ForwardError("invalid_parameters", "工具参数缺失或包含未知字段")
            adapter, origin = resolve(self.config, self._check, params, binding=self._plugin_entry_binding)
            def check() -> None:
                """等待和实际发送前复核来源与跨对话权限"""
                current, current_origin = resolve(self.config, self._check, params, binding=self._plugin_entry_binding)
                if current is not adapter or current_origin != origin:
                    raise ForwardError("stale_account", "转发来源已变化")
            data = await adapter.message_forward(str(self.tool_name).removeprefix("message_forward_"), origin, params, check)
            return {"ok": True, "data": data}
        except Exception as error:
            return _failure(error)


class ForwardTool(_ForwardMixin, Tool):
    """同步工具通过来源平台事件循环执行"""

    def execute(self, **params: Any) -> dict[str, Any]:
        """
        同步桥接来源平台的事件循环

        参数:
        - params: 模型提交的参数

        返回:
        - 平台结果或失败说明
        """
        try:
            adapter, _ = resolve(self.config, self._check, params, binding=self._plugin_entry_binding)
            return run_on_platform_loop(self._run(params), adapter._loop, 40)
        except Exception as error:
            return _failure(error)


class AsyncForwardTool(_ForwardMixin, AsyncTool):
    """异步工具直接使用共享执行路径"""

    async def execute(self, **params: Any) -> dict[str, Any]:
        """
        异步执行共享平台路径

        参数:
        - params: 模型提交的参数

        返回:
        - 平台结果或失败说明
        """
        return await self._run(params)


def get_tools(session: Session | AsyncSession, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None) -> list[Any]:
    """
    按会话类型构建三个独立转发工具

    参数:
    - session: 当前同步或异步会话
    - config: 合成后的配置, None 使用空配置
    - resources: 插件资源, 本插件使用来源平台能力

    返回:
    - 读取, 原文发送和文字合集工具, 发送工具禁止自动重试
    """
    kind = AsyncForwardTool if isinstance(session, AsyncSession) else ForwardTool
    tools = []
    for name, (description, _, _) in DEFINITIONS.items():
        tool = kind(name, description, {})
        tool.config = dict(config or {})
        tool.recovery_policy = "retry" if name.endswith("read") else "manual"
        tools.append(tool)
    bind_plugin_factory_tools(tools, __file__)
    return tools
