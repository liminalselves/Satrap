"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import pytest
from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.utils.skills import Skill, SkillsManager, SkillTool, _parse_front_matter, DEFAULT_USER_SKILLS_DIR
from satrap.core.utils.skills import SKILLS_PRESET_DIR
from mcp.types import Tool as MCPTool
import threading
import asyncio
import inspect
from typing import Any
from types import SimpleNamespace
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.mcp import MCPClient, MCPToolAdapter, MCPServerExporter, SyncMCPToolAdapter, content_to_text
from satrap.core.utils.mcp import export_tools_to_mcp
from satrap.core.type import LLMCallResponse
from satrap.edictum import SimpleSession

class _skills_SyncStubWorkflow:
    """同步轻量工作流替身: 结构满足 SkillWorkflowProtocol"""

    def __init__(self, ctx: ContextManager, tools_manager: ToolsManager) -> None:
        self.ctx = ctx
        self.tools_manager = tools_manager

class _AsyncStubWorkflow:
    """异步轻量工作流替身: 结构满足 SkillWorkflowProtocol"""

    def __init__(self, ctx: AsyncContextManager, tools_manager: AsyncToolsManager) -> None:
        self.ctx = ctx
        self.tools_manager = tools_manager

SKILL_MD = """---
name: coding-agent
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

def _write_skill(tmp_path: Path, text: str = SKILL_MD, filename: str = "coding-agent.md"):
    path = tmp_path / filename
    path.write_text(text, encoding="utf-8")
    return path

def _skills_write_folder_skill(tmp_path: Path, name: str = "demo", skill_md: str | None = None, tools_py: str | None = None, meta: str | None = None):
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    (skill_dir / "skill.md").write_text(skill_md or SKILL_MD, encoding="utf-8")
    if tools_py is not None:
        (skill_dir / "tools.py").write_text(tools_py, encoding="utf-8")
    if meta is not None:
        (skill_dir / "meta.yaml").write_text(meta, encoding="utf-8")
    return skill_dir

@pytest.mark.parametrize("text, filename, expected_name, expected_body", [
    (SKILL_MD, "coding-agent.md", "coding-agent", None),
    ("仅正文内容", "plain_skill.md", "plain_skill", "仅正文内容"),
])
def test_skill_from_file_variants(tmp_path: Path, text: str, filename: str, expected_name: str, expected_body: str | None):
    """有/无 front matter 的技能文件均能解析出名与正文"""
    skill = Skill.from_file(str(_write_skill(tmp_path, text=text, filename=filename)))
    assert skill.name == expected_name
    if expected_body is None:
        assert skill.description == "代码助手"
        assert skill.tool_names == ["code_sandbox", "search"]
        assert "使用沙箱验证代码" in skill.instructions
    else:
        assert skill.instructions == expected_body
        assert skill.tool_names == []

def test_skill_text_and_front_matter():
    """skill.to_text 带标记; 无 front matter 时元数据为空"""
    skill = Skill(name="demo", instructions="指令", tool_names=["t1"], description="简介")
    text = skill.to_text()
    assert "<skill:demo>" in text and "</skill:demo>" in text
    assert "指令" in text and "t1" in text

    meta, body = _parse_front_matter("hello\nworld")
    assert meta == {} and body == "hello\nworld"

def test_scan_files_and_missing_dir(tmp_path: Path):
    """扫描加载全部技能文件; 目录缺失返回空"""
    missing = SkillsManager(skills_dir=str(tmp_path / "nope"), include_preset=False)
    assert missing.scan() == []

    _write_skill(tmp_path, filename="a.md")
    _write_skill(tmp_path, text="---\nname: b\n---\nbody", filename="b.md")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")

    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    found = manager.scan()
    assert {s.name for s in found} == {"coding-agent", "b"}
    assert manager.has_skill("b")
    assert sorted(manager.list_skills()) == ["b", "coding-agent"]

def test_scan_loads_folder_skill_with_meta_and_tools(tmp_path: Path):
    skill_dir = _skills_write_folder_skill(
        tmp_path,
        name="demo",
        tools_py=TOOLS_PY,
        meta="author: tester\nversion: 1.2.0\n",
    )
    manager = SkillsManager(
        skills_dir=str(tmp_path),
        include_preset=False,
        trusted_code_roots=[tmp_path],
    )
    found = manager.scan()

    assert len(found) == 1
    skill = found[0]
    assert skill.name == "coding-agent"
    assert skill.source == str(skill_dir)
    assert skill.meta == {"author": "tester", "version": "1.2.0"}
    assert len(skill.tools) == 1
    assert skill.tools[0].get_tool_name() == "stopwatch"
    assert "stopwatch" in skill.tool_names   # 自带工具名自动并入
    assert "code_sandbox" in skill.tool_names

def test_scan_folder_name_fallback_and_mixed_sources(tmp_path: Path):
    """无 front matter 的文件夹技能落到目录名; 文件夹与单文件技能可混合扫描"""
    _skills_write_folder_skill(tmp_path, name="plain_folder", skill_md="# 仅正文")
    _write_skill(tmp_path, filename="single.md", text="---\nname: single\n---\nbody")
    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    assert sorted(s.name for s in manager.scan()) == ["plain_folder", "single"]

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
    _skills_write_folder_skill(tmp_path, name="auto_skill", tools_py=tools_py)
    manager = SkillsManager(
        skills_dir=str(tmp_path),
        include_preset=False,
        trusted_code_roots=[tmp_path],
    )
    skill = manager.scan()[0]
    assert skill.tools and skill.tools[0].get_tool_name() == "auto_collect"
    assert "auto_collect" in skill.tool_names

def test_activate_registers_bundled_tools(tmp_path: Path):
    _skills_write_folder_skill(tmp_path, name="demo", tools_py=TOOLS_PY)
    manager = SkillsManager(
        skills_dir=str(tmp_path),
        include_preset=False,
        trusted_code_roots=[tmp_path],
    )
    manager.scan()
    wf = _make_sync_workflow(tmp_path)

    assert manager.activate("coding-agent", wf) is True
    assert "stopwatch" in wf.tools_manager.tools
    assert wf.tools_manager.is_tool_enabled("stopwatch") is True
    assert "code_sandbox" in wf.tools_manager.tools
    assert wf.tools_manager.is_tool_enabled("code_sandbox") is True

async def test_activate_async_connects_and_deactivate_closes_mcp_clients(tmp_path: Path):
    _skills_write_folder_skill(tmp_path, name="mcp_skill", tools_py=MCP_TOOLS_PY)
    manager = SkillsManager(
        skills_dir=str(tmp_path),
        include_preset=False,
        trusted_code_roots=[tmp_path],
    )
    manager.scan()
    skill = manager.get_skill("coding-agent")
    assert skill is not None
    assert len(skill.mcp_clients) == 1
    client = skill.mcp_clients[0]

    wf = _AsyncStubWorkflow(
        ctx=AsyncContextManager("skill-mcp", db_path=str(tmp_path / "mcp.db"), keep_in_memory=True),
        tools_manager=AsyncToolsManager(),
    )
    await wf.ctx.initialize()
    await wf.ctx.reset_system_prompt("你是助手")

    assert await manager.activate_async("coding-agent", wf) is True
    assert client.closed is False
    assert "coding-agent" in manager.catalog(wf)
    assert "<skill:" not in wf.ctx.get_context()[0]["content"]

    assert await manager.deactivate_async("coding-agent", wf) is True
    assert client.closed is True
    assert "<skill:coding-agent>" not in wf.ctx.get_context()[0]["content"]

def test_scan_skips_untrusted_tools_code(tmp_path: Path):
    """用户技能目录默认只加载指令, 不执行其中的 tools.py"""
    marker = tmp_path / "executed.txt"
    tools_py = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
    _skills_write_folder_skill(tmp_path, name="untrusted", tools_py=tools_py)

    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    skill = manager.scan()[0]

    assert skill.instructions
    assert skill.tools == []
    assert skill.mcp_clients == []
    assert not marker.exists()

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
    return _skills_SyncStubWorkflow(ctx=ctx, tools_manager=tools_manager)

def test_activate_publishes_catalog_without_changing_prompt_or_tool_switches(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    _write_skill(tmp_path)
    manager.scan()
    wf = _make_sync_workflow(tmp_path)
    tools_manager = wf.tools_manager
    tools_manager.disable_tool("code_sandbox")

    assert manager.activate("coding-agent", wf) is True
    system_text = wf.ctx.get_context()[0]["content"]
    assert system_text == "你是助手"
    assert "coding-agent" in manager.catalog(wf)
    assert "使用沙箱验证代码" in manager.read_skill("coding-agent", wf)
    assert tools_manager.is_tool_enabled("code_sandbox") is False

    assert manager.activate("coding-agent", wf) is True   # 幂等
    assert manager.catalog(wf).count("- coding-agent:") == 1

def test_deactivate_removes_catalog_without_changing_existing_tools(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    _write_skill(tmp_path)
    manager.scan()
    wf = _make_sync_workflow(tmp_path)

    manager.activate("coding-agent", wf)
    assert manager.deactivate("coding-agent", wf) is True
    system_text = wf.ctx.get_context()[0]["content"]
    assert "<skill:coding-agent>" not in system_text
    assert wf.tools_manager.is_tool_enabled("code_sandbox") is True
    with pytest.raises(ValueError, match="不可用"):
        manager.read_skill("coding-agent", wf)

def test_activate_unknown_skill_returns_false(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    wf = _make_sync_workflow(tmp_path)
    assert manager.activate("ghost", wf) is False

async def test_activate_async_with_async_context(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
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
    wf = _AsyncStubWorkflow(ctx=ctx, tools_manager=tools_manager)

    assert await manager.activate_async("coding-agent", wf) is True
    assert "coding-agent" in manager.catalog(wf)
    assert wf.ctx.get_context()[0]["content"] == "你是助手"
    assert tools_manager.is_tool_enabled("code_sandbox") is False

    assert await manager.deactivate_async("coding-agent", wf) is True
    assert "<skill:coding-agent>" not in wf.ctx.get_context()[0]["content"]
    assert tools_manager.is_tool_enabled("code_sandbox") is False

async def test_skill_tool_returns_instructions(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    _write_skill(tmp_path)
    manager.scan()
    tool = SkillTool(manager)

    result = await tool.execute(skill="coding-agent")
    assert "<skill:coding-agent>" in result
    assert "使用沙箱验证代码" in result

    result = await tool.execute(skill="ghost")
    assert "ghost" in result
    assert "不可用" in result

def test_skill_tool_definition_uses_catalog_instead_of_duplicate_skill_names(tmp_path: Path):
    manager = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    _write_skill(tmp_path)
    manager.scan()
    tool = SkillTool(manager)
    defined = tool.get_tool_defined()
    assert "coding-agent" not in defined["function"]["description"]
    assert defined["function"]["name"] == "load_skill"

class _skills_dirs_SyncStubWorkflow:
    """同步轻量工作流替身: 结构满足 SkillWorkflowProtocol"""

    def __init__(self, ctx: ContextManager, tools_manager: ToolsManager) -> None:
        self.ctx = ctx
        self.tools_manager = tools_manager

USER_SAME_NAME_MD = """---
name: web-research
description: 用户定制版
tools:
  - code_sandbox
