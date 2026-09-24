"""跨重启 JSON 状态存储的清单原语: 结构校验, 降级顺序与隔离扫描

被手动唤醒状态存储与审批账本组合使用 (组合式小件, 不是基类): 本模块不拥有启动决策树, 记录解析,
容量与恢复语义, 也不缓存业务状态; 各存储的格式差异按声明传入:

- `expected_files` 的必需键与允许状态
- 额外键语义 (归一化时丢弃还是按原样保留, 含 `expected_files` 与 `degraded` 的嵌套额外键)
  与降级原因的严格程度
- 事务上下文 (FileLock 的持有方式与嵌套深度不变)

"先持久降级标记, 后隔离文件"的顺序在 `degrade_then_quarantine` 内固定: 标记写失败时不隔离任何文件,
调用方必须保留原文件, 否则下次启动会把损坏文件误判为空目录。隔离文件名生成复用
`persist.quarantine_file`, 清单写入的实现由调用方传入, 便于替换与注入失败。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
import json
import time

from satrap.core.log import logger
from satrap.core.storage.persist import quarantine_file


@dataclass(frozen=True)
class ManifestSpec:
    """
    一种存储清单的格式声明

    属性:
    - version: 清单版本, 读取时严格比对
    - expected_files: 必需键与该键允许的文件状态
    - keep_extra_keys: 写回时是否保留未声明的额外键, 含 expected_files 与 degraded 里的嵌套键
      (归一化视图始终只含声明键)
    - degraded_requires_reason: degraded.reason 是否必须为非空字符串 (False 时只要求是字符串)
    - normalize_degraded_at: 是否把 degraded.at 归一化为 float (False 时不解释该字段, 写回原值)
    """

    version: int
    expected_files: Mapping[str, frozenset[str]]
    keep_extra_keys: bool = False
    degraded_requires_reason: bool = True
    normalize_degraded_at: bool = True


@dataclass(frozen=True)
class DegradedMarker:
    """持久降级标记: 脱敏原因码, 记录时间与读取到的原始嵌套载荷"""

    reason: str
    at: float
    raw: dict[str, Any] | None = None
    """读取时的原始嵌套载荷; None 表示本轮新写入的标记, 写回时按声明字段重建"""


@dataclass(frozen=True)
class Manifest:
    """已校验的清单: 原始载荷与归一化视图, 业务额外键按 spec 决定是否保留"""

    spec: ManifestSpec
    raw: dict[str, Any]
    initialized_at: float
    updated_at: float
    expected: dict[str, str]
    degraded: DegradedMarker | None

    @property
    def version(self) -> int:
        """清单版本, 与 spec 声明一致"""
        return self.spec.version

    def payload(self) -> dict[str, Any]:
        """写回载荷: 原始载荷为底, 声明字段按归一化视图覆盖, 额外键只在 spec 允许时保留"""
        payload: dict[str, Any] = dict(self.raw) if self.spec.keep_extra_keys else {}
        payload["version"] = self.spec.version
        payload["initialized_at"] = self.initialized_at
        payload["updated_at"] = self.updated_at
        payload["expected_files"] = self._expected_payload()
        payload["degraded"] = self._degraded_payload()
        return payload

    def _expected_payload(self) -> dict[str, Any]:
        """声明键以归一化视图为准, 原始 expected_files 里的额外键按原样保留"""
        if not self.spec.keep_extra_keys:
            return dict(self.expected)
        original = self.raw.get("expected_files")
        if not isinstance(original, dict):
            return dict(self.expected)
        return {**cast(dict[str, Any], original), **self.expected}

    def _degraded_payload(self) -> dict[str, Any] | None:
        """读取到的标记按原始载荷写回 (不解释 at 时连取值与类型都不改写), 新标记按声明字段生成"""
        marker = self.degraded
        if marker is None:
            return None
        if marker.raw is not None and self.spec.keep_extra_keys:
            merged: dict[str, Any] = dict(marker.raw)
            merged["reason"] = marker.reason
            if self.spec.normalize_degraded_at:
                merged["at"] = marker.at
            return merged
        return {"reason": marker.reason, "at": marker.at}

    def with_expected(self, states: Mapping[str, str]) -> Manifest:
        """更新 expected_files 状态, 返回新清单 (调用方不得就地改动清单)"""
        return replace(self, expected={**self.expected, **states})

    def with_degraded(self, reason: str, at: float) -> Manifest:
        """写入降级标记并刷新更新时间, 返回新清单 (标记按声明字段重建, 读取到的嵌套额外键不再保留)"""
        return replace(self, degraded=DegradedMarker(reason=reason, at=at), updated_at=at)

    def with_updated_at(self, at: float) -> Manifest:
        """刷新更新时间, 返回新清单 (降级标记与文件状态保持不变)"""
        return replace(self, updated_at=at)


@dataclass(frozen=True)
class QuarantinedFile:
    """一次隔离的结果: 原路径与隔离后的路径"""

    original: Path
    target: Path


@dataclass(frozen=True)
class DegradeOutcome:
    """一次降级处理的结果: 标记是否落盘, 标记后的清单与已隔离文件"""

    persisted: bool
    manifest: Manifest
    quarantined: tuple[QuarantinedFile, ...] = ()


def validate_manifest(raw: object, spec: ManifestSpec) -> Manifest:
    """
    校验清单的基础结构与版本

    参数:
    - raw: 清单 JSON 解析结果
    - spec: 该存储的格式声明

    返回:
    - Manifest: 归一化后的清单, 额外键保留在 raw 中

    异常:
    - ValueError: 版本, expected_files 状态, 时间字段或降级字段非法
    """
    if not isinstance(raw, dict):
        raise ValueError("清单必须是对象")
    data = cast(dict[str, Any], raw)
    if data.get("version") != spec.version:
        raise ValueError("清单版本非法")
    expected_raw = data.get("expected_files")
    if not isinstance(expected_raw, dict):
        raise ValueError("清单缺少 expected_files")
    expected_source = cast(dict[str, Any], expected_raw)
    expected: dict[str, str] = {}
    for key, states in spec.expected_files.items():
        state = expected_source.get(key)
        if not isinstance(state, str) or state not in states:
            raise ValueError("清单文件状态非法")
        expected[key] = state
    raw_times = (data.get("initialized_at"), data.get("updated_at"))
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in raw_times):
        raise ValueError("清单时间字段非法")
    degraded = _parse_degraded(data.get("degraded"), spec)
    return Manifest(
        spec=spec,
        raw=data,
        initialized_at=float(cast(int | float, raw_times[0])),
        updated_at=float(cast(int | float, raw_times[1])),
        expected=expected,
        degraded=degraded,
    )


def _parse_degraded(raw: object, spec: ManifestSpec) -> DegradedMarker | None:
    """解析降级标记: 原因严格程度与时间归一化按 spec 决定, 原始载荷留待写回时按原样保留"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("清单降级字段非法")
    data = cast(dict[str, Any], raw)
    reason = data.get("reason")
    if not isinstance(reason, str) or (spec.degraded_requires_reason and not reason):
        raise ValueError("清单降级原因非法")
    at = float(data.get("at") or 0.0) if spec.normalize_degraded_at else 0.0
    return DegradedMarker(reason=reason, at=at, raw=dict(data))


