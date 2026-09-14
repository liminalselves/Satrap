"""跨进程文件锁, 仅用于已有文件数据的知识库写入互斥"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from pathlib import Path
from typing import BinaryIO, cast
import time
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl
# 操作系统专用模块只在对应平台加载

from satrap.core.storage.layout import StorageLayout, storage_key


@dataclass
class _HeldLock:
    file: BinaryIO
    depth: int = 1

_HELD_LOCKS = threading.local()


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
                    raise TimeoutError("知识库正在执行其他写入操作, 请稍后重试")
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


def session_storage_lock(layout: StorageLayout, platform_id: str, session_id: str) -> FileLock:
    """锁文件放在平台目录, 会话文件移动时仍保留同一把锁"""
    return database_session_lock(layout.platform_db(platform_id), session_id)


def database_session_lock(database: Path, session_id: str) -> FileLock:
    """覆盖配置与文件索引共用会话锁, 锁目录属于平台而非会话"""
    return FileLock(Path(database).parent / "locks" / f"{storage_key(session_id, fallback='session')}.lock")
