"""手动唤醒请求与发送尝试的持久化状态存储 (FileLock + 原子替换 + 版本化清单)

记录语义:
- 请求记录按 (adapter_id, request_id) 幂等, status ∈ accepted/executing/sent/partial/failed/unknown
- 发送尝试按 turn_id 记录, 段状态 planned → submitted → sent/partial/failed/unknown (未尝试段 skipped);
  发送 I/O 之前先落盘计划, 每段 I/O 前推进 submitted, 确认后立即落盘该段结果
- 尝试带 purpose 区分业务输出与错误反馈; 旧记录缺 purpose 按"用途未知"处理, 不作为业务送达证据
- 请求终态由同一 request_id 的全部业务尝试归并, 不只取最后一次回执
- 重启时 accepted/executing 与未确认段 (planned/submitted) 分别降级为 unknown 与 skipped, 不自动重发
- 未决记录 (accepted/executing/unknown 与未终结尝试) 不因容量被淘汰; 已满时 settled 记录轮转归档一代 (.1),
  归档也满则拒绝新记录并告警

清单与降级语义:
- 同目录清单 manual_wake_store.manifest.json 记录初始化状态, 应存在的文件与持久降级原因
- 主文件, 归档与清单的读取, 查重, 修改和写入共用同一把存储锁; 任一写入点崩溃后只能恢复为完整旧/新状态或显式降级
- 主文件或归档损坏时先持久记录降级, 再隔离坏文件; 标记写失败则保留原文件并一直拒绝依赖去重的新请求
- 已初始化的存储缺失应有文件按损坏处理, 文件不存在不再无条件解释为空库
- 降级只能经 recover() 校验主文件与归档一致后解除, 重启或隔离文件都不等于恢复
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict, cast, Generator
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
import copy
import json
import threading
import time

from satrap.core.log import logger
from satrap.core.storage.persist import atomic_write_json, quarantine_file
from satrap.core.storage.file_lock import FileLock

STORE_VERSION = 1
MANIFEST_VERSION = 1
REQUEST_CAPACITY = 1024
"""当前文件中请求记录上限"""
ATTEMPT_CAPACITY = 2048
"""当前文件中发送尝试上限"""
ARCHIVE_REQUEST_CAPACITY = 2048
ARCHIVE_ATTEMPT_CAPACITY = 4096
RETENTION_SECONDS = 7 * 24 * 3600
"""已终结记录的保留秒数, 未决记录不按保留期清理"""

REQUEST_STATUSES = frozenset({"accepted", "executing", "sent", "partial", "failed", "unknown"})
ATTEMPT_STATUSES = frozenset({"submitted", "sent", "partial", "failed", "unknown"})
SEGMENT_STATUSES = frozenset({"planned", "submitted", "sent", "partial", "failed", "unknown", "skipped"})
"""段状态缺省即旧格式未确认; planned 表示尚未尝试, skipped 表示该段未尝试且不计入发送结果"""
CONFIRMED_SEGMENT_STATUS = "sent"
PENDING_SEGMENT_STATUSES = frozenset({"planned", "submitted"})
SETTLED_STATUSES = frozenset({"sent", "partial", "failed"})
"""可归档/可按保留期清理的确认终态; unknown 属于未决, 不静默淘汰"""

ATTEMPT_PURPOSES = frozenset({"business", "error_feedback", "unknown"})
BUSINESS_PURPOSE = "business"
LEGACY_PURPOSE = "unknown"
"""旧记录缺 purpose 时的用途标记: 既可能是业务输出也可能是错误反馈, 不能当作业务送达证据"""
RESTART_DETAILS = frozenset({"restart_unconfirmed", "stopped_unconfirmed"})
"""不可由后续确认精化的未确认原因: 重启或平台停止后的历史结论"""

MAIN_FILE_STATES = frozenset({"present", "missing"})
"""主文件应存在 (present) 或已被隔离 (missing, 仅出现在降级清单中)"""
ARCHIVE_FILE_STATES = frozenset({"absent", "present", "missing"})
"""归档尚未创建 (absent), 已创建 (present), 或应存在但丢失 (missing)"""


class RequestRecord(TypedDict):
    """手动唤醒请求的持久化状态"""

    request_id: str
    fingerprint: str
    adapter_id: str
    target: str
    operator: str
    status: str
    detail: str
    created_at: float
    updated_at: float


class SendAttemptRecord(TypedDict):
    """一次逻辑回复发送尝试的持久化状态"""

    turn_id: str
    adapter_id: str
    target: str
    request_id: str
    purpose: str
    segments: list[dict[str, Any]]
    status: str
    detail: str
    created_at: float
    updated_at: float


class ExpectedFiles(TypedDict):
    """清单记录的应存在文件: main 为 present/missing, archive 为 absent/present/missing"""

    main: str
    archive: str


class StoreManifest(TypedDict):
    """版本化存储清单, 记录初始化状态, 应存在的文件与持久降级原因"""

    version: int
    initialized_at: float
    updated_at: float
    expected_files: ExpectedFiles
    degraded: dict[str, Any] | None


class ManualWakeStoreError(RuntimeError):
    """存储不可用或容量耗尽, 调用方按 reason 映射拒绝语义"""

    def __init__(self, reason: Literal["degraded", "capacity", "io", "duplicate"], message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _validate_request(item: object, key: str, source: str) -> RequestRecord:
    """逐字段校验请求记录, 身份不一致或结构问题都视为文件损坏"""
    if not isinstance(item, dict):
        raise ValueError(f"{source} 请求记录必须是对象")
    raw = cast(dict[str, Any], item)
    strings = ("request_id", "fingerprint", "adapter_id", "target", "operator", "status", "detail")
    if any(not isinstance(raw.get(field), str) for field in strings):
        raise ValueError(f"{source} 请求记录字段类型非法")
    if not isinstance(raw.get("created_at"), (int, float)) or isinstance(raw.get("created_at"), bool):
        raise ValueError(f"{source} 请求记录时间字段非法")
    if not isinstance(raw.get("updated_at"), (int, float)) or isinstance(raw.get("updated_at"), bool):
        raise ValueError(f"{source} 请求记录时间字段非法")
    if raw["status"] not in REQUEST_STATUSES:
        raise ValueError(f"{source} 未知请求状态: {raw['status']}")
    record: RequestRecord = {
        "request_id": raw["request_id"], "fingerprint": raw["fingerprint"], "adapter_id": raw["adapter_id"],
        "target": raw["target"], "operator": raw["operator"], "status": raw["status"], "detail": raw["detail"],
        "created_at": float(raw["created_at"]), "updated_at": float(raw["updated_at"]),
    }
    if key != _request_key(record["adapter_id"], record["request_id"]) or not record["adapter_id"] or not record["request_id"]:
        raise ValueError(f"{source} 请求记录身份与键不一致")
    return record


def _segment_statuses(attempt: SendAttemptRecord) -> list[str]:
    """取尝试内各段状态, 缺省按 planned 之外的历史语义视为未确认"""
    statuses: list[str] = []
    for segment in attempt["segments"]:
        status = segment.get("status")
        statuses.append(status if isinstance(status, str) and status in SEGMENT_STATUSES else "unknown")
    return statuses


def derive_attempt_status(segments: list[str]) -> tuple[str, str]:
    """
    由段状态归并整次尝试的结论, 段证据优先于调用方声明

    参数:
    - segments: 各段状态 (planned/submitted/sent/partial/failed/unknown/skipped)

    返回:
    - tuple[str, str]: 尝试状态与脱敏原因; 存在未确认段时为 unknown, 不谎报已送达或明确失败
    """
    pending = [status for status in segments if status in PENDING_SEGMENT_STATUSES or status == "unknown"]
    if pending:
        # planned 段断定尚未尝试, submitted/unknown 段可能是已提交的副作用
        reason = "not_submitted" if all(status == "planned" for status in pending) else "in_flight_unconfirmed"
        return "unknown", reason
    confirmed = [status for status in segments if status == CONFIRMED_SEGMENT_STATUS]
    unfinished = [status for status in segments if status in {"failed", "partial"}]
    if not confirmed:
        return "failed", "no_confirmed_segment" if segments else "empty_message"
    if unfinished:
        return "partial", "confirmed_prefix"
    return "sent", "all_segments_confirmed"


def _check_attempt_consistency(record: SendAttemptRecord, source: str) -> None:
    """校验尝试终态与段证据一致, 说谎的终态视为文件损坏 (未终结状态允许任意段的中间态)"""
    segments = _segment_statuses(record)
    status = record["status"]
    if status == "sent":
        if not segments or any(item != CONFIRMED_SEGMENT_STATUS for item in segments):
            # skipped 也不是确认段: 预定输出未全部确认就不能写 sent
            raise ValueError(f"{source} 已确认发送尝试仍含未确认段")
        return
    if status == "partial":
        if not any(item == CONFIRMED_SEGMENT_STATUS for item in segments):
            raise ValueError(f"{source} 部分完成发送尝试缺少已确认段")
        return
    if status == "failed":
        if any(item in {CONFIRMED_SEGMENT_STATUS, "partial"} or item in PENDING_SEGMENT_STATUSES or item == "unknown" for item in segments):
            raise ValueError(f"{source} 失败发送尝试含未确认或已确认段")
        return


def _validate_attempt(item: object, key: str, source: str) -> SendAttemptRecord:
    """逐字段校验发送尝试记录, 身份, 段状态与聚合终态的一致性都视为损坏"""
    if not isinstance(item, dict):
        raise ValueError(f"{source} 发送尝试记录必须是对象")
    raw = cast(dict[str, Any], item)
    strings = ("turn_id", "adapter_id", "target", "request_id", "status", "detail")
    if any(not isinstance(raw.get(field), str) for field in strings):
        raise ValueError(f"{source} 发送尝试字段类型非法")
    if not isinstance(raw.get("created_at"), (int, float)) or isinstance(raw.get("created_at"), bool):
        raise ValueError(f"{source} 发送尝试时间字段非法")
    if not isinstance(raw.get("updated_at"), (int, float)) or isinstance(raw.get("updated_at"), bool):
        raise ValueError(f"{source} 发送尝试时间字段非法")
    if raw["status"] not in ATTEMPT_STATUSES:
        raise ValueError(f"{source} 未知发送尝试状态: {raw['status']}")
    purpose = raw.get("purpose")
    if purpose is None:
        # 旧记录缺用途字段: 内部标为用途未知, 不按默认值当作业务送达
        purpose = LEGACY_PURPOSE
    elif not isinstance(purpose, str) or purpose not in ATTEMPT_PURPOSES:
        raise ValueError(f"{source} 未知发送用途: {purpose!r}")
    segments = raw.get("segments")
    if not isinstance(segments, list) or any(not isinstance(entry, dict) for entry in cast(list[object], segments)):
        raise ValueError(f"{source} 发送尝试段记录非法")
    copied: list[dict[str, Any]] = []
    for entry in cast(list[object], segments):
        segment = dict(cast(dict[str, Any], entry))
        status = segment.get("status")
        if status is not None:
            if not isinstance(status, str) or status not in SEGMENT_STATUSES:
                raise ValueError(f"{source} 未知发送段状态: {status!r}")
        copied.append(segment)
    record: SendAttemptRecord = {
        "turn_id": raw["turn_id"], "adapter_id": raw["adapter_id"], "target": raw["target"],
        "request_id": raw["request_id"], "purpose": purpose, "segments": copied, "status": raw["status"],
        "detail": raw["detail"], "created_at": float(raw["created_at"]), "updated_at": float(raw["updated_at"]),
    }
    if key != record["turn_id"] or not record["turn_id"]:
        raise ValueError(f"{source} 发送尝试身份与键不一致")
    _check_attempt_consistency(record, source)
    return record



def _validate_manifest(raw: object) -> StoreManifest:
    """
    校验清单结构, 任何结构问题都视为清单损坏

    参数:
    - raw: 清单 JSON 解析结果

    返回:
    - StoreManifest: 归一化后的清单, 降级字段为非空时保留原因

    异常:
    - ValueError: 版本, 文件状态或初始化状态不一致
    """
    if not isinstance(raw, dict):
        raise ValueError("清单必须是对象")
    data = cast(dict[str, Any], raw)
    if data.get("version") != MANIFEST_VERSION:
        raise ValueError("清单版本非法")
    expected_raw = data.get("expected_files")
    if not isinstance(expected_raw, dict):
        raise ValueError("清单缺少 expected_files")
    expected = cast(dict[str, Any], expected_raw)
    main_state, archive_state = expected.get("main"), expected.get("archive")
    if main_state not in MAIN_FILE_STATES or archive_state not in ARCHIVE_FILE_STATES:
        raise ValueError("清单文件状态非法")
    raw_times = (data.get("initialized_at"), data.get("updated_at"))
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in raw_times):
        raise ValueError("清单时间字段非法")
    degraded_raw = data.get("degraded")
    degraded: dict[str, Any] | None = None
    if degraded_raw is not None:
        if not isinstance(degraded_raw, dict):
            raise ValueError("清单降级字段非法")
        reason = cast(dict[str, Any], degraded_raw).get("reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError("清单降级原因非法")
        degraded = {"reason": reason, "at": float(cast(dict[str, Any], degraded_raw).get("at") or 0.0)}
    if degraded is None and main_state != "present":
        # 未降级的清单必须指向存在的主文件, 否则属于自相矛盾的状态
        raise ValueError("清单声明主文件缺失但未降级")
    initialized_at = float(cast(int | float, raw_times[0]))
    updated_at = float(cast(int | float, raw_times[1]))
    return {
        "version": MANIFEST_VERSION,
        "initialized_at": initialized_at,
        "updated_at": updated_at,
        "expected_files": {"main": cast(str, main_state), "archive": cast(str, archive_state)},
        "degraded": degraded,
    }


def _copy_request(record: RequestRecord) -> RequestRecord:
    """复制记录, 避免调用方持有内部字典引用"""
    return cast(RequestRecord, dict(record))


def _copy_attempt(record: SendAttemptRecord) -> SendAttemptRecord:
    """深复制发送尝试, 供写入失败时回滚段状态"""
    return cast(SendAttemptRecord, copy.deepcopy(record))


def _request_key(adapter_id: str, request_id: str) -> str:
    """请求记录键, 归档与主文件共用同一身份规则"""
    return f"{adapter_id}\n{request_id}"


def _parse_payload(raw: object, source: str) -> tuple[dict[str, RequestRecord], dict[str, SendAttemptRecord]]:
    """解析存储文件, 结构或身份非法即抛 ValueError 由调用方隔离"""
    if not isinstance(raw, dict):
        raise ValueError(f"{source} 不是对象")
    root = cast(dict[str, Any], raw)
    version = root.get("version")
    if version is not None and version != STORE_VERSION:
        # 缺少版本字段的历史文件按当前版本校验, 通过后随迁移补齐
        raise ValueError(f"{source} 存储版本非法: {version!r}")
    requests_raw, attempts_raw = root.get("requests"), root.get("attempts")
    if not isinstance(requests_raw, dict) or not isinstance(attempts_raw, dict):
        raise ValueError(f"{source} 缺少 requests/attempts 段")
    requests = {
        str(key): _validate_request(item, str(key), source)
        for key, item in cast(dict[str, Any], requests_raw).items()
    }
    attempts = {
        str(key): _validate_attempt(item, str(key), source)
        for key, item in cast(dict[str, Any], attempts_raw).items()
    }
    return requests, attempts


def _check_record_overlap(
    primary: dict[str, Any],
    secondary: dict[str, Any],
    *,
    label: str,
    strict_duplicates: bool,
) -> None:
    """
    检查主文件与归档的重复键, 运行期按主文件优先, 恢复校验按不一致拒绝

    参数:
    - primary: 主文件记录
    - secondary: 归档记录
    - label: 冲突告警中的记录类别
    - strict_duplicates: True 时重复键抛 ValueError, False 时保留主文件记录并告警

    异常:
    - ValueError: strict_duplicates 为 True 且同一键在两侧同时出现
    """
    for key in secondary:
        if key not in primary:
            continue
        if strict_duplicates:
            raise ValueError(f"{label}在归档与主文件中重复: {key}")
        logger.warning(f"[ManualWakeStore] {label}键在主文件与归档同时存在, 保留主文件记录: {key}")


class ManualWakeStore:
    """手动请求与发送尝试的 JSON 持久化, 进程内缓存为权威, FileLock 互斥跨进程读改写"""

    def __init__(self, path: str | Path) -> None:
        """
        初始化存储, 构造不抛异常: 文件损坏改名隔离并进入持久降级状态

        参数:
        - path: 存储 JSON 路径 (数据目录下)
        """
        self._path = Path(path)
        self._lock_path = self._path.with_name(f".{self._path.name}.lock")
        self._archive_path = self._path.with_name(f"{self._path.name}.1")
        self._archive_lock_path = self._path.with_name(f".{self._path.name}.1.lock")
        self._manifest_path = self._path.with_suffix(".manifest.json")
        self._mutex = threading.RLock()
        self.degraded = False
        self.degraded_reason = ""
        self._requests: dict[str, RequestRecord] = {}
        self._attempts: dict[str, SendAttemptRecord] = {}
        self._archive_requests: dict[str, RequestRecord] | None = None
        self._archive_attempts: dict[str, SendAttemptRecord] | None = None
        self._archive_quarantined = False
        self._manifest: StoreManifest | None = None
        try:
            with self._transaction(archive=True):
                self._startup_locked()
        except Exception as error:
            logger.error(f"[ManualWakeStore] 初始化失败, 进入降级状态 path={self._path}: {type(error).__name__}: {error}")
            try:
                self._mark_degraded("init_failed", f"{type(error).__name__}: {error}")
            except Exception as mark_error:
                # 构造阶段不抛异常: 标记失败时仅在内存中保持降级
                self.degraded = True
                self.degraded_reason = "init_failed"
                logger.error(f"[ManualWakeStore] 降级标记失败: {type(mark_error).__name__}: {mark_error}")

    # ---------- 事务与清单 ----------

    @contextmanager
    def _transaction(self, *, archive: bool = False) -> Generator[None, None, None]:
        """
        主文件, 归档与清单共用的存储锁

        参数:
        - archive: 是否同时涉及时归档文件, 叠加归档锁以兼容旧写入者
        """
        with FileLock(self._lock_path):
            if not archive:
                yield
                return
            with FileLock(self._archive_lock_path):
                yield

    def _fresh_manifest(self, *, initialized_at: float, archive_state: str) -> StoreManifest:
        """构造未降级的清单骨架, 主文件按已初始化处理"""
        now = time.time()
        return {
            "version": MANIFEST_VERSION,
            "initialized_at": initialized_at,
            "updated_at": now,
            "expected_files": {"main": "present", "archive": archive_state},
            "degraded": None,
        }

    def _read_manifest(self) -> StoreManifest | None:
        """读取并校验清单, 文件不存在返回 None, 结构非法抛 ValueError"""
        if not self._manifest_path.is_file():
            return None
        raw: object = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        return _validate_manifest(raw)

    def _write_manifest(self, manifest: StoreManifest) -> None:
        """原子写入清单, 调用方必须已持有存储锁"""
        atomic_write_json(self._manifest_path, dict(manifest))

    def _mark_degraded(self, reason: str, detail: str) -> bool:
        """
        记录持久降级原因, 不隔离文件也不丢弃记录

        参数:
        - reason: 脱敏原因码
        - detail: 仅供日志的诊断说明

        返回:
        - bool: 降级标记已落盘为 True; 写失败时保留原文件, 调用方不得隔离
        """
        self.degraded = True
        self.degraded_reason = reason
        manifest = self._manifest or self._fresh_manifest(initialized_at=time.time(), archive_state="absent")
        manifest["expected_files"]["archive"] = self._archive_state()
        manifest["degraded"] = {"reason": reason, "at": time.time()}
        manifest["updated_at"] = time.time()
        persisted = True
        try:
            with self._transaction(archive=True):
                self._write_manifest(manifest)
            self._manifest = manifest
        except (OSError, TimeoutError) as error:
            persisted = False
            logger.error(f"[ManualWakeStore] 降级标记写失败, 保留原文件不隔离: {type(error).__name__}: {error}")
        logger.error(f"[ManualWakeStore] 状态存储降级 reason={reason}: {detail}")
        return persisted

    def _archive_state(self) -> str:
        """归档当前应处的状态: 文件存在为 present, 已被隔离为 missing, 其余为从未创建"""
        if self._archive_path.is_file():
            return "present"
        return "missing" if self._archive_quarantined else "absent"

    def _quarantine(self, path: Path) -> None:
        """改名隔离损坏文件, 隔离前必须已持久记录降级"""
        target = quarantine_file(path)
        if path == self._archive_path:
            self._archive_quarantined = True
        logger.error(f"[ManualWakeStore] 存储文件损坏, 已隔离为 {target.name}")

    def _degrade(self, reason: str, detail: str, bad_files: Sequence[Path]) -> None:
        """
        先持久记录降级, 再隔离坏文件

        参数:
        - reason: 脱敏原因码
        - detail: 仅供日志的诊断说明
        - bad_files: 需要隔离的损坏文件, 标记写失败时全部保留
        """
        if not self._mark_degraded(reason, detail):
            return
        with self._transaction(archive=True):
            for path in bad_files:
                try:
                    self._quarantine(path)
                except OSError as error:
                    logger.error(f"[ManualWakeStore] 损坏文件隔离失败 {path.name}: {type(error).__name__}: {error}")

    def _quarantine_names(self) -> list[str]:
        """目录中已隔离的损坏文件, 用于判定目录是否曾初始化"""
        return sorted(item.name for item in self._path.parent.glob(f"{self._path.name}.corrupt-*"))

    # ---------- 加载与启动清扫 ----------

    def _read_payload(self, path: Path) -> tuple[dict[str, RequestRecord], dict[str, SendAttemptRecord]]:
        if not path.is_file():
            return {}, {}
        with open(path, encoding="utf-8") as handle:
            return _parse_payload(json.load(handle), path.name)

    def _startup_locked(self) -> None:
        """按清单, 目录痕迹与文件内容决定初始化, 迁移或降级"""
        manifest: StoreManifest | None = None
        manifest_error = ""
        try:
            manifest = self._read_manifest()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            manifest_error = f"{type(error).__name__}: {error}"
        main_exists = self._path.is_file()
        if manifest is not None and manifest["degraded"] is not None:
            info = cast(dict[str, Any], manifest["degraded"])
            self._manifest = manifest
            self.degraded = True
            self.degraded_reason = str(info.get("reason") or "degraded")
            logger.error(f"[ManualWakeStore] 存储保持降级状态 reason={self.degraded_reason}, 需经 recover() 显式恢复")
            return
        if manifest is None:
            quarantined = self._quarantine_names()
            if main_exists or self._archive_path.is_file() or quarantined:
                # 目录已有存储痕迹时不得当作首次初始化
                reason = "manifest_unreadable" if manifest_error else "manifest_missing_with_data"
                self._adopt_existing(reason, manifest_error or f"已有文件: {', '.join(quarantined) or '主文件或归档'}")
                return
            self._initialize_fresh()
            return
        self._manifest = manifest
        self._load_with_manifest(main_exists)

    def _initialize_fresh(self) -> None:
        """全新目录: 建立空主文件与清单, 失败即降级"""
        manifest = self._fresh_manifest(initialized_at=time.time(), archive_state="absent")
        try:
            self._save_current_locked()
            self._write_manifest(manifest)
        except OSError as error:
            self._degrade("init_io_failed", f"{type(error).__name__}: {error}", [])
            return
        self._manifest = manifest
        logger.info(f"[ManualWakeStore] 已初始化全新状态存储 path={self._path.name}")

    def _adopt_existing(self, reason: str, detail: str) -> None:
        """
        清单缺失或损坏但目录已有数据: 全量校验后迁移, 否则显式降级

        参数:
        - reason: 降级原因码
        - detail: 诊断说明
        """
        if not self._path.is_file():
            self._degrade("main_missing", f"主文件缺失但存在存储痕迹: {detail}", [])
            return
        bad: list[Path] = []
        try:
            main_requests, main_attempts = self._read_payload(self._path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._degrade(reason, f"主文件校验失败: {type(error).__name__}: {error}", [self._path])
            return
        archive_requests: dict[str, RequestRecord] = {}
        archive_attempts: dict[str, SendAttemptRecord] = {}
        if self._archive_path.is_file():
            try:
                archive_requests, archive_attempts = self._read_payload(self._archive_path)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self._degrade(reason, f"归档校验失败: {type(error).__name__}: {error}", [self._archive_path])
                return
        try:
            _check_record_overlap(main_requests, archive_requests, label="请求记录", strict_duplicates=False)
            _check_record_overlap(main_attempts, archive_attempts, label="发送尝试", strict_duplicates=False)
        except ValueError as error:
            self._degrade(reason, str(error), [])
            return
        self._requests, self._attempts = main_requests, main_attempts
        self._archive_requests, self._archive_attempts = archive_requests, archive_attempts
        manifest = self._fresh_manifest(
            initialized_at=time.time(),
            archive_state="present" if self._archive_path.is_file() else "absent",
        )
        try:
            self._write_manifest(manifest)
        except OSError as error:
            self._degrade(reason, f"清单写入失败: {type(error).__name__}: {error}", [])
            return
        self._manifest = manifest
        logger.info(
            f"[ManualWakeStore] 已迁移旧版存储 path={self._path.name} "
            f"requests={len(self._requests)} attempts={len(self._attempts)} archive={manifest['expected_files']['archive']}"
        )

    def _load_with_manifest(self, main_exists: bool) -> None:
        """清单有效时校验应存在文件与内容, 缺失或损坏按降级处理"""
        manifest = self._manifest
        if manifest is None:
            raise RuntimeError("清单未加载")
        expected = manifest["expected_files"]
        if expected["main"] == "present" and not main_exists:
            self._degrade("main_missing", "已初始化存储的主文件缺失", [])
            return
        if expected["archive"] == "present" and not self._archive_path.is_file():
            self._degrade("archive_missing", "已初始化存储的归档文件缺失", [])
            return
        try:
            self._requests, self._attempts = self._read_payload(self._path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._degrade("main_corrupt", f"{type(error).__name__}: {error}", [self._path])
            return
        if expected["archive"] == "absent" and self._archive_path.is_file():
            # 轮转在清单更新前中断: 归档已存在则按现有内容采纳
            logger.warning("[ManualWakeStore] 归档在清单登记前出现, 按未完成的轮转采纳")
        if self._archive_path.is_file():
            try:
                self._load_archive_locked()
            except ManualWakeStoreError:
                # 归档读取失败已在 _load_archive_locked 内持久记录降级并隔离坏文件
                return
        if self._sweep_restart_residue():
            try:
                self._save_current_locked()
            except OSError as error:
                self._degrade("restart_sweep_failed", f"{type(error).__name__}: {error}", [])
                return
        archive_state = self._archive_state()
        if archive_state != expected["archive"] and archive_state != "missing":
            manifest["expected_files"]["archive"] = archive_state
            manifest["updated_at"] = time.time()
            try:
                self._write_manifest(manifest)
            except OSError as error:
                logger.warning(f"[ManualWakeStore] 清单归档状态更新失败: {type(error).__name__}: {error}")

    def _sweep_restart_residue(self) -> bool:
        """重启后无法确认的记录标 unknown, 不自动重发; 已确认段保持原状, 未尝试段标 skipped"""
        now = time.time()
        changed = False
        for record in self._requests.values():
            if record["status"] in {"accepted", "executing"}:
                record["status"] = "unknown"
                record["detail"] = "restart_unconfirmed"
                record["updated_at"] = now
                changed = True
        for attempt in self._attempts.values():
            if attempt["status"] != "submitted":
                continue
            for segment in attempt["segments"]:
                if segment.get("status") == "submitted":
                    segment["status"] = "unknown"
                elif segment.get("status") == "planned":
                    segment["status"] = "skipped"
            # 段证据归并整次尝试: 已确认段保留, 未确认段变 unknown
            attempt["status"], reason = derive_attempt_status(_segment_statuses(attempt))
            attempt["detail"] = "restart_unconfirmed" if attempt["status"] == "unknown" else reason
            attempt["updated_at"] = now
            changed = True
        return changed

    def recover(self) -> bool:
        """
        校验主文件与归档一致后解除持久降级, 不提供清空历史后继续

        返回:
        - bool: 主文件与归档均完整一致时为 True 并解除降级; 缺失, 结构非法, 身份冲突, 重复或状态不一致时为 False
        """
        with self._mutex, self._transaction(archive=True):
            if not self._path.is_file():
                logger.error("[ManualWakeStore] 恢复失败: 主文件不存在, 需先恢复原文件")
                return False
            try:
                requests, attempts = self._read_payload(self._path)
                archive_requests: dict[str, RequestRecord] = {}
                archive_attempts: dict[str, SendAttemptRecord] = {}
                if self._archive_path.is_file():
                    archive_requests, archive_attempts = self._read_payload(self._archive_path)
                _check_record_overlap(requests, archive_requests, label="请求记录", strict_duplicates=True)
                _check_record_overlap(attempts, archive_attempts, label="发送尝试", strict_duplicates=True)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                logger.error(f"[ManualWakeStore] 恢复校验失败, 保持降级: {type(error).__name__}: {error}")
                return False
            manifest = self._fresh_manifest(
                initialized_at=self._manifest["initialized_at"] if self._manifest else time.time(),
                archive_state="present" if self._archive_path.is_file() else "absent",
            )
            try:
                self._write_manifest(manifest)
            except OSError as error:
                logger.error(f"[ManualWakeStore] 恢复写入清单失败, 保持降级: {type(error).__name__}: {error}")
                return False
            self._requests, self._attempts = requests, attempts
            self._archive_requests, self._archive_attempts = archive_requests, archive_attempts
            self._manifest = manifest
            self.degraded = False
            self.degraded_reason = ""
            logger.info(f"[ManualWakeStore] 状态存储已恢复 requests={len(requests)} attempts={len(attempts)}")
            return True

    # ---------- 落盘与归档 ----------

    def _save_current_locked(self) -> None:
        """写入主文件, 调用方已持有存储锁, 此处再加锁依赖 FileLock 可重入"""
        with self._transaction():
            atomic_write_json(self._path, {"version": STORE_VERSION, "requests": self._requests, "attempts": self._attempts})

    def _load_archive_locked(self) -> tuple[dict[str, RequestRecord], dict[str, SendAttemptRecord]]:
        """读取归档; 文件不存在按空集合, 解析失败进入降级而不是按空归档继续"""
        if self._archive_requests is not None and self._archive_attempts is not None:
            return self._archive_requests, self._archive_attempts
        if not self._archive_path.is_file():
            # 缓存空集合保证轮转写入落到同一份数据, 文件是否曾创建由清单区分
            self._archive_requests, self._archive_attempts = {}, {}
            return self._archive_requests, self._archive_attempts
        try:
            archive_requests, archive_attempts = self._read_payload(self._archive_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._degrade("archive_corrupt", f"{type(error).__name__}: {error}", [self._archive_path])
            raise ManualWakeStoreError("degraded", "归档读取失败, 状态存储降级") from error
        self._archive_requests, self._archive_attempts = archive_requests, archive_attempts
        return archive_requests, archive_attempts

    def _save_archive_locked(self) -> None:
        """写入归档, 调用方已持有存储锁"""
        with self._transaction(archive=True):
            atomic_write_json(self._archive_path, {
                "version": STORE_VERSION,
                "requests": self._archive_requests or {},
                "attempts": self._archive_attempts or {},
            })
            self._mark_archive_present_locked()

    def _mark_archive_present_locked(self) -> None:
        """归档落盘成功后更新清单, 保证崩溃后能按归档采纳而不是判定归档丢失"""
        manifest = self._manifest
        if manifest is None or manifest["expected_files"]["archive"] == "present":
            return
        manifest["expected_files"]["archive"] = "present"
        manifest["updated_at"] = time.time()
        try:
            self._write_manifest(manifest)
        except OSError as error:
            logger.warning(f"[ManualWakeStore] 归档状态登记失败, 下次启动按未完成轮转采纳: {type(error).__name__}: {error}")

    def _sweep_expired(self, requests: dict[str, RequestRecord], attempts: dict[str, SendAttemptRecord], now: float) -> bool:
        """清理超过保留期的已终结记录, 未决记录 (accepted/executing/unknown/submitted) 不清理"""
        cutoff = now - RETENTION_SECONDS
        stale_requests = [key for key, record in requests.items() if record["status"] in SETTLED_STATUSES and record["updated_at"] < cutoff]
        stale_attempts = [key for key, record in attempts.items() if record["status"] in SETTLED_STATUSES and record["updated_at"] < cutoff]
        for key in stale_requests:
            del requests[key]
        for key in stale_attempts:
            del attempts[key]
        return bool(stale_requests or stale_attempts)

    def _ensure_insert_room(self, section: Literal["requests", "attempts"]) -> None:
        """容量耗尽时先按保留期清理, 再把已终结记录轮转归档一代; 归档也满或未决占满则拒绝"""
        records: dict[str, Any]
        capacity: int
        archive_capacity: int
        if section == "requests":
            records, capacity, archive_capacity = self._requests, REQUEST_CAPACITY, ARCHIVE_REQUEST_CAPACITY
        else:
            records, capacity, archive_capacity = self._attempts, ATTEMPT_CAPACITY, ARCHIVE_ATTEMPT_CAPACITY
        if len(records) < capacity:
            return
        now = time.time()
        archive_requests, archive_attempts = self._load_archive_locked()
        # 清理结果随随后的插入落盘一并持久化, 无需单独写当前文件
        self._sweep_expired(self._requests, self._attempts, now)
        archive_changed = self._sweep_expired(archive_requests, archive_attempts, now)
        if len(records) < capacity:
            if archive_changed:
                self._save_archive_locked()
            return
        archive_section: dict[str, Any] = archive_requests if section == "requests" else archive_attempts
        movable = sorted(
            (record for record in records.values() if record["status"] in SETTLED_STATUSES),
            key=lambda record: record["updated_at"],
        )
        for record in movable:
            if len(records) < capacity:
                break
            if len(archive_section) >= archive_capacity:
                logger.error(f"[ManualWakeStore] {section} 归档已满且当前文件容量耗尽, 拒绝新记录")
                raise ManualWakeStoreError("capacity", "存储与归档均已满, 拒绝新记录")
            key = _request_key(record["adapter_id"], record["request_id"]) if section == "requests" else record["turn_id"]
            archive_section[key] = records.pop(key)
            archive_changed = True
        if archive_changed:
            # 先写归档再写主文件: 任一写入点中断都只留下归档超集, 不会丢记录
            self._save_archive_locked()
        if len(records) >= capacity:
            logger.error(f"[ManualWakeStore] {section} 未决记录占满容量 ({capacity}), 拒绝新记录")
            raise ManualWakeStoreError("capacity", "未决记录占满容量, 拒绝新记录")

    # ---------- 请求记录 ----------

    def lookup_request(self, request_id: str, adapter_id: str | None = None) -> RequestRecord | None:
        """按 request_id 查询 (可选适配器范围), 当前文件优先, 归档兜底"""
        with self._mutex:
            if self.degraded:
                return None
            if adapter_id is not None:
                record = self._requests.get(_request_key(adapter_id, request_id))
                if record is not None:
                    return _copy_request(record)
            else:
                matches = [record for key, record in self._requests.items() if key.endswith(f"\n{request_id}")]
                if matches:
                    return _copy_request(max(matches, key=lambda record: record["updated_at"]))
            try:
                archive_requests, _ = self._load_archive_locked()
            except ManualWakeStoreError:
                return None
            if adapter_id is not None:
                record = archive_requests.get(_request_key(adapter_id, request_id))
                return _copy_request(record) if record is not None else None
            matches = [record for key, record in archive_requests.items() if key.endswith(f"\n{request_id}")]
            if not matches:
                return None
            return _copy_request(max(matches, key=lambda record: record["updated_at"]))

    def accept_request(self, adapter_id: str, request_id: str, fingerprint: str, target: str, operator: str) -> None:
        """持久化 accepted 占位, 成功落盘才返回; 降级/容量/IO 失败均抛错由调用方拒绝"""
        with self._mutex, self._transaction(archive=True):
            if self.degraded:
                raise ManualWakeStoreError("degraded", "存储降级中, 无法保证去重")
            key = _request_key(adapter_id, request_id)
            archive_requests, _ = self._load_archive_locked()
            if key in self._requests or key in archive_requests:
                raise ManualWakeStoreError("duplicate", "请求已存在")
            self._ensure_insert_room("requests")
            now = time.time()
            self._requests[key] = {
                "request_id": request_id, "fingerprint": fingerprint, "adapter_id": adapter_id,
                "target": target, "operator": operator, "status": "accepted", "detail": "",
                "created_at": now, "updated_at": now,
            }
            try:
                self._save_current_locked()
            except OSError as error:
                self._requests.pop(key, None)
                raise ManualWakeStoreError("io", f"存储落盘失败: {type(error).__name__}") from error

    def update_request(self, adapter_id: str, request_id: str, status: str, detail: str = "", *, refine: bool = False) -> bool:
        """
        推进请求状态, 已终结记录不再回退; 落盘失败回滚内存状态只告警不阻断业务

        参数:
        - adapter_id: 平台实例 ID
        - request_id: 逻辑请求标识
        - status: 目标状态
        - detail: 脱敏原因
        - refine: 允许把在途未确认 (in_flight/not_submitted 等) 的 unknown 精化为可信终态;
          重启或平台停止造成的 unknown 属于历史结论, 不因 refine 改写
        """
        with self._mutex, self._transaction():
            if self.degraded or status not in REQUEST_STATUSES:
                return False
            key = _request_key(adapter_id, request_id)
            record = self._requests.get(key)
            if record is None or record["status"] in SETTLED_STATUSES:
                return False
            if record["status"] == "unknown" and not (refine and record["detail"] not in RESTART_DETAILS):
                return False
            if record["status"] == "unknown" and status == "unknown" and record["detail"] == detail:
                return False
            previous = _copy_request(record)
            record["status"] = status
            record["detail"] = detail
            record["updated_at"] = time.time()
            try:
                self._save_current_locked()
            except OSError as error:
                self._restore_record(self._requests, key, previous)
                logger.error(f"[ManualWakeStore] 请求状态落盘失败 request_id={request_id}: {type(error).__name__}: {error}")
                return False
            return True

    @staticmethod
    def _restore_record(records: dict[str, Any], key: str, previous: Any) -> None:
        """写入失败时恢复内存快照, 不向查询展示未持久确认的终态"""
        records[key] = previous

    def adapter_stopped(self, adapter_id: str) -> None:
        """平台停止: 未开始的请求判 failed, 执行中与未回执发送判 unknown"""
        with self._mutex, self._transaction():
            if self.degraded:
                return
            now = time.time()
            rollback: list[tuple[dict[str, Any], str, Any]] = []
            for key, record in self._requests.items():
                if record["adapter_id"] != adapter_id:
                    continue
                if record["status"] == "accepted":
                    rollback.append((self._requests, key, _copy_request(record)))
                    record["status"], record["detail"], record["updated_at"] = "failed", "platform_stopped", now
                elif record["status"] == "executing":
                    rollback.append((self._requests, key, _copy_request(record)))
                    record["status"], record["detail"], record["updated_at"] = "unknown", "stopped_unconfirmed", now
            for key, attempt in self._attempts.items():
                if attempt["adapter_id"] == adapter_id and attempt["status"] == "submitted":
                    rollback.append((self._attempts, key, _copy_attempt(attempt)))
                    for segment in attempt["segments"]:
                        if segment.get("status") == "submitted":
                            segment["status"] = "unknown"
                        elif segment.get("status") == "planned":
                            segment["status"] = "skipped"
                    attempt["status"], reason = derive_attempt_status(_segment_statuses(attempt))
                    attempt["detail"] = "stopped_unconfirmed" if attempt["status"] == "unknown" else reason
                    attempt["updated_at"] = now
            if not rollback:
                return
            try:
                self._save_current_locked()
            except OSError as error:
                for records, key, previous in rollback:
                    self._restore_record(records, key, previous)
                logger.error(f"[ManualWakeStore] 平台停止状态落盘失败 adapter={adapter_id}: {type(error).__name__}: {error}")

    # ---------- 发送尝试记录 ----------

    def record_send_attempt(
        self,
        turn_id: str,
        adapter_id: str,
        target: str,
        request_id: str,
        segments: list[dict[str, Any]],
        purpose: str = BUSINESS_PURPOSE,
    ) -> bool:
        """
        发送 I/O 之前持久化计划 (段状态 planned, 尝试状态 submitted)

        参数:
        - turn_id: 本次尝试标识
        - adapter_id: 平台实例 ID
        - target: 目标会话
        - request_id: 同一逻辑请求标识, 请求终态按它归并
        - segments: 段计划 (index/kind/chars/digest)
        - purpose: business 业务输出或 error_feedback 错误反馈

        返回:
        - bool: 计划已落盘为 True; 降级, 容量不足或写入失败为 False
        """
        with self._mutex, self._transaction(archive=True):
            if self.degraded:
                return False
            if not segments or not turn_id:
                return False
            if purpose not in ATTEMPT_PURPOSES:
                raise ValueError("发送用途必须为 business 或 error_feedback")
            try:
                self._ensure_insert_room("attempts")
            except ManualWakeStoreError as error:
                logger.error(f"[ManualWakeStore] 发送尝试容量不足 turn={turn_id}: {error}")
                return False
            now = time.time()
            planned: list[dict[str, Any]] = [{**dict(segment), "status": "planned"} for segment in segments]
            self._attempts[turn_id] = {
                "turn_id": turn_id, "adapter_id": adapter_id, "target": target, "request_id": request_id,
                "purpose": purpose, "segments": planned, "status": "submitted", "detail": "",
                "created_at": now, "updated_at": now,
            }
            try:
                self._save_current_locked()
            except OSError as error:
                self._attempts.pop(turn_id, None)
                logger.error(f"[ManualWakeStore] 发送尝试落盘失败 turn={turn_id}: {type(error).__name__}: {error}")
                return False
            return True

    def mark_segment_submitted(self, turn_id: str, index: int) -> bool:
        """
        段 I/O 之前把该段从 planned 推进 submitted, 未尝试段保持 planned

        参数:
        - turn_id: 本次尝试标识
        - index: 段序号

        返回:
        - bool: 已落盘为 True; 未终结尝试之外的状态或写入失败为 False
        """
        with self._mutex, self._transaction():
            if self.degraded:
                return False
            attempt = self._attempts.get(turn_id)
            if attempt is None or attempt["status"] != "submitted":
                return False
            segment = self._segment_at(attempt, index)
            if segment is None or segment.get("status") != "planned":
                return False
            previous = _copy_attempt(attempt)
            segment["status"] = "submitted"
            attempt["updated_at"] = time.time()
            if not self._persist_attempt(turn_id, previous, "段提交状态"):
                return False
            return True

    def record_segment_result(self, turn_id: str, index: int, status: str, *, advance_to: int | None = None) -> bool:
        """
        段结果确认后立即落盘, 可同时把下一段推进 submitted (单次写入)

        参数:
        - turn_id: 本次尝试标识
        - index: 段序号
        - status: sent/partial/failed/unknown
        - advance_to: 下一段序号, None 表示不再有后续段

        返回:
        - bool: 已落盘为 True; 状态非法或写入失败为 False
        """
        if status not in {CONFIRMED_SEGMENT_STATUS, "partial", "failed", "unknown"}:
            raise ValueError("段落定状态必须为 sent/partial/failed/unknown")
        with self._mutex, self._transaction():
            if self.degraded:
                return False
            attempt = self._attempts.get(turn_id)
            if attempt is None or attempt["status"] != "submitted":
                return False
            segment = self._segment_at(attempt, index)
            if segment is None or segment.get("status") not in PENDING_SEGMENT_STATUSES:
                return False
            previous = _copy_attempt(attempt)
            segment["status"] = status
            if advance_to is not None:
                following = self._segment_at(attempt, advance_to)
                if following is None or following.get("status") != "planned":
                    self._restore_record(self._attempts, turn_id, previous)
                    return False
                following["status"] = "submitted"
            attempt["updated_at"] = time.time()
            if not self._persist_attempt(turn_id, previous, "段结果"):
                return False
            return True

    def complete_send_attempt(
        self, turn_id: str, status: str, detail: str = "", untracked: Sequence[int] = (),
    ) -> bool:
        """
        整轮收尾: 未尝试段标 skipped, 按段证据落终态

        参数:
        - turn_id: 本次尝试标识
        - status: 调用方结论; unknown 表示存在未落盘的发送尝试, 只让结论更保守
        - detail: 脱敏原因
        - untracked: 已经发出但没有落盘证据的段序号, 这些段按无法确认处理

        返回:
        - bool: 已落盘为 True; 结论与段证据矛盾或写入失败为 False
        """
        if status not in ATTEMPT_STATUSES - {"submitted"}:
            raise ValueError("发送尝试终态必须为 sent/partial/failed/unknown")
        with self._mutex, self._transaction():
            if self.degraded:
                return False
            attempt = self._attempts.get(turn_id)
            if attempt is None or attempt["status"] != "submitted":
                return False
            previous = _copy_attempt(attempt)
            gaps = {index for index in untracked if isinstance(index, int)}
            for position, segment in enumerate(attempt["segments"]):
                if segment.get("status") == "planned":
                    # 未尝试段确定没有副作用; 已发出但证据丢失的段只能记无法确认
                    segment["status"] = "unknown" if position in gaps else "skipped"
                elif segment.get("status") == "submitted":
                    # I/O 已开始却没有回执, 收尾只能保守记为无法确认
                    segment["status"] = "unknown"
            derived, reason = derive_attempt_status(_segment_statuses(attempt))
            if status != derived and status != "unknown":
                # 段证据优先: 调用方的乐观结论不能覆盖未确认段
                logger.error(
                    f"[ManualWakeStore] 发送结论与段证据不符 turn={turn_id} 请求={status} 证据={derived}",
                )
                self._restore_record(self._attempts, turn_id, previous)
                return False
            # 调用方的 unknown 表示存在未落盘的发送尝试, 只能让结论更保守
            attempt["status"] = "unknown" if status == "unknown" else derived
            attempt["detail"] = detail or reason
            attempt["updated_at"] = time.time()
            return self._persist_attempt(turn_id, previous, "发送终态")

    def lookup_attempts(self, request_id: str, adapter_id: str | None = None) -> list[SendAttemptRecord]:
        """
        按 request_id 查询发送尝试副本, 最新在前

        参数:
        - request_id: 逻辑请求标识
        - adapter_id: 可选平台实例过滤

        返回:
        - list[SendAttemptRecord]: 匹配的尝试记录副本
        """
        with self._mutex:
            matched = [
                _copy_attempt(attempt) for attempt in self._attempts.values()
                if attempt["request_id"] == request_id and (adapter_id is None or attempt["adapter_id"] == adapter_id)
            ]
        return sorted(matched, key=lambda item: item["created_at"], reverse=True)

    def request_send_outcome(self, request_id: str, adapter_id: str | None = None) -> dict[str, Any]:
        """
        按同一 request_id 的全部业务尝试归并发送证据

        参数:
        - request_id: 逻辑请求标识
        - adapter_id: 可选平台实例过滤

        返回:
        - dict[str, Any]: confirmed/pending/planned/failed 段数, 是否有段已提交, 尝试数与脱敏原因;
          pending 只统计可能已提交副作用的段 (submitted/unknown), planned 段尚未尝试;
          旧记录 (用途未知) 与错误反馈不计入业务送达证据, 但会提高保守程度
        """
        attempts = self.lookup_attempts(request_id, adapter_id)
        business = [item for item in attempts if item["purpose"] == BUSINESS_PURPOSE]
        legacy = [item for item in attempts if item["purpose"] == LEGACY_PURPOSE]
        confirmed = pending = planned = failed = 0
        submitted = False
        reasons: list[str] = []
        for attempt in (*business, *legacy):
            for status in _segment_statuses(attempt):
                if status == CONFIRMED_SEGMENT_STATUS:
                    confirmed += 1
                    submitted = True
                elif status == "partial":
                    failed += 1
                    submitted = True
                elif status in {"submitted", "unknown"}:
                    pending += 1
                    submitted = True
                elif status == "planned":
                    planned += 1
                elif status == "failed":
                    failed += 1
                    submitted = True
            if attempt["status"] == "unknown":
                reasons.append(attempt["detail"] or "in_flight_unconfirmed")
        legacy_confirmed = sum(
            1 for attempt in legacy for status in _segment_statuses(attempt) if status == CONFIRMED_SEGMENT_STATUS
        )
        return {
            "attempts": len(business) + len(legacy), "business_attempts": len(business), "legacy_attempts": len(legacy),
            "confirmed": confirmed, "pending": pending, "planned": planned, "failed": failed,
            "submitted": submitted, "legacy_confirmed": legacy_confirmed,
            "turn_ids": [item["turn_id"] for item in attempts[:4]], "reasons": reasons,
        }

    @staticmethod
    def _segment_at(attempt: SendAttemptRecord, index: int) -> dict[str, Any] | None:
        """取段记录, 越界返回 None"""
        if not isinstance(index, int) or index < 0 or index >= len(attempt["segments"]):
            return None
        return attempt["segments"][index]

    def _persist_attempt(self, turn_id: str, previous: SendAttemptRecord, label: str) -> bool:
        """写入失败时恢复内存快照, 不向查询展示未持久的状态"""
        try:
            self._save_current_locked()
        except OSError as error:
            self._restore_record(self._attempts, turn_id, previous)
            logger.error(f"[ManualWakeStore] 发送{label}落盘失败 turn={turn_id}: {type(error).__name__}: {error}")
            return False
        return True


    # ---------- 测试与运维探针 ----------

    def pending_counts(self) -> dict[str, Any]:
        """未决记录计数与降级状态, 供健康检查与测试观察"""
        with self._mutex:
            return {
                "requests_pending": sum(1 for record in self._requests.values() if record["status"] in {"accepted", "executing"}),
                "requests_total": len(self._requests),
                "attempts_submitted": sum(1 for attempt in self._attempts.values() if attempt["status"] == "submitted"),
                "attempts_total": len(self._attempts),
                "degraded": self.degraded,
                "degraded_reason": self.degraded_reason,
            }
