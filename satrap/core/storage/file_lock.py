"""跨进程文件锁, 用于知识库及配置文档写入互斥"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from pathlib import Path
from typing import BinaryIO, cast
import time
import os

if os.name == "nt":
    import msvcrt
    import ctypes
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        """同步文件句柄的 Win32 锁范围, 偏移固定为零"""

        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.LockFileEx.restype = wintypes.BOOL
    _kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.UnlockFileEx.restype = wintypes.BOOL
else:
    import fcntl
# 操作系统专用模块只在对应平台加载

from satrap.core.storage.layout import StorageLayout, storage_key


@dataclass
class _HeldLock:
    file: BinaryIO
    depth: int = 1

_HELD_LOCKS = threading.local()
_LEASE_LOCK = threading.RLock()
_FILE_LEASES: dict[Path, tuple[BinaryIO, int]] = {}
_SHARED_LEASES: dict[Path, tuple[BinaryIO, int]] = {}


class FileLease:
    """使用同一维护锁保护在途文件, 允许跨线程释放和同进程并行下载"""

    def __init__(self, path: Path, *, shared: bool = False) -> None:
        """
        取得非阻塞租约, 维护已占用时明确拒绝

        参数:
        - path: 与 FileLock 共用的稳定锁文件路径
        - shared: 是否允许其他进程同时读取, 默认保持原独占租约语义
        """
        self.path = path.resolve()
        self.released = False
        self.shared = shared
        self._leases = _SHARED_LEASES if shared else _FILE_LEASES
        with _LEASE_LOCK:
            previous = self._leases.get(self.path)
            if previous is not None:
                self._leases[self.path] = (previous[0], previous[1] + 1)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open("a+b")
            try:
                stream.seek(0)
                if os.name == "nt":
                    if shared:
                        if not _kernel.LockFileEx(msvcrt.get_osfhandle(stream.fileno()), 1, 0, 1, 0, ctypes.byref(_Overlapped())):
                            raise ctypes.WinError(ctypes.get_last_error())
                    else:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BaseException:
                stream.close()
                raise
            self._leases[self.path] = (stream, 1)

    def close(self) -> None:
        """幂等释放租约, 最后一个持有者结束后允许维护移动或删除会话"""
        with _LEASE_LOCK:
            if self.released:
                return
            self.released = True
            stream, count = self._leases[self.path]
            if count > 1:
                self._leases[self.path] = (stream, count - 1)
            else:
                del self._leases[self.path]
                try:
                    if os.name == "nt" and self.shared:
                        if not _kernel.UnlockFileEx(msvcrt.get_osfhandle(stream.fileno()), 0, 1, 0, ctypes.byref(_Overlapped())):
                            raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    stream.close()

    def __enter__(self) -> FileLease:
        """返回已取得的租约"""
        return self

    def __exit__(self, *args: object) -> None:
        """
        在正常结束或异常时释放

        参数:
        - args: 上下文退出信息
        """
        self.close()


class FileLock:
    """使用操作系统锁保证进程退出后自动释放, 不依赖过期租约"""

    def __init__(self, path: Path, timeout: float = 30) -> None:
        self.path = path
        self.timeout = timeout
        self._file: BinaryIO | None = None

    def __enter__(self) -> FileLock:
        self._key = os.path.normcase(str(self.path.resolve()))
        held = cast(dict[str, _HeldLock] | None, getattr(_HELD_LOCKS, "locks", None))
        if held is None:
            held = {}
            _HELD_LOCKS.locks = held
        if self._key in held:
            held[self._key].depth += 1
            self._file = held[self._key].file
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                if self._file.seek(0, 2) == 0:
                    self._file.write(b"\0")
                    self._file.flush()
                held[self._key] = _HeldLock(self._file)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._file.close()
                    raise TimeoutError("目标文件正在执行其他写入操作, 请稍后重试")
                time.sleep(0.05)

    def __exit__(self, *args: object) -> None:
        held = cast(dict[str, _HeldLock], _HELD_LOCKS.locks)
        held[self._key].depth -= 1
        if held[self._key].depth:
            return
        file = held.pop(self._key).file
        try:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
        # 锁文件保留不删除: POSIX 下删除正被等待的锁文件会让后来者拿到另一个 inode, 造成双持有


def session_storage_lock(layout: StorageLayout, platform_id: str, session_id: str) -> FileLock:
    """锁文件放在平台目录, 会话文件移动时仍保留同一把锁"""
    return database_session_lock(layout.platform_db(platform_id), session_id)


def database_session_lock(database: Path, session_id: str) -> FileLock:
    """覆盖配置与文件索引共用会话锁, 锁目录属于平台而非会话"""
    return FileLock(Path(database).parent / "locks" / f"{storage_key(session_id, fallback='session')}.lock")
