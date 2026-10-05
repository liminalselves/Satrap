"""
插件管理入口的统一授权

显式声明决定哪些入口需要管理权限, 本地名单与系统管理组提供两条授权路径;
业务开关, 对话范围和审批仍由各宿主服务核验
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import Any, TYPE_CHECKING
import hashlib
import json
import weakref

from satrap.core.call_context import current_call_origin, current_tool_workflow, CallOrigin
from satrap.core.log import logger
if TYPE_CHECKING:
    from satrap.edictum.plugin_permissions import PluginPermissions
    from satrap.core.config.administrator_groups import AdministratorService


@dataclass(frozen=True)
class PermissionGrant:
    """一项管理权限的实际授权来源"""

    permission: str
    allowed: bool
    source: str = ""
    group_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorizationDecision:
    """单次授权结果, 不作为之后免检执行的凭证"""

    status: str
    plugin_name: str
    entry_kind: str
    entry_name: str
    required_permissions: tuple[str, ...] = ()
    grants: tuple[PermissionGrant, ...] = ()
    reason_code: str = ""
    policy_revision: str = ""
    permission_fingerprint: str = ""


class PluginPermissionDenied(PermissionError):
    """可由插件或 API 边界捕获的管理权限拒绝"""

    def __init__(self, decision: AuthorizationDecision) -> None:
        """
        保留稳定原因和可读提示

        参数:
        - decision: 已判定的拒绝结果
        """
        self.decision = decision
        self.code = decision.reason_code
        super().__init__(f"当前调用者未获得 {decision.plugin_name} 管理入口权限 ({self.code})")


def _ids(value: object) -> tuple[str, ...]:
    """
    严格读取名单, 不将无效对象转换成身份

    参数:
    - value: 实际安装配置中的名单

    返回:
    - 去重账号元组, 缺失按空名单; 类型错误抛出 ValueError
    """
    if value is None:
        return ()
    values = value.splitlines() if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError("调用者名单必须是逐行文字或字符串列表")
    return tuple(sorted({item.strip() for item in values if item.strip()}))


def evaluate_plugin_permissions(
    plugin_name: str, spec: PluginPermissions, kind: str, name: str, config: Mapping[str, Any],
    origin: CallOrigin | None, administrators: AdministratorService | None = None, *, subcommand: str | None = None,
) -> AuthorizationDecision:
    """
    对可信宿主输入计算权限, 不承担入口安装状态判断

    参数:
    - plugin_name: 已固定实际插件名称
    - spec: 同一插件的不可变声明
    - kind: tools 或 commands
    - name: 当前入口名称
    - config: 宿主解析的实际安装配置
    - origin: 宿主可信来源
    - administrators: 当前平台宿主授权服务, 缺失时只使用原有名单
    - subcommand: 已解析原生命令的子命令

    返回:
    - 授权结果及相关指纹; 普通入口返回 not_applicable
    """
    required = spec.required(kind, name, subcommand)
    identity = (plugin_name, kind, name)
    if not required:
        return AuthorizationDecision("not_applicable", *identity)
    if origin is None or origin.actor_kind != "platform_user" or not origin.actor_id:
        return AuthorizationDecision("denied", *identity, required, reason_code="identity_missing")
    admin = administrators.resolve(origin, plugin_name) if administrators else None
    grants: list[PermissionGrant] = []
    definitions: list[tuple[object, ...]] = []
    try:
        for permission in required:
            rule = spec.rules[permission]
            callers = _ids(config.get(rule.caller_list)) if rule.caller_list else ()
            local = bool(rule.caller_list) and (origin.actor_id in callers or not callers and rule.empty_policy == "allow")
            source = "local_list" if local and callers else "local_empty_allow" if local else ""
            allowed = local
            group_ids: tuple[str, ...] = ()
            if not local and rule.system_admin and admin and admin.allowed:
                allowed, source, group_ids = True, "administrator_group", admin.group_ids
            grants.append(PermissionGrant(permission, allowed, source, group_ids))
            definitions.append((permission, rule.caller_list, rule.empty_policy, rule.system_admin))
    except (KeyError, ValueError, TypeError):
        return AuthorizationDecision("denied", *identity, required, reason_code="invalid_permission_config")
    permitted = all(grant.allowed for grant in grants)
    fingerprint = hashlib.sha256(json.dumps([origin.adapter_id, origin.self_id, origin.actor_id, identity, definitions,
        [(grant.permission, grant.allowed, grant.source, grant.group_ids) for grant in grants]], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return AuthorizationDecision("allowed" if permitted else "denied", *identity, required, tuple(grants),
                                 "" if permitted else "permission_denied", admin.revision if admin else "", fingerprint)


@dataclass(frozen=True)
class PluginEntryBinding:
    """宿主绑定的入口, 配置和弱引用均不可由模型参数替换"""

    plugin_name: str
    kind: str
    name: str
    permissions: PluginPermissions
    config: Mapping[str, Any]
    entry_ref: Any
    session_ref: Any = None


@dataclass
class _CommandScope:
    """可撤销的原生命令作用域, 防止复制上下文后继续使用授权"""

    binding: PluginEntryBinding
    active: bool = True


_NATIVE_COMMAND: ContextVar[_CommandScope | None] = ContextVar("satrap_native_management_command", default=None)


@contextmanager
def bind_native_command(binding: PluginEntryBinding) -> Iterator[None]:
    """
    在原生命令分派期间绑定固定入口

    参数:
    - binding: 注册表绑定的命令, 不接受模型提供的身份

    返回:
    - 命令执行作用域, 结束后立即撤销
    """
    scope = _CommandScope(binding)
    token = _NATIVE_COMMAND.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _NATIVE_COMMAND.reset(token)


def bind_plugin_tool(tool: Any, plugin_name: str, permissions: PluginPermissions, config: Mapping[str, Any], session: Any = None) -> None:
    """
    为实际工具建立授权绑定, 会话仅由正式安装流程传入

    参数:
    - tool: 实际创建的工具
    - plugin_name: 固定所属插件
    - permissions: 已严格解析声明
    - config: 此工具实际使用的配置
    - session: 正式安装的原会话, 独立工厂使用 None
    """
    tool._plugin_entry_binding = PluginEntryBinding(plugin_name, "tools", tool.get_tool_name(), permissions, config,
                                                   weakref.ref(tool), weakref.ref(session) if session is not None else None)


def authorize_plugin_entry(binding: PluginEntryBinding, *, subcommand: str | None = None) -> AuthorizationDecision:
    """
    从真实作用域校验当前插件入口与名单

    参数:
    - binding: 宿主创建的入口绑定
    - subcommand: 原生命令解析后的子命令

    返回:
    - 当前授权结果, 失效或意外异常按拒绝处理并记录日志
    """
    identity = (binding.plugin_name, binding.kind, binding.name)
    try:
        from satrap.core.platform import current_adapter_manager
        if binding.kind not in ("tools", "commands"):
            return AuthorizationDecision("denied", *identity, reason_code="unknown_entry")
        entry = binding.entry_ref()
        if entry is None or binding.kind == "tools" and not entry.is_enabled():
            return AuthorizationDecision("denied", *identity, reason_code="entry_disabled")
        if binding.session_ref is not None:
            session = binding.session_ref()
            if session is None:
                return AuthorizationDecision("denied", *identity, reason_code="stale_authorization")
            plugin = next((item for item in session.list_plugins() if item.name == binding.plugin_name), None)
            if plugin is None or not plugin.enabled or not getattr(plugin, binding.kind).get(binding.name, False):
                return AuthorizationDecision("denied", *identity, reason_code="entry_disabled")
            if binding.kind == "tools":
                manager = getattr(getattr(session, "_wf", None), "tools_manager", None)
                if manager is None or manager.tools.get(binding.name) is not entry or not manager.is_tool_enabled(binding.name):
                    return AuthorizationDecision("denied", *identity, reason_code="stale_authorization")
            else:
                registry = getattr(session, "command_handler", None)
                if registry is None or registry.commands.get(binding.name) is not entry or not registry.is_command_enabled(binding.name):
                    return AuthorizationDecision("denied", *identity, reason_code="stale_authorization")
        if binding.kind == "commands":
            scope = _NATIVE_COMMAND.get()
            if scope is None or not scope.active or scope.binding is not binding or current_tool_workflow() is not None:
                return AuthorizationDecision("denied", *identity, reason_code="identity_missing")
        adapter_manager = current_adapter_manager()
        administrators = getattr(adapter_manager, "administrator_service", None)
        return evaluate_plugin_permissions(binding.plugin_name, binding.permissions, binding.kind, binding.name,
                                           binding.config, current_call_origin(), administrators, subcommand=subcommand)
    except Exception:
        logger.exception(f"[管理权限] 授权检查异常, 插件={binding.plugin_name}, 入口={binding.name}")
        return AuthorizationDecision("denied", *identity, reason_code="invalid_permission_config")


def require_plugin_entry_permission(binding: PluginEntryBinding, *, subcommand: str | None = None) -> AuthorizationDecision:
    """
    在实际执行入口拒绝未授权管理调用

    参数:
    - binding: 已绑定的插件入口
    - subcommand: 已解析的原生子命令

    返回:
    - 通过或无需管理授权的结果; 拒绝时记录日志并抛出 PluginPermissionDenied
    """
    result = authorize_plugin_entry(binding, subcommand=subcommand)
    if result.status == "denied":
        origin = current_call_origin()
        logger.warning(f"[管理权限] 调用拒绝, 插件={binding.plugin_name}, 入口={binding.name}, "
                       f"调用者={origin.actor_id if origin else ''}, 原因={result.reason_code}")
        raise PluginPermissionDenied(result)
    return result
