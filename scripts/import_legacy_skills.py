"""通过 skills 插件一次性接管旧 Markdown 文件, 默认只预览"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    """
    预览或导入旧用户目录, 不执行任何技能附带代码

    参数:
    - argv: 命令行参数, 默认读取进程参数

    返回:
    - 成功时为 0, 存在无法接管的文件时为 1
    """
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    from satrap.expend.plugins.skills.management import skill_management
    from satrap.expend.plugins.skills.store import SkillStore, MAX_MARKDOWN_BYTES
    from satrap.core.utils.skills.utils import _parse_front_matter
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=project / ".satrap" / "skills")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    existing = {entry.name for entry in SkillStore().entries()}
    report: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in {".md", ".markdown"} or not path.is_file():
            continue
        row: dict[str, object] = {"file": str(path.relative_to(root))}
        try:
            if any(part.is_symlink() for part in (path, *path.parents)) or path.stat().st_size > MAX_MARKDOWN_BYTES:
                raise ValueError("符号链接或超限文件不自动接管")
            markdown = path.read_text(encoding="utf-8-sig")
            metadata, _ = _parse_front_matter(markdown)
            name = str(metadata.get("name") or (path.parent.name if path.name.lower() == "skill.md" else path.stem))
            if not metadata.get("description"):
                raise ValueError("缺少用途简介, 请在技能库导入界面补充")
            row["name"] = name
            if name in existing:
                row["status"] = "already_imported"
            elif args.apply:
                skill_management("POST", "", {"name": name, "markdown": markdown, "filename": str(path)})
                row["status"] = "imported"
                existing.add(name)
            else:
                row["status"] = "candidate"
        except (OSError, ValueError, TypeError) as error:
            row["error"] = str(error)
        report.append(row)
    print(json.dumps({"mode": "apply" if args.apply else "preview", "files": report}, ensure_ascii=False, indent=2))
    return int(any("error" in row for row in report))


if __name__ == "__main__":
    raise SystemExit(main())