---

# 用户指令正文
"""

def _skills_dirs_write_folder_skill(tmp_path: Path, name: str, skill_md: str, meta: str | None = None) -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    (skill_dir / "skill.md").write_text(skill_md, encoding="utf-8")
    if meta is not None:
        (skill_dir / "meta.yaml").write_text(meta, encoding="utf-8")
    return skill_dir

def test_default_user_dir_and_preset_merge():
    """默认扫描 = 官方预设 + .satrap/skills; 预设目录指向 expend/skills, 官方技能带 satrap-skill-id"""
    assert SKILLS_PRESET_DIR.replace("\\", "/").endswith("satrap/expend/skills")
    mgr = SkillsManager()   # 不传目录: 使用默认用户目录
    assert mgr.skills_dir == DEFAULT_USER_SKILLS_DIR

    loaded = mgr.scan()
    assert "web-research" in {s.name for s in loaded}   # 官方预设技能
    for s in loaded:
        if s.source is not None and s.source.startswith(SKILLS_PRESET_DIR):
            assert s.skill_id == s.name
    assert mgr.get_skill("web-research") is not None

def test_preset_skill_id_and_dir_scoping(tmp_path: Path):
    """官方 meta.yaml 的 satrap-skill-id 真实可读; include_preset=False 与显式目录都只扫用户目录"""
    preset = SkillsManager(skills_dir=str(Path("nope-dir")))
    preset.scan()
    s = preset.get_skill("web-research")
    assert s is not None and s.skill_id == "web-research"
    assert s.source is not None and s.source.startswith(SKILLS_PRESET_DIR)

    _skills_dirs_write_folder_skill(tmp_path, "my-skill", USER_SAME_NAME_MD, meta="satrap-skill-id: my-skill\n")
    user_only = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    user_only.scan()
    assert user_only.list_skills() == ["my-skill"]

    explicit = SkillsManager(skills_dir=str(tmp_path / "empty"), include_preset=False)
    explicit.scan()
    assert explicit.list_skills() == []
    explicit.scan(str(tmp_path))
    assert explicit.list_skills() == ["my-skill"]

@pytest.mark.parametrize("meta, expected", [
    (None, ["web-research"]),
    ("author: user\nsatrap-skill-id: my-web-research\n", ["my-web-research", "web-research"]),
])
def test_same_name_resolution(tmp_path: Path, meta: str | None, expected: list[str]):
    """用户同名技能无 id 时官方生效; 带不同 satrap-skill-id 时共存并按 id 可寻"""
    _skills_dirs_write_folder_skill(tmp_path, "web-research", USER_SAME_NAME_MD, meta)
    mgr = SkillsManager(skills_dir=str(tmp_path))
    mgr.scan()

    assert sorted(mgr.list_skills()) == expected
    official = mgr.get_skill("web-research")
    assert official is not None and official.skill_id == "web-research"
    assert official.source is not None and official.source.startswith(SKILLS_PRESET_DIR)
    if meta is None:
        assert "用户指令正文" not in official.instructions
    else:
        mine = mgr.get_skill("my-web-research")
        assert mine is not None and mine.skill_id == "my-web-research"
        assert "用户指令正文" in mine.instructions

def test_same_name_sequential_activate_no_cross_strip(tmp_path: Path):
    """同名不同 id 技能在同一 workflow 顺序激活, 标记互不相同且停用不误伤"""
    meta = "author: user\nsatrap-skill-id: user-web\n"
    _skills_dirs_write_folder_skill(tmp_path, "web-research", USER_SAME_NAME_MD, meta)
    mgr = SkillsManager(skills_dir=str(tmp_path))
    mgr.scan()

    wf = _make_workflow(tmp_path)
    assert mgr.activate("web-research", wf) is True   # 官方版本
    assert mgr.activate("user-web", wf) is True   # 用户版本 (同名, 不应被跳过)

    assert "<skill:web-research>" in mgr.read_skill("web-research", wf)
    assert "<skill:user-web>" in mgr.read_skill("user-web", wf)

    assert mgr.deactivate("user-web", wf) is True
    assert "- user-web:" not in mgr.catalog(wf)
    assert "- web-research:" in mgr.catalog(wf)   # 官方版本未被误伤

def _make_workflow(tmp_path: Path):
    """最小 workflow stub: ctx + tools_manager"""
    ctx = ContextManager(
        "skill-id-test", db_path=str(tmp_path / "id.db"), keep_in_memory=True
    )
    tools_manager = ToolsManager()
    return _skills_dirs_SyncStubWorkflow(ctx=ctx, tools_manager=tools_manager)

def test_activate_by_skill_id(tmp_path: Path):
    """通过 satrap-skill-id 激活与停用"""
    meta = "author: user\nsatrap-skill-id: my-web-research\n"
    _skills_dirs_write_folder_skill(tmp_path, "web-research", USER_SAME_NAME_MD, meta)
    mgr = SkillsManager(skills_dir=str(tmp_path), include_preset=False)
    mgr.scan()

    wf = _make_workflow(tmp_path)
    assert mgr.activate("my-web-research", wf) is True
    assert "<skill:my-web-research>" in mgr.read_skill("my-web-research", wf)

    assert mgr.deactivate("my-web-research", wf) is True
    assert "- my-web-research:" not in mgr.catalog(wf)

def _make_mcp_tool(name: str = "read_file", description: str = "读取文件", schema: dict[str, Any] | None = None):
    return MCPTool(
        name=name,
        description=description,
        input_schema=schema if schema is not None else {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "max_length": {"type": "integer", "description": "最大长度", "default": 100},
            },
            "required": ["path"],
        },
    )

class FakeSession:
    """假 MCP 会话, 可配置 call_tool 的返回内容"""

    def __init__(self, content: list[SimpleNamespace] | None = None, is_error: bool = False, error: Exception | None = None):
        self._content = content if content is not None else [SimpleNamespace(type="text", text="ok")]
        self._is_error = is_error
        self._error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.tools = [_make_mcp_tool()]

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        self.calls.append((name, arguments))
        if self._error:
            raise self._error
        return SimpleNamespace(content=self._content, is_error=self._is_error)

def _make_adapter(session: FakeSession | None = None, name: str = "read_file", schema: dict[str, Any] | None = None):
    session = session or FakeSession()
    return MCPToolAdapter(session, _make_mcp_tool(name=name, schema=schema))

def test_adapter_tool_definition_preserves_or_defaults_schema():
    """完整 JSON schema 原样透出并映射参数; 空 schema 归一为 object"""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "查询词", "enum": ["a", "b"]},
            "limit": {"type": "integer", "description": "数量", "default": 5},
        },
        "required": ["query"],
    }
    adapter = _make_adapter(schema=schema)
    defined = adapter.get_tool_defined()

    assert defined["type"] == "function"
    fn = defined["function"]
    assert fn["name"] == "read_file"
    assert fn["description"] == "读取文件"
    assert fn["parameters"] == schema
    assert adapter.params_dict == {"query": ("string", "查询词"), "limit": ("integer", "数量")}

    empty = _make_adapter(schema={})
    assert empty.get_tool_defined()["function"]["parameters"] == {"type": "object", "properties": {}}

@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["text", "empty", "error_content", "exception"])
async def test_adapter_execute_outcomes(case: str):
    """MCP 适配器 execute: 文本, 空内容, 远端错误与抛出异常分别归一"""
    if case == "text":
        session = FakeSession(content=[SimpleNamespace(type="text", text="hello mcp")])
        result = await _make_adapter(session).execute(path="/tmp/a.txt")
        assert result == "hello mcp"
        assert session.calls == [("read_file", {"path": "/tmp/a.txt"})]
    elif case == "empty":
        assert await _make_adapter(FakeSession(content=[])).execute(path="x") == "OK"
    elif case == "error_content":
        session = FakeSession(content=[SimpleNamespace(type="text", text="file not found")], is_error=True)
        result = await _make_adapter(session).execute(path="x")
        assert result["ok"] is False
        assert result["error_type"] == "mcp_error"
        assert "file not found" in result["error"]
        assert result["tool_name"] == "read_file"
    else:
        result = await _make_adapter(FakeSession(error=RuntimeError("boom"))).execute(path="x")
        assert result["ok"] is False
        assert "boom" in result["error"]

def test_adapter_prefix_applied_to_name():
    adapter = _make_adapter(name="read_file")
    assert adapter.get_tool_name() == "read_file"
    prefixed = MCPToolAdapter(FakeSession(), _make_mcp_tool(), name_prefix="fs")
    assert prefixed.get_tool_name() == "fs_read_file"

async def test_adapter_in_async_tools_manager():
    class FakeMCPTool:
        name = "echo"
        description = "回显"

    class FakeSession2:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=str(arguments))],
                is_error=False,
            )

    manager = AsyncToolsManager()
    manager.register_tool(MCPToolAdapter(FakeSession2(), FakeMCPTool(), name_prefix="mcp"))
    assert len(manager.get_tools_definitions()) == 1
    tool_message, result = await manager.execute_tool_call(
        {"name": "mcp_echo", "arguments": {"msg": "hi"}}
    )
    assert tool_message["function"]["name"] == "mcp_echo"
    assert result == "{'msg': 'hi'}"

def test_client_requires_command_or_url():
    with pytest.raises(ValueError):
        MCPClient()

async def test_client_register_and_close(monkeypatch: pytest.MonkeyPatch):
    fake_session = FakeSession()

    async def fake_connect(self: MCPClient):
        self.session = fake_session
        self._connected = True
        return fake_session

    monkeypatch.setattr(MCPClient, "connect", fake_connect)

    mcp = MCPClient(command="npx", args=["-y", "some-server"], name="fs")
    manager = AsyncToolsManager()
    adapters = await mcp.register_tools(manager)

    assert len(adapters) == 1
    assert adapters[0].get_tool_name() == "fs_read_file"
    assert "fs_read_file" in manager.tools
    assert manager.is_tool_enabled("fs_read_file")

    await mcp.close()
    assert "fs_read_file" not in manager.tools
    assert mcp._connected is False

def _background_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """启动一个后台事件循环线程 (daemon), 返回 (loop, thread)"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread

