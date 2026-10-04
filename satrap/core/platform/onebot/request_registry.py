"""request 事件 flag 的审批身份账本与近期操作缓存

群请求与好友请求分域登记; 审批占用在持久事务中原子完成, 落盘成功才允许发网络动作;
已消费, 未知与过期身份不因重复入站, 容量轮转或进程重启回到可审批状态;
近期缓存只用于跳过同进程内重复入站的磁盘访问, 不作为审批资格依据
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast
from time import time
import asyncio
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
LEDGER_STATES = frozenset({"available", "executing", "completed", "unknown", "expired"})
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
    """账本键: 适配器 ID, 已绑定账号, 请求类别与 flag 摘要"""
    return f"{adapter_id}\n{self_id}\n{kind}\n{digest}"


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
        *, group_id: str, sub_type: str, user_id: str, now: float,
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
            digest = flag_digest(kind, self_id, flag)
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
            self._save_locked()
            return "registered"

    def _occupy_sync(
        self, adapter_id: str, self_id: str, kind: str, flag: str,
        *, group_id: str, sub_type: str, now: float,
    ) -> LedgerEntry:
        """同步占用实现, 取消调用方不会撤销已经落盘的占用"""
        with self._mutex, self._transaction():
            if self.degraded:
                raise LookupError("审批账本不可用, 拒绝执行")
            self._reload_locked()
            if self.degraded:
                raise LookupError("审批账本不可用, 拒绝执行")
            key = _entry_key(adapter_id, self_id, kind, flag_digest(kind, self_id, flag))
            entry = self._entries.get(key)
            if entry is None:
                raise LookupError("请求标识未登记或已过期, 无法确认归属")
            if entry["group_id"] != group_id or entry["sub_type"] != sub_type:
                raise LookupError("请求标识归属与参数不符")
            if entry["state"] != "available":
                raise LookupError("请求标识已被处理或结果未知, 拒绝重复执行")
            if now - entry["received_at"] >= self._ttl:
                entry["state"] = "expired"
                entry["updated_at"] = now
                self._save_locked()
                raise LookupError("请求标识已超过可审批时限, 拒绝执行")
            entry["state"] = "executing"
            entry["updated_at"] = now
            self._save_locked()
            return cast(LedgerEntry, dict(entry))

    def _settle_sync(self, adapter_id: str, self_id: str, kind: str, flag: str, state: str) -> bool:
        """同步终态实现, 只允许 executing 迁入明确终态"""
        with self._mutex, self._transaction():
            if self.degraded:
                return False
            self._reload_locked()
            if self.degraded:
                return False
            key = _entry_key(adapter_id, self_id, kind, flag_digest(kind, self_id, flag))
            entry = self._entries.get(key)
            if entry is None or entry["state"] != "executing":
                return False
            entry["state"] = state
            entry["updated_at"] = time()
            self._save_locked()
            return True

    async def register(
        self, adapter_id: str, self_id: str, kind: str, flag: str,
        *, group_id: str = "", sub_type: str = "", user_id: str = "", now: float | None = None,
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

        返回:
        - str: registered/duplicate/conflict/capacity/degraded/unknown_account
        """
        moment = time() if now is None else now
        return await asyncio.to_thread(
            self._register_sync, adapter_id, self_id, kind, flag,
            group_id=group_id, sub_type=sub_type, user_id=user_id, now=moment,
        )

    async def occupy(
        self, adapter_id: str, self_id: str, kind: str, flag: str,
        *, group_id: str = "", sub_type: str = "", now: float | None = None,
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

        返回:
        - LedgerEntry: 已占用的登记项

        异常:
        - LookupError: 未登记, 已过期, 已占用, 归属不符或账本降级, message 为用户可读原因
        """
        moment = time() if now is None else now
        return await asyncio.to_thread(
            self._occupy_sync, adapter_id, self_id, kind, flag,
            group_id=group_id, sub_type=sub_type, now=moment,
        )

    async def settle(self, adapter_id: str, self_id: str, kind: str, flag: str, state: str) -> bool:
        """
        把已占用的登记迁入明确终态

        参数:
        - adapter_id: 平台实例 ID
        - self_id: 已绑定机器人账号
        - kind: group 或 friend
        - flag: 待迁移标识
        - state: completed (动作已有明确结果) 或 unknown (超时/取消/传输异常, 不可重试)

        返回:
        - bool: 迁移并落盘成功为 True
        """
        if state not in LEDGER_SETTLE_STATES:
            raise ValueError("终态必须为 completed 或 unknown")
        return await asyncio.to_thread(self._settle_sync, adapter_id, self_id, kind, flag, state)

    def lookup(self, adapter_id: str, self_id: str, kind: str, flag: str) -> LedgerEntry | None:
        """查询登记项副本, 供诊断与测试观察"""
        with self._mutex:
            if self.degraded:
                return None
            key = _entry_key(adapter_id, self_id, kind, flag_digest(kind, self_id, flag))
            entry = self._entries.get(key)
            return cast(LedgerEntry, dict(entry)) if entry is not None else None

    def available_entries(self, adapter_id: str, self_id: str, kind: str, now: float) -> dict[str, LedgerEntry]:
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
                    and entry["state"] == "available" and now < entry["received_at"] + self.ttl}

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

    def _cached(self, kind: str, flag: str, now: float) -> RequestFlagEntry | None:
        """读取未过期的近期缓存, 只用于跳过重复入站的磁盘访问"""
        entry = self._tables[kind].get(flag)
        if entry is None or now - entry.received_at >= self.ttl:
            return None
        return entry

    def _remember(self, kind: str, flag: str, entry: RequestFlagEntry) -> None:
        """写入近期缓存并按容量淘汰最旧"""
        table = self._tables[kind]
        table[flag] = entry
        table.move_to_end(flag)
        while len(table) > self.limit:
            table.popitem(last=False)

    async def register(
        self, kind: str, flag: str, *,
        self_id: str, group_id: str = "", sub_type: str = "", user_id: str = "", now: float | None = None, comment: str = "",
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

        返回:
        - bool: 账本新增登记为 True; 重复, 冲突, 容量或降级时为 False
        """
        moment = time() if now is None else now
        cached = self._cached(kind, flag, moment)
        if (cached is not None and cached.self_id == self_id and cached.group_id == group_id
                and cached.sub_type == sub_type and cached.user_id == user_id):
            # 同进程内已登记且归属一致: 无需落盘, 状态保持不变
            return False
        result = await self.ledger.register(
            self.adapter_id, self_id, kind, flag,
            group_id=group_id, sub_type=sub_type, user_id=user_id, now=moment,
        )
        if result in {"registered", "duplicate"}:
            entry = await asyncio.to_thread(self.ledger.lookup, self.adapter_id, self_id, kind, flag)
            if entry is not None and entry["state"] == "available" and moment < entry["received_at"] + self.ledger.ttl:
                await asyncio.to_thread(self.inbox.register, self.adapter_id, self_id, kind, flag,
                                        group_id=entry["group_id"], sub_type=entry["sub_type"], user_id=entry["user_id"],
                                        comment=comment, received_at=entry["received_at"], now=moment)
        if result == "registered":
            self._remember(kind, flag, RequestFlagEntry(group_id=group_id, sub_type=sub_type, user_id=user_id,
                                                       received_at=moment, self_id=self_id))
            return True
        if result in {"capacity", "degraded"}:
            logger.warning(f"[RequestFlagRegistry] 登记未生效 result={result} adapter={self.adapter_id} kind={kind}")
        return False

    async def list_requests(self, kind: str, *, self_id: str, group_id: str = "", limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        """
        返回当前账号可执行的申请, 原始 flag 不进入结果

        参数:
        - kind: friend 或 group
        - self_id: 当前已绑定账号
        - group_id: 群申请限定目标, 好友申请留空
        - limit: 最多返回 1 到 100 条
        - cursor: 上次返回的下一页位置, 必须属于当前查询范围

        返回:
        - 不透明 ID, 申请人, 验证信息和期限, 没有下一页时 has_more 为 False
        """
        if kind not in {"friend", "group"} or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("申请类别或查询条数无效")
        def read() -> dict[str, Any]:
            """
            从账本复核后返回有限公开字段

            返回:
            - 当前查询页及完整数量, 缺失原值数量和下一页位置
            """
            now = time()
            rows = self.inbox.rows(self.adapter_id, self_id, kind, now)
            available = self.ledger.available_entries(self.adapter_id, self_id, kind, now)
            self.inbox.discard([row["request_id"] for row in rows if row["digest"] not in available])
            keys = ("request_id", "kind", "user_id", "group_id", "sub_type", "comment", "received_at", "expires_at")
            items = [{key: row[key] for key in keys} for row in rows if row["digest"] in available
                     and (not group_id or row["group_id"] == group_id)]
            for item in items:
                item["remaining_seconds"] = max(0, int(item["expires_at"] - now))
            present = {row["digest"] for row in rows}
            missing = sum(1 for digest, entry in available.items() if digest not in present
                          and (not group_id or entry["group_id"] == group_id))
            total = len(items)
            if missing:
                logger.warning(f"[RequestInbox] 部分申请缺少原值, 不可处理 adapter={self.adapter_id} kind={kind} count={missing}")
            if cursor is not None:
                offset = next((index + 1 for index, item in enumerate(items) if item["request_id"] == cursor), None)
                if offset is None:
                    raise ValueError("申请翻页位置已失效或不属于当前范围, 请重新查询")
                items = items[offset:]
            more = len(items) > limit
            return {"items": items[:limit], "has_more": more, "total": total, "unavailable_count": missing,
                    "next_cursor": items[limit - 1]["request_id"] if more else None}
        return await asyncio.to_thread(read)

    async def resolve_request(self, kind: str, request_id: str, *, self_id: str) -> dict[str, Any]:
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
            row = self.inbox.resolve(self.adapter_id, self_id, kind, request_id, now)
            available = self.ledger.available_entries(self.adapter_id, self_id, kind, now)
            if row["digest"] not in available:
                self.inbox.discard([request_id])
                raise LookupError("申请已处理或结果未知, 不能重复执行")
            return row
        return await asyncio.to_thread(resolve)

    async def occupy(
        self, kind: str, flag: str, *, self_id: str, group_id: str = "", sub_type: str = "", now: float | None = None,
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

        返回:
        - RequestFlagEntry: 已占用的登记项

        异常:
        - LookupError: 未登记, 已过期, 已占用, 归属不符或账本降级
        """
        moment = time() if now is None else now
        entry = await self.ledger.occupy(
            self.adapter_id, self_id, kind, flag, group_id=group_id, sub_type=sub_type, now=moment,
        )
        self._remember(kind, flag, RequestFlagEntry(
            group_id=entry["group_id"], sub_type=entry["sub_type"], user_id=entry["user_id"],
            received_at=entry["received_at"], state="executing", self_id=self_id,
        ))
        return self._tables[kind][flag]

    async def settle(self, kind: str, flag: str, *, self_id: str, state: str) -> None:
        """
        把已占用的登记迁入终态

        参数:
        - kind: group 或 friend
        - flag: 待迁移标识
        - self_id: 已绑定机器人账号
        - state: completed 或 unknown
        """
        if state not in LEDGER_SETTLE_STATES:
            raise ValueError("终态必须为 completed 或 unknown")
        settled = await self.ledger.settle(self.adapter_id, self_id, kind, flag, state)
        entry = self._tables[kind].get(flag)
        if entry is not None and entry.self_id == self_id and settled:
            entry.state = state
        if settled:
            try:
                await asyncio.to_thread(self.inbox.forget, self.adapter_id, self_id, kind, flag)
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