class DurabilityManifest:
    """
    清单文件的读取校验, 降级标记持久化与隔离

    清单写入由调用方以 persist 传入 (业务方持有事务与失败处理), 组件只编排"先标记后隔离"的顺序
    """

    def __init__(
        self,
        data_path: Path,
        spec: ManifestSpec,
        *,
        transaction: Callable[[], AbstractContextManager[object]],
        validate: Callable[[object], Manifest],
        log_prefix: str,
    ) -> None:
        """
        参数:
        - data_path: 业务数据文件路径, 清单路径与隔离文件扫描都从它派生
        - spec: 该存储的清单格式声明
        - transaction: 跨进程事务上下文 (业务方的 FileLock 持有方式)
        - validate: 清单校验入口, 业务方在此薄层里追加自己的交叉校验
        - log_prefix: 日志前缀, 便于区分两个存储
        """
        self._data_path = data_path
        self._manifest_path = data_path.with_suffix(".manifest.json")
        self._spec = spec
        self._transaction = transaction
        self._validate = validate
        self._log_prefix = log_prefix

    @property
    def manifest_path(self) -> Path:
        """清单文件路径, 由数据文件路径派生"""
        return self._manifest_path

    def read(self) -> Manifest | None:
        """
        读取并校验清单

        返回:
        - Manifest | None: 文件不存在时返回 None

        异常:
        - ValueError: 清单结构非法 (调用方按清单不可读处理)
        - OSError, json.JSONDecodeError: 文件读取失败
        """
        if not self._manifest_path.is_file():
            return None
        raw: object = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        return self._validate(raw)

    def validate(self, raw: object) -> Manifest:
        """
        用业务方的校验入口校验清单

        参数:
        - raw: 清单 JSON 解析结果

        返回:
        - Manifest: 归一化后的清单

        异常:
        - ValueError: 基础结构或业务交叉校验失败
        """
        return self._validate(raw)

    def fresh(self, *, initialized_at: float, expected: Mapping[str, str], updated_at: float | None = None) -> Manifest:
        """
        构造未降级的清单骨架

        参数:
        - initialized_at: 初始化时间, 恢复时由业务方沿用原值
        - expected: expected_files 各键的初始状态
        - updated_at: 更新时间, 缺省取当前时间

        返回:
        - Manifest: 未降级的清单
        """
        return Manifest(
            spec=self._spec,
            raw={},
            initialized_at=initialized_at,
            updated_at=time.time() if updated_at is None else updated_at,
            expected=dict(expected),
            degraded=None,
        )

    def mark_degraded(
        self,
        manifest: Manifest,
        *,
        reason: str,
        persist: Callable[[Manifest], None],
    ) -> DegradeOutcome:
        """
        持久记录降级标记, 不隔离文件也不丢弃记录

        参数:
        - manifest: 当前清单, 调用方已按业务状态更新 expected_files
        - reason: 脱敏原因码
        - persist: 清单写入实现 (调用方的方法), 组件在事务内调用

        返回:
        - DegradeOutcome: persisted 为假表示标记未落盘, 调用方不得隔离任何文件;
          返回的 manifest 始终带降级标记, 调用方据此保持内存降级
        """
        marked = manifest.with_degraded(reason, time.time())
        try:
            with self._transaction():
                persist(marked)
        except (OSError, TimeoutError) as error:
            self._log(f"降级标记写失败, 保留原文件不隔离: {type(error).__name__}: {error}")
            return DegradeOutcome(persisted=False, manifest=marked)
        return DegradeOutcome(persisted=True, manifest=marked)

    def degrade_then_quarantine(
        self,
        manifest: Manifest,
        *,
        reason: str,
        persist: Callable[[Manifest], None],
        bad_files: Sequence[Path] = (),
    ) -> DegradeOutcome:
        """
        先持久降级标记, 后隔离坏文件

        参数:
        - manifest: 当前清单, 调用方已按业务状态更新 expected_files
        - reason: 脱敏原因码
        - persist: 清单写入实现, 组件在事务内调用
        - bad_files: 需要隔离的数据文件; 标记写失败时全部保留

        返回:
        - DegradeOutcome: persisted 为假时未隔离任何文件
        """
        outcome = self.mark_degraded(manifest, reason=reason, persist=persist)
        if not outcome.persisted:
            return outcome
        quarantined: list[QuarantinedFile] = []
        with self._transaction():
            for path in bad_files:
                item = self.quarantine(path)
                if item is not None:
                    quarantined.append(item)
        return DegradeOutcome(persisted=True, manifest=outcome.manifest, quarantined=tuple(quarantined))

    def quarantine(self, path: Path) -> QuarantinedFile | None:
        """
        改名隔离单个损坏文件, 保留原始字节

        参数:
        - path: 待隔离文件

        返回:
        - QuarantinedFile | None: 隔离结果; 隔离失败时只记录日志并返回 None
        """
        try:
            target = quarantine_file(path)
        except OSError as error:
            self._log(f"损坏文件隔离失败 {path.name}: {type(error).__name__}: {error}")
            return None
        return QuarantinedFile(original=path, target=target)

    def quarantine_names(self) -> list[str]:
        """目录里已隔离的损坏文件名, 用于判断目录是否曾初始化"""
        return sorted(item.name for item in self._data_path.parent.glob(f"{self._data_path.name}.corrupt-*"))

    def _log(self, message: str) -> None:
        """按存储前缀记录日志"""
        logger.error(f"{self._log_prefix} {message}")
