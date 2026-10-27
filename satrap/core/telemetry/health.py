"""
服务运行身份与构建信息

在启动时冻结版本及进程身份, 用单调时钟计算运行时长, 健康轮询不访问磁盘
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from dataclasses import dataclass, field
from functools import lru_cache
import platform
from pathlib import Path
import time
from uuid import uuid4
import os
import re

from .contracts import Service, ServiceHealth


@lru_cache(maxsize=1)
def _build_identity() -> tuple[str | None, str | None]:
    """
    在首次服务构造时读取发布版本和已知构建提交

    返回:
    - 安装版本与提交, 源码环境无法确认时保留 None
    """
    try:
        release = version("satrap")
    except PackageNotFoundError:
        release = None
    revision = os.environ.get("SATRAP_BUILD_REVISION", "")
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
        return release, revision.lower()
    try:
        root = Path(__file__).resolve().parents[3]
        git = root / ".git"
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir: "):
                return release, None
            git = (root / pointer[8:]).resolve()
        head = (git / "HEAD").read_text(encoding="ascii").strip()
        if head.startswith("ref: refs/"):
            name = head[5:]
            if ".." in Path(name).parts:
                return release, None
            target = git / name
            if target.is_file():
                head = target.read_text(encoding="ascii").strip()
            else:
                packed = git / "packed-refs"
                head = next((line.split(" ", 1)[0] for line in packed.read_text(encoding="ascii").splitlines()
                             if line.endswith(" " + name)), "") if packed.is_file() and packed.stat().st_size <= 2_000_000 else ""
        return release, head.lower() if re.fullmatch(r"[0-9a-fA-F]{40,64}", head) else None
    except (OSError, UnicodeError):
        return release, None


@dataclass
class ServiceRuntime:
    """与单个服务生命周期绑定的时钟和身份快照"""
    service: Service
    runtime_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float = field(default_factory=time.time)
    _started_monotonic: float = field(default_factory=time.monotonic)
    _pid: int = field(default_factory=os.getpid)
    _python: str = field(default_factory=platform.python_version)
    _platform: str = field(default_factory=platform.platform)
    _build: tuple[str | None, str | None] = field(default_factory=_build_identity)

    def snapshot(self, *, running: bool = True, healthy: bool = True) -> ServiceHealth:
        """
        返回当前服务状态, 不访问文件或探测其它服务

        参数:
        - running: 当前服务是否运行, 默认 True
        - healthy: 当前服务是否健康, 默认 True

        返回:
        - 带稳定启动身份和单调运行时长的公开资料
        """
        return {"service": self.service, "running": running, "healthy": healthy,
                "runtime_id": self.runtime_id, "pid": self._pid, "started_at": self.started_at,
                "uptime_seconds": max(0.0, time.monotonic() - self._started_monotonic), "generated_at": time.time(),
                "python": self._python, "platform": self._platform, "version": self._build[0], "revision": self._build[1]}
