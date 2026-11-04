"""保存一次生成的图片和轻量索引, 重发不复制或重新生成图片"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Any, cast

from satrap.core.APICall.ImageCall import ImageGenResponse
from satrap.core.file_access.artifacts import FileArtifact, MAX_FILE_BYTES, artifact_from_metadata, freeze_file


_SAVE_LOCK = RLock()
_FILE_ID = re.compile(r"sf_[0-9a-f]{32}\Z")
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _index_root(root: Path) -> Path:
    """取得当前产物目录内的图片索引, 拒绝目录重定向"""
    index = root / ".image_generation"
    if index.resolve() != index.absolute() or index.is_symlink():
        raise ValueError("生图索引目录已经变化")
    return index


def check_capacity(root: Path, count: int) -> None:
    """请求前检查已知产物数量和占用, 避免目录已满时仍触发生图计费"""
    _index_root(root)
    existing = list(root.glob("sf_*"))
    retained = sum(path.stat().st_size for path in existing if path.is_file())
    if len(existing) + count > 128 or retained >= 512 * 1024 * 1024:
        raise ValueError("当前会话文件产物已达到数量或空间上限, 请先清理或新建会话")


def save_images(root: Path, response: ImageGenResponse, check: Callable[[], None]) -> list[dict[str, Any]]:
    """
    冻结图片并保存不含提示词和凭据的元信息

    参数:
    - root: 当前会话的宿主产物目录, 调用方持有维护租约
    - response: 调用层已经验证的图片响应
    - check: 线程开始和每次写入前复核宿主来源

    返回:
    - 包含文件引用和图片尺寸的记录, 图片字节仅保存一份
    """
    with _SAVE_LOCK:
        check()
        existing = list(root.glob("sf_*"))
        retained = sum(path.stat().st_size for path in existing if path.is_file())
        if len(existing) + len(response.images) > 128 or retained + sum(len(image.data) for image in response.images) > 512 * 1024 * 1024:
            raise ValueError("当前会话文件产物将超过 128 项或 512 MiB 上限")
        if any(image.mime_type not in _EXTENSIONS or not 0 < len(image.data) <= MAX_FILE_BYTES for image in response.images):
            raise ValueError("图片格式或大小不满足会话文件产物要求")
        index = _index_root(root)
        index.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        with TemporaryDirectory(prefix="satrap-image-") as temporary:
            for number, image in enumerate(response.images, 1):
                check()
                source = Path(temporary) / f"image-{number}.{_EXTENSIONS[image.mime_type]}"
                source.write_bytes(image.data)
                artifact = freeze_file(source, root)
                record: dict[str, Any] = {"file": artifact.public(), "width": image.width, "height": image.height, "seed": image.seed}
                manifest = index / (artifact.file_id + ".json")
                try:
                    with manifest.open("x", encoding="utf-8") as stream:
                        json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException:
                    manifest.unlink(missing_ok=True)
                    artifact.path.unlink(missing_ok=True)
                    raise
                records.append(record)
                source.unlink()
        return records


def load_image(root: Path, file_id: str) -> tuple[FileArtifact, dict[str, Any]]:
    """
    按当前会话中的生成记录重建图片引用, 不接受模型提供路径或摘要

    参数:
    - root: 当前宿主产物目录
    - file_id: generate_image 返回的不透明文件 ID

    返回:
    - 已校验完整性的图片产物与记录
    """
    if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
        raise ValueError("file_id 必须是当前会话生成图片的文件 ID")
    manifest = _index_root(root) / (file_id + ".json")
    if manifest.is_symlink() or manifest.resolve() != manifest.absolute() or not manifest.is_file() or manifest.stat().st_size > 16384:
        raise ValueError("生成图片记录不存在或已经回收")
    with manifest.open("r", encoding="utf-8") as stream:
        raw: object = json.load(stream)
    if not isinstance(raw, dict):
        raise ValueError("生成图片记录无效")
    record = cast(dict[str, Any], raw)
    if not isinstance(record.get("file"), dict) or record["file"].get("file_id") != file_id:
        raise ValueError("生成图片记录无效")
    artifact = artifact_from_metadata(root, record["file"])
    if artifact.mime not in _EXTENSIONS:
        raise ValueError("生成图片记录类型无效")
    artifact.verify()
    return artifact, record
