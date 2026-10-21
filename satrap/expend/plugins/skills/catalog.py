"""
插件技能的只读目录

从现有插件目录读取 Markdown 与声明, 不导入或执行插件代码,
目录预览不表示技能已在某个会话启用
"""
from __future__ import annotations

from typing import TypedDict, cast

from satrap.expend.plugins.skills.store import MAX_MARKDOWN_BYTES
from satrap.core.utils.skills.utils import _parse_front_matter
from satrap.edictum.plugin_catalog import PluginCatalog

from satrap.core.log import logger


class PluginSkillPreview(TypedDict):
    """插件目录中的技能资料, 正文为空时需到实际会话查看"""

    plugin: str
    name: str
    description: str
    source: str
    instructions: str | None
    tools: list[str]


def plugin_skill_catalog(catalog: PluginCatalog | None = None) -> dict[str, object]:
    """
    汇总插件自带技能的只读预览, 不创建运行会话或执行技能工厂

    参数:
    - catalog: 已登记插件目录, 默认使用内置和用户插件目录

    返回:
    - items 为技能资料, notices 为动态来源或未能读取条目的说明
    """
    items: list[PluginSkillPreview] = []
    notices: list[str] = []
    total = 0
    for plugin in (catalog or PluginCatalog()).scan():
        if len(items) >= 512:
            notices.append("插件技能预览已达到 512 条上限")
            break
        if plugin.name == "skills":
            continue   # 外部导入的动态技能由技能库原有管理接口展示
        found: set[str] = set()
        root = plugin.path.resolve()
        directory = root / "skills"
        if directory.is_dir() and directory.resolve().is_relative_to(root):
            try:
                children = sorted(directory.iterdir())
            except OSError as error:
                logger.warning(f"[技能库] 插件技能目录读取失败, 插件={plugin.name}, 原因={error}")
                notices.append(f"{plugin.name}: 技能目录暂时无法读取")
                children = []
            for child in children:
                if not child.is_dir():
                    continue
                path = child / "SKILL.md" if (child / "SKILL.md").is_file() else child / "skill.md"
                if not path.is_file():
                    continue
                try:
                    if not path.resolve().is_relative_to(root):
                        raise ValueError("技能文件指向插件目录之外")
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_MARKDOWN_BYTES + 1)
                    if len(raw) > MAX_MARKDOWN_BYTES:
                        raise ValueError("技能文件超过 256 KiB")
                    if total + len(raw) > 8 * 1024 * 1024 or len(items) >= 512:
                        raise ValueError("插件技能预览已达到总量上限")
                    parsed, body = _parse_front_matter(raw.decode("utf-8-sig"))
                    meta = cast(dict[str, object], parsed)
                    name = str(meta.get("name") or child.name)
                    if name in found:
                        raise ValueError("插件技能标识重复")
                    tools = meta.get("tools", [])
                    tool_names = ([part.strip() for part in tools.split(",") if part.strip()]
                                  if isinstance(tools, str) else [str(part) for part in tools] if isinstance(tools, list) else [])
                    items.append({"plugin": plugin.name, "name": name, "description": str(meta.get("description") or ""),
                                  "source": path.relative_to(root).as_posix(), "instructions": body.strip(),
                                  "tools": tool_names})
                    found.add(name)
                    total += len(raw)
                except (OSError, ValueError, UnicodeError) as error:
                    logger.warning(f"[技能库] 插件技能读取失败, 插件={plugin.name}, 文件={child.name}, 原因={error}")
                    notices.append(f"{plugin.name}/{child.name}: {error}")
        for name, description in plugin.capabilities.get("skills", {}).items():
            if len(items) >= 512:
                break
            if name not in found:
                items.append({"plugin": plugin.name, "name": name, "description": description,
                              "source": "插件能力声明", "instructions": None, "tools": []})
        if (root / "skills.py").is_file():
            notices.append(f"{plugin.name} 还可通过代码提供技能, 实际目录和正文请在启用该插件的会话中使用 /skill list 和 /skill show 查看")
    return {"items": items, "notices": notices}
