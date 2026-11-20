"""插件入口绑定与管理授权结果契约"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from satrap.core.type.plugin.permissions import PluginPermissions

if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession

    SessionType = SimpleSession | AsyncSimpleSession


@dataclass(frozen=True)
class PermissionGrant:
    """一项管理权限的实际授权来源"""

    permission: str
    """本次检查的插件管理权限标识"""
    allowed: bool
    """当前调用身份是否通过该项权限检查"""
    source: str = ""
    """实际授权来源, 如本地名单或管理员组, 空串表示未获授权"""
    group_ids: tuple[str, ...] = ()
    """授予权限的管理员组或平台群标识, 空元组表示没有关联组"""
    override_ids: tuple[str, ...] = ()
    """管理员服务返回的允许例外条目标识, 空元组表示没有关联例外"""


@dataclass(frozen=True)
class AuthorizationDecision:
    """单次授权结果, 不作为之后免检执行的凭证"""

    status: str
    """单次判定状态, 为 allowed, denied 或无需管理授权的 not_applicable"""
    plugin_name: str
    """被检查入口所属的插件名, 来自宿主绑定"""
    entry_kind: str
    """被检查的入口类别, 如 tools, services 或 commands"""
    entry_name: str
    """宿主注册的实际入口名, 与类别和插件名共同定位授权对象"""
    required_permissions: tuple[str, ...] = ()
    """该入口必须全部满足的权限标识, 空元组表示没有附加要求"""
    grants: tuple[PermissionGrant, ...] = ()
    """逐项权限的实际判定及来源, 默认空元组"""
    reason_code: str = ""
    """机器可读的拒绝或错误原因, 默认空串表示没有附加原因"""
    policy_revision: str = ""
    """管理员服务使用的策略修订号, 不使用该服务时为空串"""
    permission_fingerprint: str = ""
    """根据身份, 权限定义和授权来源计算的指纹, 未完成计算时为空串"""


@dataclass(frozen=True)
class PluginEntryBinding:
    """宿主绑定的入口, 配置和弱引用均不可由模型参数替换"""

    plugin_name: str
    """实际入口所属的插件名, 由宿主安装流程固定"""
    kind: str
    """入口类别, 用于选择工具, 服务或命令的权限规则"""
    name: str
    """实际注册的入口名称, 不接受模型参数替换"""
    permissions: PluginPermissions
    """同一插件已解析的不可变管理权限声明"""
    config: Mapping[str, Any]
    """入口实际使用的安装配置, 供授权读取名单和开关"""
    entry_ref: Callable[[], object | None]
    """入口对象的弱引用回调, 返回 None 表示入口已失效"""
    session_ref: Callable[[], SessionType | None] | None = None
    """来源会话的弱引用回调, 独立工厂调用时为 None, 引用失效时拒绝授权"""


class BoundPluginTool(Protocol):
    """
    bind_plugin_tool 绑定后的插件工具面

    config 与 _plugin_entry_binding 由安装流程注入, 各插件工具类以属性声明满足本契约;
    授权复核只依赖此面, 不绑定具体工具实现
    """

    tool_name: str | None
    """工具声明的名称, 未指定时可由工具实现确定"""
    _plugin_entry_binding: PluginEntryBinding
    """安装流程注入的入口绑定, 用于执行前复核权限"""
    is_enabled: Callable[[], bool]
    """查询工具当前启用状态的回调, 停用时不得继续授权"""

    @property
    def config(self) -> Mapping[str, Any]: ...
    """工具实际使用的安装配置, 供绑定和权限复核读取"""