def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)

@pytest.mark.parametrize("case", ["text", "error", "timeout"])
def test_sync_adapter_outcomes(case: str):
    """同步适配器: 经后台事件循环桥接异步调用, 远端异常与超时归为错误字典"""
    loop, thread = _background_loop()
    try:
        if case == "text":
            session = FakeSession(content=[SimpleNamespace(type="text", text="sync ok")])
            adapter = SyncMCPToolAdapter(MCPToolAdapter(session, _make_mcp_tool()), loop)
            assert adapter.get_tool_defined()["function"]["name"] == "read_file"
            assert adapter.execute(path="/tmp/a.txt") == "sync ok"
            assert session.calls == [("read_file", {"path": "/tmp/a.txt"})]
        elif case == "error":
            adapter = SyncMCPToolAdapter(MCPToolAdapter(FakeSession(error=RuntimeError("boom")), _make_mcp_tool()), loop)
            result = adapter.execute(path="x")
            assert result["ok"] is False and "boom" in result["error"]
        else:
            class SlowSession:
                async def list_tools(self):
                    return SimpleNamespace(tools=[])

                async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
                    await asyncio.sleep(30)

            adapter = SyncMCPToolAdapter(MCPToolAdapter(SlowSession(), _make_mcp_tool()), loop, timeout=0.1)
            result = adapter.execute(path="x")
            assert result["ok"] is False and "超时" in result["error"]
    finally:
        _stop_loop(loop, thread)

