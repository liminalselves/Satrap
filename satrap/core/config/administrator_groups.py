"""
系统管理员身份与管理组范围

以平台持久实例和真实成员 ID 隔离身份, 合并管理组的插件范围;
运行时使用不可变快照, 原有插件名单与功能开关由调用方继续校验
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast
import hashlib
import json
import re
import threading

from satrap.core.call_context import CallOrigin
from satrap.core.config.platform_identity import platform_instance_id


_GROUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _text(value: object, label: str, limit: int = 256) -> str:
    """
    校验身份字段, 保留平台自身的大小写语义

    参数:
    - value: 原始字段
    - label: 错误中的字段名称
    - limit: 最大字符数

    返回:
    - 去除首尾空白的非空字符串, 非法时抛出 ValueError
    """
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} 必须是非空文字, 长度不超过 {limit}")
    return value.strip()


def _names(value: object, label: str) -> list[str]:
    """
    校验不含重复项的插件名称列表

    参数:
    - value: 原始列表
    - label: 字段名称

    返回:
    - 规范名称列表, 非法时抛出 ValueError
    """
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError(f"{label} 必须是列表且不超过 1000 项")
    result = [_text(item, label, 128) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{label} 包含重复插件")
    return result


def normalize_administrator_groups(
    raw: object, platforms: Sequence[Mapping[str, Any]], *, bind_new: bool = False,
    previous: Sequence[Mapping[str, Any]] = (),
    normalize_user: Callable[[Mapping[str, Any], str], str] | None = None,
) -> list[dict[str, Any]]:
    """
    校验管理组并为新成员绑定服务端平台代次

    参数:
    - raw: 配置正文, 缺失时使用空列表
    - platforms: 当前真实平台注册配置
    - bind_new: 管理接口保存时绑定新成员, 不信任提交的实例代次
    - previous: 已保存管理组, 保留旧身份绑定而不自动重新授权
    - normalize_user: 保存新身份时使用适配器的用户 ID 规范接口

    返回:
    - 规范管理组, 无效结构抛出 ValueError; 已保存的失效平台身份保留
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 256:
        raise ValueError("administrator_groups 必须是列表且不超过 256 组")
    configured = {item["id"]: item for item in platforms if isinstance(item, Mapping)
                  and isinstance(item.get("id"), str) and item["id"]}
    # 无效平台由平台初始化边界记录并隔离, 不阻止其他实例与空管理员配置启动
    old_members = {(group["id"], member["platform_id"], member["user_id"]): member["platform_instance_id"]
                   for group in previous for member in group.get("members", [])}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, dict) or set(value) - {"id", "name", "enabled", "members", "plugin_scope"}:
            raise ValueError("管理员组含有未知字段或结构错误")
        identity = _text(value.get("id"), "管理组 ID", 64)
        if not _GROUP_ID.fullmatch(identity) or identity in seen:
            raise ValueError("管理组 ID 格式无效或重复")
        seen.add(identity)
        name = _text(value.get("name"), "管理组名称", 128)
        enabled = value.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("管理组 enabled 必须是布尔值")
        scope = value.get("plugin_scope")
        if not isinstance(scope, dict) or set(scope) - {"mode", "included", "excluded"} or scope.get("mode") not in ("selected", "all"):
            raise ValueError("管理组插件范围无效")
        included = _names(scope.get("included", []), "适用插件")
        excluded = _names(scope.get("excluded", []), "排除插件")
        if scope["mode"] == "all" and included:
            raise ValueError("所有插件模式不能携带指定插件列表")
        members = value.get("members", [])
        if not isinstance(members, list) or len(members) > 1000:
            raise ValueError("管理员成员必须是列表且不超过 1000 项")
        normalized: list[dict[str, str]] = []
        member_keys: set[tuple[str, str]] = set()
        for member in members:
            if not isinstance(member, dict) or set(member) - {"platform_id", "platform_instance_id", "user_id"}:
                raise ValueError("管理员成员字段无效")
            platform_id = _text(member.get("platform_id"), "平台 ID", 128)
            user_id = _text(member.get("user_id"), "平台用户识别号")
            platform = configured.get(platform_id)
            if bind_new and platform is not None and normalize_user is not None:
                user_id = _text(normalize_user(platform, user_id), "平台用户识别号")
            key = (platform_id, user_id)
            if key in member_keys:
                raise ValueError("同一管理组包含重复平台成员")
            member_keys.add(key)
            stored = old_members.get((identity, platform_id, user_id))
            if bind_new:
                if stored is not None:
                    instance = stored   # 保留旧绑定, 同名平台重建不会自动恢复授权
                elif platform is not None:
                    instance = platform_instance_id(platform)
                else:
                    raise ValueError("新增管理员成员的平台不存在")
            else:
                instance = member.get("platform_instance_id")
                if instance is None and platform is not None:
                    instance = platform_instance_id(platform)   # 初次导入手写配置时绑定当前实例
            instance = _text(instance, "平台实例绑定", 128)
            normalized.append({"platform_id": platform_id, "platform_instance_id": instance, "user_id": user_id})
        result.append({"id": identity, "name": name, "enabled": enabled, "members": normalized,
                       "plugin_scope": {"mode": scope["mode"], "included": included, "excluded": excluded}})
    return result


