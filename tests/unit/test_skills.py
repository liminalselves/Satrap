"""Skill 扩展单元测试"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pathlib import Path

from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.utils.skills import Skill, SkillsManager, SkillTool, _parse_front_matter

SKILL_MD = """---
name: coding_agent
description: 代码助手
tools:
  - code_sandbox
  - search
---

# 指令正文
- 使用沙箱验证代码
"""

TOOLS_PY = """\
from satrap.core.utils.TCBuilder import Tool


class StopwatchTool(Tool):
    tool_name = "stopwatch"
    description = "秒表"
    params_dict = {}

    def execute(self):
        return "0"


def get_tools():
    return [StopwatchTool()]
"""

MCP_TOOLS_PY = """\
class FakeMCPClient:
    def __init__(self):
        self.closed = False

    async def register_tools(self, tools_manager):
        self.closed = False

    async def close(self):
        self.closed = True


def get_mcp_clients():
    return [FakeMCPClient()]
"""


def _write_skill(tmp_path: Path, text: str = SKILL_MD, filename: str = "coding_agent.md"):
    path = tmp_path / filename
    path.write_text(text, encoding="utf-8")
    return path


def _write_folder_skill(tmp_path: Path, name: str = "demo", skill_md: str | None = None, tools_py: str | None = None, meta: str | None = None):
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    (skill_dir / "skill.md").write_text(skill_md or SKILL_MD, encoding="utf-8")
    if tools_py is not None:
        (skill_dir / "tools.py").write_text(tools_py, encoding="utf-8")
    if meta is not None:
        (skill_dir / "meta.yaml").write_text(meta, encoding="utf-8")
    return skill_dir


# ================= Skill 解析 =================

def test_skill_from_file_with_front_matter(tmp_path: Path):
    skill = Skill.from_file(str(_write_skill(tmp_path)))
    assert skill.name == "coding_agent"
    assert skill.description == "代码助手"
    assert skill.tool_names == ["code_sandbox", "search"]
    assert "使用沙箱验证代码" in skill.instructions


def test_skill_from_file_without_front_matter(tmp_path: Path):
    path = _write_skill(tmp_path, text="仅正文内容", filename="plain_skill.md")
    skill = Skill.from_file(str(path))
    assert skill.name == "plain_skill"
    assert skill.instructions == "仅正文内容"
    assert skill.tool_names == []


def test_skill_to_text_contains_markers():
    skill = Skill(name="demo", instructions="指令", tool_names=["t1"], description="简介")
    text = skill.to_text()
    assert "<skill:demo>" in text
    assert "</skill:demo>" in text
    assert "指令" in text
    assert "t1" in text


def test_parse_front_matter_no_match():
    meta, body = _parse_front_matter("hello\nworld")
    assert meta == {}
    assert body == "hello\nworld"


# ================= SkillsManager =================

def test_scan_loads_all_skill_files(tmp_path: Path):
    _write_skill(tmp_path, filename="a.md")
    _write_skill(tmp_path, text="---\nname: b\n---\nbody", filename="b.md")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")

    manager = SkillsManager(skills_dir=str(tmp_path))
    found = manager.scan()
    assert {s.name for s in found} == {"coding_agent", "b"}
    assert manager.has_skill("b")
    assert sorted(manager.list_skills()) == ["b", "coding_agent"]


def test_scan_missing_directory_returns_empty(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path / "nope"))
    assert manager.scan() == []


# ================= 文件夹式技能 =================

def test_scan_loads_folder_skill_with_meta_and_tools(tmp_path: Path):
    skill_dir = _write_folder_skill(
        tmp_path,
        name="demo",
        tools_py=TOOLS_PY,
        meta="author: tester\nversion: 1.2.0\n",
    )
    manager = SkillsManager(skills_dir=str(tmp_path))
    found = manager.scan()

    assert len(found) == 1
    skill = found[0]
    assert skill.name == "coding_agent"
    assert skill.source == str(skill_dir)
    assert skill.meta == {"author": "tester", "version": "1.2.0"}
    assert len(skill.tools) == 1
    assert skill.tools[0].get_tool_name() == "stopwatch"
    assert "stopwatch" in skill.tool_names   # 自带工具名自动并入
    assert "code_sandbox" in skill.tool_names


def test_scan_folder_skill_name_falls_back_to_dirname(tmp_path: Path):
    _write_folder_skill(tmp_path, name="plain_folder", skill_md="# 仅正文")
    manager = SkillsManager(skills_dir=str(tmp_path))
    skill = manager.scan()[0]
    assert skill.name == "plain_folder"


def test_scan_mixes_folder_and_single_file_skills(tmp_path: Path):
    _write_folder_skill(tmp_path, name="folder_skill")
    _write_skill(tmp_path, filename="single.md", text="---\nname: single\n---\nbody")
    manager = SkillsManager(skills_dir=str(tmp_path))
    assert sorted(s.name for s in manager.scan()) == ["coding_agent", "single"]


def test_scan_folder_skill_auto_collects_tool_classes(tmp_path: Path):
    tools_py = """\
