"""
插件间交互声明与公共数据契约

严格解析六类能力的导出, 依赖, 替代和排斥关系;
普通数据在调用边界复制并校验, 不向消费者透传运行时对象
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypeAlias, cast
import math


CapabilityKind = Literal["tools", "services", "handlers", "mcp", "skills", "commands"]
CAPABILITY_KINDS: tuple[CapabilityKind, ...] = (
    "tools", "skills", "handlers", "commands", "mcp", "services",
)
CapabilityNames: TypeAlias = Mapping[CapabilityKind, tuple[str, ...]]


class PluginInteractionError(RuntimeError):
    """可供 SDK 与宿主边界统一识别的插件交互错误"""

    def __init__(self, code: str, message: str, *, plugin: str = "", capability: str = "",
                 reason_code: str = "", replaced_by: tuple[str, ...] = ()) -> None:
        """
        保存稳定错误码与定位字段

        参数:
        - code: 交互错误码
        - message: 可读失败原因
        - plugin: 相关插件名, 默认空字符串
        - capability: 相关能力名, 默认空字符串
        - reason_code: 具体不可用原因, 默认空字符串
        - replaced_by: 生效替代者名称, 默认空元组
        """
        super().__init__(message)
        self.code = code
        self.plugin = plugin
        self.capability = capability
        self.reason_code = reason_code
        self.replaced_by = replaced_by


@dataclass(frozen=True)
class ImageArtifact:
    """本次宿主执行产生的只读图片字节引用"""

    _payload: bytes = field(repr=False)
    _valid: Callable[[], bool] = field(repr=False, compare=False)

    def read(self) -> bytes:
        """
        在原执行范围仍有效时读取固定内容

        返回:
        - 冻结的图片字节; 来源或环境失效时拒绝读取
        """
        if not self._valid():
            raise PluginInteractionError("capability_stale", "图片产物的执行来源或宿主环境已失效")
        return self._payload


ServiceValue: TypeAlias = str | int | float | bool | None | ImageArtifact | list["ServiceValue"] | dict[str, "ServiceValue"]


@dataclass(frozen=True)
class Dependency:
    """一个提供者的必需或可选依赖"""

    optional: bool = False
    capabilities: CapabilityNames = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结调用者传入的类别映射, 不共享可变容器"""
        object.__setattr__(self, "capabilities", MappingProxyType({kind: tuple(names) for kind, names in self.capabilities.items()}))