def test_sync_register_and_close(monkeypatch: pytest.MonkeyPatch):
    """同步注册: 工具注册进同步 ToolsManager, 可执行, 关闭后注销"""
    fake_session = FakeSession()

    async def fake_connect(self: MCPClient):
        self.session = fake_session
        self._connected = True
        return fake_session

    monkeypatch.setattr(MCPClient, "connect", fake_connect)

    mcp = MCPClient(command="npx", args=["-y", "some-server"], name="fs")
    manager = ToolsManager()
    adapters = mcp.sync_register_tools(manager)

    assert len(adapters) == 1
    assert adapters[0].get_tool_name() == "fs_read_file"
    assert "fs_read_file" in manager.tools
    assert manager.is_tool_enabled("fs_read_file")
    assert manager.execute_tool("fs_read_file", {"path": "/x"}) == "ok"

    mcp.sync_close()
    assert "fs_read_file" not in manager.tools
    assert mcp._connected is False

def test_sync_plugin_install_mcp_register_and_uninstall(tmp_path: Path):
    """同步 SimpleSession: 含 mcp.py 插件可安装, 工具注册, 卸载时断开连接"""
    class _FakeLLM(LLM):
        def __init__(self) -> None:
            super().__init__(api_key="demo")

        def call(self, messages: list[dict[str, Any]], model: str | None = None, **kwargs: Any):
            return LLMCallResponse(type="answer", content="回复")

    plugin_dir = tmp_path / "mcp-demo"
    plugin_dir.mkdir()
    (plugin_dir / "meta.yaml").write_text("name: mcp-demo\nversion: 0.1\n", encoding="utf-8")
    (plugin_dir / "mcp.py").write_text(
        """
from satrap.core.utils.TCBuilder import Tool

class _EchoTool(Tool):
    tool_name = "remote_echo"
    description = "远端回显"
    params_dict = {"msg": ("string", "消息")}

    def execute(self, *input, **kwargs):
        return "remote:" + str(kwargs.get("msg", ""))

class _FakeMCPClient:
    def __init__(self):
        self.closed = False

    def sync_register_tools(self, tools_manager, name_prefix=None):
        tool = _EchoTool()
        tools_manager.register_tool(tool)
        return [tool]

    def sync_close(self):
        self.closed = True

clients = {"fake": _FakeMCPClient()}
""",
        encoding="utf-8",
    )

    session = SimpleSession("mcp-sync", _FakeLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=False)
    plugin = session.install_plugin(str(plugin_dir))

    assert plugin.mcp == {"fake": True}
    assert "remote_echo" in session.tools_manager.tools
    assert session.tools_manager.execute_tool("remote_echo", {"msg": "hi"}) == "remote:hi"

    assert session.uninstall_plugin("mcp-demo") is True
    assert "remote_echo" not in session.tools_manager.tools
    assert plugin._mcp_clients["fake"][0].closed is True

