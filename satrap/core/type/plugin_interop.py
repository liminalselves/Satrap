"""
插件间交互声明与公共数据契约

严格解析六类能力的导出, 依赖, 替代和排斥关系;
普通数据在调用边界复制并校验, 不向消费者透传运行时对象
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypeAlias


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
    """执行时冻结的图片字节, 不在对象展示中暴露"""
    _valid: Callable[[], bool] = field(repr=False, compare=False)
    """检查来源执行范围是否仍有效的回调, 失效后拒绝读取图片"""

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
    """是否允许提供者缺失, 默认 False 表示依赖是必需的"""
    capabilities: CapabilityNames = field(default_factory=dict)
    """按类别列出的提供者能力名称, 空映射表示只依赖插件本身"""

    def __post_init__(self) -> None:
        """冻结调用者传入的类别映射, 不共享可变容器"""
        object.__setattr__(self, "capabilities", MappingProxyType({kind: tuple(names) for kind, names in self.capabilities.items()}))


@dataclass(frozen=True)
class InteropSpec:
    """一次解析的交互声明, 缺省不导出或依赖任何能力"""

    exports: CapabilityNames = field(default_factory=dict)
    """按类别列出向其他插件公开的能力, 空映射表示不导出"""
    requires: Mapping[str, Dependency] = field(default_factory=dict)
    """提供者插件名到依赖声明的映射, 空映射表示没有依赖"""
    replaces: Mapping[str, Mapping[CapabilityKind, Mapping[str, str]]] = field(default_factory=dict)
    """按被替代插件和能力类别将目标能力名映射到本插件实现名, 空映射表示没有替代"""
    conflicts: Mapping[str, CapabilityNames | None] = field(default_factory=dict)
    """冲突插件及其能力范围, None 表示与整个插件冲突, 空映射表示没有冲突"""

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
    """宿主登记的插件名称, 供消费者识别提供者"""
    version: str
    """提供者公开的插件版本, 源元数据未声明时可为空串"""
    description: str
    """提供者公开的用途说明, 源元数据未声明时可为空串"""
    enabled: bool = True
    """插件当前是否启用, 默认 True"""


@dataclass(frozen=True)
class HandlerInfo:
    """处理器公开资料, 不包含回调或可变实例"""

    name: str
    """宿主登记的处理器名称, 供消费者查询能力"""
    priority: int
    """处理器注册时的执行优先级, 用于处理顺序"""
    stages: tuple[str, ...]
    """处理器已提供回调的执行阶段, 空元组表示没有阶段回调"""
    description: str = ""
    """供消费者展示的处理器用途, 默认空串"""


@dataclass(frozen=True)
class SkillInfo:
    """技能公开资料, 不包含会话或技能管理器"""

    name: str
    """宿主登记的技能名称, 供消费者查询"""
    description: str
    """技能的公开用途说明, 来源于技能定义"""
    content: str
    """技能定义中的指令正文, 不包含会话或管理器对象"""