def administrator_revision(groups: Sequence[Mapping[str, Any]]) -> str:
    """
    计算仅属于管理员区段的修订

    参数:
    - groups: 规范管理组

    返回:
    - 不包含明文身份的稳定摘要
    """
    return hashlib.sha256(json.dumps(list(groups), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AdministratorGrant:
    """当前身份对某个插件的有效管理组授权"""

    allowed: bool
    group_ids: tuple[str, ...] = ()
    excluded_by: tuple[str, ...] = ()
    revision: str = ""


def _freeze(value: Any) -> Any:
    """
    冻结已规范化的管理组结构

    参数:
    - value: 规范化后的配置值

    返回:
    - 字典转为只读映射, 列表转为元组, 其余原样
    """
    if isinstance(value, Mapping):
        entries = cast("Mapping[str, Any]", value)
        return MappingProxyType({str(key): _freeze(item) for key, item in entries.items()})
    if isinstance(value, (list, tuple)):
        items = cast("Sequence[Any]", value)
        return tuple(_freeze(item) for item in items)
    return value


def _frozen_groups(groups: list[dict[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """
    生成可安全共享的管理组只读视图

    参数:
    - groups: 已规范化的管理组

    返回:
    - 深只读的管理组元组, 读取时不需再复制或解析
    """
    return _freeze(groups)


class AdministratorService:
    """按真实平台目录与原子快照解析管理员授权"""

    def __init__(self, platforms: Callable[[], Sequence[Mapping[str, Any]]], groups: object = None) -> None:
        """
        初始化独立宿主授权服务

        参数:
        - platforms: 读取当前平台注册配置的回调
        - groups: 初始管理组, 未配置不授予管理员身份
        """
        self._platforms = platforms
        self._lock = threading.RLock()
        self._snapshot = "[]"
        self._revision = administrator_revision([])
        self._frozen: tuple[Mapping[str, Any], ...] = ()
        self.apply(groups)

    def apply(self, groups: object) -> str:
        """
        校验后原子替换管理员快照

        参数:
        - groups: 已保存管理组

        返回:
        - 新区段修订; 校验失败保留原快照并抛出 ValueError
        """
        normalized = normalize_administrator_groups(groups, self._platforms())
        encoded = json.dumps(normalized, ensure_ascii=False)
        revision = administrator_revision(normalized)
        frozen = _frozen_groups(normalized)
        with self._lock:
            self._snapshot, self._revision, self._frozen = encoded, revision, frozen
        return revision

    def snapshot(self) -> tuple[list[dict[str, Any]], str]:
        """
        读取隔离副本与修订

        返回:
        - 同一原子快照中的管理组副本和修订
        """
        with self._lock:
            return json.loads(self._snapshot), self._revision

    def _matching(self, platform_id: str, user_id: str, groups: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """
        核验启用组内的平台持久绑定与用户身份

        参数:
        - platform_id: 可信平台实例 ID
        - user_id: 当前成员 ID
        - groups: 单次授权读取的管理组快照

        返回:
        - 有效匹配组, 已移除或重建的平台不会匹配
        """
        platform = next((item for item in self._platforms() if isinstance(item, Mapping) and item.get("id") == platform_id), None)
        if platform is None:
            return []
        instance = platform_instance_id(platform)
        return [group for group in groups if group["enabled"] and any(
            member["platform_id"] == platform_id and member["platform_instance_id"] == instance and member["user_id"] == user_id
            for member in group["members"])]

    def resolve(self, origin: CallOrigin | None, plugin_name: str) -> AdministratorGrant:
        """
        以排除优先规则合并当前平台成员的插件授权

        参数:
        - origin: 宿主可信来源, 无身份或非平台用户不授予权限
        - plugin_name: 当前实际安装插件名称

        返回:
        - 当前授权及命中组, 不支持的身份返回拒绝
        """
        with self._lock:
            groups, revision = self._frozen, self._revision
        if origin is None or origin.actor_kind != "platform_user" or not origin.actor_id:
            return AdministratorGrant(False, revision=revision)
        matched = self._matching(origin.adapter_id, origin.actor_id, groups)
        excluded = tuple(sorted(group["id"] for group in matched if plugin_name in group["plugin_scope"]["excluded"]))
        allowed = tuple(sorted(group["id"] for group in matched if group["plugin_scope"]["mode"] == "all"
                               or plugin_name in group["plugin_scope"]["included"]))
        return AdministratorGrant(bool(allowed) and not excluded, allowed, excluded, revision)

    def protected_users(self, platform_id: str) -> list[str]:
        """
        读取有效系统管理员, 独立于插件范围提供账号保护

        参数:
        - platform_id: 当前真实平台注册实例

        返回:
        - 当前实例的受保护管理员 ID
        """
        with self._lock:
            groups = self._frozen
        candidates = {member["user_id"] for group in groups for member in group["members"] if member["platform_id"] == platform_id}
        return sorted(user_id for user_id in candidates if self._matching(platform_id, user_id, groups))
