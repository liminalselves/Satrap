"""技能扫描目录体系测试: 官方预设 + 用户目录

覆盖:
- 默认用户技能目录 (.satrap/skills) 与官方预设合并扫描
- meta.yaml 的 satrap-skill-id 解析 (技能身份识别符)
- 同名无 id: 官方优先
- 同名有不同 id: 共存不冲突, get_skill 按 id/name 均可查找
- include_preset=False 仅扫用户目录
- scan(显式目录) 仅扫指定目录
- 通过 id 激活 / 停用技能
- 同名不同 id 顺序激活, 注入标记互不干扰, 停用不误伤
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from satrap.core.utils.skills import (
    DEFAULT_USER_SKILLS_DIR,
    SKILLS_PRESET_DIR,
    SkillsManager,
)
from satrap.core.utils.TCBuilder import ToolsManager
from satrap.core.utils.context import ContextManager

USER_SAME_NAME_MD = """---
name: web-research
description: 用户定制版
tools:
  - code_sandbox
---

# 用户指令正文
"""


def _write_folder_skill(tmp_path: Path, name: str, skill_md: str, meta: str | None = None) -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    (skill_dir / "skill.md").write_text(skill_md, encoding="utf-8")
    if meta is not None:
        (skill_dir / "meta.yaml").write_text(meta, encoding="utf-8")
    return skill_dir


def test_default_user_dir_and_preset_merge():
    """默认扫描 = 官方预设 + .satrap/skills, 官方技能带 satrap-skill-id"""
    mgr = SkillsManager()   # 不传目录: 使用默认用户目录
    assert mgr.skills_dir == DEFAULT_USER_SKILLS_DIR

    loaded = mgr.scan()
    names = {s.name for s in loaded}
    assert "web-research" in names   # 官方预设技能

    # 官方技能解析出 satrap-skill-id
    for s in loaded:
        if s.source is not None and s.source.startswith(SKILLS_PRESET_DIR):
            assert s.skill_id == s.name

    # 官方技能以 id 为 key 可寻
    assert mgr.get_skill("web-research") is not None


def test_preset_meta_skill_id_from_disk():
    """官方 meta.yaml 的 satrap-skill-id 真实可读"""
    mgr = SkillsManager(skills_dir=str(Path("nope-dir")))
    mgr.scan()
    s = mgr.get_skill("web-research")
    assert s is not None
    assert s.skill_id == "web-research"
    assert s.source is not None and s.source.startswith(SKILLS_PRESET_DIR)


def test_same_name_no_id_official_wins(tmp_path: Path):
    """用户同名技能 (无 meta id) 不覆盖官方"""
    _write_folder_skill(tmp_path, "web-research", USER_SAME_NAME_MD)
    mgr = SkillsManager(skills_dir=str(tmp_path))   # include_preset 默认 True
    mgr.scan()

    s = mgr.get_skill("web-research")
    assert s is not None
    assert "用户指令正文" not in s.instructions   # 官方版本生效
    assert s.source is not None and s.source.startswith(SKILLS_PRESET_DIR)


def test_same_name_different_id_coexist(tmp_path: Path):
    """同名技能携带不同 satrap-skill-id 时共存, 各按 id 可寻"""
    meta = "author: user\nsatrap-skill-id: my-web-research\n"
    _write_folder_skill(tmp_path, "web-research", USER_SAME_NAME_MD, meta)
    mgr = SkillsManager(skills_dir=str(tmp_path))
    mgr.scan()

    assert sorted(mgr.list_skills()) == ["my-web-research", "web-research"]

    official = mgr.get_skill("web-research")
    mine = mgr.get_skill("my-web-research")
    assert official is not None and official.skill_id == "web-research"
    assert mine is not None and mine.skill_id == "my-web-research"
    assert "用户指令正文" in mine.instructions

    # 按 name 查找返回官方 (官方优先)
    by_name = mgr.get_skill("web-research")
    assert by_name is not None and by_name.skill_id == "web-research"


def test_same_name_sequential_activate_no_cross_strip(tmp_path: Path):
    """同名不同 id 技能在同一 workflow 顺序激活, 标记互不相同且停用不误伤"""
    meta = "author: user\nsatrap-skill-id: user-web\n"
    _write_folder_skill(tmp_path, "web-research", USER_SAME_NAME_MD, meta)
    mgr = SkillsManager(skills_dir=str(tmp_path))
    mgr.scan()

    wf = _make_workflow(tmp_path)
    assert mgr.activate("web-research", wf) is True   # type: ignore[arg-type]   # 官方版本
    assert mgr.activate("user-web", wf) is True   # type: ignore[arg-type]   # 用户版本 (同名, 不应被跳过)

    system_text = wf.ctx.get_context()[0]["content"]
    assert "<skill:web-research>" in system_text
    assert "<skill:user-web>" in system_text

    assert mgr.deactivate("user-web", wf) is True   # type: ignore[arg-type]
    system_text = wf.ctx.get_context()[0]["content"]
    assert "<skill:user-web>" not in system_text
    assert "<skill:web-research>" in system_text   # 官方版本未被误伤


def test_include_preset_false_scans_user_only(tmp_path: Path):
    """include_preset=False 时仅扫用户目录"""
    _write_folder_skill(
        tmp_path, "my-skill", USER_SAME_NAME_MD,
        meta="satrap-skill-id: my-skill\n",
    )
    mgr = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    mgr.scan()
    assert mgr.list_skills() == ["my-skill"]


def test_scan_explicit_dir_scans_only_that_dir(tmp_path: Path):
    """scan(显式目录) 只扫指定目录 (不合并官方)"""
    mgr = SkillsManager(skills_dir=str(tmp_path / "empty"), include_preset=False)
    mgr.scan()
    assert mgr.list_skills() == []

    _write_folder_skill(tmp_path, "solo", USER_SAME_NAME_MD, meta="satrap-skill-id: solo\n")
    mgr.scan(str(tmp_path))
    assert mgr.list_skills() == ["solo"]


def _make_workflow(tmp_path: Path):
    """最小 workflow stub: ctx + tools_manager"""
    ctx = ContextManager(
        "skill-id-test", db_path=str(tmp_path / "id.db"), keep_in_memory=True
    )
    tools_manager = ToolsManager()
    return SimpleNamespace(ctx=ctx, tools_manager=tools_manager)


def test_activate_by_skill_id(tmp_path: Path):
    """通过 satrap-skill-id 激活与停用"""
    meta = "author: user\nsatrap-skill-id: my-web-research\n"
    _write_folder_skill(tmp_path, "web-research", USER_SAME_NAME_MD, meta)
    mgr = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    mgr.scan()

    wf = _make_workflow(tmp_path)
    assert mgr.activate("my-web-research", wf) is True   # type: ignore[arg-type]
    system_text = wf.ctx.get_context()[0]["content"]
    assert "<skill:my-web-research>" in system_text

    assert mgr.deactivate("my-web-research", wf) is True   # type: ignore[arg-type]
    assert "<skill:my-web-research>" not in wf.ctx.get_context()[0]["content"]
