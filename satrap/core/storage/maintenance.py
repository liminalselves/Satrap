"""Satrap v2 数据审计、可恢复回收和孤儿清理服务"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import shutil
from typing import Any, Iterable, cast
import json
import time

from satrap.core.storage.database import (
    delete_session_domain_rows,
    restore_session_domain,
    snapshot_session_domain,
)
from satrap.core.storage.layout import StorageLayout


@dataclass(frozen=True)
class StorageAuditItem:
    """一个可预览的数据维护项目"""

    item_id: str
    category: str
    platform_id: str
    session_id: str
    path: str
    size_bytes: int
    modified_at: float
    reason: str
    recommended_action: str
    auto_safe: bool
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """返回适合 HTTP API 的字典"""
        return asdict(self)


class StorageMaintenanceService:
    """扫描和维护 StorageLayout 管理的数据"""

    def __init__(self, layout: StorageLayout) -> None:
        """
        初始化维护服务

        参数:
        - layout: v2 数据布局
        """
        self.layout = layout

    @staticmethod
    def _item_id(*parts: str) -> str:
        """根据稳定身份字段生成审计项目 ID"""
        raw = "\0".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _directory_size(path: Path) -> int:
        """计算目录大小且不跟随符号链接"""
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        total = 0
        for child in path.rglob("*"):
            if child.is_symlink() or not child.is_file():
                continue
            try:
                total += child.stat().st_size
            except OSError:
                continue
        return total

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        """读取 JSON 对象, 无效时返回 None"""
        try:
            value: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        return {
            str(key): item
            for key, item in cast(dict[object, Any], value).items()
        }

    def _resolve_archive_path(self, platform_id: str, archive_id: str) -> Path:
        """
        解析并校验单个会话回收包路径

        参数:
        - platform_id: 平台实例 ID
        - archive_id: 回收包 ID

        返回:
        - Path: 位于平台回收目录内的真实路径
        """
        normalized_id = archive_id.strip()
        if (
            not normalized_id
            or normalized_id != archive_id
            or normalized_id in {".", ".."}
            or Path(normalized_id).name != normalized_id
            or "/" in normalized_id
            or "\\" in normalized_id
        ):
            raise ValueError("非法回收包路径")
        trash_root = (self.layout.trash_root(platform_id) / "sessions").resolve()
        candidate = trash_root / normalized_id
        if candidate.is_symlink():
            raise ValueError("拒绝使用符号链接回收包")
        target = candidate.resolve()
        if target.parent != trash_root:
            raise ValueError("非法回收包路径")
        return target

    @staticmethod
    def _resolve_archive_member(archive: Path, name: str) -> Path:
        """
        解析回收包固定成员并拒绝符号链接逃逸

        参数:
        - archive: 已验证的回收包目录
        - name: 服务端定义的固定成员名

        返回:
        - Path: 位于回收包内的真实成员路径
        """
        member = archive / name
        if member.is_symlink():
            raise ValueError(f"拒绝使用符号链接回收包成员: {name}")
        resolved = member.resolve()
        if resolved.parent != archive:
            raise ValueError(f"非法回收包成员路径: {name}")
        return resolved

    def _audit_item(
        self,
        *,
        category: str,
        platform_id: str,
        session_id: str = "",
        path: Path,
        reason: str,
        recommended_action: str,
        auto_safe: bool,
        details: dict[str, Any] | None = None,
    ) -> StorageAuditItem:
        """构建包含大小和时间的审计项目"""
        try:
            modified_at = path.lstat().st_mtime
        except OSError:
            modified_at = 0.0
        return StorageAuditItem(
            item_id=self._item_id(category, platform_id, session_id, str(path.resolve())),
            category=category,
            platform_id=platform_id,
            session_id=session_id,
            path=str(path),
            size_bytes=self._directory_size(path),
            modified_at=modified_at,
            reason=reason,
            recommended_action=recommended_action,
            auto_safe=auto_safe,
            details=dict(details or {}),
        )

    @staticmethod
    def _database_session_ids(database: Path) -> set[str]:
        """读取平台数据库中的持久化会话 ID"""
        if not database.exists():
            return set()
        with sqlite3.connect(str(database)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "session_configs" not in tables:
                return set()
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT session_id FROM session_configs"
                ).fetchall()
            }

    @staticmethod
    def _orphan_database_refs(database: Path, valid_ids: set[str]) -> dict[str, list[str]]:
        """返回不存在会话配置的领域记录引用"""
        if not database.exists():
            return {}
        refs: dict[str, set[str]] = {}
        with sqlite3.connect(str(database)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            selectors = {
                "chat_history": "SELECT DISTINCT conversation_id FROM chat_history",
                "state_scopes": (
                    "SELECT DISTINCT scope_id FROM state_scopes "
                    "WHERE namespace = 'conversation'"
                ),
                "state_checkpoints": (
                    "SELECT DISTINCT scope_id FROM state_checkpoints "
                    "WHERE namespace = 'conversation'"
                ),
                "state_snapshots": (
                    "SELECT DISTINCT scope_id FROM state_snapshots "
                    "WHERE namespace = 'conversation'"
                ),
                "context_sessions": "SELECT DISTINCT session_id FROM context_sessions",
            }
            for table, query in selectors.items():
                if table not in tables:
                    continue
                for row in connection.execute(query).fetchall():
                    session_id = str(row[0])
                    if session_id and session_id not in valid_ids:
                        refs.setdefault(session_id, set()).add(table)
            if "memories" in tables:
                for row in connection.execute(
                    "SELECT DISTINCT scope FROM memories WHERE scope LIKE 'session:%'"
                ).fetchall():
                    scope = str(row[0])
                    session_id = scope.removeprefix("session:")
                    if session_id and session_id not in valid_ids:
                        refs.setdefault(session_id, set()).add("memories")
            if "user_info" in tables:
                for row in connection.execute("SELECT user_session FROM user_info").fetchall():
                    try:
                        sessions: object = json.loads(row[0] or "[]")
                    except Exception:
                        sessions = []
                    if not isinstance(sessions, list):
                        continue
                    for raw_session_id in cast(list[object], sessions):
                        session_id = str(raw_session_id)
                        if session_id and session_id not in valid_ids:
                            refs.setdefault(session_id, set()).add("user_info")
        return {session_id: sorted(tables) for session_id, tables in refs.items()}

    def scan(
        self,
        configured_platform_ids: Iterable[str] | None = None,
    ) -> list[StorageAuditItem]:
        """
        只读扫描孤儿数据、脱离配置的平台和回收项

        参数:
        - configured_platform_ids: 可选的当前平台配置 ID

        返回:
        - list[StorageAuditItem]: 全部审计项目
        """
        configured = (
            {str(item).strip() for item in configured_platform_ids if str(item).strip()}
            if configured_platform_ids is not None
            else None
        )
        results: list[StorageAuditItem] = []
        root = self.layout.platforms_root
        if not root.exists():
            return results
        for platform_root in sorted(root.iterdir()):
            if platform_root.is_symlink():
                results.append(self._audit_item(
                    category="unsafe_entry",
                    platform_id="",
                    path=platform_root,
                    reason="平台目录是符号链接",
                    recommended_action="manual_review",
                    auto_safe=False,
                ))
                continue
            manifest = self._read_json(platform_root / "platform.json")
            platform_id = str((manifest or {}).get("platform_id", "")).strip()
            if not platform_id:
                results.append(self._audit_item(
                    category="invalid_manifest",
                    platform_id="",
                    path=platform_root,
                    reason="平台身份清单缺失或无效",
                    recommended_action="manual_review",
                    auto_safe=False,
                ))
                continue
            if configured is not None and platform_id not in configured:
                results.append(self._audit_item(
                    category="detached_platform",
                    platform_id=platform_id,
                    path=platform_root,
                    reason="平台数据存在, 但主配置中没有该平台",
                    recommended_action="manual_review",
                    auto_safe=False,
                ))
            database = platform_root / "platform.db"
            valid_ids = self._database_session_ids(database)
            sessions_root = platform_root / "sessions"
            if sessions_root.exists():
                for session_root in sorted(sessions_root.iterdir()):
                    if session_root.is_symlink():
                        results.append(self._audit_item(
                            category="unsafe_entry",
                            platform_id=platform_id,
                            path=session_root,
                            reason="会话目录是符号链接",
                            recommended_action="manual_review",
                            auto_safe=False,
                        ))
                        continue
                    session_manifest = self._read_json(session_root / "meta.json")
                    session_id = str((session_manifest or {}).get("session_id", "")).strip()
                    if not session_id:
                        results.append(self._audit_item(
                            category="invalid_manifest",
                            platform_id=platform_id,
                            path=session_root,
                            reason="会话身份清单缺失或无效",
                            recommended_action="manual_review",
                            auto_safe=False,
                        ))
                    elif session_id not in valid_ids:
                        results.append(self._audit_item(
                            category="orphan_session_directory",
                            platform_id=platform_id,
                            session_id=session_id,
                            path=session_root,
                            reason="会话目录没有对应的持久化 SessionConfig",
                            recommended_action="move_to_trash",
                            auto_safe=True,
                        ))
            for session_id, tables in self._orphan_database_refs(database, valid_ids).items():
                results.append(self._audit_item(
                    category=(
                        "broken_user_reference"
                        if set(tables).issubset({"context_sessions", "user_info"})
                        else "orphan_database_rows"
                    ),
                    platform_id=platform_id,
                    session_id=session_id,
                    path=database,
                    reason="数据库领域记录引用不存在的会话",
                    recommended_action="delete_database_rows",
                    auto_safe=True,
                    details={"tables": tables},
                ))
            trash_sessions = platform_root / "trash" / "sessions"
            if trash_sessions.exists():
                for archive in sorted(trash_sessions.iterdir()):
                    manifest = self._read_json(archive / "manifest.json")
                    results.append(self._audit_item(
                        category="trash_entry",
                        platform_id=platform_id,
                        session_id=str((manifest or {}).get("session_id", "")),
                        path=archive,
                        reason="会话数据位于回收区",
                        recommended_action="restore_or_purge",
                        auto_safe=False,
                        details={"archive_id": archive.name, "manifest": manifest or {}},
                    ))
        results.sort(key=lambda item: (item.platform_id, item.category, item.path))
        return results

    def archive_session(
        self,
        platform_id: str,
        session_id: str,
        *,
        database_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """
        将会话文件和数据库记录保存为可恢复回收包

        参数:
        - platform_id: 平台实例 ID
        - session_id: 会话 ID
        - database_path: 可选平台数据库路径, 默认由布局解析

        返回:
        - dict[str, Any]: 回收包清单
        """
        database = (
            Path(database_path)
            if database_path is not None
            else self.layout.platform_db(platform_id)
        )
        records = snapshot_session_domain(database, session_id)
        source = self.layout.session_root(platform_id, session_id)
        if not records and not source.exists():
            raise ValueError(f"会话不存在: {session_id}")
        archive_id = (
            time.strftime("%Y%m%d-%H%M%S", time.localtime())
            + "-"
            + str(time.time_ns())
            + "-"
            + self._item_id(platform_id, session_id, str(time.time_ns()))[:8]
        )
        archive = self.layout.trash_root(platform_id) / "sessions" / archive_id
        archive.mkdir(parents=True, exist_ok=False)
        files_root = archive / "files"
        moved_files = False
        try:
            (archive / "records.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if source.exists() or source.is_symlink():
                if source.is_symlink():
                    raise ValueError("拒绝回收符号链接会话目录")
                shutil.move(str(source), str(files_root))
                moved_files = True
            delete_session_domain_rows(database, session_id)
            manifest: dict[str, Any] = {
                "layout_version": self.layout.layout_version,
                "archive_version": 2,
                "archive_id": archive_id,
                "platform_id": platform_id,
                "session_id": session_id,
                "deleted_at": time.time(),
                "has_files": moved_files,
                "tables": sorted(records),
                "records_sha256": hashlib.sha256((archive / "records.json").read_bytes()).hexdigest(),
            }
            (archive / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return manifest
        except Exception:
            if moved_files and files_root.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(files_root), str(source))
            if records:
                try:
                    restore_session_domain(database, session_id, records)
                except Exception:
                    pass
            shutil.rmtree(archive, ignore_errors=True)
            raise

    def list_archives(self, platform_id: str) -> list[dict[str, Any]]:
        """
        列出指定平台的会话回收包及可展示元数据

        参数:
        - platform_id: 平台实例 ID

        返回:
        - list[dict[str, Any]]: 按删除时间倒序的回收项
        """
        trash_root = self.layout.trash_root(platform_id) / "sessions"
        if not trash_root.exists():
            return []
        results: list[dict[str, Any]] = []
        for archive in trash_root.iterdir():
            if archive.is_symlink() or not archive.is_dir():
                continue
            manifest = self._read_json(archive / "manifest.json")
            receipt = self._restore_receipt(self.layout.platform_db(platform_id), archive.name)
            if manifest is None and receipt is not None:
                manifest = json.loads(receipt["metadata"])
            if manifest is None or str(manifest.get("platform_id", "")) != platform_id:
                continue
            try:
                records = self._read_json_records(archive / "records.json")
                self._validate_archive_records(manifest, records, archive / "records.json")
                if manifest.get("layout_version") != self.layout.layout_version:
                    raise ValueError("回收包数据布局版本不匹配")
                metadata = self._archive_display_metadata(records)
                restore_error = ""
            except (ValueError, TypeError, OSError) as error:
                records = {}
                metadata = self._archive_display_metadata({})
                restore_error = str(error)
            if receipt and receipt["completed"]:
                restore_error = ""   # 已恢复包只需重试清理, records.json 可能已经移除
            deleted_at = manifest.get("deleted_at")
            results.append({
                "archive_id": archive.name,
                "platform_id": platform_id,
                "session_id": str(manifest.get("session_id", "")),
                "deleted_at": float(deleted_at) if isinstance(deleted_at, (int, float)) else 0,
                "size_bytes": self._directory_size(archive),
                "has_files": bool(manifest.get("has_files", False)),
                "tables": manifest.get("tables") if isinstance(manifest.get("tables"), list) else [],
                "restorable": not restore_error,
                "restore_error": restore_error,
                "cleanup_pending": bool(receipt and receipt["completed"]),
                **metadata,
            })
        results.sort(key=lambda item: float(item["deleted_at"]), reverse=True)
        return results

    @staticmethod
    def _read_json_records(path: Path) -> dict[str, list[dict[str, Any]]]:
        """
        读取回收包数据库记录

        参数:
        - path: records.json 路径

        返回:
        - dict[str, list[dict[str, Any]]]: 规范化后的表记录
        """
        try:
            value: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("回收包数据库记录无法读取") from error
        if not isinstance(value, dict):
            raise ValueError("回收包数据库记录必须为对象")
        records: dict[str, list[dict[str, Any]]] = {}
        for raw_table, raw_rows in cast(dict[object, object], value).items():
            if not isinstance(raw_rows, list):
                raise ValueError(f"归档表 {raw_table} 的记录必须为数组")
            rows: list[dict[str, Any]] = []
            for raw_row in cast(list[object], raw_rows):
                if not isinstance(raw_row, dict) or not raw_row:
                    raise ValueError(f"归档表 {raw_table} 包含无效记录")
                row = cast(dict[str, Any], raw_row)
                if any(isinstance(item, (dict, list)) for item in row.values()):
                    raise ValueError(f"归档表 {raw_table} 包含非标量字段")
                rows.append(row)
            records[str(raw_table)] = rows
        return records

    @staticmethod
    def _validate_archive_records(manifest: dict[str, Any], records: dict[str, Any], path: Path) -> None:
        """验证格式版本, 表清单和新版归档内容摘要"""
        if manifest.get("archive_version") not in (1, 2):
            raise ValueError("不支持的回收包版本")
        if manifest.get("tables") != sorted(records):
            raise ValueError("归档表清单与数据库记录不匹配")
        if not isinstance(manifest.get("has_files"), bool):
            raise ValueError("归档文件清单无效")
        if not records and not manifest["has_files"]:
            raise ValueError("回收包没有可恢复数据")
        if manifest["archive_version"] == 2:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if manifest.get("records_sha256") != digest:
                raise ValueError("回收包数据库记录摘要不匹配")

    @staticmethod
    def _restore_receipt(database: Path, archive_id: str) -> dict[str, Any] | None:
        """读取与数据库恢复事务一起提交的凭据"""
        if not database.exists():
            return None
        with closing(sqlite3.connect(str(database))) as connection:
            connection.row_factory = sqlite3.Row
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_restores'"
            ).fetchone():
                return None
            row = connection.execute(
                "SELECT * FROM archive_restores WHERE archive_id = ?", (archive_id,),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _finish_restore_cleanup(archive: Path, platform_id: str, session_id: str, archive_id: str) -> dict[str, Any]:
        """清理失败只影响回收包, 不把已完成恢复报告为写入失败"""
        cleanup_error = ""
        try:
            if archive.exists():
                shutil.rmtree(archive)
        except OSError as error:
            cleanup_error = str(error)
        return {
            "ok": True, "platform_id": platform_id, "session_id": session_id,
            "archive_id": archive_id, "cleanup_pending": bool(cleanup_error), "cleanup_error": cleanup_error,
        }

    @staticmethod
    def _archive_display_metadata(
        records: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """
        从回收包中提取 Chat 历史展示字段

        参数:
        - records: 按表组织的回收记录

        返回:
        - dict[str, Any]: 标题、模型、思考等级和轮数
        """
        metadata = records.get("conversation_meta", [])
        meta = metadata[0] if metadata else {}
        turns = records.get("display_turns", [])
        ordered_turns = sorted(turns, key=lambda row: int(row.get("turn_index") or 0))
        title = str(ordered_turns[0].get("user_input") or "新对话") if ordered_turns else "新对话"
        return {
            "title": title,
            "model": str(meta.get("model") or "default"),
            "think": str(meta.get("think") or "off"),
            "project_id": meta.get("project_id"),
            "created_at": float(meta.get("created_at") or 0),
            "turn_count": len(turns),
        }

    def restore_archive(
        self,
        platform_id: str,
        archive_id: str,
        *,
        database_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """
        恢复一个完整会话回收包

        参数:
        - platform_id: 平台实例 ID
        - archive_id: 回收包 ID
        - database_path: 可选数据库路径, 默认由布局解析

        返回:
        - dict[str, Any]: 恢复结果
        """
        archive = self._resolve_archive_path(platform_id, archive_id)
        database = Path(database_path) if database_path is not None else self.layout.platform_db(platform_id)
        receipt = self._restore_receipt(database, archive_id)
        if receipt and receipt["completed"]:
            return self._finish_restore_cleanup(archive, platform_id, receipt["session_id"], archive_id)
        if not archive.is_dir():
            raise ValueError("回收包不存在或身份不匹配")
        manifest_path = self._resolve_archive_member(archive, "manifest.json")
        manifest = self._read_json(manifest_path)
        if (
            manifest is None
            or str(manifest.get("platform_id", "")) != platform_id
            or str(manifest.get("archive_id", "")) != archive_id
        ):
            raise ValueError("回收包不存在或身份不匹配")
        session_id = str(manifest.get("session_id", "")).strip()
        if not session_id:
            raise ValueError("回收包缺少 session_id")
        records_path = self._resolve_archive_member(archive, "records.json")
        if not records_path.is_file():
            raise ValueError("回收包数据库记录无效")
        records = self._read_json_records(records_path)
        self._validate_archive_records(manifest, records, records_path)
        if manifest.get("layout_version") != self.layout.layout_version:
            raise ValueError("回收包数据布局版本不匹配")
        destination = self.layout.session_root(platform_id, session_id)
        files_root = self._resolve_archive_member(archive, "files")
        if destination.is_symlink() or destination.resolve() != destination.parent.resolve() / destination.name:
            raise ValueError("拒绝恢复到链接会话目录")
        if (destination.exists() or destination.is_symlink()) and receipt is None:
            raise ValueError(f"会话目录已存在: {session_id}")
        if manifest["has_files"]:
            if not files_root.is_dir() and not (receipt and destination.is_dir()):
                raise ValueError("回收包文件目录缺失或无效")
        elif files_root.exists():
            raise ValueError("回收包文件与清单不匹配")
        restore_session_domain(
            database, session_id, records, restore_id=archive_id,
            restore_metadata=json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        )   # 先提交记录和恢复凭据; 后续文件发布失败时保留归档供重试
        if manifest["has_files"] and files_root.exists():
            if destination.exists() or destination.is_symlink():
                raise ValueError(f"会话目录已存在: {session_id}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            files_root.rename(destination)   # 同一布局内原子发布, 不使用可能留下部分文件的复制删除
        with closing(sqlite3.connect(str(database))) as connection, connection:
            connection.execute("UPDATE archive_restores SET completed = 1 WHERE archive_id = ?", (archive_id,))
        return self._finish_restore_cleanup(archive, platform_id, session_id, archive_id)

    def purge_archive(self, platform_id: str, archive_id: str) -> bool:
        """
        永久删除一个回收包

        参数:
        - platform_id: 平台实例 ID
        - archive_id: 回收包 ID

        返回:
        - bool: 是否删除
        """
        target = self._resolve_archive_path(platform_id, archive_id)
        if not target.exists() and not target.is_symlink():
            return False
        shutil.rmtree(target)
        return True

    def purge_archives(
        self,
        *,
        archive_refs: Iterable[dict[str, str]] | None = None,
        older_than_days: float | None = None,
        platform_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        批量永久删除显式选中或超过保留期的回收项

        参数:
        - archive_refs: 可选的平台和回收包引用
        - older_than_days: 可选的最小存放天数
        - platform_id: 按保留期清理时可选的平台过滤

        返回:
        - list[dict[str, Any]]: 逐回收项删除结果
        """
        requested = {
            (
                str(item.get("platform_id", "")).strip(),
                str(item.get("archive_id", "")).strip(),
            )
            for item in archive_refs or []
            if str(item.get("platform_id", "")).strip()
            and str(item.get("archive_id", "")).strip()
        }
        if not requested and older_than_days is None:
            raise ValueError("必须选择回收项或提供保留天数")
        if older_than_days is not None and older_than_days < 0:
            raise ValueError("保留天数不能小于 0")
        cutoff = (
            time.time() - float(older_than_days) * 86400
            if older_than_days is not None
            else None
        )
        available: dict[tuple[str, str], StorageAuditItem] = {}
        for item in self.scan():
            if item.category != "trash_entry":
                continue
            archive_id = str(item.details.get("archive_id", "")).strip()
            if not archive_id:
                continue
            key = (item.platform_id, archive_id)
            if requested and key in requested:
                available[key] = item
            if (
                cutoff is not None
                and item.modified_at <= cutoff
                and (not platform_id or item.platform_id == platform_id)
            ):
                available[key] = item
        results: list[dict[str, Any]] = []
        missing = requested - set(available)
        results.extend(
            {
                "platform_id": item_platform,
                "archive_id": archive_id,
                "ok": False,
                "error": "回收项不存在或已变化, 请重新扫描",
            }
            for item_platform, archive_id in sorted(missing)
        )
        for item_platform, archive_id in sorted(available):
            try:
                deleted = self.purge_archive(item_platform, archive_id)
                results.append({
                    "platform_id": item_platform,
                    "archive_id": archive_id,
                    "ok": deleted,
                })
            except Exception as error:
                results.append({
                    "platform_id": item_platform,
                    "archive_id": archive_id,
                    "ok": False,
                    "error": str(error),
                })
        return results

    def cleanup(self, item_ids: Iterable[str]) -> list[dict[str, Any]]:
        """
        重新扫描并清理选中的安全孤儿项目

        参数:
        - item_ids: 最新扫描项目 ID

        返回:
        - list[dict[str, Any]]: 逐项执行结果
        """
        selected = {str(item).strip() for item in item_ids if str(item).strip()}
        current = {item.item_id: item for item in self.scan()}
        results: list[dict[str, Any]] = []
        handled_database_sessions: set[tuple[str, str]] = set()
        for item_id in selected:
            item = current.get(item_id)
            if item is None:
                results.append({"item_id": item_id, "ok": False, "error": "项目已变化, 请重新扫描"})
                continue
            if not item.auto_safe:
                results.append({"item_id": item_id, "ok": False, "error": "该项目必须人工处理"})
                continue
            try:
                if item.category == "orphan_session_directory":
                    archived = self.layout.trash_session(item.platform_id, item.session_id)
                    ok = archived is not None
                elif item.category in {"orphan_database_rows", "broken_user_reference"}:
                    key = (item.platform_id, item.session_id)
                    if key not in handled_database_sessions:
                        delete_session_domain_rows(
                            self.layout.platform_db(item.platform_id),
                            item.session_id,
                        )
                        handled_database_sessions.add(key)
                    ok = True
                else:
                    ok = False
                results.append({"item_id": item_id, "ok": ok})
            except Exception as error:
                results.append({"item_id": item_id, "ok": False, "error": str(error)})
        return results
