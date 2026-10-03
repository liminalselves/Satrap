"""群图片的内容校验, 来源授权与发送租约; 模型只接触不透明资产 ID"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO
import hashlib
import json
import sqlite3
import threading
import uuid
import warnings
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from PIL import Image as PillowImage

from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.core.group_chat.summaries import _digest
from satrap.core.group_chat.types import GroupChatError


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REPLY_BYTES = 20 * 1024 * 1024
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
_LOCK = threading.RLock()
_LEASES: dict[Path, int] = {}
_PINS: dict[Path, tuple[BinaryIO, int]] = {}


def _pin_path(path: Path) -> Path:
    """
    使用固定 64 个锁槽保护跨进程文件生命周期, 避免每图残留锁文件

    参数:
    - path: 宿主受控图片文件

    返回:
    - 所属缓存或表情目录旁的锁槽路径
    """
    slot = int(hashlib.sha256(path.name.encode()).hexdigest()[:8], 16) % 64
    return path.parent.parent / "leases" / f"media-{slot:02d}.lock"


def _open_pin(path: Path) -> BinaryIO:
    """
    取得可跨线程释放且进程退出自动释放的操作系统锁

    参数:
    - path: 有界锁槽文件

    返回:
    - 已加锁文件句柄, 被其它进程占用时抛出 OSError
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError("媒体锁目录不可为符号链接")
    stream = path.open("a+b")
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except BaseException:
        stream.close()
        raise


def remove_unleased_file(path: Path) -> bool:
    """
    清理进程必须取得同一锁槽, 后端在途媒体不会被控制进程删除

    参数:
    - path: 已确定没有活动目录引用的受控文件

    返回:
    - 文件已删除时为 True, 有租约时延后清理
    """
    with _LOCK:
        pin = _pin_path(path)
        if path in _LEASES or pin in _PINS:
            return False
        try:
            stream = _open_pin(pin)
        except OSError:
            return False
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
                return True
            return False
        finally:
            stream.close()


def _source_digest(source: dict[str, Any]) -> str:
    """
    同时绑定消息内容和实际媒体引用

    参数:
    - source: 已核验原消息

    返回:
    - 防止替换原媒体的内容摘要
    """
    return hashlib.sha256((_digest(source) + json.dumps(source["media"], sort_keys=True, ensure_ascii=False)).encode("utf-8")).hexdigest()


def read_blob(path: Path, blob_key: str, size_bytes: int) -> bytes:
    """
    有界读取已登记图片, 文件被外部修改时拒绝而不是无界加载

    参数:
    - path: 受控文件路径
    - blob_key: 登记时的内容哈希
    - size_bytes: 登记时的实际字节数

    返回:
    - 未被修改的原图片字节
    """
    if not 0 < size_bytes <= MAX_IMAGE_BYTES or not path.is_file() or path.is_symlink() or path.stat().st_size != size_bytes:
        raise GroupChatError("asset_unavailable", "缓存图片丢失或大小发生变化")
    with path.open("rb") as stream:
        payload = stream.read(MAX_IMAGE_BYTES + 1)
    if len(payload) != size_bytes or hashlib.sha256(payload).hexdigest() != blob_key:
        raise GroupChatError("asset_unavailable", "缓存图片内容损坏或被修改")
    return payload


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
                pin = _pin_path(self.path)
                stream, references = _PINS[pin]
                if references == 1:
                    _PINS.pop(pin)
                    stream.close()
                else:
                    _PINS[pin] = (stream, references - 1)


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
        if path not in _LEASES:
            pin = _pin_path(path)
            if pin in _PINS:
                stream, references = _PINS[pin]
                _PINS[pin] = (stream, references + 1)
            else:
                try:
                    _PINS[pin] = (_open_pin(pin), 1)
                except OSError as exc:
                    raise GroupChatError("asset_unavailable", "图片正在由其它进程清理或发送, 请稍后重试", retryable=True) from exc
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
        digest = _source_digest(source) if source else None
        blob_key = hashlib.sha256(payload).hexdigest()
        asset_id = "ga_" + uuid.uuid4().hex
        expires = self.archive._clock() + 86400
        if source:
            expires = min(expires, source["message_time"] + self.archive.retention_days * 86400)
        with _LOCK, self._transaction() as connection:
            self._purge(connection)
            if connection.execute("SELECT COUNT(*) FROM group_chat_assets").fetchone()[0] >= 4096:
                raise GroupChatError("quota_exceeded", "平台图片资产登记已达到 4096 条上限")
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or self.root.resolve() != self.root.absolute():
                raise GroupChatError("asset_unavailable", "图片缓存路径不是受管理目录")
            path = self.root / blob_key
            if path.exists():
                read_blob(path, blob_key, len(payload))
            if not path.exists():
                platform_size = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
                platforms_root = self.archive.database.parent.parent
                global_size = sum(p.stat().st_size for p in platforms_root.glob("*/cache/group-chat/assets/*") if p.is_file())
                if platform_size + len(payload) > 256 * 1024 * 1024 or global_size + len(payload) > 1024 * 1024 * 1024:
                    raise GroupChatError("quota_exceeded", "群图片缓存已达到容量上限")
                path.write_bytes(payload)
            if source_message_id:
                current = connection.execute("SELECT * FROM platform_messages WHERE scope_key=? AND message_id=?",
                                             (scope.key, source_message_id)).fetchone()
                if not current or current["status"] != "active" or current["verified"] != 1 or _source_digest(self.archive._item(current)) != digest:
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
                if not source or source["status"] != "active" or _source_digest(source) != row["source_digest"]:
                    raise GroupChatError("asset_unavailable", "原消息已经失效或发生变化")
            path = self.root / row["blob_key"]
            read_blob(path, row["blob_key"], row["size_bytes"])
            return lease_file(path), dict(row)

    def find(self, scope: MessageScope, message_id: str, index: int) -> dict[str, Any] | None:
        """
        复用已核验的同群同消息图片登记, 不复用过期或损坏文件

        参数:
        - scope: 当前群
        - message_id: 原消息
        - index: 媒体目录位置

        返回:
        - 有效资产或 None
        """
        self.archive._check_scope(scope)
        with _LOCK, self._transaction() as connection:
            row = connection.execute("SELECT * FROM group_chat_assets WHERE scope_key=? AND source_message_id=? AND media_index=? "
                                     "AND state='active' ORDER BY expires_at DESC LIMIT 1", (scope.key, message_id, index)).fetchone()
            if row is None:
                return None
            source = self.archive.get(scope, message_id)
            path = self.root / row["blob_key"]
            if not source or _source_digest(source) != row["source_digest"]:
                return None
            try:
                read_blob(path, row["blob_key"], row["size_bytes"])
            except (GroupChatError, OSError):
                return None
            return {"schema_version": 1, "asset_id": row["asset_id"], "kind": "image", "origin": "message",
                    "source_message_id": message_id, "available": True,
                    **{key: row[key] for key in ("mime_type", "width", "height", "size_bytes", "expires_at")}}

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
                    remove_unleased_file(path)

    def purge(self) -> None:
        """定期回收失效登记和缓存, 不删除在途文件"""
        with _LOCK, self._transaction() as connection:
            self._purge(connection)
