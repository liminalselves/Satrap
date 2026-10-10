"""request 事件 flag 的审批身份账本与近期操作缓存

群请求与好友请求分域登记; 审批占用在持久事务中原子完成, 落盘成功才允许发网络动作;
已消费与未知身份不因重复入站, 容量轮转或进程重启回到可审批状态;
本地到期身份只可在归档确认路径直接占用, 不恢复为近期可审批状态;
好友请求按原始事件时间区分同一处理凭据的新申请, 旧申请不能处理新一轮请求;
近期缓存仅用于诊断, 不作为审批资格依据
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast
from time import time
import asyncio
import hashlib
import json
import threading
import traceback

from satrap.core.log import logger
from satrap.core.storage.persist import atomic_write_json
from satrap.core.storage.file_lock import FileLock
from satrap.core.platform.request_inbox import RequestInbox, flag_digest
from satrap.core.storage.durability import (
    DurabilityManifest,
    Manifest,
    ManifestSpec,
    QuarantinedFile,
    validate_manifest,
)

REQUEST_FLAG_LIMIT = 512
"""每张近期缓存表的 flag 容量, 超出淘汰最旧, 淘汰不影响账本身份"""
REQUEST_FLAG_TTL = 600.0
"""flag 可审批秒数, 到期转为不可审批的 expired 墓碑而不是删除身份"""

LEDGER_VERSION = 1
LEDGER_INSTANCE_CAPACITY = 4096
"""每个 (适配器, 已绑定账号) 的账本上限, 达限拒绝新登记而不是淘汰旧身份"""
LEDGER_TOTAL_CAPACITY = 16384
"""账本文件总上限, 防止多实例累计无界增长"""
LEDGER_STATES = frozenset({"available", "executing", "completed", "unknown", "expired", "superseded"})
LEDGER_SETTLE_STATES = frozenset({"completed", "unknown"})
"""审批动作的明确终态; 到期墓碑与它并列但不可由 settle 写入"""
LEDGER_RESTART_STATE = "unknown"
"""重启后无法确认的占用统一保守结束为 unknown"""

LEDGER_EXPECTED_ENTRIES = "present"
"""清单始终要求账本文件存在: 初始化后文件缺失按损坏处理, 不解释为空账本"""

LEDGER_MANIFEST_SPEC = ManifestSpec(
    version=LEDGER_VERSION,
    expected_files={"entries": frozenset({LEDGER_EXPECTED_ENTRIES})},
    # 账本清单保留历史额外键, 降级原因只要求字符串, at 字段原样保留不解释
    keep_extra_keys=True,
    degraded_requires_reason=False,
    normalize_degraded_at=False,
)
"""审批账本清单格式"""

LEDGER_MANIFEST_EXPECTED: dict[str, str] = {"entries": LEDGER_EXPECTED_ENTRIES}
"""清单 expected_files 的初始状态"""


def _validate_ledger_manifest(raw: object) -> Manifest:
    """
    校验账本清单: 基础结构由 durability 组件负责

    参数:
    - raw: 清单 JSON 解析结果

    返回:
    - Manifest: 归一化后的清单

    异常:
    - ValueError: 版本, 文件状态, 时间字段或降级字段非法
    """
    return validate_manifest(raw, LEDGER_MANIFEST_SPEC)


class LedgerEntry(TypedDict):
    """一条审批身份账本记录, 归属与首次接收时间在登记后不再变化"""

    adapter_id: str
    self_id: str
    kind: str
    digest: str
    group_id: str
    sub_type: str
    user_id: str
    received_at: float
    updated_at: float
    state: str
    credential_digest: NotRequired[str]
    event_time: NotRequired[int]


@dataclass
class RequestFlagEntry:
    """近期缓存中的一条登记, state 仅 available→executing→completed/unknown 单向迁移"""

    group_id: str
    sub_type: str
    user_id: str
    received_at: float
    state: str = "available"
    self_id: str = ""


def _entry_key(adapter_id: str, self_id: str, kind: str, digest: str) -> str:
    """账本键: 适配器 ID, 已绑定账号, 请求类别与申请身份摘要"""
    return f"{adapter_id}\n{self_id}\n{kind}\n{digest}"


def request_digest(kind: str, self_id: str, flag: str, event_time: int | None = None) -> str:
    """
    将好友申请事件身份与可复用的平台处理凭据分开

    参数:
    - kind: friend 或 group
    - self_id: 已绑定机器人账号
    - flag: 平台处理凭据, 不记录原值
    - event_time: 平台原始好友申请时间, None 保留旧版身份规则

    返回:
    - 一次申请的稳定摘要, 同一事件重放仍得到同一身份
    """
    credential = flag_digest(kind, self_id, flag)
    if event_time is None:
        return credential
    if kind != "friend" or type(event_time) is not int or event_time <= 0:
        raise ValueError("好友申请原始事件时间必须为正整数")
    return hashlib.sha256(f"friend-event\x00{credential}\x00{event_time}".encode("utf-8")).hexdigest()[:32]


def _validate_entry(item: object, key: str, source: str) -> LedgerEntry:
    """逐字段校验账本记录, 身份不一致或状态非法都视为文件损坏"""
    if not isinstance(item, dict):
        raise ValueError(f"{source} 账本记录必须是对象")
    raw = cast(dict[str, Any], item)
    strings = ("adapter_id", "self_id", "kind", "digest", "group_id", "sub_type", "user_id", "state")
    if any(not isinstance(raw.get(field), str) for field in strings):
        raise ValueError(f"{source} 账本记录字段类型非法")
    if raw["kind"] not in {"group", "friend"} or raw["state"] not in LEDGER_STATES:
        raise ValueError(f"{source} 账本记录类别或状态非法")
    if not isinstance(raw.get("received_at"), (int, float)) or isinstance(raw.get("received_at"), bool):
        raise ValueError(f"{source} 账本接收时间非法")
    if not isinstance(raw.get("updated_at"), (int, float)) or isinstance(raw.get("updated_at"), bool):
        raise ValueError(f"{source} 账本更新时间非法")
    if not raw["adapter_id"] or not raw["self_id"] or not raw["digest"]:
        raise ValueError(f"{source} 账本记录身份字段为空")
    entry: LedgerEntry = {
        "adapter_id": raw["adapter_id"], "self_id": raw["self_id"], "kind": raw["kind"], "digest": raw["digest"],
        "group_id": raw["group_id"], "sub_type": raw["sub_type"], "user_id": raw["user_id"],
        "received_at": float(raw["received_at"]), "updated_at": float(raw["updated_at"]), "state": raw["state"],
    }
    if "event_time" in raw or "credential_digest" in raw:
        if (raw["kind"] != "friend" or type(raw.get("event_time")) is not int or raw["event_time"] <= 0
                or not isinstance(raw.get("credential_digest"), str) or len(raw["credential_digest"]) != 32
                or any(ch not in "0123456789abcdef" for ch in raw["credential_digest"])):
            raise ValueError(f"{source} 申请事件身份非法")
        entry["event_time"] = raw["event_time"]
        entry["credential_digest"] = raw["credential_digest"]
        expected_digest = hashlib.sha256(f"friend-event\x00{entry['credential_digest']}\x00{entry['event_time']}".encode("utf-8")).hexdigest()[:32]
        if entry["digest"] != expected_digest:
            raise ValueError(f"{source} 申请事件摘要不一致")
    if key != _entry_key(entry["adapter_id"], entry["self_id"], entry["kind"], entry["digest"]):
        raise ValueError(f"{source} 账本记录身份与键不一致")
    return entry


def _parse_entries(raw: object, source: str) -> dict[str, LedgerEntry]:
    """解析账本文件, 结构或身份非法即抛 ValueError 由调用方隔离"""
    if not isinstance(raw, dict):
        raise ValueError(f"{source} 不是对象")
    root = cast(dict[str, Any], raw)
    if root.get("version") != LEDGER_VERSION:
        raise ValueError(f"{source} 账本版本非法")
    entries_raw = root.get("entries")
    if not isinstance(entries_raw, dict):
        raise ValueError(f"{source} 缺少 entries 段")
    return {str(key): _validate_entry(item, str(key), source) for key, item in cast(dict[str, Any], entries_raw).items()}


class RequestApprovalLedger:
    """审批身份账本: 进程内为读缓存, 每次变更在文件锁内重新读取后原子写回"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        ttl: float = REQUEST_FLAG_TTL,
        instance_capacity: int = LEDGER_INSTANCE_CAPACITY,
        total_capacity: int = LEDGER_TOTAL_CAPACITY,
    ) -> None:
        """
        初始化账本, 构造不抛异常: 文件损坏先持久标记降级再隔离

        参数:
        - path: 账本 JSON 路径, None 表示仅进程内账本 (测试与试算适配器)
        - ttl: 可审批秒数
        - instance_capacity: 每个 (适配器, 账号) 的上限
        - total_capacity: 账本文件总上限
        """
        if ttl <= 0 or instance_capacity <= 0 or total_capacity <= 0:
            raise ValueError("账本 TTL 与容量必须为正数")
        self._path = Path(path) if path is not None else None
        self._lock_path = self._path.with_name(f".{self._path.name}.lock") if self._path is not None else None
        self._mutex = threading.RLock()
        self._ttl, self._instance_capacity, self._total_capacity = ttl, instance_capacity, total_capacity
        self._entries: dict[str, LedgerEntry] = {}
        self._manifest: Manifest | None = None
        self._durability = (
            DurabilityManifest(
                self._path, LEDGER_MANIFEST_SPEC,
                transaction=self._transaction,
                validate=_validate_ledger_manifest,
                log_prefix="[RequestLedger]",
            )
            if self._path is not None else None
        )
        self.degraded = False
        self.degraded_reason = ""
        if self._path is None:
            return
        try:
            with self._transaction():
                self._startup_locked()
        except Exception as error:
            logger.error(f"[RequestLedger] 初始化失败, 进入降级状态 path={self._path}: {type(error).__name__}: {error}")
            self._mark_degraded("init_failed", f"{type(error).__name__}: {error}")

    # ---------- 文件与事务 ----------

    @property
    def persistent(self) -> bool:
        """是否具备跨重启的持久账本"""
        return self._path is not None

    @property
    def ttl(self) -> float:
        """可审批秒数"""
        return self._ttl

    @property
    def inbox_path(self) -> Path | None:
        """
        返回宿主私有收件箱的位置

        返回:
        - 持久账本同级的 requests/inbox.db, 内存账本为 None
        """
        return self._path.parent / "requests" / "inbox.db" if self._path is not None else None

    def _transaction(self) -> AbstractContextManager[Any]:
        """内存账本无需跨进程锁, 持久账本的锁覆盖读取, 判定, 占用与写回全过程"""
        if self._lock_path is None:
            return nullcontext()
        return FileLock(self._lock_path)

    def _read_entries(self) -> dict[str, LedgerEntry]:
        if self._path is None or not self._path.is_file():
            return {}
        with open(self._path, encoding="utf-8") as handle:
            return _parse_entries(json.load(handle), self._path.name)

    def _save_locked(self) -> None:
        """写入账本文件, 调用方必须已持有账本锁"""
        if self._path is None:
            return
        atomic_write_json(self._path, {"version": LEDGER_VERSION, "entries": self._entries})

    def _write_manifest(self, manifest: Manifest) -> None:
        """原子写入清单, 调用方必须已持有账本锁"""
        durability = self._durability
        if durability is None:
            raise RuntimeError("内存账本没有清单文件")
        atomic_write_json(durability.manifest_path, manifest.payload())

    def _fresh_ledger_manifest(self, durability: DurabilityManifest) -> Manifest:
        """当前清单, 缺失时按全新骨架 (降级标记始终有清单可写)"""
        if self._manifest is not None:
            return self._manifest
        return durability.fresh(initialized_at=time(), expected=LEDGER_MANIFEST_EXPECTED, updated_at=time())

    def _mark_degraded(self, reason: str, detail: str) -> bool:
        """
        持久记录降级原因, 不隔离文件也不清空账本

        参数:
        - reason: 脱敏原因码
        - detail: 仅供日志的诊断说明

        返回:
        - bool: 降级标记已落盘为 True; 写失败时保留原文件, 调用方不得隔离
        """
        durability = self._durability
        persisted = True
        if durability is not None:
            outcome = durability.mark_degraded(
                self._fresh_ledger_manifest(durability), reason=reason, persist=self._write_manifest,
            )
            self._manifest = outcome.manifest
            persisted = outcome.persisted
        self._apply_degrade(reason, detail, ())
        return persisted

    def _degrade(self, reason: str, detail: str, corrupt: bool) -> None:
        """
        先持久记录降级, 再隔离损坏文件

        参数:
        - reason: 脱敏原因码
        - detail: 诊断说明
        - corrupt: 账本文件是否损坏到需要隔离
        """
        durability = self._durability
        if durability is None:
            self._apply_degrade(reason, detail, ())
            return
        bad_files = [self._path] if corrupt and self._path is not None and self._path.is_file() else []
        outcome = durability.degrade_then_quarantine(
            self._fresh_ledger_manifest(durability), reason=reason,
            persist=self._write_manifest, bad_files=bad_files,
        )
        self._manifest = outcome.manifest
        self._apply_degrade(reason, detail, outcome.quarantined)

    def _apply_degrade(self, reason: str, detail: str, quarantined: Sequence[QuarantinedFile]) -> None:
        """
        把降级结果落到内存与日志

        参数:
        - reason: 脱敏原因码
        - detail: 诊断说明
        - quarantined: 已隔离的文件; 标记未落盘时为空
        """
        self.degraded = True
        self.degraded_reason = reason
        logger.error(f"[RequestLedger] 审批账本降级 reason={reason}: {detail}")
        for item in quarantined:
            logger.error(f"[RequestLedger] 账本文件损坏, 已隔离为 {item.target.name}")

    def _quarantine_names(self) -> list[str]:
        """目录中已隔离的损坏文件, 用于判定目录是否曾初始化"""
        durability = self._durability
        return [] if durability is None else durability.quarantine_names()

    def _startup_locked(self) -> None:
        """按清单与文件内容决定初始化, 迁移或降级"""
        manifest: Manifest | None = None
        manifest_error = ""
        try:
            manifest = self._read_manifest()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            manifest_error = f"{type(error).__name__}: {error}"
        entries_exists = self._path is not None and self._path.is_file()
        if manifest is not None and manifest.degraded is not None:
            self._manifest = manifest
            self.degraded = True
            self.degraded_reason = manifest.degraded.reason or "degraded"
            logger.error(f"[RequestLedger] 账本保持降级状态 reason={self.degraded_reason}, 需经 recover() 显式恢复")
            return
        if manifest is None:
            quarantined = self._quarantine_names()
            if entries_exists or quarantined:
                reason = "manifest_unreadable" if manifest_error else "manifest_missing_with_data"
                detail = manifest_error or f"已有文件: {', '.join(quarantined) or (self._path.name if self._path else '')}"
                self._adopt_existing(reason, detail)
                return
            self._initialize_fresh()
            return
        self._manifest = manifest
        if not entries_exists:
            self._degrade("entries_missing", "已初始化账本的文件缺失", False)
            return
        self._load_entries_locked(restart=True)

    def _read_manifest(self) -> Manifest | None:
        """读取并校验清单, 文件不存在返回 None, 结构非法抛 ValueError"""
        durability = self._durability
        return None if durability is None else durability.read()

    def _initialize_fresh(self) -> None:
        """全新账本: 建立空文件与清单, 失败即降级"""
        durability = self._durability
        if self._path is None or durability is None:
            return
        manifest = durability.fresh(initialized_at=time(), expected=LEDGER_MANIFEST_EXPECTED, updated_at=time())
        try:
            self._save_locked()
            self._write_manifest(manifest)
        except OSError as error:
            self._degrade("init_io_failed", f"{type(error).__name__}: {error}", False)
            return
        self._manifest = manifest
        logger.info(f"[RequestLedger] 已初始化全新审批账本 path={self._path.name}")

    def _adopt_existing(self, reason: str, detail: str) -> None:
        """清单缺失或损坏但账本文件存在: 校验后迁移, 否则降级"""
        try:
            entries = self._read_entries()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._degrade(reason, f"账本校验失败: {type(error).__name__}: {error}", True)
            return
        self._entries = entries
        durability = self._durability
        if durability is None:
            return
        manifest = durability.fresh(initialized_at=time(), expected=LEDGER_MANIFEST_EXPECTED, updated_at=time())
        try:
            self._save_locked()
            self._write_manifest(manifest)
        except OSError as error:
            self._degrade(reason, f"清单写入失败: {type(error).__name__}: {error}", False)
            return
        self._manifest = manifest
        self._sweep_restart_locked()
        logger.info(f"[RequestLedger] 已迁移旧版账本 path={self._path.name if self._path else ''} entries={len(entries)}")

    def _load_entries_locked(self, *, restart: bool) -> None:
        """
        重新读取账本文件, 每次变更前都在锁内执行

        参数:
        - restart: 仅进程启动时为 True, 此时把无法确认的占用标注为 unknown
        """
        self._entries = self._read_entries()
        if restart:
            self._sweep_restart_locked()

    def _sweep_restart_locked(self) -> None:
        """重启后无法确认的占用与到期的可审批身份改标墓碑, 不删除身份"""
        now = time()
        changed = False
        for entry in self._entries.values():
            if entry["state"] == "executing":
                entry["state"] = LEDGER_RESTART_STATE
                entry["updated_at"] = now
                changed = True
            elif entry["state"] == "available" and now - entry["received_at"] >= self._ttl:
                entry["state"] = "expired"
                entry["updated_at"] = now
                changed = True
        if changed:
            self._save_locked()

    def _reload_locked(self) -> None:
        """变更前重新读取: 文件锁覆盖读取, 判定, 占用与写回整个过程"""
        if self._path is None:
            return
        if self._manifest is None:
            self._startup_locked()
            return
        if not self._path.is_file():
            self._degrade("entries_missing", "已初始化账本的文件缺失", False)
            return
        try:
            self._load_entries_locked(restart=False)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._degrade("entries_corrupt", f"{type(error).__name__}: {error}", True)

    def recover(self) -> bool:
        """
        校验账本文件后解除持久降级, 不提供清空账本后继续

        返回:
        - bool: 账本结构, 身份与状态一致时为 True 并解除降级, 否则保持降级返回 False
        """
        durability = self._durability
        if self._path is None or durability is None:
            self.degraded = False
            self.degraded_reason = ""
            return True
        with self._mutex, self._transaction():
            if not self._path.is_file():
                logger.error("[RequestLedger] 恢复失败: 账本文件不存在, 需先恢复原文件")
                return False
            try:
                entries = self._read_entries()
            except (OSError, ValueError, json.JSONDecodeError) as error:
                logger.error(f"[RequestLedger] 恢复校验失败, 保持降级: {type(error).__name__}: {error}")
                return False
            manifest = durability.fresh(
                initialized_at=self._manifest.initialized_at if self._manifest else time(),
                expected=LEDGER_MANIFEST_EXPECTED,
                updated_at=time(),
            )
            try:
                self._write_manifest(manifest)
            except OSError as error:
                logger.error(f"[RequestLedger] 恢复写入清单失败, 保持降级: {type(error).__name__}: {error}")
                return False
            self._entries = entries
            self._manifest = manifest
            self.degraded = False
            self.degraded_reason = ""
            self._sweep_restart_locked()
            logger.info(f"[RequestLedger] 审批账本已恢复 entries={len(entries)}")
            return True

    # ---------- 登记, 占用与终态 ----------

    def _instance_count(self, adapter_id: str, self_id: str) -> int:
        return sum(1 for entry in self._entries.values() if entry["adapter_id"] == adapter_id and entry["self_id"] == self_id)

    def _register_sync(
        self, adapter_id: str, self_id: str, kind: str, flag: str,
        *, group_id: str, sub_type: str, user_id: str, now: float, event_time: int | None = None,
    ) -> str:
        """同步登记实现, 调用方已通过 to_thread 移出事件循环"""
        if not self_id or not adapter_id:
            return "unknown_account"
        with self._mutex, self._transaction():
            if self.degraded:
                return "degraded"
            self._reload_locked()
            if self.degraded:
                return "degraded"
            credential = flag_digest(kind, self_id, flag)
            digest = request_digest(kind, self_id, flag, event_time)
            key = _entry_key(adapter_id, self_id, kind, digest)
            existing = self._entries.get(key)
            if existing is not None:
                if (existing["group_id"], existing["sub_type"], existing["user_id"]) != (group_id, sub_type, user_id):
                    # 同一 key 归属冲突: 只记摘要与类别, 不记录原始 flag
                    logger.warning(
                        f"[RequestLedger] 同 key 归属冲突已拒绝 adapter={adapter_id} kind={kind} "
                        f"digest={existing['digest'][:8]}",
                    )
                    return "conflict"
                if existing["state"] == "available" and now - existing["received_at"] >= self._ttl:
                    existing["state"] = "expired"
                    existing["updated_at"] = now
                    self._save_locked()
                return "duplicate"
            family = [entry for entry in self._entries.values()
                      if entry["adapter_id"] == adapter_id and entry["self_id"] == self_id and entry["kind"] == kind
                      and entry.get("credential_digest", entry["digest"]) == credential]
            if event_time is not None and family:
                if any((entry["group_id"], entry["sub_type"], entry["user_id"]) != (group_id, sub_type, user_id) for entry in family):
                    return "conflict"
                if event_time <= max(entry.get("event_time", entry["received_at"]) for entry in family):
                    return "stale_event"
            elif family and any("event_time" in entry for entry in family):
                return "missing_event_identity"
            if self._instance_count(adapter_id, self_id) >= self._instance_capacity:
                logger.error(
                    f"[RequestLedger] 账本容量已满 adapter={adapter_id} self_id={self_id} "
                    f"cap={self._instance_capacity}, 拒绝新登记",
                )
                return "capacity"
            if len(self._entries) >= self._total_capacity:
                logger.error(f"[RequestLedger] 账本文件总容量已满 cap={self._total_capacity}, 拒绝新登记")
                return "capacity"
            self._entries[key] = {
                "adapter_id": adapter_id, "self_id": self_id, "kind": kind,
                "digest": digest, "group_id": group_id, "sub_type": sub_type,
                "user_id": user_id, "received_at": now, "updated_at": now, "state": "available",
            }
            if event_time is not None:
                self._entries[key]["credential_digest"] = credential
                self._entries[key]["event_time"] = event_time
                for prior in family:
                    if prior["state"] in {"available", "expired"}:
                        prior["state"] = "superseded"
                        prior["updated_at"] = now
            self._save_locked()
            return "registered"

    def _occupy_sync(
        self, adapter_id: str, self_id: str, kind: str, flag: str,
        *, group_id: str, sub_type: str, now: float, allow_archived: bool = False, identity_digest: str | None = None,
    ) -> LedgerEntry:
        """同步占用实现, 取消调用方不会撤销已经落盘的占用"""
        with self._mutex, self._transaction():
            if self.degraded:
                raise LookupError("审批账本不可用, 拒绝执行")
            self._reload_locked()
            if self.degraded:
                raise LookupError("审批账本不可用, 拒绝执行")
            key = _entry_key(adapter_id, self_id, kind, identity_digest or flag_digest(kind, self_id, flag))
            entry = self._entries.get(key)
            if entry is None:
                raise LookupError("请求标识未登记或已过期, 无法确认归属")
            if entry["group_id"] != group_id or entry["sub_type"] != sub_type:
                raise LookupError("请求标识归属与参数不符")
            if entry.get("credential_digest", entry["digest"]) != flag_digest(kind, self_id, flag):
                raise LookupError("申请身份与平台处理凭据不符")
            if entry["state"] == "superseded":
                raise LookupError("该申请已有更新记录, 旧申请不能处理新申请")
            if entry["state"] != "available" and not (allow_archived and entry["state"] == "expired"):
                raise LookupError("请求标识已被处理或结果未知, 拒绝重复执行")
            if now - entry["received_at"] >= self._ttl and not allow_archived:
                entry["state"] = "expired"
                entry["updated_at"] = now
                self._save_locked()
                raise LookupError("请求标识已超过可审批时限, 拒绝执行")
            entry["state"] = "executing"
            entry["updated_at"] = now
            self._save_locked()
            return cast(LedgerEntry, dict(entry))

    def _settle_sync(self, adapter_id: str, self_id: str, kind: str, flag: str, state: str, identity_digest: str | None = None) -> bool:
        """同步终态实现, 只允许 executing 迁入明确终态"""
        with self._mutex, self._transaction():
            if self.degraded:
                return False
            self._reload_locked()
            if self.degraded:
                return False
            key = _entry_key(adapter_id, self_id, kind, identity_digest or flag_digest(kind, self_id, flag))
            entry = self._entries.get(key)
            if (entry is None or entry["state"] != "executing"
                    or entry.get("credential_digest", entry["digest"]) != flag_digest(kind, self_id, flag)):
                return False
            entry["state"] = state
            entry["updated_at"] = time()
            self._save_locked()
            return True

    async def register(
        self, adapter_id: str, self_id: str, kind: str, flag: str,
        *, group_id: str = "", sub_type: str = "", user_id: str = "", now: float | None = None, event_time: int | None = None,
    ) -> str:
        """
        登记入站 request 事件: 首次登记固定归属与首次接收时间, 重复入站不改写状态

        参数:
        - adapter_id: 平台实例 ID
        - self_id: 已绑定机器人账号, 未知时不允许登记
        - kind: group 或 friend, 分域隔离
        - flag: 事件上报的审批标识, 只以摘要入账
        - group_id: 群请求的群号
        - sub_type: 群请求的 add/invite
        - user_id: 请求来源用户
        - now: 墙钟时间, 默认读取当前时间
        - event_time: 好友请求原始事件时间, None 沿用旧标识规则

        返回:
        - str: 登记, 重复或明确拒绝原因, 新一轮好友申请不会复活旧身份
        """
        moment = time() if now is None else now
        return await asyncio.to_thread(
            self._register_sync, adapter_id, self_id, kind, flag,
            group_id=group_id, sub_type=sub_type, user_id=user_id, now=moment, event_time=event_time,
        )

    async def occupy(
        self, adapter_id: str, self_id: str, kind: str, flag: str,
        *, group_id: str = "", sub_type: str = "", now: float | None = None, allow_archived: bool = False, identity_digest: str | None = None,
    ) -> LedgerEntry:
        """
        在持久事务中原子占用登记, 落盘成功才返回; 并发至多一个成功

        参数:
        - adapter_id: 平台实例 ID
        - self_id: 已绑定机器人账号
        - kind: group 或 friend
        - flag: 待核验标识
        - group_id: 调用方声明的群号, 必须与登记一致
        - sub_type: 调用方声明的子类型, 必须与登记一致
        - now: 墙钟时间
        - allow_archived: 是否已经明确确认归档处理
        - identity_digest: 收件箱固定的一次申请身份, None 仅允许旧版身份

        返回:
        - LedgerEntry: 已占用的登记项

        异常:
        - LookupError: 未登记, 已过期, 已占用, 归属不符或账本降级, message 为用户可读原因
        """
        moment = time() if now is None else now
        return await asyncio.to_thread(
            self._occupy_sync, adapter_id, self_id, kind, flag,
            group_id=group_id, sub_type=sub_type, now=moment, allow_archived=allow_archived, identity_digest=identity_digest,
        )

    async def settle(self, adapter_id: str, self_id: str, kind: str, flag: str, state: str, *, identity_digest: str | None = None) -> bool:
        """
        把已占用的登记迁入明确终态

        参数:
        - adapter_id: 平台实例 ID
        - self_id: 已绑定机器人账号
        - kind: group 或 friend
        - flag: 待迁移标识
        - state: completed (动作已有明确结果) 或 unknown (超时/取消/传输异常, 不可重试)
        - identity_digest: 被占用的一次申请身份, 不根据复用凭据选择新申请

        返回:
        - bool: 迁移并落盘成功为 True
        """
        if state not in LEDGER_SETTLE_STATES:
            raise ValueError("终态必须为 completed 或 unknown")
        return await asyncio.to_thread(self._settle_sync, adapter_id, self_id, kind, flag, state, identity_digest)

    def lookup(self, adapter_id: str, self_id: str, kind: str, flag: str, *, identity_digest: str | None = None) -> LedgerEntry | None:
        """查询登记项副本, 供诊断与测试观察"""
        with self._mutex:
            if self.degraded:
                return None
            key = _entry_key(adapter_id, self_id, kind, identity_digest or flag_digest(kind, self_id, flag))
            entry = self._entries.get(key)
            return cast(LedgerEntry, dict(entry)) if entry is not None else None

    def verify_occupied_friend(self, adapter_id: str, self_id: str, flag: str, identity_digest: str) -> None:
        """
        在实际协议发送前拒绝已被新申请替代的旧占用

        参数:
        - adapter_id: 固定平台实例
        - self_id: 固定机器人账号
        - flag: 平台处理凭据
        - identity_digest: 当前动作已占用的申请身份
        """
        with self._mutex, self._transaction():
            self._reload_locked()
            entry = self._entries.get(_entry_key(adapter_id, self_id, "friend", identity_digest))
            if self.degraded or entry is None or entry["state"] != "executing":
                raise PermissionError("好友申请占用已失效")
            credential = flag_digest("friend", self_id, flag)
            if entry.get("credential_digest", entry["digest"]) != credential:
                raise PermissionError("好友申请处理凭据不匹配")
            if any(other["adapter_id"] == adapter_id and other["self_id"] == self_id and other["kind"] == "friend"
                   and other.get("credential_digest", other["digest"]) == credential
                   and other.get("event_time", other["received_at"]) > entry.get("event_time", entry["received_at"])
                   for other in self._entries.values()):
                raise PermissionError("该好友申请已有更新记录, 请处理新申请")

    def available_entries(self, adapter_id: str, self_id: str, kind: str, now: float,
                          *, include_history: bool = False) -> dict[str, LedgerEntry]:
        """
        从当前持久账本读取仍可执行的申请身份

        参数:
        - adapter_id: 来源平台实例
        - self_id: 当前已绑定账号
        - kind: friend 或 group
        - now: 当前时间

        返回:
        - 按摘要索引的有限身份, 降级时拒绝查询
        """
        with self._mutex, self._transaction():
            self._reload_locked()
            if self.degraded:
                raise LookupError("审批账本不可用, 拒绝查询申请")
            return {entry["digest"]: cast(LedgerEntry, dict(entry)) for entry in self._entries.values()
                    if entry["adapter_id"] == adapter_id and entry["self_id"] == self_id and entry["kind"] == kind
                    and (include_history or entry["state"] == "available" and now < entry["received_at"] + self.ttl)}

    def pending_counts(self) -> dict[str, Any]:
        """账本状态与占用计数, 供健康检查与测试观察"""
        with self._mutex:
            states: dict[str, int] = {}
            for entry in self._entries.values():
                states[entry["state"]] = states.get(entry["state"], 0) + 1
            return {
                "entries_total": len(self._entries), "states": states, "persistent": self.persistent,
                "degraded": self.degraded, "degraded_reason": self.degraded_reason,
            }


