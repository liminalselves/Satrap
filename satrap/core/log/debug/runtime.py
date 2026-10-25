"""用操作系统锁确认诊断写入进程是否仍存在, 不靠 PID 或超时猜测执行结果"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from satrap.core.log.managed import LogFileLock


class RuntimeLease:
    """每个诊断写入器独立持有的生命周期锁"""

    def __init__(self, root: Path, service: str) -> None:
        """
        在首次开启写入时取得可跨平台自动释放的进程租约

        参数:
        - root: 诊断库所在目录
        - service: 当前实际服务
        """
        self.identity = uuid4().hex
        self.lock = LogFileLock(root / ".runtimes" / f"{service}-{self.identity}.lock")
        if not self.lock.acquire():
            raise OSError("无法建立诊断写入器生命周期锁")

    def close(self) -> None:
        """所有已接收事件收尾后释放租约, 异常退出由操作系统释放"""
        self.lock.close()


def runtime_alive(root: Path, service: str, identity: object) -> bool | None:
    """
    检查开始事件所指写入器的实际存活状态

    参数:
    - root: 当前服务固定诊断目录
    - service: 当前服务标识
    - identity: 开始事件记录的随机标识

    返回:
    - 锁仍被持有为 True, 已释放为 False, 无身份或无法确认时为 None
    """
    if not isinstance(identity, str) or len(identity) != 32 or any(char not in "0123456789abcdef" for char in identity):
        return None
    path = root / ".runtimes" / f"{service}-{identity}.lock"
    try:
        if not path.is_file():
            return False
        lock = LogFileLock(path)
        acquired = lock.acquire()
        if acquired:
            lock.close()
        return not acquired
    except (OSError, ValueError):
        return None
