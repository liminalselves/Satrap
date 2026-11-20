"""插件交互声明解析与服务数据复制。"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast
import math

from satrap.core.type.plugin_interop import (
    CAPABILITY_KINDS, CapabilityKind, CapabilityNames, Dependency, ImageArtifact, InteropSpec, ServiceValue,
)


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
