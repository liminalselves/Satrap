"""
插件可用的宿主审批入口

业务请求保留实际工具或服务的身份, 模式管理仅接受可信用户命令;
接口不向插件返回内部审批引擎
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from satrap.core.plugin_interop.contracts import PluginInteractionError
from satrap.core.plugin_authorization import PluginEntryBinding, authorize_management_permissions, require_plugin_entry_permission
from satrap.core.approval.coordinator import approve_sync, approve_async
from satrap.core.approval.policy import RiskLevel
from satrap.core.approval.state import session_approval
from satrap.core.call_context import current_call_origin, current_tool_workflow


if TYPE_CHECKING:
    from satrap.core.plugin_interop.capabilities import PluginHost


@dataclass(frozen=True)
class ApprovalAccess:
    """绑定当前插件安装代次的审批访问对象"""

    _host: PluginHost

    def snapshot(self) -> dict[str, object]:
        """
        查询当前共享策略, 不把查询结果作为执行许可

        返回:
        - 审批模式, 版本, 计划模式及迁移说明
        """
        runtime = self._host._runtime()
        runtime.require_execution(self._host)
        engine = session_approval(runtime.session())
        return {"mode": engine.mode, "revision": engine.revision, "plan_mode": engine.plan_mode,
                "migration_notice": engine.migration_notice}

    def set_mode(self, mode: str) -> dict[str, object]:
        """
        从获授权的用户原生命令修改模式

        参数:
        - mode: user, auto 或 full-access

        返回:
        - 修改后的状态; 模型, 服务转发或来源缺失时拒绝
        """
        runtime = self._host._runtime()
        invocation = runtime.require_execution(self._host)
        session = runtime.session()
        if invocation.kind != "commands" or invocation.parent is not None or current_tool_workflow() is not None:
            raise PermissionError("修改审批模式必须使用可信的用户原生命令入口")
        registry = runtime.command_registry()
        binding = getattr(registry, "_permission_bindings", {}).get(invocation.name)
        if not isinstance(binding, PluginEntryBinding) or binding.plugin_name != invocation.plugin:
            raise PermissionError("审批命令缺少有效权限绑定")
        require_plugin_entry_permission(binding, refresh=True)
        origin = current_call_origin()
        if origin is None and session.plugin_environment.session_type == "platform":
            raise PermissionError("平台审批命令缺少有效的调用者身份")
        if origin is not None and authorize_management_permissions(binding, ("approval_manage",)).status != "allowed":
            raise PermissionError("只有获得宿主管理权限的用户可以管理审批模式")
        session_approval(session).set_mode(mode)
        return self.snapshot()

    def request_sync(self, operation: str, risk: RiskLevel, description: str,
                     action: dict[str, object]) -> tuple[bool, str]:
        """
        同步请求当前实际操作的审批

        参数:
        - operation: 宿主策略识别的操作类别
        - risk: 调用实现判定的风险级别
        - description: 用户可读的操作摘要
        - action: 完整且精确的实际参数

        返回:
        - 是否批准及拒绝原因; 审批完成后调用作用域失效时拒绝
        """
        runtime = self._host._runtime()
        invocation = runtime.require_execution(self._host)
        session = runtime.session()
        from satrap.edictum import SimpleSession

        if not isinstance(session, SimpleSession):
            raise PluginInteractionError("dependency_unavailable", "同步审批要求同步会话")
        result = approve_sync(session, session_approval(session), operation, risk, description,
                              action=action, tool_name=f"{invocation.plugin}.{invocation.name}")
        runtime.require_execution(self._host)
        return result

    async def request_async(self, operation: str, risk: RiskLevel, description: str,
                            action: dict[str, object]) -> tuple[bool, str]:
        """
        异步请求当前实际操作的审批

        参数:
        - operation: 宿主策略识别的操作类别
        - risk: 调用实现判定的风险级别
        - description: 用户可读的操作摘要
        - action: 完整且精确的实际参数

        返回:
        - 是否批准及拒绝原因; 等待后重新核验真实入口
        """
        runtime = self._host._runtime()
        invocation = runtime.require_execution(self._host)
        session = runtime.session()
        from satrap.edictum import AsyncSimpleSession

        if not isinstance(session, AsyncSimpleSession):
            raise PluginInteractionError("dependency_unavailable", "异步审批要求异步会话")
        result = await approve_async(session, session_approval(session), operation, risk, description,
                                     action=action, tool_name=f"{invocation.plugin}.{invocation.name}")
        runtime.require_execution(self._host)
        return result
