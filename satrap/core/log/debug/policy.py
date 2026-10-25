"""
高级诊断的整体默认与稳定对象覆盖

复用配置版本和原子写入, 每次受理检查文件签名, 不按每条执行事件重复解析设置
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
import json
import os
import threading
from typing import TypedDict, cast

from satrap.core.config.document import ConfigRevisionConflict, config_document_revision
from satrap.core.config_paths import get_config_path
from satrap.core.storage.file_lock import FileLock
from satrap.core.storage.persist import atomic_write_text
from .contracts import DebugMode, DebugObject, ObjectPayload


class OverridePayload(TypedDict):
    """持久对象覆盖, inherit 不占用覆盖条目"""
    object: ObjectPayload
    mode: str


class PolicyPayload(TypedDict):
    """整体默认, 原始字节预算与对象覆盖"""
    version: int
    default_enabled: bool
    retention_days: int
    max_total_mb: int
    max_body_mb: int
    max_request_mb: int
    objects: list[OverridePayload]


@dataclass(frozen=True)
class DebugSelection:
    """请求受理时冻结的实际策略"""
    object: DebugObject
    mode: DebugMode
    enabled: bool
    revision: str
    retention_days: int
    max_total_mb: int
    max_body_mb: int
    max_request_mb: int


@dataclass(frozen=True)
class DebugPolicy:
    """不可变设置, 明确区分继承和强制开关"""
    default_enabled: bool = False
    retention_days: int = 7
    max_total_mb: int = 512
    max_body_mb: int = 8
    max_request_mb: int = 32
    objects: tuple[tuple[DebugObject, DebugMode], ...] = ()

    @cached_property
    def _overrides(self) -> dict[DebugObject, DebugMode]:
        """
        缓存不可变配置的对象索引

        返回:
        - 每次配置修订仅构建一次的查找表
        """
        return dict(self.objects)

    @cached_property
    def revision(self) -> str:
        """
        复用不可变设置的内容修订

        返回:
        - 每份策略只计算一次的配置摘要
        """
        return config_document_revision(self.payload())

    def payload(self) -> PolicyPayload:
        """
        导出固定结构的设置文档

        返回:
        - 不含 revision 的可保存配置
        """
        return PolicyPayload(version=1, default_enabled=self.default_enabled, retention_days=self.retention_days,
                             max_total_mb=self.max_total_mb, max_body_mb=self.max_body_mb, max_request_mb=self.max_request_mb,
                             objects=[OverridePayload(object=obj.payload(), mode=mode) for obj, mode in sorted(self.objects, key=lambda item: item[0].key)])

    @classmethod
    def parse(cls, value: object) -> DebugPolicy:
        """
        严格校验已保存设置和 API 草稿

        参数:
        - value: 完整设置文档

        返回:
        - 不可变策略, 缺少或非法字段抛出 ValueError
        """
        if not isinstance(value, dict) or set(value) != set(cls().payload()) or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("高级 Debug 设置格式或版本无效")
        if type(value.get("default_enabled")) is not bool:
            raise ValueError("default_enabled 必须为布尔值")
        for name, maximum in (("retention_days", 3650), ("max_total_mb", 1_000_000), ("max_body_mb", 1024), ("max_request_mb", 4096)):
            if type(value.get(name)) is not int or not 1 <= value[name] <= maximum:
                raise ValueError(f"{name} 必须为 1 至 {maximum} 的整数")
        if value["max_body_mb"] > value["max_request_mb"]:
            raise ValueError("单份正文预算不能超过单请求预算")
        raw_objects: object = value["objects"]
        if not isinstance(raw_objects, list) or len(raw_objects) > 10000:
            raise ValueError("对象覆盖必须为不超过 10000 项的列表")
        entries: list[tuple[DebugObject, DebugMode]] = []
        seen: set[str] = set()
        for entry in raw_objects:
            if not isinstance(entry, dict) or set(entry) != {"object", "mode"} or entry.get("mode") not in ("on", "off"):
                raise ValueError("对象覆盖只能包含 object 和 on/off 模式")
            obj = DebugObject.parse(entry["object"])
            if obj.key in seen:
                raise ValueError("Debug 对象覆盖重复")
            seen.add(obj.key)
            entries.append((obj, cast(DebugMode, entry["mode"])))
        return cls(value["default_enabled"], value["retention_days"], value["max_total_mb"], value["max_body_mb"],
                   value["max_request_mb"], tuple(entries))

    def resolve(self, obj: DebugObject) -> DebugSelection:
        """
        为下一请求解析固定策略

        参数:
        - obj: 完整可信对象

        返回:
        - 明确包含设置来源和修订的请求快照
        """
        mode: DebugMode = self._overrides.get(obj, "inherit")
        return DebugSelection(obj, mode, self.default_enabled if mode == "inherit" else mode == "on",
                              self.revision, self.retention_days, self.max_total_mb,
                              self.max_body_mb, self.max_request_mb)


class DebugPolicyStore:
    """按文件签名缓存解析结果, 跨进程修改在下一次读取可见"""

    def __init__(self, path: Path | None = None) -> None:
        """
        初始化独立设置位置

        参数:
        - path: 可选隔离路径, 默认共享 debug_logging.json
        """
        self.path = (path or get_config_path("debug_logging.json")).resolve()
        self._lock = threading.RLock()
        self._signature: tuple[int, int, int, int] | None = None
        self._cached: DebugPolicy | None = None

    def read(self) -> DebugPolicy:
        """
        读取最新设置, 缺少文件时不开启高级采集

        返回:
        - 签名匹配时复用不可变策略, 损坏文件显式抛错
        """
        with self._lock:
            try:
                info = self.path.stat()
            except FileNotFoundError:
                if self._signature is not None or self._cached is None:
                    self._cached = DebugPolicy()
                self._signature = None
                return self._cached
            signature = (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)
            if signature == self._signature and self._cached is not None:
                return self._cached
            policy = DebugPolicy.parse(json.loads(self.path.read_text(encoding="utf-8")))
            self._signature, self._cached = signature, policy
            return policy

    def snapshot(self) -> dict[str, object]:
        """
        导出带修订的管理快照

        返回:
        - 完整设置与 expected_revision 所需修订
        """
        policy = self.read()
        return {"policy": policy.payload(), "revision": policy.revision}

    def save(self, policy: DebugPolicy, expected_revision: str) -> dict[str, object]:
        """
        在最新修订一致时原子保存配置

        参数:
        - policy: 已校验的完整目标策略
        - expected_revision: 调用方读取到的修订

        返回:
        - 保存结果, 冲突或磁盘错误向管理边界报告
        """
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("缺少高级 Debug 设置修订")
        policy = DebugPolicy.parse(policy.payload())
        with FileLock(self.path.with_name(f".{self.path.name}.lock"), timeout=5), self._lock:
            current = self.read()
            if current.revision != expected_revision:
                raise ConfigRevisionConflict("高级 Debug 设置已改变, 请刷新后重试")
            atomic_write_text(self.path, json.dumps(policy.payload(), ensure_ascii=False, indent=2)+"\n", newline="\n", durable=True)
            self._signature, self._cached = None, None
            return self.snapshot()

    def set_object(self, obj: DebugObject, mode: DebugMode, expected_revision: str) -> dict[str, object]:
        """
        修改对象覆盖, reset 不留下脱离默认的副本

        参数:
        - obj: 当前实际对象
        - mode: inherit, on 或 off
        - expected_revision: 修改前的设置修订

        返回:
        - 已保存管理快照
        """
        if mode not in {"inherit", "on", "off"}:
            raise ValueError("Debug 模式必须为 inherit, on 或 off")
        current = self.read()
        entries = tuple((target, value) for target, value in current.objects if target != obj)
        if mode != "inherit":
            entries += ((obj, mode),)
        return self.save(replace(current, objects=entries), expected_revision)

    def set_default(self, enabled: bool, expected_revision: str) -> dict[str, object]:
        """
        修改动态继承的整体默认值

        参数:
        - enabled: 整体默认是否开启
        - expected_revision: 修改前的设置修订

        返回:
        - 已保存管理快照
        """
        if type(enabled) is not bool:
            raise ValueError("整体默认必须为布尔值")
        return self.save(replace(self.read(), default_enabled=enabled), expected_revision)


_STORES: dict[tuple[str, str], DebugPolicyStore] = {}
_STORES_LOCK = threading.Lock()


def policy_store() -> DebugPolicyStore:
    """
    取得当前部署路径对应的进程内共享设置读取器

    返回:
    - 独立于 Session 生命周期的仓储
    """
    root = os.getenv("SATRAP_CONFIG_ROOT", "")
    key = root, os.getcwd() if root and not Path(root).is_absolute() else ""
    with _STORES_LOCK:
        if key not in _STORES:
            _STORES[key] = DebugPolicyStore(get_config_path("debug_logging.json"))
        return _STORES[key]
