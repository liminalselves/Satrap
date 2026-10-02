"""
按进程和日期隔离的日志文件管理

文件切换和清理共用跨进程锁, 活动文件由操作系统租约保护,
失败通过独立诊断通道报告, 不反向调用文件日志
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Any, BinaryIO, TextIO
import threading
import traceback
import logging
import secrets
import ctypes
import json
import time
import sys
import os
import re

from satrap.core.log.stream import standard_log_stream


if os.name == "nt":
    import msvcrt
else:
    import fcntl

_MANAGED = re.compile(r"^SATRAP-([a-z0-9_-]+)-(\d{8})-(\d+-[a-f0-9]{16})\.log$")
_LEGACY = re.compile(r"^SATRAP-(\d{8})\.log$")
_RUNTIME = re.compile(r"^runtime-(\d+-[a-f0-9]{16})\.lock$")


def report_failure(message: str) -> None:
    """
    向原始标准错误和实时流报告日志失败, 避免递归写入故障文件

    参数:
    - message: 操作, 原因和必要的堆栈
    """
    content = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [ERROR]: [日志管理] {message}"
    try:
        stream = sys.__stderr__
        if stream is not None:
            stream.write(content + "\n")
            stream.flush()
    except Exception as error:
        content += f"\n[日志管理] 原始标准错误无法写入: {error}"
    try:
        for line in content.splitlines():
            standard_log_stream.publish(line, "ERROR")
    except Exception as error:
        try:
            os.write(2, f"{content}\n[日志管理] 实时流报告失败: {error}\n".encode("utf-8"))
        except OSError:
            return   # 全部诊断通道不可用时仍隔离日志故障, 此处不能递归报告


class LogFileLock:
    """独立的操作系统锁, 不导入依赖 logger 的存储模块"""

    def __init__(self, path: Path):
        """
        初始化锁路径

        参数:
        - path: 日志管理目录内的锁文件
        """
        self.path = path
        self.file: BinaryIO | None = None

    def acquire(self, timeout: float = 0) -> bool:
        """
        获取进程锁, 不把锁文件存在视为仍有持有者

        参数:
        - timeout: 等待秒数, 默认立即返回

        返回:
        - 已获取时返回 True, 有其它持有者时返回 False; 文件系统错误由调用边界处理
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.file = handle
                return True
            except OSError as error:
                if error.errno not in {11, 13, 35, 36} and getattr(error, "winerror", None) not in {32, 33}:
                    handle.close()
                    raise
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.01)

    def release(self) -> None:
        """释放锁和句柄, 进程异常退出时操作系统也会释放"""
        handle, self.file = self.file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def management_lock(root: Path, timeout: float = 1) -> Iterator[None]:
    """
    串行化文件切换和清理, 防止活动文件登记与删除竞态

    参数:
    - root: 日志根目录
    - timeout: 等待秒数

    返回:
    - 持有锁的上下文, 超时抛出 TimeoutError 由业务边界记录
    """
    lock = LogFileLock(root / ".locks" / "maintenance.lock")
    if not lock.acquire(timeout):
        raise TimeoutError("日志目录正在执行其它管理操作")
    try:
        yield
    finally:
        lock.release()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """
    原子替换日志策略或运行状态文件

    参数:
    - path: 管理目录内的目标文件
    - payload: JSON 对象
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def log_root() -> Path:
    """
    获取稳定的日志目录, 不依赖进程工作目录

    返回:
    - 环境覆盖或项目数据目录下的 logs
    """
    return Path(os.getenv("SATRAP_LOG_ROOT") or Path(__file__).resolve().parents[3] / ".satrap" / "logs").resolve()
    # 启动阶段直接推导路径, 避免 utils 包导入 logger 造成循环依赖


def active_runtimes(root: Path, *, prune: bool = False) -> list[dict[str, Any]]:
    """
    通过操作系统租约读取活动日志进程, 不使用 PID 或心跳超时猜测

    参数:
    - root: 日志根目录, 调用方持有管理锁
    - prune: 是否清理已释放的独立进程登记, 默认不清理

    返回:
    - 活动进程状态, 状态文件损坏时标明未知并保护该进程全部文件
    """
    result = []
    for path in (root / ".locks").glob("runtime-*.lock"):
        match = _RUNTIME.fullmatch(path.name)
        if not match or path.is_symlink():
            continue
        identity = match[1]
        lock = LogFileLock(path)
        if lock.acquire():
            lock.release()
            if prune:
                state_path = root / ".runtime" / f"{identity}.json"
                if not state_path.is_symlink() and state_path.resolve().parent == (root / ".runtime").resolve():
                    state_path.unlink(missing_ok=True)
                if path.resolve().parent == (root / ".locks").resolve():
                    path.unlink(missing_ok=True)
            continue
        try:
            state = json.loads((root / ".runtime" / f"{identity}.json").read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("id") != identity:
                raise ValueError("进程状态格式无效")
            current_file = state.get("current_file")
            if current_file is not None and (not isinstance(current_file, str) or Path(current_file).name != current_file):
                raise ValueError("活动文件名无效")
            result.append(state)
        except (OSError, ValueError, TypeError) as error:
            report_failure(f"进程状态读取失败: {identity}, {error}")
            result.append({"id": identity, "unknown": True, "error": str(error)})
    return result


def _legacy_delete(path: Path) -> str:
    """
    仅在能独占旧版文件句柄时删除, 不依赖旧版进程配合租约

    参数:
    - path: 已验证名称和根目录的旧版日志

    返回:
    - deleted, active 或 unverified; 不可确认占用的平台保留文件, 其它错误由清理边界记录
    """
    if os.name != "nt":
        return "unverified"
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    dispose = kernel.SetFileInformationByHandle
    dispose.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    dispose.restype = wintypes.BOOL
    handle = create(str(path), 0x00010000, 0, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in {32, 33}:
            return "active"
        raise ctypes.WinError(error)
    try:
        class Disposition(ctypes.Structure):
            _fields_ = [("delete", wintypes.BOOL)]

        information = Disposition(True)
        if not dispose(handle, 4, ctypes.byref(information), ctypes.sizeof(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        return "deleted"
    finally:
        close(handle)


def cleanup_logs(root: Path, retention_days: int, *, today: date | None = None) -> dict[str, Any]:
    """
    清理保留范围外的受管日志, 所有单文件失败均记录并继续

    参数:
    - root: 日志根目录
    - retention_days: 包含当天的保留自然日数量
    - today: 可选日期, 默认本地当天; 测试使用固定日期

    返回:
    - 删除, 跳过及失败列表; 目录锁和结果写入失败由调用边界捕获
    """
    if type(retention_days) is not int or not 1 <= retention_days <= 3650:
        raise ValueError("保留天数必须为 1 至 3650 的整数")
    root = root.resolve()
    current = today or date.today()
    cutoff = current - timedelta(days=retention_days - 1)
    result: dict[str, Any] = {"created_at": time.time(), "retention_days": retention_days, "cutoff": cutoff.isoformat(), "deleted": [], "skipped": [], "errors": []}
    with management_lock(root):
        runtimes = active_runtimes(root, prune=True)
        protected = {item.get("current_file") for item in runtimes}
        unknown = {item["id"] for item in runtimes if item.get("unknown")}
        for path in root.glob("*.log"):
            managed, legacy = _MANAGED.fullmatch(path.name), _LEGACY.fullmatch(path.name)
            if not managed and not legacy:
                continue
            try:
                if path.is_symlink() or not path.is_file() or path.resolve().parent != root.resolve():
                    result["skipped"].append({"file": path.name, "reason": "路径或文件类型不属于受管日志"})
                    continue
                matched = managed or legacy
                assert matched is not None
                day = datetime.strptime(matched[2] if managed else matched[1], "%Y%m%d").date()
                if day >= cutoff:
                    continue
                if path.name in protected or (managed and managed[3] in unknown):
                    result["skipped"].append({"file": path.name, "reason": "进程仍在使用或无法确认活动状态"})
                    continue
                if legacy:
                    status = _legacy_delete(path)
                    if status != "deleted":
                        result["skipped"].append({"file": path.name, "reason": "旧日志正在使用" if status == "active" else "无法确认旧版进程占用, 保留文件"})
                        continue
                else:
                    path.unlink()
                result["deleted"].append(path.name)
            except Exception as error:
                report_failure(f"清理失败: {path.name}, {error}\n{traceback.format_exc()}")
                result["errors"].append({"file": path.name, "reason": str(error)})
        atomic_json(root / ".cleanup-result.json", result)
    return result


class ManagedDailyHandler(logging.Handler):
    """按日期及进程实例独立写入日志, 空闲时也可关闭旧日期文件"""

    def __init__(self, root: Path, service: str = "process", *, clock: Callable[[], date] = date.today, file_path: Path | None = None):
        """
        初始化租约和文件状态, 失败保持控制台日志可用

        参数:
        - root: 日志目录
        - service: 可扩展的服务标签
        - clock: 日期来源, 默认本地日期
        - file_path: 显式指定的兼容文件路径, 默认使用独立的每日文件
        """
        super().__init__()
        self.root = root.resolve()
        self.identity = f"{os.getpid()}-{secrets.token_hex(8)}"
        self.service = re.sub(r"[^a-z0-9_-]", "-", service.lower())[:40] or "process"
        self.clock = clock
        self.stream: TextIO | None = None
        self.file_path = file_path.resolve() if file_path else None
        self._io_lock = threading.RLock()
        self.current_file: Path | None = None
        self.current_day: date | None = None
        self.stopped = False
        self.state: dict[str, Any] = {"id": self.identity, "pid": os.getpid(), "service": self.service, "current_file": None}
        self.lease = LogFileLock(self.root / ".locks" / f"runtime-{self.identity}.lock")
        self._last_error: tuple[str, float] | None = None
        try:
            with management_lock(self.root):
                if not self.lease.acquire():
                    raise RuntimeError("日志进程租约冲突")
                self._publish_state()
        except Exception as error:
            self._failure(f"初始化失败: {error}\n{traceback.format_exc()}")

    def _failure(self, message: str) -> None:
        """
        限频报告重复的日志故障, 不让单次失败递归或刷屏

        参数:
        - message: 故障内容
        """
        now = time.monotonic()
        if self._last_error is None or self._last_error[0] != message or now - self._last_error[1] >= 60:
            self._last_error = message, now
            report_failure(message)

    def _publish_state(self) -> None:
        """在目录锁内发布活动文件, 未成功登记的文件不允许写入"""
        self.state["current_file"] = self.current_file.name if self.current_file else None
        self.state["updated_at"] = time.time()
        atomic_json(self.root / ".runtime" / f"{self.identity}.json", self.state)

    def tick(self, *, open_file: bool = False) -> None:
        """
        检查日期并切换文件, 没有新消息时只关闭旧文件

        参数:
        - open_file: 是否为当前写入打开当天文件
        """
        if self.stopped:
            return
        with self._io_lock:
            current = self.clock()
            if self.current_day == current and (self.stream is not None or not open_file):
                return
            with management_lock(self.root):
                if self.lease.file is None and not self.lease.acquire():
                    raise RuntimeError("日志进程租约不可用")
                if self.stream is not None:
                    self.stream.close()
                self.stream = None
                self.current_file = None
                self.current_day = current
                if open_file:
                    self.current_file = self.file_path or self.root / f"SATRAP-{self.service}-{current:%Y%m%d}-{self.identity}.log"
                try:
                    self._publish_state()
                    if self.current_file:
                        self.stream = self.current_file.open("a", encoding="utf-8")
                except Exception:
                    self.current_day = None
                    self.current_file = None
                    raise

    def emit(self, record: logging.LogRecord) -> None:
        """
        写入单条 UTF-8 日志, 写入失败在此隔离

        参数:
        - record: Python 日志记录
        """
        if self.stopped:
            return
        try:
            with self._io_lock:
                self.tick(open_file=True)
                if self.stream is not None:
                    self.stream.write(self.format(record) + "\n")
                    self.stream.flush()
        except Exception as error:
            self._failure(f"写入或轮转失败: {error}\n{traceback.format_exc()}")
            with self._io_lock:
                if self.stream is not None:
                    try:
                        self.stream.close()
                    except Exception as close_error:
                        self._failure(f"故障文件关闭失败: {close_error}\n{traceback.format_exc()}")
                self.stream = None
                self.current_day = None   # 下次写入重新打开, 活动登记保持到重新切换

    def set_service(self, service: str) -> None:
        """
        修改服务标签, 首次业务写入前调用

        参数:
        - service: 任意服务名称, 文件名中转换为安全的小写标签
        """
        with self._io_lock:
            sanitized = re.sub(r"[^a-z0-9_-]", "-", service.lower())[:40] or "process"
            if sanitized != self.service:
                self.service = sanitized
                self.state["service"] = sanitized
                self.current_day = None

    def close(self) -> None:
        """关闭文件并释放租约, 多次关闭保持幂等"""
        with self._io_lock:
            if self.stopped:
                return
            self.stopped = True
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception as error:
                self._failure(f"关闭文件失败: {error}")
            finally:
                self.stream = None
                try:
                    self.lease.release()
                except Exception as error:
                    self._failure(f"关闭租约失败: {error}\n{traceback.format_exc()}")
                super().close()
