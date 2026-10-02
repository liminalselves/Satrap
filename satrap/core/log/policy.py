"""
日志保留策略和后台维护

策略独立于后端业务配置, 使用原子保存和版本校验,
维护线程在空闲时轮转文件并按小时协调清理, 失败由独立通道隔离
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Any
import threading
import traceback
import hashlib
import json
import time
import os

from satrap.core.log.managed import ManagedDailyHandler, LogFileLock, atomic_json, cleanup_logs, log_root, report_failure


class LoggingPolicyConflict(ValueError):
    """策略已被其它客户端修改, 当前草稿必须重新核对"""


@dataclass(frozen=True)
class LoggingPolicy:
    """默认保留包含当天的最近 30 个自然日"""

    enabled: bool = True
    retention_days: int = 30


def validate_policy(payload: object) -> LoggingPolicy:
    """
    校验独立日志策略

    参数:
    - payload: 包含 enabled 和 retention_days 的对象

    返回:
    - 完整的策略, 无效字段抛出 ValueError 由请求或维护边界捕获
    """
    if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
        raise ValueError("自动清理开关必须为布尔值")
    days = payload.get("retention_days")
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("保留天数必须为 1 至 3650 的整数")
    return LoggingPolicy(payload["enabled"], days)


class LoggingPolicyStore:
    """独立的日志配置和清理结果管理, 不导入依赖 logger 的配置模块"""

    def __init__(self, path: Path | None = None, root: Path | None = None):
        """
        初始化日志管理路径

        参数:
        - path: 配置路径, 默认 SATRAP_LOG_CONFIG 或项目 .satrap/logging.json
        - root: 日志路径, 默认稳定的项目日志目录
        """
        self.path = (path or Path(os.getenv("SATRAP_LOG_CONFIG") or Path(__file__).resolve().parents[3] / ".satrap" / "logging.json")).resolve()
        self.root = (root or log_root()).resolve()

    def read(self) -> tuple[LoggingPolicy, str]:
        """
        读取完整策略和内容版本

        返回:
        - 策略及版本, 缺少配置时返回默认策略; 损坏配置由调用边界处理
        """
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return LoggingPolicy(), "missing"
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("日志配置版本无效")
        return validate_policy(payload), hashlib.sha256(content).hexdigest()

    def _lock(self) -> LogFileLock:
        """
        获取独立策略锁对象

        返回:
        - 未获取的文件锁, 统一锁顺序为策略锁再日志目录锁
        """
        return LogFileLock(self.path.with_name(self.path.name + ".lock"))

    def save(self, payload: object, expected_revision: object) -> dict[str, Any]:
        """
        在版本一致时原子保存策略

        参数:
        - payload: 待保存策略
        - expected_revision: 客户端读取到的字符串版本

        返回:
        - 保存后的完整快照, 冲突和写入错误交给请求边界记录
        """
        policy = validate_policy(payload)
        if not isinstance(expected_revision, str):
            raise ValueError("保存日志策略必须提供读取版本")
        lock = self._lock()
        if not lock.acquire(1):
            raise TimeoutError("日志策略正在被其它操作使用")
        try:
            _, revision = self.read()
            if revision != expected_revision:
                raise LoggingPolicyConflict("日志策略已更新, 请刷新后核对草稿")
            atomic_json(self.path, {"version": 1, **asdict(policy)})
            return self.snapshot()
        finally:
            lock.release()

    def last_result(self) -> dict[str, Any] | None:
        """
        读取最近清理结果

        返回:
        - 已保存的清理结果或 None, 无效记录抛出错误由调用边界报告
        """
        try:
            value = json.loads((self.root / ".cleanup-result.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or type(value.get("created_at")) not in (int, float):
            raise ValueError("日志清理结果格式无效")
        for field in ("deleted", "skipped", "errors"):
            if not isinstance(value.get(field), list):
                raise ValueError("日志清理结果列表无效")
        return value

    def snapshot(self) -> dict[str, Any]:
        """
        获取策略和最近清理状态

        返回:
        - 配置, 内容版本, 目录和最近结果; 状态读取失败明确标注
        """
        policy, revision = self.read()
        status_error = None
        try:
            result = self.last_result()
        except Exception as error:
            report_failure(f"清理结果读取失败: {error}\n{traceback.format_exc()}")
            result, status_error = None, str(error)
        maintenance_error = None
        try:
            maintenance_error = json.loads((self.root / ".cleanup-error.json").read_text(encoding="utf-8"))
            if not isinstance(maintenance_error, dict):
                raise ValueError("维护失败记录格式无效")
        except FileNotFoundError:
            maintenance_error = None
        except Exception as error:
            report_failure(f"维护状态读取失败: {error}\n{traceback.format_exc()}")
            maintenance_error, status_error = None, str(error)
        return {"ok": True, "policy": asdict(policy), "revision": revision, "directory": str(self.root), "config_path": str(self.path), "last_cleanup": result, "maintenance_error": maintenance_error, "status_error": status_error}

    def record_failure(self, error: Exception) -> None:
        """
        尽力保存最近维护失败, 写入失败再次走独立诊断通道

        参数:
        - error: 已由调用边界捕获并记录的清理故障
        """
        try:
            atomic_json(self.root / ".cleanup-error.json", {"created_at": time.time(), "error": str(error)})
        except Exception as write_error:
            report_failure(f"维护失败状态无法保存: {write_error}\n{traceback.format_exc()}")

    def cleanup(self, *, automatic: bool = False, expected_revision: object = None, now: float | None = None) -> dict[str, Any] | None:
        """
        按当前已保存策略执行清理, 多进程自动清理共用执行时间

        参数:
        - automatic: 自动调用时尊重开关和一小时执行间隔
        - expected_revision: 手动请求必须提供的已保存版本
        - now: 可选调度时间, 测试使用固定时钟

        返回:
        - 清理结果, 自动模式关闭或尚未到期时返回 None; 失败交给请求或线程边界记录
        """
        current = time.time() if now is None else now
        lock = self._lock()
        if not lock.acquire(1):
            raise TimeoutError("日志策略正在被其它操作使用")
        try:
            policy, revision = self.read()
            if automatic:
                if not policy.enabled:
                    return None
                try:
                    previous = self.last_result()
                except Exception as error:
                    report_failure(f"清理调度记录读取失败: {error}\n{traceback.format_exc()}")
                    previous = None
                if previous and previous.get("policy_revision") == revision and 0 <= current - previous["created_at"] < 3600:
                    return None
            elif expected_revision != revision or not isinstance(expected_revision, str):
                raise LoggingPolicyConflict("日志策略已更新, 请刷新后再清理")
            result = cleanup_logs(self.root, policy.retention_days, policy_revision=revision, created_at=current)
            (self.root / ".cleanup-error.json").unlink(missing_ok=True)
            return result
        finally:
            lock.release()


class LogMaintenance:
    """在无日志消息时也检查日期, 热读取策略并定时清理"""

    def __init__(self, handler: ManagedDailyHandler, store: LoggingPolicyStore, *, interval: float = 10, clock: Callable[[], float] = time.time):
        """
        初始化可停止的维护线程

        参数:
        - handler: 当前进程文件处理器
        - store: 独立日志策略存储
        - interval: 日期和配置检查秒数, 默认 10 秒
        - clock: 调度时钟, 默认墙钟
        """
        self.handler, self.store = handler, store
        self.interval, self.clock = interval, clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="satrap-log-maintenance", daemon=True)
        self._last_error: tuple[str, float] | None = None

    def _failure(self, message: str) -> None:
        """
        限频报告持续故障, 完整错误保留到诊断流

        参数:
        - message: 失败操作和堆栈
        """
        now = time.monotonic()
        if self._last_error is None or self._last_error[0] != message or now - self._last_error[1] >= 60:
            self._last_error = message, now
            report_failure(message)

    def step(self) -> None:
        """隔离文件切换与清理失败, 让后续维护周期继续执行"""
        try:
            self.handler.tick()
        except Exception as error:
            self._failure(f"空闲日志轮转失败: {error}\n{traceback.format_exc()}")
        try:
            self.store.cleanup(automatic=True, now=self.clock())
        except Exception as error:
            self._failure(f"自动日志清理失败: {error}\n{traceback.format_exc()}")
            self.store.record_failure(error)

    def _run(self) -> None:
        """线程边界捕获维护故障, 启动后立即检查并可中断等待"""
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as error:
                self._failure(f"日志维护线程失败: {error}\n{traceback.format_exc()}")
            self._stop.wait(self.interval)

    def start(self) -> None:
        """启动一次维护线程, 启动失败保持业务日志可用"""
        try:
            self._thread.start()
        except Exception as error:
            self._failure(f"日志维护启动失败: {error}\n{traceback.format_exc()}")

    def close(self) -> None:
        """停止维护并等待正在执行的操作结束"""
        self._stop.set()
        if self._thread.ident is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self._failure("日志维护尚未退出, 文件处理器将保持关闭状态")
