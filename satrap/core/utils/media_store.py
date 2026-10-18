"""按数据库和工作流隔离媒体引用, 原媒体去重保存且不重复进入消息 JSON"""
from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit
from contextvars import ContextVar
from dataclasses import dataclass
from contextlib import contextmanager, closing
import tempfile
from pathlib import Path
import sqlite3
import hashlib
from typing import cast
import shutil
import json
import os

from satrap.core.storage.file_lock import FileLock
from satrap.core.utils.paths import get_db_path


MAX_BYTES = 32 * 1024 * 1024
REFERENCE_PREFIX = "satrap-media://"
_CURRENT: ContextVar[MediaStore | None] = ContextVar("satrap_media_store", default=None)


@dataclass(frozen=True)
class MediaStore:
    """平台内共享原文件, 会话引用清单单独控制可读范围"""

    root: Path
    scope: str
    database: Path | None = None

    def _owner(self) -> str | None:
        """读取宿主登记的会话归属, 独立上下文没有归属时保留旧前缀规则"""
        if self.database is None or not self.database.is_file():
            return None
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='context_catalog'").fetchone():
                return None
            row = connection.execute("SELECT session_id FROM context_catalog WHERE context_id=?", (self.scope,)).fetchone()
            return str(row[0]) if row and row[0] is not None else None

    @property
    def references(self) -> Path:
        """返回当前工作流的引用目录"""
        return self.root / "references" / hashlib.sha256(self.scope.encode()).hexdigest()

    def put(self, data: bytes, mime: str) -> str:
        """
        有界保存媒体并登记当前工作流的持有权

        参数:
        - data: 已通过来源检查的媒体字节
        - mime: 媒体实际 MIME

        返回:
        - 不含本地路径的稳定媒体引用
        """
        if not data or len(data) > MAX_BYTES or not mime.startswith(("image/", "video/")):
            raise ValueError("媒体为空, 格式无效或超过 32 MiB")
        key = hashlib.sha256(data).hexdigest()
        blob = self.root / "blobs" / key
        self.references.mkdir(parents=True, exist_ok=True)
        with FileLock(self.root / ".store.lock"):
            if not blob.exists():
                atomic_write(blob, data)
            marker = self.references / (key + ".json")
            if not marker.exists():
                atomic_write(marker, json.dumps({"mime": mime, "size": len(data), "scope": self.scope, "owner": self._owner()}).encode())
        return REFERENCE_PREFIX + key

    def read(self, reference: str) -> tuple[bytes, str]:
        """
        只读取当前工作流登记且内容未变化的媒体

        参数:
        - reference: 历史持有的内部媒体引用, 可附读取时间区间

        返回:
        - 原始字节与 MIME; 越权, 缺失或损坏时抛出 ValueError
        """
        key = urlsplit(reference).netloc
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("媒体引用无效")
        marker = self.references / (key + ".json")
        blob = self.root / "blobs" / key
        try:
            if marker.is_symlink() or blob.is_symlink():
                raise ValueError("媒体引用不允许符号链接")
            metadata = cast(dict[str, object], json.loads(marker.read_text(encoding="utf-8")))
            with blob.open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            if not data or len(data) > MAX_BYTES or len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != key:
                raise ValueError("原媒体内容损坏")
            return data, str(metadata["mime"])
        except (OSError, KeyError, json.JSONDecodeError) as error:
            raise ValueError("原媒体已不可用或不属于当前会话, 请重新上传或读取") from error

    def write_upload(self, data: bytes, mime: str, target: Path) -> str:
        """
        将宿主上传文件登记为原媒体的硬链接别名, 卷不支持时才复制

        参数:
        - data: 上传接口取得的固定字节
        - mime: 已确认的媒体 MIME
        - target: 宿主生成的私有上传路径, 不是任意工作区文件

        返回:
        - 当前工作流媒体引用, 上传路径继续用于既有预览接口
        """
        with FileLock(self.root / ".store.lock"):
            reference = self.put(data, mime)
            blob = self.root / "blobs" / urlsplit(reference).netloc
            try:
                os.link(blob, target)
            except OSError:
                atomic_write(target, data)
            return reference

    def copy_to(self, target: MediaStore, media_ids: set[str] | None = None) -> None:
        """
        为分支复制媒体持有权, 不复制同一平台内的原始字节

        参数:
        - target: 分支目标工作流存储
        - media_ids: 仅复制分支历史实际引用的媒体, None 表示复制全部
        """
        if self.root != target.root:
            raise ValueError("媒体分支必须属于同一个数据库")
        with FileLock(self.root / ".store.lock"):
            if self.references.exists():
                for marker in self.references.glob("*.json"):
                    if media_ids is not None and marker.stem not in media_ids:
                        continue
                    metadata = cast(dict[str, object], json.loads(marker.read_text(encoding="utf-8")))
                    metadata["scope"] = target.scope
                    metadata["owner"] = target._owner()
                    atomic_write(target.references / marker.name, json.dumps(metadata).encode())