@dataclass(frozen=True)
class InteropSpec:
    """一次解析的交互声明, 缺省不导出或依赖任何能力"""

    exports: CapabilityNames = field(default_factory=dict)
    requires: Mapping[str, Dependency] = field(default_factory=dict)
    replaces: Mapping[str, Mapping[CapabilityKind, Mapping[str, str]]] = field(default_factory=dict)
    conflicts: Mapping[str, CapabilityNames | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结所有嵌套声明, 使应用快照可安全复用该对象"""
        object.__setattr__(self, "exports", MappingProxyType({kind: tuple(names) for kind, names in self.exports.items()}))
        object.__setattr__(self, "requires", MappingProxyType(dict(self.requires)))
        object.__setattr__(self, "replaces", MappingProxyType({
            plugin: MappingProxyType({kind: MappingProxyType(dict(pairs)) for kind, pairs in entries.items()})
            for plugin, entries in self.replaces.items()
        }))
        object.__setattr__(self, "conflicts", MappingProxyType({
            plugin: None if entries is None else MappingProxyType({kind: tuple(names) for kind, names in entries.items()})
            for plugin, entries in self.conflicts.items()
        }))

    def __deepcopy__(self, memo: dict[int, object]) -> InteropSpec:
        """
        为宿主应用快照复用已完全冻结的声明

        参数:
        - memo: Python 深复制的对象复用表

        返回:
        - 当前不可变声明对象, 其余规格字段继续由宿主正常复制
        """
        memo[id(self)] = self
        return self

    def to_payload(self) -> dict[str, object]:
        """
        转为可展示和恢复核验的普通声明

        返回:
        - 不包含运行时对象的交互配置, 缺省空分组不重复输出
        """
        result: dict[str, object] = {}
        if self.exports:
            result["exports"] = {kind: list(names) for kind, names in self.exports.items()}
        if self.requires:
            result["requires"] = {
                plugin: {**({"optional": True} if entry.optional else {}),
                         **{kind: list(names) for kind, names in entry.capabilities.items()}}
                for plugin, entry in self.requires.items()
            }
        if self.replaces:
            result["replaces"] = {plugin: {kind: dict(pairs) for kind, pairs in entries.items()}
                                  for plugin, entries in self.replaces.items()}
        if self.conflicts:
            result["conflicts"] = {plugin: True if entries is None else {kind: list(names) for kind, names in entries.items()}
                                   for plugin, entries in self.conflicts.items()}
        return result


@dataclass(frozen=True)
class PluginInfo:
    """消费者可查询的插件公开资料"""

    name: str
    version: str
    description: str
    enabled: bool = True


@dataclass(frozen=True)
class HandlerInfo:
    """处理器公开资料, 不包含回调或可变实例"""

    name: str
    priority: int
    stages: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class SkillInfo:
    """技能公开资料, 不包含会话或技能管理器"""

    name: str
    description: str
    content: str


def _object(value: object, path: str) -> Mapping[str, object]:
    """
    严格读取配置对象

    参数:
    - value: 待校验值
    - path: 错误定位字段

    返回:
    - 字符串键对象; 格式不符时抛出 ValueError
    """
    if not isinstance(value, Mapping) or any(not isinstance(key, str) or not key.strip() or key != key.strip() for key in value):
        raise ValueError(f"{path} 必须是非空字符串键对象")
    return cast(Mapping[str, object], value)


def _capabilities(value: object, path: str) -> CapabilityNames:
    """
    读取六类能力的显式名称列表

    参数:
    - value: 原始类别映射
    - path: 错误定位字段

    返回:
    - 不可变类别映射; 未知类别, 空名称或重复名称时抛出 ValueError
    """
    result: dict[CapabilityKind, tuple[str, ...]] = {}
    for kind, names in _object(value, path).items():
        if kind not in CAPABILITY_KINDS:
            raise ValueError(f"{path} 包含未知能力类别: {kind}")
        if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() or name != name.strip() or name in {"*", "all"} for name in names):
            raise ValueError(f"{path}.{kind} 必须是显式能力名称列表")
        entries = cast(list[str], names)
        if len(set(entries)) != len(entries):
            raise ValueError(f"{path}.{kind} 包含重复能力名称")
        result[cast(CapabilityKind, kind)] = tuple(entries)
    return MappingProxyType(result)


def parse_interop(meta: Mapping[str, object]) -> InteropSpec:
    """
    严格解析交互声明并拒绝自引用与直接矛盾

    参数:
    - meta: 插件元数据, 不含 interop 时使用空声明

    返回:
    - 不可变交互声明; 配置错误抛出 ValueError 供安装边界处理
    """
    raw = _object(meta.get("interop", {}), "interop")
    if set(raw) - {"exports", "requires", "replaces", "conflicts"}:
        raise ValueError("interop 包含未知分组")
    owner = meta.get("name")
    exports = _capabilities(raw.get("exports", {}), "interop.exports")
    requires: dict[str, Dependency] = {}
    for plugin, entry in _object(raw.get("requires", {}), "interop.requires").items():
        config = {} if entry is True else dict(_object(entry, f"interop.requires.{plugin}"))
        optional = config.pop("optional", False)
        if type(optional) is not bool:
            raise ValueError(f"interop.requires.{plugin}.optional 必须是布尔值")
        requires[plugin] = Dependency(optional, _capabilities(config, f"interop.requires.{plugin}"))
    conflicts: dict[str, CapabilityNames | None] = {}
    for plugin, entry in _object(raw.get("conflicts", {}), "interop.conflicts").items():
        conflicts[plugin] = None if entry is True else _capabilities(entry, f"interop.conflicts.{plugin}")
    replaces: dict[str, Mapping[CapabilityKind, Mapping[str, str]]] = {}
    for plugin, entry in _object(raw.get("replaces", {}), "interop.replaces").items():
        mappings: dict[CapabilityKind, Mapping[str, str]] = {}
        for kind, pairs in _object(entry, f"interop.replaces.{plugin}").items():
            if kind not in CAPABILITY_KINDS:
                raise ValueError(f"interop.replaces.{plugin} 包含未知能力类别: {kind}")
            values = _object(pairs, f"interop.replaces.{plugin}.{kind}")
            if any(not isinstance(name, str) or not name.strip() or name != name.strip() or name in {"*", "all"} for name in values.values()) or any(name in {"*", "all"} for name in values):
                raise ValueError(f"interop.replaces.{plugin}.{kind} 包含无效名称")
            mappings[cast(CapabilityKind, kind)] = MappingProxyType(cast(dict[str, str], dict(values)))
        replaces[plugin] = MappingProxyType(mappings)
    if owner in set(requires) | set(conflicts) | set(replaces):
        raise ValueError("插件交互关系不能指向自身")
    for plugin, dependency in requires.items():
        if dependency.optional or plugin not in conflicts:
            continue
        conflict = conflicts[plugin]
        if conflict is None or any(set(names).intersection(conflict.get(kind, ())) for kind, names in dependency.capabilities.items()):
            raise ValueError(f"requires 与 conflicts 自相矛盾: {plugin}")
    return InteropSpec(exports, MappingProxyType(requires), MappingProxyType(replaces), MappingProxyType(conflicts))


def copy_data(value: object, *, _active: set[int] | None = None) -> ServiceValue:
    """
    复制普通业务数据并拒绝循环引用与运行时对象

    参数:
    - value: 服务输入或输出数据
    - _active: 当前递归容器标识, 仅供内部循环检查

    返回:
    - 不与原容器共享状态的 JSON 兼容数据; 非法数据抛出 TypeError
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, ImageArtifact):
        value.read()
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    active = _active if _active is not None else set()
    if id(value) in active:
        raise TypeError("服务数据不能包含循环引用")
    active.add(id(value))
    try:
        if isinstance(value, list):
            return [copy_data(item, _active=active) for item in value]
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            return {key: copy_data(item, _active=active) for key, item in cast(dict[str, object], value).items()}
        raise TypeError("服务只接受普通 JSON 数据或宿主支持的产物引用")
    finally:
        active.remove(id(value))