def test_content_to_text_handles_various_blocks():
    assert content_to_text(None) == "OK"
    assert content_to_text([SimpleNamespace(type="text", text="a")]) == "a"
    assert (
        content_to_text(
            [SimpleNamespace(type="text", text="a"), SimpleNamespace(type="text", text="b")]
        )
        == "a\nb"
    )
    assert content_to_text(
        [SimpleNamespace(type="image", mimeType="image/png", data="xx")]
    ).startswith("[image:")

def test_exporter_registers_sync_tools():
    class EchoTool(Tool):
        def __init__(self):
            super().__init__(
                tool_name="echo",
                description="回显消息",
                params_dict={"msg": ("string", "消息内容")},
            )

        def execute(self, msg: str):
            return {"echo": msg}

    manager = ToolsManager()
    manager.register_tool(EchoTool())

    server = export_tools_to_mcp(manager, name="test-server")
    assert server.name == "test-server"

    exporter = MCPServerExporter(manager, name="test-server-2")
    handler = exporter._build_handler(EchoTool())
    assert list(inspect.signature(handler).parameters) == ["msg"]
    assert handler(msg="hi") == {"echo": "hi"}
    assert inspect.signature(handler).parameters["msg"].annotation is str


@pytest.mark.asyncio
async def test_exporter_preserves_rich_parameters_and_optional_defaults():
    class RichTool(Tool):
        tool_name = "rich"
        description = "处理文字列表"
        params_dict = {
            "options": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["auto", "text"], "default": "auto"},
        }

        def execute(self, options: list[str], mode: str = "auto"):
            return {"options": options, "mode": mode}

    manager = ToolsManager()
    manager.register_tool(RichTool())
    server = export_tools_to_mcp(manager)
    tools = await server.list_tools()
    assert len(tools) == 1
    schema = tools[0].input_schema
    assert schema["required"] == ["options"]
    assert schema["properties"]["options"]["items"] == {"type": "string"}
    assert schema["properties"]["mode"]["enum"] == ["auto", "text"]
    assert schema["properties"]["mode"]["default"] == "auto"
    result = await server.call_tool("rich", {"options": ["one"]})
    assert not result.is_error
    assert '"mode": "auto"' in str(result.content)


