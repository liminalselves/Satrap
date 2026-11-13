"""插件权限声明数据契约。"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

@dataclass(frozen=True)
class ManagementPermission:
    """不可变的插件内部管理权限定义"""

    description: str
    """管理权限的用途说明, 供目录和管理界面展示"""
    caller_list: str | None
    """引用配置中的调用者名单, None 表示不使用本地名单授权"""
    empty_policy: str
    """关联名单为空时的判定, 为 allow 或 deny, 无名单时不参与授权"""
    system_admin: bool
    """是否接受宿主系统管理员授权作为本地名单之外的途径"""
    requirements: tuple[str, ...] = ()
    """业务条件的展示说明, 默认空元组, 不替代业务检查"""
    platform_group_admin: str | None = None
    """引用允许平台群管理员授权的布尔配置, None 表示不启用此途径"""


@dataclass(frozen=True)
class CommandPermissions:
    """命令默认门槛及精确子命令映射"""

    default: tuple[str, ...]
    """命令入口始终需要的权限标识, 空元组表示没有基础门槛"""
    subcommands: Mapping[str, tuple[str, ...]]
    """精确子命令到追加权限的映射, 空映射表示没有子命令门槛"""


@dataclass(frozen=True)
class PluginPermissions:
    """一次解析的不可变权限规则集合"""

    version: int
    """权限声明版本, 0 表示旧插件无声明, 当前严格声明版本为 1"""
    rules: Mapping[str, ManagementPermission]
    """权限标识到管理规则的不可变映射, 供入口引用"""
    tools: Mapping[str, tuple[str, ...]]
    """工具名到必须全部满足的管理权限的映射, 未列出的工具无需管理权限"""
    commands: Mapping[str, CommandPermissions]
    """命令名到基础及子命令权限的映射, 未列出的命令无需管理权限"""
    declared_tools: frozenset[str] = frozenset()
    """元数据声明的全部工具名, 用于拒绝未声明入口, 默认空集合"""
    declared_commands: frozenset[str] = frozenset()
    """元数据声明的全部命令名, 用于拒绝未声明入口, 默认空集合"""
    services: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    """服务名到必须全部满足的管理权限的映射, 默认空映射"""
    declared_services: frozenset[str] = frozenset()
    """元数据声明的全部服务名, 用于拒绝未声明入口, 默认空集合"""

    @property
    def supports_administrators(self) -> bool:
        """声明中是否至少有一项接受系统管理员授权"""
        return any(rule.system_admin for rule in self.rules.values())

    def required(self, kind: str, name: str, subcommand: str | None = None) -> tuple[str, ...]:
        """
        读取当前入口的权限并集

        参数:
        - kind: tools, services 或 commands
        - name: 宿主绑定的入口名
        - subcommand: 原生命令解析后的子命令, 不按前缀匹配

        返回:
        - 必须全部满足的权限 ID, 普通入口返回空元组
        """
        if kind in ("tools", "services"):
            entries = self.tools if kind == "tools" else self.services
            return entries.get(name, ())
        rule = self.commands.get(name)
        return tuple(dict.fromkeys((*rule.default, *rule.subcommands.get(subcommand or "", ())))) if rule else ()

    def to_payload(self) -> dict[str, Any]:
        """
        转为目录和前端可读的权限说明

        返回:
        - 只包含声明, 不包含当前成员名单或调用身份
        """
        return {"permission_schema_version": self.version,
                "management_permissions": {name: {"description": rule.description, "system_admin": rule.system_admin,
                    "requirements": list(rule.requirements),
                    **({"platform_group_admin": rule.platform_group_admin} if rule.platform_group_admin else {}),
                    **({"caller_list": rule.caller_list, "empty_policy": rule.empty_policy} if rule.caller_list else {})}
                    for name, rule in self.rules.items()},
                "tool_permissions": {name: list(values) for name, values in self.tools.items()},
                "service_permissions": {name: list(values) for name, values in self.services.items()},
                "command_permissions": {name: {**({"default": list(rule.default)} if rule.default else {}),
                    "subcommands": {key: list(values) for key, values in rule.subcommands.items()}} for name, rule in self.commands.items()},
                "supports_administrators": self.supports_administrators}


EMPTY_PERMISSIONS = PluginPermissions(0, MappingProxyType({}), MappingProxyType({}), MappingProxyType({}))
