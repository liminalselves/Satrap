"""纯文本技能目录, 使用跨进程锁与原子替换保存导入结果"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4
import copy
import yaml
import json
import re

from satrap.core.utils.skills.utils import _parse_front_matter, _FRONT_MATTER_RE
from satrap.core.storage.file_lock import FileLock
from satrap.core.utils.skills import Skill
from satrap.core.utils.paths import get_project_root

MAX_MARKDOWN_BYTES = 256 * 1024
MAX_SKILLS = 200
SESSION_TYPES = {"chat", "platform", "embedded"}


class SkillConflict(ValueError):
    """导入重名或保存版本已经过时"""


@dataclass(frozen=True)
class SkillEntry:
    """已安装的外部文本技能, 版本用于编辑冲突与任务快照"""

    name: str
    description: str
    markdown: str
    source: str
    revision: int
    enabled: bool
    scopes: list[str]

    def skill(self) -> Skill:
        """
        转换为无可执行资源的技能说明

        返回:
        - 外部来源专用标识的纯文本技能
        """
        _, body = _parse_front_matter(self.markdown)
        return Skill(self.name, body.strip(), description=self.description,
                     skill_id=f"user.{self.name}", meta={"revision": self.revision, "source": self.source})


class SkillStore:
    """不执行外部代码的技能持久目录"""

    def __init__(self, root: Path | None = None):
        """
        指定技能目录

        参数:
        - root: 管理目录, 默认使用项目 .satrap/skills
        """
        self.root = root or get_project_root() / ".satrap" / "skills"
        self.path = self.root / "managed.json"
        self._signature: tuple[int, int, int] | None = None
        self._cached: list[SkillEntry] = []

    def entries(self) -> list[SkillEntry]:
        """
        读取原子发布的完整目录

        返回:
        - 已安装技能; 目录损坏时明确报错, 不覆盖原数据
        """
        if not self.path.exists():
            return []
        stat = self.path.stat()
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if self._signature == signature:
            return copy.deepcopy(self._cached)
        if stat.st_size > MAX_MARKDOWN_BYTES * MAX_SKILLS * 2:
            raise ValueError("技能目录超过大小限制")
        data: object = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or len(data) > MAX_SKILLS:
            raise ValueError("技能目录格式无效")
        entries: list[SkillEntry] = []
        for value in data:
            if not isinstance(value, dict) or set(value) != {"name", "description", "markdown", "source", "revision", "enabled", "scopes"}:
                raise ValueError("技能目录条目格式无效")
            row = cast(dict[str, object], value)
            if any(not isinstance(row[key], str) for key in ("name", "description", "markdown", "source")):
                raise ValueError("技能目录文本字段无效")
            scope = row["scopes"]
            if (type(row["enabled"]) is not bool or type(row["revision"]) is not int or cast(int, row["revision"]) < 1
                    or not isinstance(scope, list) or not scope or any(not isinstance(item, str) or item not in SESSION_TYPES for item in scope)):
                raise ValueError("技能目录版本, 开关或范围无效")
            entries.append(SkillEntry(cast(str, row["name"]), cast(str, row["description"]), cast(str, row["markdown"]),
                                      cast(str, row["source"]), cast(int, row["revision"]), cast(bool, row["enabled"]), cast(list[str], scope)))
        if len({entry.name for entry in entries}) != len(entries):
            raise ValueError("技能目录包含重名条目")
        self._signature, self._cached = signature, entries
        return copy.deepcopy(entries)

    def _write(self, entries: list[SkillEntry]) -> None:
        """
        在持有写锁时原子发布目录

        参数:
        - entries: 已校验的完整目标目录
        """
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps([asdict(item) for item in entries], ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, markdown: str, *, name: str = "", description: str = "", source: str = "upload",
             expected_revision: int | None = None, enabled: bool = True, scopes: list[str] | None = None) -> SkillEntry:
        """
        导入或按当前版本更新 Markdown, 同名技能不静默覆盖

        参数:
        - markdown: 原始 UTF-8 Markdown 正文
        - name: 缺少 front matter 或显式编辑时的名称
        - description: 用于任务匹配的简介
        - source: 文件名或实际下载地址
        - expected_revision: 更新需要当前版本, 新建时省略
        - enabled: 是否允许提供给已启用此插件的会话, 默认 True
        - scopes: 允许的会话类型, 默认全部类型

        返回:
        - 保存后的技能及递增版本, 失败时原目录不变
        """
        if not isinstance(markdown, str) or not markdown.strip() or len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            raise ValueError("技能正文不能为空且不能超过 256 KiB")
        normalized = markdown.lstrip("\ufeff").replace("\r\n", "\n")
        if normalized.startswith("---\n"):
            header = _FRONT_MATTER_RE.match(normalized)
            if header is None:
                raise ValueError("技能 front matter 缺少结束分隔符")
            try:
                fields: object = yaml.safe_load(header.group(1)) or {}
            except yaml.YAMLError as error:
                raise ValueError("技能 front matter 不是有效 YAML") from error
            if not isinstance(fields, dict) or any(key in fields and not isinstance(fields[key], str) for key in ("name", "description")):
                raise ValueError("技能元数据必须是对象, name 和 description 必须是文本")
        metadata, body = _parse_front_matter(normalized)
        identity = name.strip() or str(metadata.get("name") or "").strip()
        intro = description.strip() or str(metadata.get("description") or "").strip()
        scope = sorted(SESSION_TYPES if scopes is None else set(scopes))
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", identity):
            raise ValueError("技能名称需要 1 至 64 位英文字母, 数字, 点, 下划线或连字符")
        if not intro or len(intro) > 512 or not body.strip():
            raise ValueError("技能需要非空正文及 1 至 512 字的用途简介")
        if type(enabled) is not bool or not scope or set(scope) - SESSION_TYPES:
            raise ValueError("技能启用状态或适用会话类型无效")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("更新需要有效的 expected_revision")
        with FileLock(self.root / "write.lock"):
            entries = self.entries()
            previous = next((entry for entry in entries if entry.name == identity), None)
            if (previous.revision if previous else None) != expected_revision:
                raise SkillConflict("技能已存在或版本已变化, 请刷新后明确选择更新目标")
            if previous is None and len(entries) >= MAX_SKILLS:
                raise ValueError("外部技能数量不能超过 200")
            item = SkillEntry(identity, intro, markdown.lstrip("\ufeff"), source, (previous.revision if previous else 0) + 1, enabled, scope)
            self._write(sorted([entry for entry in entries if entry.name != identity] + [item], key=lambda entry: entry.name))
            return item

    def delete(self, name: str, revision: int) -> None:
        """
        按版本删除外部技能, 不影响其它插件自带技能

        参数:
        - name: 已导入技能名称
        - revision: 当前版本
        """
        with FileLock(self.root / "write.lock"):
            entries = self.entries()
            previous = next((entry for entry in entries if entry.name == name), None)
            if previous is None or type(revision) is not int or previous.revision != revision:
                raise SkillConflict("技能不存在或版本已变化")
            self._write([entry for entry in entries if entry.name != name])
