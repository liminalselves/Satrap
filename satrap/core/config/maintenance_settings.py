"""
回收区维护设置与 Control 唯一清理任务

默认不自动永久删除, 显式开启后仅沿原维护服务处理超过保留期的回收包
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import TypedDict, cast

from satrap.core.config.document import ConfigRevisionConflict
from satrap.core.config_paths import get_config_path
from satrap.core.log import logger
from satrap.core.log.managed import LogFileLock
from satrap.core.storage import StorageLayout, StorageMaintenanceService
from satrap.core.storage.file_lock import FileLock
from satrap.core.storage.persist import atomic_write_json
from satrap.core.utils.async_worker import BoundedAsyncWorker


class MaintenanceSnapshot(TypedDict):
    """维护设置的公开完整快照"""

    trash_retention_days: int
    auto_purge_enabled: bool
    revision: str


@dataclass(frozen=True)
class MaintenancePolicy:
    """管理员明确保存的永久回收策略"""

    trash_retention_days: int = 30
    auto_purge_enabled: bool = False

    @classmethod
    def parse(cls, value: object) -> MaintenancePolicy:
        """
        严格解析设置, 不把错误的开启值当作允许清理

        参数:
        - value: 仅包含两个固定策略字段的对象

        返回:
        - 已验证策略, 未知字段或非法值明确拒绝
        """
        if not isinstance(value, dict):
            raise ValueError("维护设置必须为对象")
        value = cast(dict[str, object], value)
        if set(value) != {"trash_retention_days", "auto_purge_enabled"}:
            raise ValueError("必须提供 trash_retention_days 和 auto_purge_enabled, 不接受未知字段")
        days, enabled = value["trash_retention_days"], value["auto_purge_enabled"]
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3660 or not isinstance(enabled, bool):
            raise ValueError("保留期必须为 1 到 3660 天的整数, 自动清理必须为布尔值")
        return cls(days, enabled)


class MaintenanceSettingsStore:
    """原子保存维护设置, 读取默认值不创建文件"""

    def __init__(self, path: Path | None = None) -> None:
        """
        绑定宿主配置文件

        参数:
        - path: 可选的测试或宿主指定路径
        """
        self.path = (path or get_config_path("storage-maintenance.json")).resolve()

    def read(self) -> MaintenanceSnapshot:
        """
        读取设置及其真实内容修订

        返回:
        - 缺失时为默认关闭且 revision=missing, 损坏时抛出错误
        """
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return MaintenanceSnapshot(trash_retention_days=30, auto_purge_enabled=False, revision="missing")
        if len(content) > 65536:
            raise ValueError("维护设置超过大小限制")
        policy = MaintenancePolicy.parse(json.loads(content))
        return MaintenanceSnapshot(trash_retention_days=policy.trash_retention_days, auto_purge_enabled=policy.auto_purge_enabled,
                                   revision=hashlib.sha256(content).hexdigest())

    def save(self, payload: object) -> MaintenanceSnapshot:
        """
        核验修订后整体保存策略

        参数:
        - payload: 两个策略字段与 expected_revision

        返回:
        - 新设置与修订, 冲突时保持文件不变
        """
        if not isinstance(payload, dict):
            raise ValueError("维护设置必须为对象")
        payload = cast(dict[str, object], payload)
        if set(payload) != {"trash_retention_days", "auto_purge_enabled", "expected_revision"}:
            raise ValueError("维护设置必须包含两个策略字段和 expected_revision")
        expected = payload["expected_revision"]
        if not isinstance(expected, str) or not expected:
            raise ValueError("expected_revision 必须是最近读取的修订")
        policy = MaintenancePolicy.parse({key: value for key, value in payload.items() if key != "expected_revision"})
        with FileLock(self.path.with_suffix(".json.lock"), timeout=1):
            if self.read()["revision"] != expected:
                raise ConfigRevisionConflict("维护设置已变化, 请刷新后重试")
            atomic_write_json(self.path, asdict(policy), indent=2)
            return self.read()


class MaintenanceRunner:
    """仅由 Control 启动的低频永久清理器, 同一数据根最多一个持有者"""

    def __init__(self, layout: StorageLayout, *, store: MaintenanceSettingsStore | None = None, interval: float = 300,
                 layout_is_current: Callable[[], bool] | None = None) -> None:
        """
        创建尚未启动的清理器

        参数:
        - layout: 当前服务固定的数据根
        - store: 可选设置来源
        - interval: 周期间隔秒数, 默认五分钟
        - layout_is_current: 工作线程内核验当前配置仍指向该根目录, 默认固定宿主范围
        """
        self.layout = layout
        self.store = store or MaintenanceSettingsStore()
        self.interval = interval
        self.layout_is_current = layout_is_current or (lambda: True)
        self._worker = BoundedAsyncWorker("storage-maintenance", 1, 1)
        self._lease = LogFileLock(layout.root / "maintenance" / "control-purge.lock")
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> bool:
        """
        获取跨进程租约后启动唯一后台循环

        返回:
        - 本进程持有清理任务时为 True, 其他持有者存在时为 False
        """
        if self._task is not None:
            return True
        if not await asyncio.to_thread(self._lease.acquire):
            return False
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="storage-auto-purge")
        return True

    def cycle(self) -> None:
        """在线程中重读生效设置, 默认关闭时不扫描任何回收包"""
        policy = self.store.read()
        if not policy["auto_purge_enabled"]:
            return
        if not self.layout_is_current():
            logger.warning("[回收区] 当前数据根已变化, 本轮不清理旧目录")
            return
        results = StorageMaintenanceService(self.layout).purge_archives(older_than_days=policy["trash_retention_days"])
        removed = sum(item.get("ok") is True for item in results)
        failed = sum(item.get("ok") is not True for item in results)
        if removed:
            logger.info(f"[回收区] 自动清理完成, 已永久删除 {removed} 项")
        if failed:
            logger.warning(f"[回收区] {failed} 项未清理, 请查看维护目录或稍后重试")

    async def _run(self) -> None:
        """周期结束或显式停止后释放租约, 异常周期不静默开启或重写策略"""
        try:
            while not self._stop.is_set():
                try:
                    await self._worker.run(self.cycle)
                except Exception as error:
                    logger.error(f"[回收区] 自动维护周期失败, type={type(error).__name__}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except TimeoutError:
                    continue
        finally:
            try:
                await asyncio.to_thread(self._worker.close)
            finally:
                await asyncio.to_thread(self._lease.release)

    async def close(self) -> None:
        """等待在途清理结束, 不在工作线程仍使用回收包时释放拥有权"""
        self._stop.set()
        if self._task is not None:
            await asyncio.shield(self._task)
            self._task = None
        else:
            await asyncio.to_thread(self._worker.close)