@pytest.mark.asyncio
async def test_exporter_preserves_combined_root_schema_and_original_arguments():
    """MCP 保留组合类型, 根条件和局部引用, 合法参数与本地执行一致"""
    from mcp.server.mcpserver.exceptions import ToolError
    calls = []

    class ContractTool(Tool):
        tool_name = "contract"
        description = "测试完整输入契约"
        params_dict = {
            "text": {"type": "string"},
            "path": {"type": "string"},
            "value": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
            "items": {"type": "array", "items": {"$ref": "#/$defs/entry"}},
        }

        def get_tool_defined(self):
            definition = super().get_tool_defined()
            schema = definition["function"]["parameters"]
            schema.update(required=[], oneOf=[
                {"required": ["text"], "not": {"required": ["path"]}},
                {"required": ["path"], "not": {"required": ["text"]}},
            ])
            schema["$defs"] = {"entry": {"type": "object", "properties": {"label": {"type": "string"}},
                                         "required": ["label"], "additionalProperties": False}}
            return definition

        def execute(self, **kwargs):
            calls.append(kwargs)
            return kwargs

    tool = ContractTool()
    manager = ToolsManager()
    manager.register_tool(tool)
    server = export_tools_to_mcp(manager)
    schema = (await server.list_tools())[0].input_schema
    assert schema == tool.get_tool_defined()["function"]["parameters"]
    schema["required"].append("value")
    assert (await server.list_tools())[0].input_schema["required"] == []
    for arguments in [{"text": "hello", "value": 7, "items": [{"label": "ok"}]},
                      {"path": "document.txt", "value": None}, {"text": "no optional fields"}]:
        await server.call_tool("contract", arguments)
        assert calls[-1] == arguments
    count = len(calls)
    for invalid in [{}, {"text": "a", "path": "b"}, {"text": "a", "value": 1.5},
                    {"text": "a", "items": [{}]}, {"text": "a", "extra": True}]:
        with pytest.raises(ToolError):
            await server.call_tool("contract", invalid)
        assert len(calls) == count
