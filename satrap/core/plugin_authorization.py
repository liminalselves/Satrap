"""
插件管理入口的统一授权

显式声明决定哪些入口需要管理权限, 本地名单与系统管理组提供两条授权路径;
业务开关, 对话范围和审批仍由各宿主服务核验; 入口判定只在当前处理步骤内复用
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TYPE_CHECKING
import hashlib
import json
import weakref
import traceback

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
        if self.code == "authorization_error":   # 系统故障与权限决定必须可区分, 避免运维误判
            message = f"{decision.plugin_name} 管理入口权限检查暂时失败, 请查看后端日志 ({self.code})"
        else:
            message = f"当前调用者未获得 {decision.plugin_name} 管理入口权限 ({self.code})"
        super().__init__(message)


def permission_id_list(value: object) -> tuple[str, ...]:
    """
    严格读取权限名单, 不将无效对象转换成身份

    权限名单 (调用者, 允许群, 保护对象等限定访问范围或保护对象的名单) 一律走本函数; 普通业务
    配置请用 satrap.core.config.model_tool_authorization.config_ids, 它宽松接受任意类型且不去重排序

    参数:
    - value: 实际安装配置中的名单

    返回:
    - 去重排序后的账号元组, 缺失按空名单; 类型错误抛出 ValueError
    """
    if value is None:
        return ()
    values = value.splitlines() if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError("权限名单必须是逐行文字或字符串列表")
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
    identity = (plugin_name, kind, name)
    if kind not in ("tools", "commands") or spec.version and name not in (spec.declared_tools if kind == "tools" else spec.declared_commands):
        return AuthorizationDecision("denied", *identity, reason_code="unknown_entry")
    required = spec.required(kind, name, subcommand)
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
            callers = permission_id_list(config.get(rule.caller_list)) if rule.caller_list else ()
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


@dataclass
class _AuthorizationStep:
    """单步判定缓存, 作用域结束后复制出去的上下文同样不能复用"""

    decisions: list[tuple[PluginEntryBinding, str | None, CallOrigin | None, AuthorizationDecision]] = field(
        default_factory=list[tuple[PluginEntryBinding, str | None, CallOrigin | None, AuthorizationDecision]])
    active: bool = True


_AUTHORIZATION_STEP: ContextVar[_AuthorizationStep | None] = ContextVar(
    "satrap_authorization_step", default=None)
"""当前处理步骤内的入口判定缓存, 未进入作用域或作用域已失效时不缓存任何结果"""


@contextmanager
def bind_authorization_step() -> Iterator[None]:
    """
    在当前处理步骤内复用同一入口的授权判定

    同一入口在该步骤内只判定一次, 声明过滤与执行前检查共享结果; 嵌套进入只沿用仍有效的
    外层缓存, 退出最外层后立即撤销, 复制出去的上下文与之后的调用都重新判定

    返回:
    - 单步作用域
    """
    outer = _AUTHORIZATION_STEP.get()
    if outer is not None and outer.active:
        yield
        return
    scope = _AuthorizationStep()
    token = _AUTHORIZATION_STEP.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _AUTHORIZATION_STEP.reset(token)


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


_FACTORY_DECLARATIONS: dict[str, tuple[tuple[int, int], str, PluginPermissions]] = {}
"""工厂声明按路径缓存, meta.yaml 以修改时间与大小标识内容"""

_FACTORY_DECLARATIONS_LIMIT = 128


def _factory_declarations(meta_path: Path) -> tuple[str, PluginPermissions]:
    """
    读取工厂声明, meta.yaml 未变化时复用已解析结果

    参数:
    - meta_path: 工厂模块相邻的 meta.yaml

    返回:
    - 插件名与已严格解析的声明; 文件缺失或声明非法时抛出与直接解析一致的错误
    """
    from satrap.edictum.plugin import load_plugin_meta
    from satrap.edictum.plugin_config import parse_config_schema
    from satrap.edictum.plugin_permissions import parse_plugin_permissions

    try:
        stat = meta_path.stat()
    except OSError:
        # 缺失或不可读时仍由 load_plugin_meta 抛出既有错误
        meta = load_plugin_meta(meta_path.parent)
        return meta["name"], parse_plugin_permissions(meta, parse_config_schema(meta))
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _FACTORY_DECLARATIONS.get(str(meta_path))
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    meta = load_plugin_meta(meta_path.parent)
    name = meta["name"]
    permissions = parse_plugin_permissions(meta, parse_config_schema(meta))
    # 上限内保持稳定, 超出后整体丢弃, 避免常驻增长
    if len(_FACTORY_DECLARATIONS) >= _FACTORY_DECLARATIONS_LIMIT:
        _FACTORY_DECLARATIONS.clear()
    _FACTORY_DECLARATIONS[str(meta_path)] = (signature, name, permissions)
    return name, permissions


def bind_plugin_factory_tools(tools: list[Any], source_file: str) -> None:
    """
    为直接工厂调用绑定同一份声明, 正式安装时追加真实会话注册状态

    参数:
    - tools: 工厂实际构造的工具列表
    - source_file: 工厂模块路径, 固定从相邻 meta.yaml 读取声明
    """

    name, permissions = _factory_declarations(Path(source_file).parent / "meta.yaml")
    for tool in tools:
        bind_plugin_tool(tool, name, permissions, tool.config)


def plugin_tool_live(session: Any, plugin_name: str, name: str, tool: Any) -> str | None:
    """
    判定工具是否仍是来源会话主工作流中启用的同一插件工具

    参数:
    - session: 来源会话
    - plugin_name: 工具所属插件
    - name: 工具注册名
    - tool: 工具实例

    返回:
    - 仍有效返回 None; 插件或该能力停用返回 entry_disabled, 工具被移除, 替换或停用返回 stale_authorization
    """
    plugin = next((item for item in session.list_plugins() if item.name == plugin_name), None)
    if plugin is None or not plugin.enabled or not plugin.tools.get(name, False):
        return "entry_disabled"
    manager = getattr(getattr(session, "_wf", None), "tools_manager", None)
    if manager is None or manager.tools.get(name) is not tool or not manager.is_tool_enabled(name) or not tool.is_enabled():
        return "stale_authorization"
    return None


def authorize_plugin_entry(binding: PluginEntryBinding, *, subcommand: str | None = None) -> AuthorizationDecision:
    """
    读取当前处理步骤内的入口判定

    同一入口在同一处理步骤内只判定一次, 声明过滤与执行前检查共享同一结果;
    缓存按当前调用身份区分, 未进入步骤作用域, 作用域已失效或跨步骤调用时每次都重新判定并记录日志

    参数:
    - binding: 宿主创建的入口绑定
    - subcommand: 原生命令解析后的子命令

    返回:
    - 当前授权结果, 失效或意外异常按拒绝处理并记录日志
    """
    step = _AUTHORIZATION_STEP.get()
    if step is None or not step.active:
        return _evaluate_plugin_entry(binding, subcommand)
    origin = current_call_origin()
    for cached_binding, cached_subcommand, cached_origin, decision in step.decisions:
        if cached_binding is binding and cached_subcommand == subcommand and cached_origin is origin:
            return decision
    decision = _evaluate_plugin_entry(binding, subcommand)
    step.decisions.append((binding, subcommand, origin, decision))
    return decision


def _evaluate_plugin_entry(binding: PluginEntryBinding, subcommand: str | None) -> AuthorizationDecision:
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
            if binding.kind == "tools":
                reason = plugin_tool_live(session, binding.plugin_name, binding.name, entry)
                if reason is not None:
                    return AuthorizationDecision("denied", *identity, reason_code=reason)
            else:
                plugin = next((item for item in session.list_plugins() if item.name == binding.plugin_name), None)
                if plugin is None or not plugin.enabled or not plugin.commands.get(binding.name, False):
                    return AuthorizationDecision("denied", *identity, reason_code="entry_disabled")
                registry = getattr(session, "command_handler", None) or getattr(session, "cmd_handler", None)
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
        logger.error(f"[管理权限] 授权检查异常, 插件={binding.plugin_name}, 入口={binding.name}: {traceback.format_exc()}")
        return AuthorizationDecision("denied", *identity, reason_code="authorization_error")


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
                       f"平台={origin.adapter_id if origin else ''}, 请求={origin.request_id if origin else ''}, "
                       f"调用者={origin.actor_id if origin else ''}, 原因={result.reason_code}")
        raise PluginPermissionDenied(result)
    if result.status == "allowed":
        origin = current_call_origin()
        logger.debug(f"[管理权限] 调用允许, 插件={binding.plugin_name}, 入口={binding.name}, "
                     f"平台={origin.adapter_id if origin else ''}, 调用者={origin.actor_id if origin else ''}, "
                     f"请求={origin.request_id if origin else ''}, "
                     f"授权={[(item.permission, item.source, item.group_ids) for item in result.grants]}")
    return result
