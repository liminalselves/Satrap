"""用户表情库和逐群集合授权, 内容不可原位替换, 删除不破坏在途文件"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any
import base64
import hashlib
import json
import re
import sqlite3
import uuid
import time

from PIL import Image as PillowImage

from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
from satrap.core.group_chat.assets import AssetLease, inspect_image, lease_file, read_blob, remove_unleased_file, _LOCK, _LEASES
from satrap.core.group_chat.types import GroupChatError
from satrap.core.storage.layout import StorageLayout


def library_for(archive: PlatformMessageStore) -> StickerStore:
    """
    从可信平台数据库位置定位共享用户表情库

    参数:
    - archive: 宿主装配的档案, 独立测试数据库使用其同级目录

    返回:
    - 用户数据根下的共享表情库
    """
    parent = archive.database.parent
    root = parent.parent.parent if parent.parent.name == "platforms" else parent
    return StickerStore(StorageLayout(root))


def _text(value: object, maximum: int = 80) -> str:
    """
    校验用户提供的表情名称, 标签和集合名

    参数:
    - value: 用户输入
    - maximum: 字符上限

    返回:
    - 去除两端空白的有限文本
    """
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("名称和标签不能为空, 含控制字符或超过长度限制")
    return value.strip()


class StickerStore:
    """控制 API 管理全库, 模型只读取当前群已授权且平台兼容的条目"""

    def __init__(self, layout: StorageLayout) -> None:
        """
        定位用户共享目录

        参数:
        - layout: 当前实例的数据布局
        """
        self.root = layout.root / "group-chat"
        self.files = self.root / "stickers"
        self.database = self.root / "catalog.db"

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """在单个短事务中修改表情或群授权"""
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.resolve() != self.root.absolute() or self.database.is_symlink():
            raise ValueError("表情库路径不是受管理目录")
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE IF NOT EXISTS stickers(id TEXT PRIMARY KEY,name TEXT NOT NULL,tags_json TEXT NOT NULL,"
                                   "collection TEXT NOT NULL,kind TEXT NOT NULL,blob_key TEXT,mime_type TEXT,width INTEGER,height INTEGER,"
                                   "size_bytes INTEGER,adapter_type TEXT,native_key TEXT,revision INTEGER NOT NULL DEFAULT 1,"
                                   "enabled INTEGER NOT NULL DEFAULT 1,deleted INTEGER NOT NULL DEFAULT 0,"
                                   "create_key TEXT UNIQUE,last_key TEXT,last_fingerprint TEXT,deleted_at REAL)")
                connection.execute("CREATE TABLE IF NOT EXISTS sticker_scopes(scope_key TEXT PRIMARY KEY,collections_json TEXT NOT NULL,"
                                   "revision INTEGER NOT NULL,last_key TEXT,last_fingerprint TEXT)")
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _item(row: sqlite3.Row) -> dict[str, Any]:
        """
        隐去物理位置和原生平台编码

        参数:
        - row: 受控表情目录行

        返回:
        - 用户和模型均可读取的有限目录字段
        """
        return {"sticker_id": row["id"], "name": row["name"], "tags": json.loads(row["tags_json"]), "kind": row["kind"],
                "collection": row["collection"], "content_revision": row["revision"], "enabled": bool(row["enabled"]),
                "adapter_type": row["adapter_type"], "mime_type": row["mime_type"], "size_bytes": row["size_bytes"],
                "width": row["width"], "height": row["height"]}

    def list(self, *, keyword: str = "", limit: int = 20, cursor: str | None = None, scope: MessageScope | None = None,
             adapter_type: str = "", formats: tuple[str, ...] | None = None, native: bool = False) -> dict[str, Any]:
        """
        分页返回全库或当前群的兼容表情, 游标绑定所有筛选条件

        参数:
        - keyword: 名称或标签文字
        - limit: 每页 1 到 100 条
        - cursor: 上页继续位置
        - scope: 模型查询必须提供的完整群身份
        - adapter_type: 当前适配器协议类型
        - formats: 当前平台可发送图片格式
        - native: 当前平台是否支持原生表情

        返回:
        - 有限条目和续页游标
        """
        if not isinstance(keyword, str) or len(keyword) > 256 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("表情筛选或条数无效")
        with _LOCK, self._transaction() as connection:
            settings = self._settings(connection, scope) if scope else None
            collections = settings["collections"] if settings else []
            fingerprint = hashlib.sha256(json.dumps([scope.key if scope else None, keyword, adapter_type, formats,
                                                       native, settings], ensure_ascii=False).encode("utf-8")).hexdigest()
            offset = 0
            if cursor:
                try:
                    if len(cursor) > 4096:
                        raise ValueError
                    value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                    if len(value) != 2 or value[0] != fingerprint or type(value[1]) is not int or not 0 <= value[1] <= 2000:
                        raise ValueError
                    offset = value[1]
                except Exception as exc:
                    raise ValueError("表情游标已失效或不属于当前查询, 请重新查询") from exc
            rows = connection.execute("SELECT * FROM stickers WHERE deleted=0 ORDER BY name,id").fetchall()
            items = []
            for row in rows:
                if scope and (not row["enabled"] or row["collection"] not in collections):
                    continue
                if scope and ((row["kind"] == "native" and (not native or row["adapter_type"] != adapter_type))
                              or (row["kind"] == "image" and row["mime_type"] not in (formats or ()))):
                    continue
                if keyword.casefold() not in (row["name"] + " " + " ".join(json.loads(row["tags_json"]))).casefold():
                    continue
                items.append(self._item(row))
            more = offset + limit < len(items)
            next_cursor = base64.urlsafe_b64encode(json.dumps([fingerprint, offset + limit]).encode()).decode().rstrip("=") if more else None
            return {"ok": True, "items": items[offset:offset + limit], "has_more": more, "next_cursor": next_cursor}

    def create(self, metadata: dict[str, Any], payload: bytes | None = None, *, adapter_type: str = "",
               native_key: str = "") -> dict[str, Any]:
        """
        登记用户上传或适配器已确认目录中的表情, 重试不重复添加

        参数:
        - metadata: 名称, 标签, 集合及幂等键
        - payload: 图片字节, 原生表情为空
        - adapter_type: 原生表情所属协议, 由控制宿主固定
        - native_key: 适配器确认的原生目录键, 不从模型参数取得

        返回:
        - 新条目或同一上传的重放结果
        """
        if set(metadata) != {"name", "tags", "collection", "idempotency_key"}:
            raise ValueError("表情添加字段不符")
        name, collection, key = _text(metadata["name"]), _text(metadata["collection"], 64), _text(metadata["idempotency_key"], 256)
        tags = self._tags(metadata["tags"])
        image = inspect_image(payload) if payload is not None else {}
        if payload is None and (not adapter_type or not native_key):
            raise ValueError("原生表情必须来自适配器目录")
        blob_key = hashlib.sha256(payload).hexdigest() if payload is not None else None
        fingerprint = hashlib.sha256(json.dumps([name, tags, collection, blob_key, adapter_type, native_key], ensure_ascii=False).encode()).hexdigest()
        with _LOCK, self._transaction() as connection:
            old = connection.execute("SELECT * FROM stickers WHERE create_key=?", (key,)).fetchone()
            if old:
                if old["last_fingerprint"] != fingerprint or old["deleted"]:
                    raise GroupChatError("revision_conflict", "上传请求已经改变或原条目已删除")
                return {"ok": True, "sticker": self._item(old), "replayed": True}
            if connection.execute("SELECT COUNT(*) FROM stickers WHERE deleted=0").fetchone()[0] >= 2000:
                raise GroupChatError("quota_exceeded", "表情库最多保存 2000 个条目")
            self.files.mkdir(parents=True, exist_ok=True)
            if self.files.is_symlink():
                raise ValueError("表情文件目录不可为符号链接")
            if payload is not None and blob_key is not None:
                path = self.files / blob_key
                if path.exists():
                    read_blob(path, blob_key, len(payload))
                used = sum(p.stat().st_size for p in self.files.iterdir() if p.is_file())
                if not path.exists() and used + len(payload) > 256 * 1024 * 1024:
                    raise GroupChatError("quota_exceeded", "表情图片库最多占用 256 MiB")
                if not path.exists():
                    path.write_bytes(payload)
            identity = "st_" + uuid.uuid4().hex
            connection.execute("INSERT INTO stickers(id,name,tags_json,collection,kind,blob_key,mime_type,width,height,size_bytes,"
                               "adapter_type,native_key,create_key,last_key,last_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (identity, name, json.dumps(tags, ensure_ascii=False), collection, "image" if payload is not None else "native",
                                blob_key, image.get("mime_type"), image.get("width"), image.get("height"), image.get("size_bytes"),
                                adapter_type or None, native_key or None, key, key, fingerprint))
            row = connection.execute("SELECT * FROM stickers WHERE id=?", (identity,)).fetchone()
            return {"ok": True, "sticker": self._item(row), "replayed": False}

    @staticmethod
    def _tags(value: object) -> list[str]:
        """
        校验有限标签列表

        参数:
        - value: 用户标签

        返回:
        - 去重后的标签
        """
        if not isinstance(value, list) or len(value) > 12:
            raise ValueError("表情标签必须是最多 12 个名称")
        return list(dict.fromkeys(_text(item, 32) for item in value))

    def mutate(self, identity: str, payload: dict[str, Any], *, delete: bool = False) -> dict[str, Any]:
        """
        按修订编辑或删除表情, 相同意图重放不会覆盖后来修改

        参数:
        - identity: 目录 ID
        - payload: 期望修订, 幂等键及编辑字段
        - delete: 是否删除并撤销授权

        返回:
        - 已保存条目或删除状态
        """
        expected = {"expected_revision", "idempotency_key"} | (set() if delete else {"name", "tags", "collection", "enabled"})
        if set(payload) != expected or type(payload["expected_revision"]) is not int:
            raise ValueError("表情编辑必须提供正确字段和整数修订")
        key = _text(payload["idempotency_key"], 256)
        fingerprint = hashlib.sha256(json.dumps([delete, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with _LOCK, self._transaction() as connection:
            row = connection.execute("SELECT * FROM stickers WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise GroupChatError("not_found", "表情不存在")
            if row["last_key"] == key and row["last_fingerprint"] == fingerprint:
                return {"ok": True, "deleted": bool(row["deleted"]), "sticker": self._item(row), "replayed": True}
            if row["deleted"] or row["revision"] != payload["expected_revision"]:
                raise GroupChatError("revision_conflict", "表情已被修改或删除, 请刷新后重试")
            if delete:
                connection.execute("UPDATE stickers SET deleted=1,enabled=0,revision=revision+1,last_key=?,last_fingerprint=?,deleted_at=? WHERE id=?",
                                   (key, fingerprint, time.time(), identity))
            else:
                if type(payload["enabled"]) is not bool:
                    raise ValueError("表情启用状态必须是布尔值")
                connection.execute("UPDATE stickers SET name=?,tags_json=?,collection=?,enabled=?,revision=revision+1,last_key=?,last_fingerprint=? WHERE id=?",
                                   (_text(payload["name"]), json.dumps(self._tags(payload["tags"]), ensure_ascii=False),
                                    _text(payload["collection"], 64), int(payload["enabled"]), key, fingerprint, identity))
            updated = connection.execute("SELECT * FROM stickers WHERE id=?", (identity,)).fetchone()
            return {"ok": True, "deleted": delete, "sticker": self._item(updated)}

    def _settings(self, connection: sqlite3.Connection, scope: MessageScope) -> dict[str, Any]:
        """
        读取逐群授权, 没有记录时默认不启用集合

        参数:
        - connection: 当前目录事务
        - scope: 完整群身份

        返回:
        - 已选集合与修订
        """
        row = connection.execute("SELECT * FROM sticker_scopes WHERE scope_key=?", (scope.key,)).fetchone()
        return {"collections": json.loads(row["collections_json"]) if row else [], "revision": row["revision"] if row else 0}

    def settings(self, scope: MessageScope, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        查看或保存当前群可用的表情集合

        参数:
        - scope: 管理入口或工具宿主确认的完整对话身份
        - payload: 可选的新集合, 期望修订和幂等键

        返回:
        - 逐群授权和全库可选集合, 包括已经选择的空集合
        """
        with _LOCK, self._transaction() as connection:
            current = self._settings(connection, scope)
            if payload is not None:
                if set(payload) != {"collections", "expected_revision", "idempotency_key"} or type(payload["expected_revision"]) is not int:
                    raise ValueError("集合设置字段不符")
                values = payload["collections"]
                if not isinstance(values, list) or len(values) > 50:
                    raise ValueError("每群最多选择 50 个集合")
                values = sorted(set(_text(item, 64) for item in values))
                key = _text(payload["idempotency_key"], 256)
                fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                previous = connection.execute("SELECT * FROM sticker_scopes WHERE scope_key=?", (scope.key,)).fetchone()
                replayed = previous is not None and previous["last_key"] == key and previous["last_fingerprint"] == fingerprint
                if not replayed:
                    if payload["expected_revision"] != current["revision"]:
                        raise GroupChatError("revision_conflict", "群表情设置已变化, 请刷新后重试")
                    connection.execute("INSERT INTO sticker_scopes VALUES(?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET "
                                       "collections_json=excluded.collections_json,revision=excluded.revision,last_key=excluded.last_key,last_fingerprint=excluded.last_fingerprint",
                                       (scope.key, json.dumps(values, ensure_ascii=False), current["revision"] + 1, key, fingerprint))
                current = self._settings(connection, scope)
            choices = sorted({row[0] for row in connection.execute("SELECT DISTINCT collection FROM stickers WHERE deleted=0")} | set(current["collections"]))
            return {"ok": True, **current, "available_collections": choices}

    def acquire(self, scope: MessageScope, identity: str, adapter_type: str, formats: tuple[str, ...], native: bool) -> tuple[dict[str, Any], AssetLease | None]:
        """
        发送之前重新核验群集合授权并固定原图片文件

        参数:
        - scope: 当前群
        - identity: 目录 ID
        - adapter_type: 当前适配器协议
        - formats: 支持的图片格式
        - native: 是否支持原生表情组件

        返回:
        - 内部目录和文件租约, 原生表情没有文件
        """
        with _LOCK, self._transaction() as connection:
            row = connection.execute("SELECT * FROM stickers WHERE id=? AND deleted=0 AND enabled=1", (identity,)).fetchone()
            if row is None or row["collection"] not in self._settings(connection, scope)["collections"]:
                raise GroupChatError("asset_unavailable", "表情已停用, 删除或未在当前群启用")
            if row["kind"] == "native":
                if not native or row["adapter_type"] != adapter_type:
                    raise GroupChatError("unsupported", "该原生表情与当前平台不兼容")
                return dict(row), None
            if row["mime_type"] not in formats:
                raise GroupChatError("unsupported", "该表情图片格式与当前平台不兼容")
            path = self.files / row["blob_key"]
            read_blob(path, row["blob_key"], row["size_bytes"])
            return dict(row), lease_file(path)

    def preview(self, identity: str) -> dict[str, Any]:
        """
        生成认证界面专用的有限缩略图, 不暴露路径和原始文件令牌

        参数:
        - identity: 表情目录 ID

        返回:
        - 最多 256 像素的 PNG 预览或原生表情名称
        """
        with _LOCK, self._transaction() as connection:
            row = connection.execute("SELECT * FROM stickers WHERE id=? AND deleted=0", (identity,)).fetchone()
            if row is None:
                raise GroupChatError("not_found", "表情已删除或不存在")
            if row["kind"] == "native":
                return {"ok": True, "name": row["name"], "preview": None}
            path = self.files / row["blob_key"]
            payload = read_blob(path, row["blob_key"], row["size_bytes"])
            with PillowImage.open(BytesIO(payload)) as image:
                image.thumbnail((256, 256))
                output = BytesIO()
                image.convert("RGBA").save(output, format="PNG")
            return {"ok": True, "preview": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")}

    def purge(self) -> None:
        """清除没有活动授权和在途租约的表情文件, 保留删除幂等墓碑"""
        with _LOCK, self._transaction() as connection:
            keep = {row[0] for row in connection.execute("SELECT blob_key FROM stickers WHERE deleted=0 AND blob_key IS NOT NULL")}
            connection.execute("DELETE FROM stickers WHERE deleted=1 AND deleted_at<?", (time.time() - 30 * 86400,))
            if self.files.is_dir() and not self.files.is_symlink():
                for path in self.files.iterdir():
                    if re.fullmatch(r"[a-f0-9]{64}", path.name) and path.name not in keep and path not in _LEASES and path.is_file() and not path.is_symlink():
                        remove_unleased_file(path)