from satrap.core.utils.TCBuilder import Tool


class AutoTool(Tool):
    tool_name = "auto_collect"
    description = "自动收集"
    params_dict = {}
    def execute(self):
        return "ok"
"""
    _write_folder_skill(tmp_path, name="auto_skill", tools_py=tools_py)
    manager = SkillsManager(skills_dir=str(tmp_path))
    skill = manager.scan()[0]
    assert skill.tools and skill.tools[0].get_tool_name() == "auto_collect"
    assert "auto_collect" in skill.tool_names


def test_activate_registers_bundled_tools(tmp_path: Path):
    _write_folder_skill(tmp_path, name="demo", tools_py=TOOLS_PY)
    manager = SkillsManager(skills_dir=str(tmp_path))
    manager.scan()
    wf = _make_sync_workflow(tmp_path)

    assert manager.activate("coding_agent", wf) is True   # type: ignore[arg-type]
    assert "stopwatch" in wf.tools_manager.tools
    assert wf.tools_manager.is_tool_enabled("stopwatch") is True
    assert "code_sandbox" in wf.tools_manager.tools
    assert wf.tools_manager.is_tool_enabled("code_sandbox") is True


async def test_activate_async_connects_and_deactivate_closes_mcp_clients(tmp_path: Path):
    _write_folder_skill(tmp_path, name="mcp_skill", tools_py=MCP_TOOLS_PY)
    manager = SkillsManager(skills_dir=str(tmp_path))
    manager.scan()
    skill = manager.get_skill("coding_agent")
    assert skill is not None
    assert len(skill.mcp_clients) == 1
    client = skill.mcp_clients[0]

    wf = SimpleNamespace(
        ctx=AsyncContextManager("skill-mcp", db_path=str(tmp_path / "mcp.db"), keep_in_memory=True),
        tools_manager=AsyncToolsManager(),
    )
    await wf.ctx.initialize()
    await wf.ctx.reset_system_prompt("你是助手")

    assert await manager.activate_async("coding_agent", wf) is True   # type: ignore[arg-type]
    assert client.closed is False
    assert "<skill:coding_agent>" in wf.ctx.get_context()[0]["content"]

    assert await manager.deactivate_async("coding_agent", wf) is True   # type: ignore[arg-type]
    assert client.closed is True
    assert "<skill:coding_agent>" not in wf.ctx.get_context()[0]["content"]


def test_preset_dir_points_to_expend_skills():
    from satrap.core.utils.skills import SKILLS_PRESET_DIR
    assert SKILLS_PRESET_DIR.replace("\\", "/").endswith("satrap/expend/skills")


def _make_sync_workflow(tmp_path: Path):
    class DummyTool(Tool):
        def __init__(self):
            super().__init__(
                tool_name="code_sandbox",
                description="沙箱",
                params_dict={"code": ("string", "代码")},
            )

        def execute(self, code: str):
            return {"stdout": code}

    tools_manager = ToolsManager()
    tools_manager.register_tool(DummyTool())
    ctx = ContextManager("skill-test", db_path=str(tmp_path / "skill.db"), keep_in_memory=True)
    ctx.reset_system_prompt("你是助手")
    return SimpleNamespace(ctx=ctx, tools_manager=tools_manager)


def test_activate_injects_instructions_and_enables_tools(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path))
    _write_skill(tmp_path)
    manager.scan()
    wf = _make_sync_workflow(tmp_path)
    tools_manager = wf.tools_manager
    tools_manager.disable_tool("code_sandbox")

    assert manager.activate("coding_agent", wf) is True   # type: ignore[arg-type]
    system_text = wf.ctx.get_context()[0]["content"]
    assert "<skill:coding_agent>" in system_text
    assert "使用沙箱验证代码" in system_text
    assert tools_manager.is_tool_enabled("code_sandbox") is True

    assert manager.activate("coding_agent", wf) is True   # type: ignore[arg-type]   # 幂等
    assert system_text.count("<skill:coding_agent>") == 1


def test_deactivate_strips_instructions_and_disables_tools(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path))
    _write_skill(tmp_path)
    manager.scan()
    wf = _make_sync_workflow(tmp_path)

    manager.activate("coding_agent", wf)   # type: ignore[arg-type]
    assert manager.deactivate("coding_agent", wf) is True   # type: ignore[arg-type]
    system_text = wf.ctx.get_context()[0]["content"]
    assert "<skill:coding_agent>" not in system_text
    assert wf.tools_manager.is_tool_enabled("code_sandbox") is False


def test_activate_unknown_skill_returns_false(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path))
    wf = _make_sync_workflow(tmp_path)
    assert manager.activate("ghost", wf) is False   # type: ignore[arg-type]


async def test_activate_async_with_async_context(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path))
    _write_skill(tmp_path)
    manager.scan()

    class DummyAsyncTool(AsyncTool):
        def __init__(self):
            super().__init__(
                tool_name="code_sandbox",
                description="沙箱",
                params_dict={"code": ("string", "代码")},
            )

    tools_manager = AsyncToolsManager()
    tools_manager.register_tool(DummyAsyncTool())
    tools_manager.disable_tool("code_sandbox")

    ctx = AsyncContextManager("skill-async", db_path=str(tmp_path / "skill-async.db"), keep_in_memory=True)
    await ctx.initialize()
    await ctx.reset_system_prompt("你是助手")
    wf = SimpleNamespace(ctx=ctx, tools_manager=tools_manager)

    assert await manager.activate_async("coding_agent", wf) is True   # type: ignore[arg-type]
    assert "<skill:coding_agent>" in wf.ctx.get_context()[0]["content"]
    assert tools_manager.is_tool_enabled("code_sandbox") is True

    assert await manager.deactivate_async("coding_agent", wf) is True   # type: ignore[arg-type]
    assert "<skill:coding_agent>" not in wf.ctx.get_context()[0]["content"]
    assert tools_manager.is_tool_enabled("code_sandbox") is False


# ================= SkillTool =================

async def test_skill_tool_returns_instructions(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path))
    _write_skill(tmp_path)
    manager.scan()
    tool = SkillTool(manager)

    result = await tool.execute(skill="coding_agent")
    assert "<skill:coding_agent>" in result
    assert "使用沙箱验证代码" in result

    result = await tool.execute(skill="ghost")
    assert "ghost" in result
    assert "coding_agent" in result


def test_skill_tool_definition_contains_available_skills(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path))
    _write_skill(tmp_path)
    manager.scan()
    tool = SkillTool(manager)
    defined = tool.get_tool_defined()
    assert "coding_agent" in defined["function"]["description"]
