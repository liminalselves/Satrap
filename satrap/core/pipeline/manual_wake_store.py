"""手动唤醒请求与发送尝试的持久化状态存储 (FileLock + 原子替换)

记录语义:
- 请求记录按 (adapter_id, request_id) 幂等, status ∈ accepted/executing/sent/partial/failed/unknown
- 发送尝试按 turn_id 记录, 发送 I/O 之前落盘 submitted, 完成后逐段更新 (混合链允许 partial)
- 重启时 accepted/executing/submitted 一律降级为 unknown, 不自动重发
- 文件损坏 → 改名隔离并进入显式降级: 拒绝依赖去重的新手动请求, 其余功能照常
- 未决记录 (accepted/executing/unknown 与 submitted) 不因容量被淘汰; 已满时 settled 记录轮转归档一代 (.1),
  归档也满则拒绝新记录并告警
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypedDict, cast
import json
import os
import tempfile
import threading
import time

from satrap.core.log import logger
from satrap.core.storage.file_lock import FileLock

STORE_VERSION = 1
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
SETTLED_STATUSES = frozenset({"sent", "partial", "failed"})
"""可归档/可按保留期清理的确认终态; unknown 属于未决, 不静默淘汰"""


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
    segments: list[dict[str, Any]]
    status: str
    detail: str
    created_at: float
    updated_at: float


class ManualWakeStoreError(RuntimeError):
    """存储不可用或容量耗尽, 调用方按 reason 映射拒绝语义"""

    def __init__(self, reason: Literal["degraded", "capacity", "io", "duplicate"], message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _validate_request(value: object) -> RequestRecord:
    """逐字段校验请求记录, 任何结构问题都视为文件损坏"""
    if not isinstance(value, dict):
        raise ValueError("请求记录必须是对象")
    raw = cast(dict[str, Any], value)
    strings = ("request_id", "fingerprint", "adapter_id", "target", "operator", "status", "detail")
    if any(not isinstance(raw.get(key), str) for key in strings):
        raise ValueError("请求记录字段类型非法")
    if not isinstance(raw.get("created_at"), (int, float)) or not isinstance(raw.get("updated_at"), (int, float)):
        raise ValueError("请求记录时间字段非法")
    if raw["status"] not in REQUEST_STATUSES:
        raise ValueError(f"未知请求状态: {raw['status']}")
    return {
        "request_id": raw["request_id"], "fingerprint": raw["fingerprint"], "adapter_id": raw["adapter_id"],
        "target": raw["target"], "operator": raw["operator"], "status": raw["status"], "detail": raw["detail"],
        "created_at": float(raw["created_at"]), "updated_at": float(raw["updated_at"]),
    }


def _validate_attempt(value: object) -> SendAttemptRecord:
    """逐字段校验发送尝试记录, 任何结构问题都视为文件损坏"""
    if not isinstance(value, dict):
        raise ValueError("发送尝试记录必须是对象")
    raw = cast(dict[str, Any], value)
    strings = ("turn_id", "adapter_id", "target", "request_id", "status", "detail")
    if any(not isinstance(raw.get(key), str) for key in strings):
        raise ValueError("发送尝试字段类型非法")
    if not isinstance(raw.get("created_at"), (int, float)) or not isinstance(raw.get("updated_at"), (int, float)):
        raise ValueError("发送尝试时间字段非法")
    if raw["status"] not in ATTEMPT_STATUSES:
        raise ValueError(f"未知发送尝试状态: {raw['status']}")
    segments = raw.get("segments")
    if not isinstance(segments, list) or any(not isinstance(item, dict) for item in cast(list[object], segments)):
        raise ValueError("发送尝试段记录非法")
    return {
        "turn_id": raw["turn_id"], "adapter_id": raw["adapter_id"], "target": raw["target"],
        "request_id": raw["request_id"], "segments": [dict(cast(dict[str, Any], item)) for item in cast(list[object], segments)],
        "status": raw["status"], "detail": raw["detail"],
        "created_at": float(raw["created_at"]), "updated_at": float(raw["updated_at"]),
    }


def _copy_request(record: RequestRecord) -> RequestRecord:
    """复制记录, 避免调用方持有内部字典引用"""
    return cast(RequestRecord, dict(record))


def _parse_payload(raw: object) -> tuple[dict[str, RequestRecord], dict[str, SendAttemptRecord]]:
    """解析存储文件, 结构非法即抛 ValueError 由调用方隔离"""
    if not isinstance(raw, dict) or cast(dict[str, Any], raw).get("version") != STORE_VERSION:
        raise ValueError("存储版本或结构非法")
    root = cast(dict[str, Any], raw)
    requests_raw, attempts_raw = root.get("requests"), root.get("attempts")
    if not isinstance(requests_raw, dict) or not isinstance(attempts_raw, dict):
        raise ValueError("存储缺少 requests/attempts 段")
    requests = {str(key): _validate_request(item) for key, item in cast(dict[str, Any], requests_raw).items()}
    attempts = {str(key): _validate_attempt(item) for key, item in cast(dict[str, Any], attempts_raw).items()}
    return requests, attempts


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 + os.replace 原子落盘"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", encoding="utf-8", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ManualWakeStore:
    """手动请求与发送尝试的 JSON 持久化, 进程内缓存为权威, FileLock 互斥跨进程写入"""

    def __init__(self, path: str | Path) -> None:
        """
        初始化存储, 构造不抛异常: 文件损坏改名隔离并进入降级状态

        参数:
        - path: 存储 JSON 路径 (数据目录下)
        """
        self._path = Path(path)
        self._lock_path = self._path.with_name(f".{self._path.name}.lock")
        self._archive_path = self._path.with_name(f"{self._path.name}.1")
        self._archive_lock_path = self._path.with_name(f".{self._path.name}.1.lock")
        self._mutex = threading.RLock()
        self.degraded = False
        self._requests: dict[str, RequestRecord] = {}
        self._attempts: dict[str, SendAttemptRecord] = {}
        self._archive_requests: dict[str, RequestRecord] | None = None
        self._archive_attempts: dict[str, SendAttemptRecord] | None = None
        try:
            self._load_or_quarantine()
        except Exception as error:
            logger.error(f"[ManualWakeStore] 初始化失败, 进入降级状态 path={self._path}: {type(error).__name__}: {error}")
            self.degraded = True

    # ---------- 加载与启动清扫 ----------

    @staticmethod
    def _request_key(adapter_id: str, request_id: str) -> str:
        return f"{adapter_id}\n{request_id}"

    def _read_payload(self, path: Path) -> tuple[dict[str, RequestRecord], dict[str, SendAttemptRecord]]:
        if not path.is_file():
            return {}, {}
        with open(path, encoding="utf-8") as handle:
            return _parse_payload(json.load(handle))

    def _quarantine(self, path: Path) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{path.name}.corrupt-{stamp}")
        suffix = 0
        while target.exists():
            suffix += 1
            target = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
        os.replace(path, target)
        logger.error(f"[ManualWakeStore] 存储文件损坏, 已隔离为 {target.name}, 进入降级状态")

    def _load_or_quarantine(self) -> None:
        try:
            self._requests, self._attempts = self._read_payload(self._path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error(f"[ManualWakeStore] 读取存储失败 path={self._path}: {type(error).__name__}: {error}")
            try:
                self._quarantine(self._path)
            except OSError as quarantine_error:
                logger.error(f"[ManualWakeStore] 损坏文件隔离失败: {type(quarantine_error).__name__}: {quarantine_error}")
            self.degraded = True
            return
        if self._sweep_restart_residue():
            try:
                self._save_current_locked()
            except OSError as error:
                logger.error(f"[ManualWakeStore] 启动清扫落盘失败, 进入降级状态: {type(error).__name__}: {error}")
                self.degraded = True

    def _sweep_restart_residue(self) -> bool:
        """重启后无法确认的记录标 unknown, 不自动重发"""
        now = time.time()
        changed = False
        for record in self._requests.values():
            if record["status"] in {"accepted", "executing"}:
                record["status"] = "unknown"
                record["detail"] = "restart_unconfirmed"
                record["updated_at"] = now
                changed = True
        for attempt in self._attempts.values():
            if attempt["status"] == "submitted":
                attempt["status"] = "unknown"
                attempt["detail"] = "restart_unconfirmed"
                attempt["updated_at"] = now
                for segment in attempt["segments"]:
                    if segment.get("status") == "submitted":
                        segment["status"] = "unknown"
                changed = True
        return changed

    # ---------- 落盘与归档 ----------

    def _save_current_locked(self) -> None:
        with FileLock(self._lock_path):
            _atomic_write(self._path, {"version": STORE_VERSION, "requests": self._requests, "attempts": self._attempts})

    def _load_archive_locked(self) -> tuple[dict[str, RequestRecord], dict[str, SendAttemptRecord]]:
        if self._archive_requests is not None and self._archive_attempts is not None:
            return self._archive_requests, self._archive_attempts
        try:
            self._archive_requests, self._archive_attempts = self._read_payload(self._archive_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.error(f"[ManualWakeStore] 归档读取失败, 隔离后按空归档继续: {type(error).__name__}: {error}")
            try:
                self._quarantine(self._archive_path)
            except OSError:
                pass
            self._archive_requests, self._archive_attempts = {}, {}
        return self._archive_requests, self._archive_attempts

    def _save_archive_locked(self) -> None:
        with FileLock(self._archive_lock_path):
            _atomic_write(self._archive_path, {
                "version": STORE_VERSION,
                "requests": self._archive_requests or {},
                "attempts": self._archive_attempts or {},
            })

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
            key = self._request_key(record["adapter_id"], record["request_id"]) if section == "requests" else record["turn_id"]
            archive_section[key] = records.pop(key)
            archive_changed = True
        if archive_changed:
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
                record = self._requests.get(self._request_key(adapter_id, request_id))
                if record is not None:
                    return _copy_request(record)
            else:
                matches = [record for key, record in self._requests.items() if key.endswith(f"\n{request_id}")]
                if matches:
                    return _copy_request(max(matches, key=lambda record: record["updated_at"]))
            archive_requests, _ = self._load_archive_locked()
            if adapter_id is not None:
                record = archive_requests.get(self._request_key(adapter_id, request_id))
                return _copy_request(record) if record is not None else None
            matches = [record for key, record in archive_requests.items() if key.endswith(f"\n{request_id}")]
            if not matches:
                return None
            return _copy_request(max(matches, key=lambda record: record["updated_at"]))

    def accept_request(self, adapter_id: str, request_id: str, fingerprint: str, target: str, operator: str) -> None:
        """持久化 accepted 占位, 成功落盘才返回; 降级/容量/IO 失败均抛错由调用方拒绝"""
        with self._mutex:
            if self.degraded:
                raise ManualWakeStoreError("degraded", "存储降级中, 无法保证去重")
            key = self._request_key(adapter_id, request_id)
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

    def update_request(self, adapter_id: str, request_id: str, status: str, detail: str = "") -> bool:
        """推进请求状态, 已终结记录不再回退; 写失败只告警不阻断业务"""
        with self._mutex:
            if self.degraded or status not in REQUEST_STATUSES:
                return False
            record = self._requests.get(self._request_key(adapter_id, request_id))
            if record is None or record["status"] in SETTLED_STATUSES or record["status"] == "unknown":
                return False
            record["status"] = status
            record["detail"] = detail
            record["updated_at"] = time.time()
            try:
                self._save_current_locked()
            except OSError as error:
                logger.error(f"[ManualWakeStore] 请求状态落盘失败 request_id={request_id}: {type(error).__name__}: {error}")
                return False
            return True

    def adapter_stopped(self, adapter_id: str) -> None:
        """平台停止: 未开始的请求判 failed, 执行中与未回执发送判 unknown"""
        with self._mutex:
            if self.degraded:
                return
            now = time.time()
            changed = False
            for record in self._requests.values():
                if record["adapter_id"] != adapter_id:
                    continue
                if record["status"] == "accepted":
                    record["status"], record["detail"], record["updated_at"] = "failed", "platform_stopped", now
                    changed = True
                elif record["status"] == "executing":
                    record["status"], record["detail"], record["updated_at"] = "unknown", "stopped_unconfirmed", now
                    changed = True
            for attempt in self._attempts.values():
                if attempt["adapter_id"] == adapter_id and attempt["status"] == "submitted":
                    attempt["status"], attempt["detail"], attempt["updated_at"] = "unknown", "stopped_unconfirmed", now
                    for segment in attempt["segments"]:
                        if segment.get("status") == "submitted":
                            segment["status"] = "unknown"
                    changed = True
            if changed:
                try:
                    self._save_current_locked()
                except OSError as error:
                    logger.error(f"[ManualWakeStore] 平台停止状态落盘失败 adapter={adapter_id}: {type(error).__name__}: {error}")

    # ---------- 发送尝试记录 ----------

    def record_send_attempt(
        self,
        turn_id: str,
        adapter_id: str,
        target: str,
        request_id: str,
        segments: list[dict[str, Any]],
    ) -> bool:
        """发送 I/O 之前持久化 submitted 占位; 失败只告警不阻断发送"""
        with self._mutex:
            if self.degraded:
                return False
            if not segments or not turn_id:
                return False
            try:
                self._ensure_insert_room("attempts")
            except ManualWakeStoreError as error:
                logger.error(f"[ManualWakeStore] 发送尝试容量不足 turn={turn_id}: {error}")
                return False
            now = time.time()
            planned: list[dict[str, Any]] = [{**dict(segment), "status": "submitted"} for segment in segments]
            self._attempts[turn_id] = {
                "turn_id": turn_id, "adapter_id": adapter_id, "target": target, "request_id": request_id,
                "segments": planned, "status": "submitted", "detail": "", "created_at": now, "updated_at": now,
            }
            try:
                self._save_current_locked()
            except OSError as error:
                self._attempts.pop(turn_id, None)
                logger.error(f"[ManualWakeStore] 发送尝试落盘失败 turn={turn_id}: {type(error).__name__}: {error}")
                return False
            return True

    def complete_send_attempt(self, turn_id: str, segment_statuses: list[str], status: str, detail: str = "") -> bool:
        """回执到达后逐段更新, 未到达的段由调用方标 skipped; 混合链允许 partial"""
        with self._mutex:
            if self.degraded:
                return False
            attempt = self._attempts.get(turn_id)
            if attempt is None or attempt["status"] != "submitted" or status not in ATTEMPT_STATUSES - {"submitted"}:
                return False
            for index, segment_status in enumerate(segment_statuses):
                if index >= len(attempt["segments"]):
                    break
                attempt["segments"][index]["status"] = segment_status
            attempt["status"] = status
            attempt["detail"] = detail
            attempt["updated_at"] = time.time()
            try:
                self._save_current_locked()
            except OSError as error:
                logger.error(f"[ManualWakeStore] 发送回执落盘失败 turn={turn_id}: {type(error).__name__}: {error}")
                return False
            return True

    # ---------- 测试与运维探针 ----------

    def pending_counts(self) -> dict[str, int]:
        """未决记录计数, 供健康检查与测试观察"""
        with self._mutex:
            return {
                "requests_pending": sum(1 for record in self._requests.values() if record["status"] in {"accepted", "executing"}),
                "requests_total": len(self._requests),
                "attempts_submitted": sum(1 for attempt in self._attempts.values() if attempt["status"] == "submitted"),
                "attempts_total": len(self._attempts),
            }
