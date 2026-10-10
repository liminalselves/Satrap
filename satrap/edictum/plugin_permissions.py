"""
插件管理权限声明

目录扫描与实际安装共用严格解析, 管理入口通过显式映射关联权限;
未声明接入的旧插件保持原有授权规则
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class ManagementPermission:
    """不可变的插件内部管理权限定义"""

    description: str
    caller_list: str | None
    empty_policy: str
    system_admin: bool
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandPermissions:
    """命令默认门槛及精确子命令映射"""

    default: tuple[str, ...]
    subcommands: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class PluginPermissions:
    """一次解析的不可变权限规则集合"""

    version: int
    rules: Mapping[str, ManagementPermission]
    tools: Mapping[str, tuple[str, ...]]
    commands: Mapping[str, CommandPermissions]
    declared_tools: frozenset[str] = frozenset()
    declared_commands: frozenset[str] = frozenset()

    @property
    def supports_administrators(self) -> bool:
        """声明中是否至少有一项接受系统管理员授权"""
        return any(rule.system_admin for rule in self.rules.values())

    def required(self, kind: str, name: str, subcommand: str | None = None) -> tuple[str, ...]:
        """
        读取当前入口的权限并集

        参数:
        - kind: tools 或 commands
        - name: 宿主绑定的入口名
        - subcommand: 原生命令解析后的子命令, 不按前缀匹配

        返回:
        - 必须全部满足的权限 ID, 普通入口返回空元组
        """
        if kind == "tools":
            return self.tools.get(name, ())
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
                    **({"caller_list": rule.caller_list, "empty_policy": rule.empty_policy} if rule.caller_list else {})}
                    for name, rule in self.rules.items()},
                "tool_permissions": {name: list(values) for name, values in self.tools.items()},
                "command_permissions": {name: {**({"default": list(rule.default)} if rule.default else {}),
                    "subcommands": {key: list(values) for key, values in rule.subcommands.items()}} for name, rule in self.commands.items()},
                "supports_administrators": self.supports_administrators}


EMPTY_PERMISSIONS = PluginPermissions(0, MappingProxyType({}), MappingProxyType({}), MappingProxyType({}))


def parse_plugin_permissions(meta: Mapping[str, Any], config_schema: Mapping[str, Any]) -> PluginPermissions:
    """
    严格解析插件管理声明, 对无声明旧插件保持兼容

    参数:
    - meta: 实际插件元数据
    - config_schema: 同一插件已解析配置 schema

    返回:
    - 不可变规则; 声明不合法时抛出 ValueError 供加载边界记录
    """
    keys = {"permission_schema_version", "management_permissions", "tool_permissions", "command_permissions"}
    if not keys.intersection(meta):
        return EMPTY_PERMISSIONS
    if type(meta.get("permission_schema_version")) is not int or meta["permission_schema_version"] != 1:
        raise ValueError("插件权限声明需要 permission_schema_version: 1")
    raw_rules = meta.get("management_permissions")
    if not isinstance(raw_rules, dict) or not raw_rules:
        raise ValueError("management_permissions 必须是非空对象")
    rules: dict[str, ManagementPermission] = {}
    for name, value in raw_rules.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip() or not isinstance(value, dict):
            raise ValueError("管理权限名称或定义无效")
        if set(value) - {"description", "caller_list", "empty_policy", "system_admin", "requirements"}:
            raise ValueError(f"管理权限 {name} 含有未知字段")
        description = value.get("description")
        if not isinstance(description, str) or not description.strip() or type(value.get("system_admin")) is not bool:
            raise ValueError(f"管理权限 {name} 需要用途说明和 system_admin 布尔值")
        caller_list = value.get("caller_list")
        policy = value.get("empty_policy", "deny")
        if caller_list is None:
            if "caller_list" in value or "empty_policy" in value:
                raise ValueError(f"管理权限 {name} 未关联名单时不能声明 caller_list 或 empty_policy")
        else:
            config_field = config_schema.get(caller_list) if isinstance(caller_list, str) else None
            field_type = config_field.get("type") if isinstance(config_field, dict) else getattr(config_field, "type", None)
            if config_field is None or field_type not in ("string", "textarea") or value.get("empty_policy") not in ("allow", "deny"):
                raise ValueError(f"管理权限 {name} 的名单引用或空值规则无效")
        requirements = value.get("requirements", [])
        if not isinstance(requirements, list) or len(requirements) > 32 or any(not isinstance(item, str) or not item.strip() for item in requirements):
            raise ValueError(f"管理权限 {name} 的业务条件说明无效")
        rules[name] = ManagementPermission(description.strip(), caller_list, policy, value["system_admin"], tuple(item.strip() for item in requirements))

    def permission_ids(value: object) -> tuple[str, ...]:
        """
        校验同一插件内的权限引用

        参数:
        - value: 入口声明的权限列表

        返回:
        - 不含重复引用的非空元组, 错误时抛出 ValueError
        """
        if not isinstance(value, list) or not value or any(not isinstance(item, str) or item not in rules for item in value):
            raise ValueError("管理入口权限列表为空或引用不存在的权限")
        if len(set(value)) != len(value):
            raise ValueError("管理入口权限列表含有重复项")
        return tuple(value)

    tools: dict[str, tuple[str, ...]] = {}
    raw_tools = meta.get("tool_permissions", {})
    if not isinstance(raw_tools, dict):
        raise ValueError("tool_permissions 必须是对象")
    declared_tools = meta.get("tools", {})
    if not isinstance(declared_tools, dict):
        raise ValueError("接入管理权限的工具声明必须是对象")
    for name, value in raw_tools.items():
        if not isinstance(name, str) or not isinstance(declared_tools, dict) or name not in declared_tools:
            raise ValueError("管理工具映射引用未声明工具")
        tools[name] = permission_ids(value)
    commands: dict[str, CommandPermissions] = {}
    raw_commands = meta.get("command_permissions", {})
    if not isinstance(raw_commands, dict):
        raise ValueError("command_permissions 必须是对象")
    declared_commands = meta.get("commands", {})
    if not isinstance(declared_commands, dict):
        raise ValueError("接入管理权限的命令声明必须是对象")
    for name, value in raw_commands.items():
        if not isinstance(name, str) or not isinstance(declared_commands, dict) or name not in declared_commands:
            raise ValueError("管理命令映射引用未声明命令")
        if not isinstance(value, dict) or not value or set(value) - {"default", "subcommands"}:
            raise ValueError("管理命令权限结构无效")
        default = permission_ids(value["default"]) if "default" in value else ()
        children = value.get("subcommands", {})
        if not isinstance(children, dict) or any(not isinstance(key, str) or not key or any(c.isspace() for c in key) for key in children):
            raise ValueError("管理子命令名称或映射无效")
        if not default and not children:
            raise ValueError("管理命令需要至少一项权限映射")
        commands[name] = CommandPermissions(default, MappingProxyType({key: permission_ids(item) for key, item in children.items()}))
    return PluginPermissions(1, MappingProxyType(rules), MappingProxyType(tools), MappingProxyType(commands),
                             frozenset(declared_tools), frozenset(declared_commands))


def validate_permission_install(spec: PluginPermissions, tools: Mapping[str, Any], commands: Mapping[str, Any]) -> None:
    """
    安装时核验权限绑定到实际注册入口

    参数:
    - spec: 目录解析的权限规则
    - tools: 实际创建的工具
    - commands: 实际创建的命令处理函数
    """
    if set(spec.tools) - set(tools) or set(spec.commands) - set(commands):
        raise ValueError("插件管理权限映射未匹配实际注册入口")
    for name, rule in spec.commands.items():
        children = getattr(commands[name], "subcommands", ())
        if not isinstance(children, (list, tuple, dict)) or set(rule.subcommands) - set(children):
            raise ValueError(f"管理命令 {name} 的子命令没有注册说明")