def referenced_media(value: object) -> set[str]:
    """
    收集分支上下文实际持有的媒体 ID, 不带入源会话未来轮次的资产

    参数:
    - value: JSON 消息或消息列表

    返回:
    - 内部媒体引用的内容标识集合
    """
    if isinstance(value, str):
        return {urlsplit(value).netloc} if value.startswith(REFERENCE_PREFIX) else set()
    if isinstance(value, dict):
        return set().union(*(referenced_media(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(referenced_media(item) for item in value))
    return set()


def _session_references(root: Path, session_id: str) -> list[Path]:
    """
    与数据库会话域一致地匹配主上下文和子工作流的媒体引用

    参数:
    - root: 平台媒体根目录
    - session_id: 主会话 ID

    返回:
    - 属于该会话的引用清单路径
    """
    matches: list[Path] = []
    for marker in (root / "references").glob("*/*.json"):
        metadata = cast(dict[str, object], json.loads(marker.read_text(encoding="utf-8")))
        scope = str(metadata.get("scope", ""))
        owner = metadata.get("owner")
        if owner == session_id or (owner is None and (scope == session_id or scope.startswith(session_id + "_"))):
            matches.append(marker)
    return matches


def archive_session_media(database: str, session_id: str, destination: Path) -> None:
    """
    把会话引用和去重原文件复制到回收包, 不归档可重建的抽帧缓存

    参数:
    - database: 实际数据库路径
    - session_id: 主会话 ID
    - destination: 本次归档的媒体子目录
    """
    root = store_for(database, session_id).root
    if not root.exists():
        return
    with FileLock(root / ".store.lock"):
        for marker in _session_references(root, session_id):
            target = destination / marker.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(marker, target)
            blob = root / "blobs" / marker.stem
            copied = destination / "blobs" / marker.stem
            if not copied.exists():
                copied.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(blob, copied)
                except OSError:
                    shutil.copy2(blob, copied)


def restore_session_media(database: str, source: Path) -> None:
    """
    在数据库恢复前发布原媒体和持有权, 重复恢复保持幂等

    参数:
    - database: 目标数据库路径
    - source: 受控回收包内的媒体目录
    """
    if not source.exists():
        return
    root = store_for(database, "restore").root
    with FileLock(root / ".store.lock"):
        for blob in (source / "blobs").glob("*"):
            if blob.is_symlink() or not blob.is_file() or len(blob.name) != 64 or any(c not in "0123456789abcdef" for c in blob.name):
                raise ValueError("回收包媒体文件无效")
            with blob.open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != blob.name:
                raise ValueError("回收包媒体内容损坏")
            target = root / "blobs" / blob.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    os.link(blob, target)
                except OSError:
                    atomic_write(target, data)
        for marker in (source / "references").glob("*/*.json"):
            metadata = cast(dict[str, object], json.loads(marker.read_text(encoding="utf-8")))
            scope = str(metadata["scope"])
            expected = hashlib.sha256(scope.encode()).hexdigest()
            if marker.is_symlink() or marker.parent.name != expected or len(marker.stem) != 64 or any(c not in "0123456789abcdef" for c in marker.stem):
                raise ValueError("回收包媒体引用无效")
            atomic_write(root / marker.relative_to(source), marker.read_bytes())


def release_session_media(database: str, session_id: str) -> None:
    """
    释放已删除或已归档会话的持有权, 仅回收无人引用的原文件

    参数:
    - database: 平台数据库路径
    - session_id: 已停止使用的主会话 ID
    """
    root = store_for(database, session_id).root
    if not root.exists():
        return
    with FileLock(root / ".store.lock"):
        for marker in _session_references(root, session_id):
            marker.unlink()
            if not any(marker.parent.iterdir()):
                marker.parent.rmdir()
        remaining = {marker.stem for marker in (root / "references").glob("*/*.json")}
        for blob in (root / "blobs").glob("*"):
            if blob.name not in remaining and blob.is_file():
                blob.unlink()


def atomic_write(path: Path, data: bytes) -> None:
    """
    同目录原子替换文件, 不暴露半写入内容

    参数:
    - path: 宿主计算的受控路径
    - data: 完整文件内容
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".media-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def store_for(database: str, scope: str) -> MediaStore:
    """
    使用实际数据库目录派生媒体存储, 兼容自定义后端数据根

    参数:
    - database: 工作流实际数据库路径
    - scope: 工作流上下文标识

    返回:
    - 隔离后的媒体存储
    """
    resolved = Path(database).resolve()
    database_key = hashlib.sha256(os.path.normcase(str(resolved)).encode()).hexdigest()[:16]
    return MediaStore(resolved.parent / "media" / database_key, scope, resolved)


def current_store() -> MediaStore:
    """返回当前工作流媒体存储, 独立 LLM 调用使用 local 作用域"""
    return _CURRENT.get() or store_for(get_db_path(), "direct-llm")


@contextmanager
def media_scope(database: str, scope: str) -> Iterator[None]:
    """
    在同步, 异步和复制到后台线程的上下文中绑定媒体持有权

    参数:
    - database: 实际数据库路径
    - scope: 实际工作流标识

    返回:
    - 调用结束后恢复上一个绑定的上下文管理器
    """
    token = _CURRENT.set(store_for(database, scope))
    try:
        yield
    finally:
        _CURRENT.reset(token)
