"""CLI 后端实例锁与进程标识管理"""
from __future__ import annotations

from pathlib import Path
from typing import IO
from types import TracebackType
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from satrap.core.runtime_paths import get_runtime_path


class BackendInstanceLock:
    """后端单实例锁, 防止同一工作目录启动多个后端"""

    def __init__(self, path: str | Path | None = None):
        """
        初始化 BackendInstanceLock

        参数:
        - path: 路径
        """
        self.path = (
            Path(path)
            if path is not None
            else get_runtime_path("backend.lock", require_idle=True)
        )
        self._fh: IO[str] | None = None

    def acquire(self, host: str, port: int) -> bool:
        """
        尝试获取锁, 成功返回 True

        参数:
        - host: 主机
        - port: 端口

        返回:
        - bool: 尝试获取锁, 成功返回 True
        """
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")

        try:
            self._lock_file()
        except OSError:
            self._close()
            return False

        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"pid={os.getpid()}\napi={host}:{port}\n")
            self._fh.flush()
        except BaseException:
            self.release()
            raise
        return True

    def release(self) -> None:
        """释放锁并关闭文件句柄"""
        if self._fh is None:
            return

        try:
            self._unlock_file()
        finally:
            self._close()

    def _lock_file(self) -> None:
        """使用本机非阻塞文件锁, 竞争失败时不修改已有实例信息"""
        assert self._fh is not None

        self._fh.seek(0)

        if os.name == "nt":
            self._fh.seek(0, os.SEEK_END)
            if self._fh.tell() == 0:
                self._fh.write(" ")   # 空文件为 Windows 字节锁预留位置
                self._fh.flush()
            self._fh.seek(0)
            msvcrt.locking(
                self._fh.fileno(),
                msvcrt.LK_NBLCK,
                1,
            )
        else:
            fcntl.flock(
                self._fh.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )

    def _unlock_file(self) -> None:
        """释放当前文件上的本机锁"""
        assert self._fh is not None

        self._fh.seek(0)

        if os.name == "nt":
            msvcrt.locking(
                self._fh.fileno(),
                msvcrt.LK_UNLCK,
                1,
            )
        else:
            fcntl.flock(
                self._fh.fileno(),
                fcntl.LOCK_UN,
            )

    def _close(self) -> None:
        """关闭本次锁句柄"""
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> BackendInstanceLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ):
        self.release()
