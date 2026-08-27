"""按平台实例和会话划分 Satrap 持久化数据路径"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from satrap.core.utils.paths import get_data_dir


CHAT_PLATFORM_ID = "chat"
"""React Chat 使用的保留平台实例 ID"""

LOCAL_PLATFORM_ID = "local"
"""CLI、TUI 和未绑定平台会话使用的保留平台实例 ID"""

_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")


def storage_key(raw_id: str, *, fallback: str) -> str:
    """
    将外部 ID 转换为适合跨平台文件系统的稳定目录键

    参数:
    - raw_id: 平台、用户或会话的原始 ID
    - fallback: ID 为空或不含可读字符时使用的短名称

    返回:
    - str: 可读短名称与稳定哈希组成的目录键
    """
    normalized = raw_id.strip()
    readable = _SLUG_PATTERN.sub("-", normalized).strip(".-_").lower()
    readable = (readable or fallback)[:40]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{readable}--{digest}"


@dataclass(frozen=True)
class StorageScope:
    """一次存储操作所属的平台、用户、会话和项目作用域"""

    platform_id: str
    user_id: str = ""
    session_id: str = ""
    project_id: str = ""


class StorageLayout:
    """解析并创建 `.satrap/data` 下的 v2 数据目录"""

    layout_version = 2

    def __init__(self, root: str | Path | None = None) -> None:
        """
        初始化数据布局

        参数:
        - root: 可选数据根目录, 默认 `.satrap/data`
        """
        self.root = Path(root).resolve() if root is not None else (get_data_dir() / "data").resolve()

    @property
    def platforms_root(self) -> Path:
        """返回平台数据根目录"""
        return self.root / "platforms"

    def platform_key(self, platform_id: str) -> str:
        """
        返回平台实例目录键

        参数:
        - platform_id: 平台实例 ID

        返回:
        - str: 平台目录键
        """
        return storage_key(platform_id, fallback="platform")

    def platform_root(self, platform_id: str) -> Path:
        """
        返回平台实例数据目录

        参数:
        - platform_id: 平台实例 ID

        返回:
        - Path: 平台实例数据目录
        """
        return self.platforms_root / self.platform_key(platform_id)

    def platform_db(self, platform_id: str) -> Path:
        """
        返回平台唯一 SQLite 数据库路径

        参数:
        - platform_id: 平台实例 ID

        返回:
        - Path: `platform.db` 路径
        """
        return self.platform_root(platform_id) / "platform.db"

    def platform_cache(self, platform_id: str) -> Path:
        """返回平台实例的非会话临时缓存目录"""
        return self.platform_root(platform_id) / "cache"

    def user_root(self, platform_id: str, user_id: str) -> Path:
        """
        返回平台内用户数据目录

        参数:
        - platform_id: 平台实例 ID
        - user_id: 用户 ID

        返回:
        - Path: 用户目录
        """
        return self.platform_root(platform_id) / "users" / storage_key(user_id, fallback="user")

    def session_root(self, platform_id: str, session_id: str) -> Path:
        """
        返回平台内会话独占目录

        参数:
        - platform_id: 平台实例 ID
        - session_id: 会话 ID

        返回:
        - Path: 会话独占目录
        """
        return self.platform_root(platform_id) / "sessions" / storage_key(session_id, fallback="session")

    def session_sandbox(self, platform_id: str, session_id: str) -> Path:
        """返回会话独占 sandbox 目录"""
        return self.session_root(platform_id, session_id) / "sandbox"

    def session_uploads(self, platform_id: str, session_id: str) -> Path:
        """返回会话独占 uploads 目录"""
        return self.session_root(platform_id, session_id) / "uploads"

    def session_artifacts(self, platform_id: str, session_id: str) -> Path:
        """返回会话独占 artifacts 目录"""
        return self.session_root(platform_id, session_id) / "artifacts"

    def session_indexes(self, platform_id: str, session_id: str) -> Path:
        """返回会话独占 indexes 目录"""
        return self.session_root(platform_id, session_id) / "indexes"

    def session_cache(self, platform_id: str, session_id: str) -> Path:
        """返回会话独占 cache 目录"""
        return self.session_root(platform_id, session_id) / "cache"

    def project_root(self, platform_id: str, project_id: str) -> Path:
        """
        返回平台内项目共享数据目录

        参数:
        - platform_id: 平台实例 ID
        - project_id: 项目 ID

        返回:
        - Path: 项目共享数据目录
        """
        return self.platform_root(platform_id) / "projects" / storage_key(project_id, fallback="project")

    def trash_root(self, platform_id: str) -> Path:
        """返回平台回收目录"""
        return self.platform_root(platform_id) / "trash"

    def ensure_user(self, scope: StorageScope) -> Path:
        """
        创建用户目录和身份清单

        参数:
        - scope: 至少包含平台和用户 ID 的存储作用域

        返回:
        - Path: 用户目录
        """
        if not scope.user_id.strip():
            raise ValueError("user_id 不能为空")
        self.ensure_platform(scope.platform_id)
        root = self.user_root(scope.platform_id, scope.user_id)
        root.mkdir(parents=True, exist_ok=True)
        payload: dict[str, str | int] = {
            "layout_version": self.layout_version,
            "platform_id": scope.platform_id,
            "user_id": scope.user_id,
            "storage_key": root.name,
        }
        (root / "meta.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return root

    def ensure_platform(self, platform_id: str) -> Path:
        """
        创建平台基础目录并写入身份清单

        参数:
        - platform_id: 平台实例 ID

        返回:
        - Path: 平台实例数据目录
        """
        clean_platform_id = platform_id.strip()
        if not clean_platform_id:
            raise ValueError("platform_id 不能为空")
        root = self.platform_root(clean_platform_id)
        for path in (
            root / "users",
            root / "sessions",
            root / "projects",
            root / "cache",
            root / "trash",
        ):
            path.mkdir(parents=True, exist_ok=True)
        manifest = root / "platform.json"
        payload: dict[str, str | int] = {
            "layout_version": self.layout_version,
            "platform_id": clean_platform_id,
            "storage_key": root.name,
        }
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return root

    def ensure_session(self, scope: StorageScope) -> Path:
        """
        创建会话独占目录和身份清单

        参数:
        - scope: 完整存储作用域

        返回:
        - Path: 会话独占目录
        """
        if not scope.session_id.strip():
            raise ValueError("session_id 不能为空")
        self.ensure_platform(scope.platform_id)
        root = self.session_root(scope.platform_id, scope.session_id)
        for name in ("sandbox", "uploads", "artifacts", "indexes", "cache"):
            (root / name).mkdir(parents=True, exist_ok=True)
        payload: dict[str, str | int] = {
            "layout_version": self.layout_version,
            "platform_id": scope.platform_id,
            "user_id": scope.user_id,
            "session_id": scope.session_id,
            "project_id": scope.project_id,
            "storage_key": root.name,
        }
        (root / "meta.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return root

    def trash_session(self, platform_id: str, session_id: str) -> Path | None:
        """
        将会话目录整体移入同平台回收区

        参数:
        - platform_id: 平台实例 ID
        - session_id: 会话 ID

        返回:
        - Path | None: 回收后的目录, 原目录不存在时返回 None
        """
        source = self.session_root(platform_id, session_id)
        if not source.exists() and not source.is_symlink():
            return None
        trash_sessions = self.trash_root(platform_id) / "sessions"
        trash_sessions.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        destination = trash_sessions / f"{stamp}-{time.time_ns()}--{source.name}"
        shutil.move(str(source), str(destination))
        return destination

    def purge_session(self, platform_id: str, session_id: str) -> bool:
        """
        永久删除会话目录

        参数:
        - platform_id: 平台实例 ID
        - session_id: 会话 ID

        返回:
        - bool: 是否删除了目录
        """
        target = self.session_root(platform_id, session_id)
        if not target.exists() and not target.is_symlink():
            return False
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True


default_storage_layout = StorageLayout()
"""项目默认 v2 数据布局"""
