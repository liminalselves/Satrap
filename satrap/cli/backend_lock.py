from __future__ import annotations

import os
from pathlib import Path
from typing import IO
import msvcrt


class BackendInstanceLock:
    """后端单实例锁, 防止同一工作目录启动多个后端"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else Path.cwd() / ".satrap" / "backend.lock"
        self._fh: IO[str] | None = None

    def acquire(self, host: str, port: int) -> bool:
        """尝试获取锁, 成功返回 True"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")
        try:
            self._lock_file()
        except OSError:
            self._close()
            return False

        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid={os.getpid()}\napi={host}:{port}\n")
        self._fh.flush()
        return True

    def release(self):
        """释放锁并关闭文件句柄"""
        if self._fh is None:
            return
        try:
            self._unlock_file()
        finally:
            self._close()

    def _lock_file(self):
        assert self._fh is not None
        self._fh.seek(0)
        self._fh.write(" ")
        self._fh.flush()
        self._fh.seek(0)
        msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(self):
        assert self._fh is not None
        self._fh.seek(0)
        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)


    def _close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> BackendInstanceLock:
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
