"""群图片的内容校验, 来源授权与发送租约; 模型只接触不透明资产 ID"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
import hashlib
import sqlite3
import threading
import uuid
import warnings

from PIL import Image as PillowImage

from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.core.group_chat.summaries import _digest
from satrap.core.group_chat.types import GroupChatError


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REPLY_BYTES = 20 * 1024 * 1024
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
_LOCK = threading.RLock()
_LEASES: dict[Path, int] = {}


def inspect_image(payload: bytes, mime_type: str | None = None) -> dict[str, Any]:
    """
    按实际内容验证图片, 拒绝超限文件和解码炸弹

    参数:
    - payload: 可信上传或下载取得的有界字节
    - mime_type: 可选的声明类型, 提供时必须与实际类型一致

    返回:
    - 实际 MIME, 尺寸及字节数, 不返回图片内容
    """
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_IMAGE_BYTES:
        raise GroupChatError("quota_exceeded", "单张图片必须大于 0 且不超过 10 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
            with PillowImage.open(BytesIO(payload)) as image:
                mime = FORMATS.get(str(image.format))
                if mime is None or mime_type is not None and mime_type != mime:
                    raise ValueError("图片类型不符")
                width, height = image.size
                if width * height > 20_000_000 or getattr(image, "n_frames", 1) > 200:
                    raise GroupChatError("quota_exceeded", "图片超过 2000 万像素或 200 帧")
                image.verify()
            with PillowImage.open(BytesIO(payload)) as image:
                for index in range(getattr(image, "n_frames", 1)):
                    image.seek(index)
                    if (index + 1) * width * height > 40_000_000:
                        raise GroupChatError("quota_exceeded", "动画累计解码超过 4000 万像素")
                    image.load()
    except GroupChatError:
        raise
    except Exception as exc:
        raise GroupChatError("invalid_media", "图片损坏或不是支持的 PNG/JPEG/WebP/GIF") from exc
    return {"mime_type": mime, "width": width, "height": height, "size_bytes": len(payload)}


@dataclass
class AssetLease:
    """一个文件引用; 可以复制持有权, 实际发送任务结束后独立释放"""

    path: Path
    released: bool = False

    def fork(self) -> AssetLease:
        """为发送子任务增加独立引用, 不依赖调用方的取消状态"""
        with _LOCK:
            if self.released or not self.path.is_file():
                raise GroupChatError("asset_unavailable", "图片文件已不可用")
            _LEASES[self.path] = _LEASES.get(self.path, 0) + 1
            return AssetLease(self.path)

    def release(self) -> None:
        """幂等释放引用, 最后一个引用结束前清理任务不得删除文件"""
        with _LOCK:
            if self.released:
                return
            self.released = True
            remaining = _LEASES.get(self.path, 1) - 1
            if remaining:
                _LEASES[self.path] = remaining
            else:
                _LEASES.pop(self.path, None)


def lease_file(path: Path) -> AssetLease:
    """
    持有已核验的受控文件

    参数:
    - path: 宿主生成的缓存或表情文件路径

    返回:
    - 独立发送租约
    """
    with _LOCK:
        if not path.is_file() or path.is_symlink():
            raise GroupChatError("asset_unavailable", "图片文件已不可用")
        _LEASES[path] = _LEASES.get(path, 0) + 1
        return AssetLease(path)


def fork_message_leases(message: Any) -> list[AssetLease]:
    """
    在进入发送队列之前冻结全部媒体文件的持有权

    参数:
    - message: 已通过宿主校验的组件链

    返回:
    - 发送任务独占的租约列表, 任一失败时释放已取得引用
    """
    leases: list[AssetLease] = []
    try:
        for component in message.components:
            lease = getattr(component, "asset_lease", None)
            if isinstance(lease, AssetLease):
                leases.append(lease.fork())
        return leases
    except BaseException:
        for lease in leases:
            lease.release()
        raise


def invalidate_assets(connection: sqlite3.Connection, now: float) -> None:
    """
    在档案删除事务内撤销所有失效来源图片, 文件稍后由清理器释放

    参数:
    - connection: 已迁移的平台数据库事务
    - now: 档案使用的当前时间
    """
    connection.execute(
        "UPDATE group_chat_assets SET state='revoked' WHERE state='active' AND (expires_at<=? OR "
        "(source_message_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM platform_messages m "
        "WHERE m.scope_key=group_chat_assets.scope_key AND m.message_id=group_chat_assets.source_message_id "
        "AND m.status='active' AND m.verified=1)))", (now,),
    )


class AssetStore:
    """平台数据库授权与平台缓存文件共同组成图片资产, ID 不赋予跨群权限"""

    def __init__(self, archive: PlatformMessageStore) -> None:
        """
        装配当前平台的缓存

        参数:
        - archive: 宿主装配的可信档案, 不接受模型提供的路径
        """
        self.archive = archive
        self.root = archive.database.parent / "cache" / "group-chat" / "assets"

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """使用平台存储迁移和短写事务, 不在下载或发送期间持锁"""
        connection = self.archive._connect(create=True)
        assert connection is not None
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                invalidate_assets(connection, self.archive._clock())
                yield connection
        finally:
            connection.close()

    def register(self, scope: MessageScope, payload: bytes, *, owner: str = "", source_message_id: str | None = None,
                 media_index: int | None = None, mime_type: str | None = None) -> dict[str, Any]:
        """
        将已验证图片写入受管理缓存并授予当前群权限

        参数:
        - scope: 可信平台和群身份
        - payload: 真实图片字节, 不接受路径或 URL
        - owner: 工具产物的主工作流轮次, 消息资产为空
        - source_message_id: 可选的已核验原消息
        - media_index: 来源媒体在目录中的顺序
        - mime_type: 声明 MIME, 提供时核验实际内容

        返回:
        - 不包含文件路径的有限资产对象
        """
        self.archive._check_scope(scope)
        metadata = inspect_image(payload, mime_type)
        source = self.archive.get(scope, source_message_id) if source_message_id else None
        if source_message_id and (not source or source["status"] != "active" or not source["verified"]):
            raise GroupChatError("asset_unavailable", "图片来源已删除, 撤回或无法核验")
        if not source_message_id and not owner:
            raise GroupChatError("wrong_executor", "工具产物必须绑定有效主工作流")
        digest = _digest(source) if source else None
        blob_key = hashlib.sha256(payload).hexdigest()
        asset_id = "ga_" + uuid.uuid4().hex
        expires = self.archive._clock() + 86400
        if source:
            expires = min(expires, source["message_time"] + self.archive.retention_days * 86400)
        with _LOCK, self._transaction() as connection:
            self._purge(connection)
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or self.root.resolve() != self.root.absolute():
                raise GroupChatError("asset_unavailable", "图片缓存路径不是受管理目录")
            path = self.root / blob_key
            if not path.exists():
                platform_size = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
                platforms_root = self.archive.database.parent.parent
                global_size = sum(p.stat().st_size for p in platforms_root.glob("*/cache/group-chat/assets/*") if p.is_file())
                if platform_size + len(payload) > 256 * 1024 * 1024 or global_size + len(payload) > 1024 * 1024 * 1024:
                    raise GroupChatError("quota_exceeded", "群图片缓存已达到容量上限")
                path.write_bytes(payload)
            if source_message_id:
                current = connection.execute("SELECT status,verified FROM platform_messages WHERE scope_key=? AND message_id=?",
                                             (scope.key, source_message_id)).fetchone()
                if not current or current[0] != "active" or current[1] != 1:
                    raise GroupChatError("asset_unavailable", "图片登记前来源已失效")
            connection.execute("INSERT INTO group_chat_assets(asset_id,scope_key,owner,source_message_id,source_digest,media_index,"
                               "blob_key,mime_type,width,height,size_bytes,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (asset_id, scope.key, owner, source_message_id, digest, media_index, blob_key,
                                metadata["mime_type"], metadata["width"], metadata["height"], metadata["size_bytes"], expires))
        return {"schema_version": 1, "asset_id": asset_id, "kind": "image", "origin": "message" if source else "tool",
                "source_message_id": source_message_id, **metadata, "available": True, "expires_at": expires}

    def acquire(self, scope: MessageScope, asset_id: str, owner: str) -> tuple[AssetLease, dict[str, Any]]:
        """
        重新核验归属, 轮次和来源后租用文件

        参数:
        - scope: 当前群可信身份
        - asset_id: 模型取得的不透明 ID
        - owner: 当前主工作流轮次

        返回:
        - 文件租约与实际图片元数据
        """
        self.archive._check_scope(scope)
        with _LOCK, self._transaction() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM group_chat_assets WHERE asset_id=? AND scope_key=? AND state='active'",
                                     (asset_id, scope.key)).fetchone()
            if row is None or row["owner"] and row["owner"] != owner:
                raise GroupChatError("asset_unavailable", "图片不属于当前群和轮次, 或已经失效")
            if row["source_message_id"]:
                source = self.archive.get(scope, row["source_message_id"])
                if not source or source["status"] != "active" or _digest(source) != row["source_digest"]:
                    raise GroupChatError("asset_unavailable", "原消息已经失效或发生变化")
            path = self.root / row["blob_key"]
            payload = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
            if hashlib.sha256(payload).hexdigest() != row["blob_key"]:
                raise GroupChatError("asset_unavailable", "缓存图片损坏或丢失")
            return lease_file(path), dict(row)

    def _purge(self, connection: sqlite3.Connection) -> None:
        """
        清除失效登记及没有授权和租约的缓存文件

        参数:
        - connection: 当前平台短事务
        """
        connection.execute("DELETE FROM group_chat_assets WHERE state!='active' OR expires_at<=?", (self.archive._clock(),))
        keep = {row[0] for row in connection.execute("SELECT blob_key FROM group_chat_assets")}
        if self.root.is_dir() and not self.root.is_symlink():
            for path in self.root.iterdir():
                if path.name not in keep and path.is_file() and not path.is_symlink() and path not in _LEASES:
                    path.unlink()

    def purge(self) -> None:
        """定期回收失效登记和缓存, 不删除在途文件"""
        with _LOCK, self._transaction() as connection:
            self._purge(connection)
