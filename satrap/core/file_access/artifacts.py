"""普通文件产物的流式冻结, 完整性验证与当前会话交付协议"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import asyncio
import mimetypes
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO
import uuid

from satrap.core.storage.file_lock import FileLease

MAX_FILE_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
_ID = re.compile(r"sf_[0-9a-f]{32}\Z")


def _checked_root(root: Path) -> None:
    """
    拒绝宿主目录绑定后出现的符号链接, 重解析点或路径越界

    参数:
    - root: 宿主冻结的规范产物目录
    """
    if root.resolve() != root.absolute():
        raise ValueError("文件产物目录已重定向或不再属于原会话")


def display_name(value: str) -> str:
    """
    校验下载文件名, 不允许目录或响应头控制字符

    参数:
    - value: 用户显示名称或源文件名

    返回:
    - 可用于附件响应的名称, 无效时抛出 ValueError
    """
    if (not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 240
            or any(ord(char) < 32 or char in '/\\:' for char in value) or value in {".", ".."}):
        raise ValueError("文件名必须是最多 240 UTF-8 字节的单个名称, 不能包含路径或控制字符")
    return value


def artifact_lease(root: Path) -> FileLease:
    """
    锁定会话目录, 与回收, 归档和删除共用平台级维护锁

    参数:
    - root: 宿主提供的 session_root/artifacts 路径

    返回:
    - 发送或下载结束时必须关闭的租约
    """
    return FileLease(root.parent.parent.parent / "locks" / (root.parent.name + ".lock"))


@dataclass(frozen=True)
class FileArtifact:
    """宿主缓存中的不可变普通文件引用, 真实路径不进入模型结果"""

    root: Path
    file_id: str
    name: str
    size: int
    sha256: str
    mime: str = "application/octet-stream"

    @property
    def path(self) -> Path:
        """
        解析已登记文件, 不接受模型路径或符号链接

        返回:
        - 只属于当前产物根的文件路径
        """
        if not _ID.fullmatch(self.file_id) or type(self.size) is not int or not 0 <= self.size <= MAX_FILE_BYTES or not re.fullmatch("[0-9a-f]{64}", self.sha256):
            raise ValueError("文件产物资料无效")
        display_name(self.name)
        _checked_root(self.root)
        if not self.root.is_dir() or self.root.is_symlink() or self.root.parent.is_symlink():
            raise ValueError("文件产物目录不可用")
        path = self.root / self.file_id
        if path.is_symlink() or not path.is_file():
            raise ValueError("文件产物不存在或已经回收")
        return path

    def public(self) -> dict[str, object]:
        """
        返回不含本机路径的可持久化附件资料

        返回:
        - 不透明 ID, 名称, 大小, 校验摘要和类型
        """
        return {"file_id": self.file_id, "name": self.name, "size": self.size, "sha256": self.sha256, "mime": self.mime}

    def open(self) -> BinaryIO:
        """
        流式校验缓存内容后返回已打开的二进制流

        返回:
        - 定位到开头的文件流, 调用者负责关闭; 损坏或超限时拒绝
        """
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        stream = os.fdopen(os.open(self.path, flags), "rb")
        try:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != self.size:
                raise ValueError("文件产物大小或类型发生变化")
            digest, count = hashlib.sha256(), 0
            while chunk := stream.read(CHUNK_BYTES):
                count += len(chunk)
                if count > MAX_FILE_BYTES:
                    raise ValueError("文件产物超过 32 MiB")
                digest.update(chunk)
            if count != self.size or digest.hexdigest() != self.sha256:
                raise ValueError("文件产物校验失败")
            stream.seek(0)
            return stream
        except BaseException:
            stream.close()
            raise


def freeze_file(source: Path, root: Path, name: str | None = None) -> FileArtifact:
    """
    有界流式复制已授权文件, 不覆盖已有产物

    参数:
    - source: 已经过 FileRead 审批及指纹复核的普通文件
    - root: 当前宿主产物目录
    - name: 可选显示名称, 默认源名称

    返回:
    - 内容固定的文件引用; 复制失败会清理未完成文件
    """
    label = display_name(source.name if name is None else name)
    _checked_root(root)
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError("文件产物目录不可为符号链接")
    root.mkdir(parents=True, exist_ok=True)
    count, retained = 0, 0
    for item in root.glob("sf_*"):
        count += 1
        if item.is_file():
            retained += item.stat().st_size
        if count >= 128 or retained >= 512 * 1024 * 1024:
            raise ValueError("当前会话文件产物已达到 128 项或 512 MiB 上限, 请先清理或新建会话")
    target = root / ("sf_" + uuid.uuid4().hex)
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(source, flags), "rb") as stream, target.open("xb") as output:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= MAX_FILE_BYTES:
                raise ValueError("仅支持不超过 32 MiB 的普通文件")
            if retained + info.st_size > 512 * 1024 * 1024:
                raise ValueError("当前会话文件产物总量超过 512 MiB")
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ValueError("文件在复制期间超过 32 MiB")
                digest.update(chunk)
                output.write(chunk)
            if size != info.st_size:
                raise ValueError("文件在复制期间发生变化")
            output.flush()
            os.fsync(output.fileno())
        return FileArtifact(root, target.name, label, size, digest.hexdigest(), mimetypes.guess_type(label)[0] or "application/octet-stream")
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def artifact_from_metadata(root: Path, data: Mapping[str, object]) -> FileArtifact:
    """
    从会话记录重建文件引用, 不接受记录中的路径

    参数:
    - root: 宿主解析的本会话目录
    - data: 已从当前会话回复版本查到的文件资料

    返回:
    - 类型完整的受控文件引用, 无效数据拒绝
    """
    file_id, name, size, digest, mime = (data.get(key) for key in ("file_id", "name", "size", "sha256", "mime"))
    if (not isinstance(file_id, str) or not isinstance(name, str) or not isinstance(size, int) or isinstance(size, bool)
            or not isinstance(digest, str) or not isinstance(mime, str)):
        raise ValueError("文件附件资料无效")
    return FileArtifact(root, file_id, name, size, digest, mime)


def copy_artifact(artifact: FileArtifact, root: Path) -> None:
    """
    分支只复制已选回复版本的文件, 保留 ID 但建立独立会话所有权

    参数:
    - artifact: 来源会话中已校验的附件
    - root: 目标会话产物目录
    """
    _checked_root(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / artifact.file_id
    if target.exists():
        from dataclasses import replace
        replace(artifact, root=root).open().close()
        return
    try:
        with artifact.open() as source, target.open("xb") as output:
            while chunk := source.read(CHUNK_BYTES):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


async def open_artifact(artifact: FileArtifact) -> BinaryIO:
    """
    在线程中校验并打开文件, 取消时等待操作完成再关闭句柄

    参数:
    - artifact: 受控文件引用

    返回:
    - 已打开的文件流, 调用者负责关闭
    """
    task = asyncio.create_task(asyncio.to_thread(artifact.open))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await asyncio.gather(task, return_exceptions=True)
        if result and not isinstance(result[0], BaseException):
            result[0].close()
        raise


@dataclass
class _Receiver:
    callback: Callable[[FileArtifact], Awaitable[dict[str, object]]]
    loop: asyncio.AbstractEventLoop | None = None
    active: bool = True


_RECEIVER: ContextVar[_Receiver | None] = ContextVar("satrap_file_receiver", default=None)


@contextmanager
def bind_file_receiver(callback: Callable[[FileArtifact], Awaitable[dict[str, object]]]) -> Iterator[None]:
    """
    绑定 Chat 本轮文件接收器, 退出后撤销被复制的任务上下文

    参数:
    - callback: 宿主负责绑定会话及回复版本的回调

    返回:
    - 仅本次执行有效的接收作用域
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    receiver = _Receiver(callback, loop)
    token = _RECEIVER.set(receiver)
    try:
        yield
    finally:
        receiver.active = False
        _RECEIVER.reset(token)


def current_file_receiver() -> Callable[[FileArtifact], Awaitable[dict[str, object]]] | None:
    """
    获取仍有效的本轮接收器

    返回:
    - 宿主回调或 None, 不从模型参数解析接收对象
    """
    receiver = _RECEIVER.get()
    return receiver.callback if receiver is not None and receiver.active else None


def file_receiver_loop() -> asyncio.AbstractEventLoop | None:
    """
    获取绑定接收器的事件循环, 供同步工具跨线程交付

    返回:
    - 原始宿主循环或 None
    """
    receiver = _RECEIVER.get()
    return receiver.loop if receiver is not None and receiver.active else None
