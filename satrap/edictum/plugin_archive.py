"""ZIP 插件预览与原子安装, 校验期间不执行插件代码"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from typing import Any
import zipfile
import yaml

from satrap.edictum.plugin_catalog import PluginCatalog

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 2000
PREVIEW_TTL_SECONDS = 600
MAX_PENDING_PREVIEWS = 3
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}


def _safe_parts(name: str) -> tuple[str, ...]:
    """
    校验同时适用于 Windows 和 POSIX 的压缩包相对路径

    参数:
    - name: ZIP 成员路径

    返回:
    - 路径分量, 非法路径抛出 ValueError
    """
    raw = name.rstrip("/")
    parts = tuple(raw.split("/"))
    if not raw or PurePosixPath(raw).is_absolute() or any(
        not part or part in {".", ".."} or part.endswith((".", " "))
        or part.split(".")[0].upper() in _RESERVED_NAMES
        or any(ord(char) < 32 or char in '\\:<>"|?*' for char in part)
        for part in parts
    ):
        raise ValueError(f"压缩包包含非法路径: {name}")
    return parts


def _move_no_replace(source: Path, target: Path) -> None:
    """
    原子移动目录并拒绝覆盖并发创建的目标

    参数:
    - source: 同一文件系统中的完整插件目录
    - target: 用户插件目标目录
    """
    if os.name == "nt":
        source.rename(target)
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        move = library.renameat2
        move.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = move(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    elif sys.platform == "darwin":
        move = library.renamex_np
        move.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = move(os.fsencode(source), os.fsencode(target), 4)
    else:
        raise OSError("当前系统不支持不覆盖目标的原子安装")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


@dataclass
class _Preview:
    temporary: tempfile.TemporaryDirectory[str]
    plugin_dir: Path
    name: str
    expires_at: float


class PluginArchiveInstaller:
    """有界临时预览与用户目录安装服务"""

    def __init__(self, catalog: PluginCatalog) -> None:
        """
        初始化安装服务

        参数:
        - catalog: 用于冲突检测及用户安装位置的共享目录
        """
        self.catalog = catalog
        self._previews: dict[str, _Preview] = {}
        self._lock = threading.RLock()

    def _prune(self) -> None:
        """清理已过期的预览, 调用方需持有锁"""
        for token, preview in list(self._previews.items()):
            if preview.expires_at <= time.monotonic():
                self._previews.pop(token).temporary.cleanup()

    def _check_conflict(self, name: str) -> None:
        """
        拒绝同名目录及同名元数据插件, 包括内置插件

        参数:
        - name: 已校验的插件名称
        """
        if os.path.lexists(self.catalog.user_dir / name) or any(
            entry.name.casefold() == name.casefold() for entry in self.catalog.scan()
        ):
            raise ValueError(f"已存在同名插件: {name}; 本版本不支持覆盖安装")

    def preview(self, content: bytes) -> dict[str, Any]:
        """
        有界解包并读取元数据, 返回一次性安装凭据

        参数:
        - content: 原始 ZIP 文件内容, 最大 20 MiB

        返回:
        - 插件元数据和十分钟有效的 token; 校验失败不保留临时文件
        """
        if not content or len(content) > MAX_ARCHIVE_BYTES:
            raise ValueError("ZIP 文件必须非空且不超过 20 MiB")
        with self._lock:
            self._prune()
            if len(self._previews) >= MAX_PENDING_PREVIEWS:
                raise ValueError("待安装预览过多, 请关闭已有预览后重试")
            staging = self.catalog.user_dir.resolve().parent / ".plugin-install"
            staging.mkdir(parents=True, exist_ok=True)
            temporary = tempfile.TemporaryDirectory(prefix="preview-", dir=staging)
            root = Path(temporary.name)
            try:
                # Step.1 验证成员结构与大小, 不直接使用 extractall
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    members = archive.infolist()
                    if not members or len(members) > MAX_ARCHIVE_FILES:
                        raise ValueError("ZIP 文件为空或条目数量超过 2000")
                    if sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                        raise ValueError("ZIP 解压后大小超过 100 MiB")
                    files: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
                    seen: set[str] = set()
                    casing: dict[str, str] = {}
                    for item in members:
                        parts = _safe_parts(item.orig_filename)
                        if item.orig_filename != item.filename:
                            raise ValueError("ZIP 包含截断的文件名")
                        key = "/".join(parts).casefold()
                        if key in seen:
                            raise ValueError(f"ZIP 包含重复路径: {item.filename}")
                        seen.add(key)
                        for length in range(1, len(parts) + 1):
                            prefix = "/".join(parts[:length])
                            if casing.setdefault(prefix.casefold(), prefix) != prefix:
                                raise ValueError("ZIP 包含大小写冲突的路径")
                        mode = item.external_attr >> 16
                        if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR} or item.external_attr & 0x400:
                            raise ValueError("ZIP 不允许链接或特殊文件")
                        if item.flag_bits & 1:
                            raise ValueError("ZIP 不支持加密文件")
                        if not item.is_dir():
                            files.append((item, parts))
                    manifests = [parts for _, parts in files if parts[-1] == "meta.yaml"]
                    if len(manifests) != 1 or len(manifests[0]) not in {1, 2}:
                        raise ValueError("ZIP 必须包含一个根目录或单层目录下的 meta.yaml")
                    if any(item.file_size > 1024 * 1024 for item, parts in files if parts[-1] == "meta.yaml"):
                        raise ValueError("插件 meta.yaml 不得超过 1 MiB")
                    prefix = manifests[0][:-1]
                    if any(parts[:len(prefix)] != prefix for _, parts in files):
                        raise ValueError("ZIP 包含插件目录以外的文件")

                    # Step.2 流式解压, 实际读取量也受总大小限制
                    expanded = 0
                    for item, parts in files:
                        destination = root.joinpath(*parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item) as source, destination.open("xb") as output:
                            while chunk := source.read(64 * 1024):
                                expanded += len(chunk)
                                if expanded > MAX_EXPANDED_BYTES:
                                    raise ValueError("ZIP 解压后大小超过 100 MiB")
                                output.write(chunk)

                # Step.3 仅解析声明, 冲突检测通过后提供预览
                plugin_dir = root.joinpath(*prefix)
                try:
                    entry = self.catalog._load_entry(plugin_dir)
                    json.dumps(entry.to_payload(), allow_nan=False)
                except (yaml.YAMLError, RecursionError, TypeError) as error:
                    raise ValueError("插件元数据格式无效") from error
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", entry.name):
                    raise ValueError("插件名称须为 1 到 64 位英文字母、数字、下划线、点或连字符")
                _safe_parts(entry.name)
                self._check_conflict(entry.name)
                token = secrets.token_urlsafe(32)
                self._previews[token] = _Preview(temporary, plugin_dir, entry.name, time.monotonic() + PREVIEW_TTL_SECONDS)
                return {"token": token, "plugin": entry.to_payload(), "expires_in": PREVIEW_TTL_SECONDS, "expanded_bytes": expanded, "file_count": len(files)}
            except Exception:
                temporary.cleanup()
                raise

    def install(self, token: str) -> dict[str, Any]:
        """
        消费预览凭据, 原子移动插件到用户目录

        参数:
        - token: 预览返回的一次性凭据

        返回:
        - 已安装插件元数据; 冲突或过期时抛出 ValueError, 不修改已有插件
        """
        with self._lock:
            self._prune()
            preview = self._previews.pop(token, None)
            if preview is None:
                raise ValueError("安装预览已过期或已使用, 请重新选择 ZIP")
            try:
                self._check_conflict(preview.name)
                self.catalog.user_dir.mkdir(parents=True, exist_ok=True)
                entry = self.catalog._load_entry(preview.plugin_dir)
                if entry.name != preview.name:
                    raise ValueError("预览元数据已变化, 请重新上传")
                _move_no_replace(preview.plugin_dir, self.catalog.user_dir / preview.name)
                # 临时目录与目标目录位于同一文件系统, 不暴露半解压状态
                return {"ok": True, "plugin": {**entry.to_payload(), "source": "user"}}
            finally:
                preview.temporary.cleanup()

    def discard(self, token: str) -> None:
        """
        取消预览并立即清理临时文件

        参数:
        - token: 预览返回的凭据, 不存在时忽略
        """
        with self._lock:
            self._prune()
            preview = self._previews.pop(token, None)
            if preview is not None:
                preview.temporary.cleanup()
