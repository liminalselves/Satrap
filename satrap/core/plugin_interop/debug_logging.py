"""
原生命令的对象级诊断设置接口

真实入口和调用者由宿主核验, 插件不能传入任意群号或通过模型工具修改设置
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from satrap.core.call_context import current_call_origin, current_tool_workflow
from satrap.core.log import logger
from satrap.core.log.debug.contracts import DebugMode, current_debug_object
from satrap.core.log.debug.policy import policy_store
from satrap.core.plugin_authorization import PluginEntryBinding, authorize_management_permissions, require_plugin_entry_permission

if TYPE_CHECKING:
    from .capabilities import PluginHost


@dataclass(frozen=True)
class DebugLoggingAccess:
    """绑定插件安装代次和真实原生命令的诊断管理入口"""
    _host: PluginHost

    def _authorize(self, *, global_default: bool = False) -> object:
        """
        核验实际命令及当前来源, 群内授权不能修改全局默认

        参数:
        - global_default: 是否要求全局管理授权

        返回:
        - 已通过授权的实际宿主会话
        """
        runtime = self._host._runtime()
        invocation = runtime.require_execution(self._host)
        session = runtime.session()
        if invocation.kind != "commands" or invocation.parent is not None or current_tool_workflow() is not None:
            raise PermissionError("Debug 设置只能由可信用户原生命令管理")
        registry = runtime.command_registry()
        binding = getattr(registry, "_permission_bindings", {}).get(invocation.name)
        if not isinstance(binding, PluginEntryBinding) or binding.plugin_name != invocation.plugin:
            raise PermissionError("Debug 命令缺少有效权限绑定")
        require_plugin_entry_permission(binding, refresh=True)
        origin = current_call_origin()
        if origin is None and session.plugin_environment.session_type == "platform":
            raise PermissionError("平台 Debug 命令缺少真实发话者身份")
        if origin is not None:
            name = "debug_default_manage" if global_default else "debug_manage"
            decision = authorize_management_permissions(binding, (name,))
            if decision.status != "allowed":
                raise PermissionError("当前用户没有高级 Debug 管理权限")
            if global_default and any(grant.source == "administrator_chat" for grant in decision.grants):
                raise PermissionError("群内管理授权不能修改整体 Debug 默认")
        return session

    def status(self) -> dict[str, object]:
        """
        查询当前可信对象设置, 不激活其它上下文

        返回:
        - 稳定对象, 模式, 生效值及修订
        """
        session = self._authorize()
        obj = current_debug_object(session)
        selection = policy_store().read().resolve(obj)
        return {**asdict(selection), "object": obj.payload()}

    def set_mode(self, mode: DebugMode) -> dict[str, object]:
        """
        修改当前对象覆盖, 下一请求读取新设置

        参数:
        - mode: inherit, on 或 off

        返回:
        - 修改后的对象状态
        """
        session = self._authorize()
        obj = current_debug_object(session)
        store = policy_store()
        previous = store.read().resolve(obj)
        store.set_object(obj, mode, previous.revision)
        origin = current_call_origin()
        logger.info(f"[高级 Debug 设置] object={obj.key} actor={origin.actor_id if origin else 'local'} old={previous.mode} new={mode}")
        return self.status()

    def default_status(self) -> dict[str, object]:
        """
        读取整体默认, 需要全局管理身份

        返回:
        - 整体默认及修订
        """
        self._authorize(global_default=True)
        snapshot = policy_store().snapshot()
        policy = snapshot["policy"]
        assert isinstance(policy, dict)
        return {"default_enabled": policy["default_enabled"], "revision": snapshot["revision"]}

    def set_default(self, enabled: bool) -> dict[str, object]:
        """
        修改未覆盖对象动态继承的默认设置

        参数:
        - enabled: 新的默认开关

        返回:
        - 修改后的默认与修订
        """
        self._authorize(global_default=True)
        store = policy_store()
        previous = store.snapshot()
        revision = previous["revision"]
        assert isinstance(revision, str)
        store.set_default(enabled, revision)
        origin = current_call_origin()
        logger.info(f"[高级 Debug 设置] default={enabled} actor={origin.actor_id if origin else 'local'}")
        return self.default_status()