class RequestFlagRegistry:
    """审批 flag 的近期缓存与账本门面, 审批资格以持久账本为准"""

    def __init__(
        self,
        adapter_id: str = "",
        ledger: RequestApprovalLedger | None = None,
        limit: int = REQUEST_FLAG_LIMIT,
        ttl: float = REQUEST_FLAG_TTL,
    ) -> None:
        """
        初始化登记门面

        参数:
        - adapter_id: 所属平台实例 ID
        - ledger: 审批账本, 缺省为仅进程内账本
        - limit: 近期缓存容量, 淘汰不影响账本身份
        - ttl: 可审批秒数
        """
        if limit <= 0 or ttl <= 0:
            raise ValueError("登记容量和 TTL 必须为正数")
        self.adapter_id = adapter_id
        self.limit, self.ttl = limit, ttl
        self.ledger = ledger if ledger is not None else RequestApprovalLedger(ttl=ttl)
        self._tables: dict[str, OrderedDict[str, RequestFlagEntry]] = {"group": OrderedDict(), "friend": OrderedDict()}
        self.inbox = RequestInbox(self.ledger.inbox_path, self.ledger.ttl)

    def set_ledger(self, ledger: RequestApprovalLedger) -> None:
        """装配持久账本 (后端在适配器创建后注入), 清空仅进程内的近期缓存"""
        self.ledger = ledger
        self._tables = {"group": OrderedDict(), "friend": OrderedDict()}
        self.inbox.close()
        self.inbox = RequestInbox(ledger.inbox_path, ledger.ttl)

    def _remember(self, kind: str, flag: str, entry: RequestFlagEntry) -> None:
        """写入近期缓存并按容量淘汰最旧"""
        table = self._tables[kind]
        table[flag] = entry
        table.move_to_end(flag)
        while len(table) > self.limit:
            table.popitem(last=False)

    async def register(
        self, kind: str, flag: str, *,
        self_id: str, group_id: str = "", sub_type: str = "", user_id: str = "", now: float | None = None, comment: str = "", event_time: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """
        登记入站 request 事件; 重复入站不改写身份, 过期时间或消费状态

        参数:
        - kind: group 或 friend, 分表隔离
        - flag: 事件上报的审批标识
        - self_id: 已绑定机器人账号
        - group_id: 群请求的群号
        - sub_type: 群请求的 add/invite
        - user_id: 请求来源用户
        - now: 墙钟时间, 默认读取当前时间
        - comment: 平台申请验证信息, 保存在私有收件箱
        - event_time: 平台好友申请原始事件时间, 不使用本地接收时间代替
        - details: 平台提供的申请类别和展示详情

        返回:
        - bool: 账本新增登记为 True; 重复, 冲突, 容量或降级时为 False
        """
        moment = time() if now is None else now
        result = await self.ledger.register(
            self.adapter_id, self_id, kind, flag,
            group_id=group_id, sub_type=sub_type, user_id=user_id, now=moment, event_time=event_time,
        )
        restored = False
        if result in {"registered", "duplicate"}:
            identity = request_digest(kind, self_id, flag, event_time)
            entry = await asyncio.to_thread(self.ledger.lookup, self.adapter_id, self_id, kind, flag, identity_digest=identity)
            if entry is not None:
                restored = await asyncio.to_thread(self.inbox.register, self.adapter_id, self_id, kind, flag,
                                        group_id=entry["group_id"], sub_type=entry["sub_type"], user_id=entry["user_id"],
                                        comment=comment, received_at=entry["received_at"], now=moment, identity_digest=identity,
                                        details={"requested_at": event_time, **(details or {})},
                                        restore_missing=entry["state"] in {"available", "expired"})
        log = logger.info if result == "registered" or restored else logger.debug
        log(f"[RequestFlagRegistry] 申请登记 adapter={self.adapter_id} kind={kind} user_id={user_id} result={result} event_time={event_time} details_restored={restored}")
        if result == "registered":
            self._remember(kind, flag, RequestFlagEntry(group_id=group_id, sub_type=sub_type, user_id=user_id,
                                                       received_at=moment, self_id=self_id))
            return True
        if result not in {"registered", "duplicate"}:
            logger.warning(f"[RequestFlagRegistry] 登记未生效 result={result} adapter={self.adapter_id} kind={kind}")
        return False

    @staticmethod
    def _public_request(row: dict[str, Any], entry: LedgerEntry | None, now: float) -> dict[str, Any]:
        """生成不含凭据的申请详情, 归档位置与执行资格分别计算"""
        keys = ("request_id", "kind", "user_id", "group_id", "sub_type", "comment", "received_at", "expires_at",
                "revision", "archived_at", "platform_state", "execution_state", "last_checked_at", "decision",
                "request_category", "nickname", "request_source", "suspicious_reason", "requested_at")
        state = entry["state"] if entry else "unknown"
        item = {key: row[key] for key in keys}
        item["archived"] = row["archived_at"] is not None or state != "available"
        item["remaining_seconds"] = max(0, int(row["expires_at"] - now))
        item["can_handle"] = bool(row["flag"]) and state in {"available", "expired"} and row["execution_state"] == "not_started" and row["platform_state"] not in {"processed", "invalid"}
        item["requires_confirmation"] = item["archived"] and item["can_handle"]
        item["handling_reason"] = "归档申请需重新核验或由管理员确认尝试" if item["requires_confirmation"] else "可处理" if item["can_handle"] else "凭据缺失, 已处理或执行结果未知"
        if state == "superseded":
            item["handling_reason"] = "同一处理凭据已有更新申请, 旧申请不能再处理"
        if state in {"executing", "unknown"}:
            item["execution_state"] = state
        return item

    async def list_requests(self, kind: str, *, self_id: str, group_id: str = "", limit: int = 20,
                            cursor: str | None = None, view: str = "active", owner_user_id: str = "", request_category: str = "all") -> dict[str, Any]:
        """
        返回当前账号近期或归档申请, 执行资格单独声明, 原始 flag 不进入结果

        参数:
        - kind: friend 或 group
        - self_id: 当前已绑定账号
        - group_id: 群申请限定目标, 好友申请留空
        - limit: 最多返回 1 到 100 条
        - cursor: 上次返回的下一页位置, 必须属于当前查询范围
        - view: active, archived 或 all, 默认 active
        - owner_user_id: 宿主指定的本人范围, 默认空值为管理范围
        - request_category: all, normal 或 suspicious, 默认 all

        返回:
        - 不透明 ID, 申请人, 验证信息和期限, 没有下一页时 has_more 为 False
        """
        if kind not in {"friend", "group"} or type(limit) is not int or not 1 <= limit <= 100 or view not in {"active", "archived", "all"} or request_category not in {"all", "normal", "suspicious"}:
            raise ValueError("申请类别或查询条数无效")
        def read() -> dict[str, Any]:
            """
            从账本复核后返回有限公开字段

            返回:
            - 当前查询页及完整数量, 缺失原值数量和下一页位置
            """
            now = time()
            rows = self.inbox.rows(self.adapter_id, self_id, kind, now)
            entries = self.ledger.available_entries(self.adapter_id, self_id, kind, now, include_history=True)
            items = []
            for row in rows:
                if group_id and row["group_id"] != group_id:
                    continue
                if owner_user_id and row["user_id"] != owner_user_id:
                    continue
                if request_category != "all" and row["request_category"] != request_category:
                    continue
                entry = entries.get(row["digest"])
                if entry is not None and entry["state"] != "available":
                    self.inbox.archive(row["request_id"], now, clear_credential=entry["state"] in {"completed", "unknown", "superseded"})
                    row = self.inbox.resolve(self.adapter_id, self_id, kind, row["request_id"], now)
                item = self._public_request(row, entry, now)
                if view == "active" and item["archived"] or view == "archived" and not item["archived"]:
                    continue
                items.append(item)
            present = {row["digest"] for row in rows}
            missing = sum(1 for digest, entry in entries.items() if digest not in present and entry["state"] in {"available", "expired"}
                          and (not group_id or entry["group_id"] == group_id)
                          and (not owner_user_id or entry["user_id"] == owner_user_id)
                          and (view == "all" or (entry["state"] == "available" and now < entry["received_at"] + self.ledger.ttl) == (view == "active")))
            total = len(items)
            if missing:
                logger.warning(f"[RequestInbox] 部分申请缺少原值, 不可处理 adapter={self.adapter_id} kind={kind} count={missing}")
            if cursor is not None:
                offset = next((index + 1 for index, item in enumerate(items) if item["request_id"] == cursor), None)
                if offset is None:
                    raise ValueError("申请翻页位置已失效或不属于当前范围, 请重新查询")
                items = items[offset:]
            more = len(items) > limit
            return {"items": items[:limit], "has_more": more, "total": total, "unavailable_count": missing if request_category == "all" else 0,
                    "unclassified_unavailable_count": missing if request_category != "all" else 0,
                    "scope": "self" if owner_user_id else "all",
                    "unavailable_reason": "历史申请详情缺失, 刷新不会自动恢复" if missing else None,
                    "next_cursor": items[limit - 1]["request_id"] if more else None}
        return await asyncio.to_thread(read)

    async def resolve_request(self, kind: str, request_id: str, *, self_id: str, allow_archived: bool = False,
                              expected_revision: int | None = None) -> dict[str, Any]:
        """
        在宿主中把当前账号申请 ID 换成平台原值, 再次核验执行资格

        参数:
        - kind: 期望申请类别
        - request_id: 受权限控制的查询返回值
        - self_id: 当前已绑定账号

        返回:
        - 宿主内部参数, 失效或未知时抛 LookupError
        """
        if not isinstance(request_id, str) or len(request_id) != 35 or not request_id.startswith("rq_"):
            raise ValueError("申请 ID 无效, 请从申请查询结果中选择")
        def resolve() -> dict[str, Any]:
            """
            用同一次时间读取收件箱和权威账本

            返回:
            - 通过账本复核的宿主内部申请参数
            """
            now = time()
            row = self.inbox.resolve(self.adapter_id, self_id, kind, request_id, now, expected_revision)
            entries = self.ledger.available_entries(self.adapter_id, self_id, kind, now, include_history=True)
            entry = entries.get(row["digest"])
            if entry is not None and entry["state"] == "superseded":
                raise LookupError("该申请已有更新记录, 旧申请不能处理新申请")
            if entry is None or entry["state"] not in {"available", "expired"} or row["execution_state"] != "not_started":
                raise LookupError("申请已处理或结果未知, 不能重复执行")
            if not row["flag"] or row["platform_state"] in {"processed", "invalid"}:
                raise LookupError("申请凭据缺失或平台申请已失效")
            if row["expires_at"] <= now and (not allow_archived or expected_revision is None):
                raise LookupError("申请已归档, 请查询当前修订号并由管理员确认处理")
            return row
        return await asyncio.to_thread(resolve)

    async def recheck_request(self, kind: str, request_id: str, *, self_id: str) -> dict[str, Any]:
        """复核本地资格, 不把缺少平台查询接口解释为仍然有效"""
        now = time()
        row = await asyncio.to_thread(self.inbox.resolve, self.adapter_id, self_id, kind, request_id, now)
        entries = await asyncio.to_thread(self.ledger.available_entries, self.adapter_id, self_id, kind, now, include_history=True)
        entry = entries.get(row["digest"])
        state = "processed" if entry and entry["state"] == "completed" and row["decision"] else row["platform_state"]
        await asyncio.to_thread(self.inbox.annotate, request_id, platform_state=state, now=now)
        row = await asyncio.to_thread(self.inbox.resolve, self.adapter_id, self_id, kind, request_id, now)
        item = self._public_request(row, entry, now)
        return {**item, "verification": "local_only", "platform_query_supported": False}

    async def delete_request(self, kind: str, request_id: str, *, self_id: str, expected_revision: int) -> None:
        """删除归档展示记录, 保留不可重放账本"""
        await asyncio.to_thread(self.inbox.delete, self.adapter_id, self_id, kind, request_id, expected_revision, time())

    async def occupy(
        self, kind: str, flag: str, *, self_id: str, group_id: str = "", sub_type: str = "", now: float | None = None,
        allow_archived: bool = False, identity_digest: str | None = None,
    ) -> RequestFlagEntry:
        """
        在持久账本中原子占用, 缓存不作为资格依据

        参数:
        - kind: group 或 friend
        - flag: 待核验标识
        - self_id: 已绑定机器人账号
        - group_id: 调用方声明的群号, 必须与登记一致
        - sub_type: 调用方声明的子类型, 必须与登记一致
        - now: 墙钟时间
        - allow_archived: 已明确确认归档处理
        - identity_digest: 收件箱固定的一次申请身份, None 沿用旧版规则

        返回:
        - RequestFlagEntry: 已占用的登记项

        异常:
        - LookupError: 未登记, 已过期, 已占用, 归属不符或账本降级
        """
        moment = time() if now is None else now
        entry = await self.ledger.occupy(
            self.adapter_id, self_id, kind, flag, group_id=group_id, sub_type=sub_type, now=moment, allow_archived=allow_archived, identity_digest=identity_digest,
        )
        self._remember(kind, flag, RequestFlagEntry(
            group_id=entry["group_id"], sub_type=entry["sub_type"], user_id=entry["user_id"],
            received_at=entry["received_at"], state="executing", self_id=self_id,
        ))
        return self._tables[kind][flag]

    async def settle(self, kind: str, flag: str, *, self_id: str, state: str, decision: str | None = None, identity_digest: str | None = None) -> None:
        """
        把已占用的登记迁入终态

        参数:
        - kind: group 或 friend
        - flag: 待迁移标识
        - self_id: 已绑定机器人账号
        - state: completed 或 unknown
        - decision: 实际动作结果, None 表示未确认同意或拒绝
        - identity_digest: 实际被占用的申请身份, 不根据凭据选择新记录
        """
        if state not in LEDGER_SETTLE_STATES:
            raise ValueError("终态必须为 completed 或 unknown")
        settled = await self.ledger.settle(self.adapter_id, self_id, kind, flag, state, identity_digest=identity_digest)
        entry = self._tables[kind].get(flag)
        if entry is not None and entry.self_id == self_id and settled:
            entry.state = state
        if settled:
            try:
                await asyncio.to_thread(self.inbox.forget, self.adapter_id, self_id, kind, flag, state=state, decision=decision, now=time(), identity_digest=identity_digest)
            except Exception:
                logger.error(f"[RequestInbox] 终态原值清理失败 adapter={self.adapter_id} kind={kind}: {traceback.format_exc()}")
        if not settled:
            logger.warning(
                f"[RequestFlagRegistry] 终态未落盘, 该标识保持不可重放 adapter={self.adapter_id} kind={kind} state={state}",
            )

    def cached_state(self, kind: str, flag: str) -> str | None:
        """读取近期缓存中的状态, 仅用于诊断与测试观察"""
        entry = self._tables[kind].get(flag)
        return entry.state if entry is not None else None
