"""Shell 逐次授权, 计划模式和受保护文件边界回归"""
import pytest
import shutil
from types import SimpleNamespace
import os
from typing import Any, cast

from satrap.expend.plugins.satrap_coding.core.permission import PermissionEngine, RiskLevel
from satrap.expend.plugins.satrap_coding import tools as coding


@pytest.fixture
def shell_env(tmp_path, monkeypatch):
    if os.name != "nt" or shutil.which("powershell") is None:
        pytest.skip("实际 Shell 复现需要 Windows PowerShell")
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(coding, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(coding, "_SYSTEM_PROTECTED_ROOT", root / ".satrap")
    engine = PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "approval.jsonl")
    engine.set_plan_mode(True)
    session = SimpleNamespace(coding_workspace_root=root, user_input_provider=lambda *args: "deny")
    return root, engine, session


async def execute_shell(shell_env, asynchronous, command, shell="powershell"):
    _, engine, session = shell_env
    tool = coding.AsyncShellTool(engine) if asynchronous else coding.ShellTool(engine)
    tool._bind(cast(Any, session))
    if isinstance(tool, coding.AsyncShellTool):
        return await tool.execute(command, timeout=10, shell=shell)
    return tool.execute(command, timeout=10, shell=shell)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shell", ["powershell", "cmd"])
async def test_shell_harness_read_control(shell_env, asynchronous, shell):
    root, engine, session = shell_env
    (root / "control.txt").write_text("audit-control", encoding="utf-8")
    reader = coding.AsyncReadFileTool() if asynchronous else coding.ReadFileTool()
    reader._bind(cast(Any, session))
    output = await reader.execute("control.txt") if isinstance(reader, coding.AsyncReadFileTool) else reader.execute("control.txt")
    assert "audit-control" in output
    engine.set_plan_mode(False)
    session.user_input_provider = lambda *args: "y"
    output = await execute_shell(shell_env, asynchronous, "echo 审计中文-control", shell=shell)
    assert "审计中文-control" in output


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("command", [
    "echo (New-Item audit-marker)",
    "echo audit\nNew-Item audit-marker",
    "git diff --output=audit-marker --no-index before.txt after.txt",
], ids=["parentheses", "newline", "git-output"])
async def test_shell_must_not_write_in_plan_mode(shell_env, asynchronous, command):
    root, _, _ = shell_env
    if command.startswith("git") and shutil.which("git") is None:
        pytest.skip("git-output 复现需要 Git")
    (root / "before.txt").write_text("before\n", encoding="utf-8")
    (root / "after.txt").write_text("after\n", encoding="utf-8")
    output = await execute_shell(shell_env, asynchronous, command)
    assert not (root / "audit-marker").exists(), f"未经批准创建了标记文件; output={output!r}"


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_shell_must_not_read_protected_token(shell_env, asynchronous):
    root, _, session = shell_env
    (root / ".satrap").mkdir()
    token = "audit-fake-token-does-not-authorize-anything"
    (root / ".satrap" / "api-token").write_text(token, encoding="utf-8")
    reader = coding.ReadFileTool()
    reader._bind(cast(Any, session))
    if "拒绝读取" not in reader.execute(".satrap/api-token"):
        raise RuntimeError("复现前置条件失败: 文件工具未拒绝受保护路径")
    output = await execute_shell(shell_env, asynchronous, "Get-Content .satrap/api-token")
    assert token not in output, "Shell 输出了文件工具拒绝读取的伪造令牌"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["user", "full", "auto-agent"])
@pytest.mark.parametrize("answer", [None, "n", "all", "y"])
async def test_shell_requires_each_explicit_approval(tmp_path, monkeypatch, asynchronous, mode, answer):
    engine = PermissionEngine(mode, rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl")
    engine.add_persistent_rule("shell", RiskLevel.HIGH)
    engine.approve("shell", RiskLevel.HIGH)
    calls = []
    approvals = []

    def provider(question):
        approvals.append(question)
        return answer

    session = SimpleNamespace(coding_workspace_root=tmp_path, user_input_provider=provider if answer else None)
    monkeypatch.setattr(coding, "_run_shell", lambda *args, **kwargs: calls.append(args) or "executed")
    tool = coding.AsyncShellTool(engine) if asynchronous else coding.ShellTool(engine)
    tool._bind(cast(Any, session))
    for _ in range(2):
        result = await tool.execute("echo harmless") if isinstance(tool, coding.AsyncShellTool) else tool.execute("echo harmless")
        assert (result == "executed") == (answer == "y")
    assert len(calls) == (2 if answer == "y" else 0)
    assert len(approvals) == (2 if answer else 0)
    assert all("all 本会话" not in prompt and str(tmp_path) in prompt for prompt in approvals)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("change", ["plan", "workspace"])
async def test_shell_rechecks_scope_after_approval(tmp_path, monkeypatch, asynchronous, change):
    engine = PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl")
    session = SimpleNamespace(coding_workspace_root=tmp_path)

    def provider(question):
        if change == "plan":
            engine.set_plan_mode(True)
        else:
            session.coding_workspace_root = tmp_path.parent
        return "y"

    session.user_input_provider = provider
    monkeypatch.setattr(coding, "_run_shell", lambda *args, **kwargs: pytest.fail("已失效授权不能执行"))
    tool = coding.AsyncShellTool(engine) if asynchronous else coding.ShellTool(engine)
    tool._bind(cast(Any, session))
    result = await tool.execute("echo audit") if isinstance(tool, coding.AsyncShellTool) else tool.execute("echo audit")
    assert "执行已取消" in result


def test_allowed_env_vars_parse_per_session():
    """allowlist 按会话配置解析, 空配置得到空集合, 不存在跨会话共享的模块级状态"""
    assert coding.parse_allowed_env_vars({"allowed_env_vars": "TEST_API_KEY,OTHER_TOKEN"}) == frozenset(
        {"TEST_API_KEY", "OTHER_TOKEN"}
    )
    assert coding.parse_allowed_env_vars({"allowed_env_vars": ""}) == frozenset()
    assert coding.parse_allowed_env_vars({}) == frozenset()
    assert not hasattr(coding, "ALLOWED_ENV_VARS")


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_factory_environment_isolated_across_sessions(tmp_path, monkeypatch, asynchronous):
    import asyncio
    from satrap.edictum import SimpleSession, AsyncSimpleSession
    from satrap.core.utils import proc_env

    base = AsyncSimpleSession if asynchronous else SimpleSession
    class FakeSession(base):
        def __init__(self):
            self._wf = SimpleNamespace(llm=None, tools_manager=None)
            self.coding_workspace_root = tmp_path
            self.user_input_provider = lambda *args: "y"

    def state(session, root):
        return {"engine": PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl"), "todos": {}}

    monkeypatch.setattr(coding, "get_plugin_state", state)
    monkeypatch.setattr(proc_env.os, "environ", {"FIRST_TOKEN": "first", "LAST_API_KEY": "last"})
    def run(args, workdir, timeout, allow=frozenset()):
        return ",".join(sorted(proc_env.sanitized_child_env(allow=allow)))
    monkeypatch.setattr(coding, "_run_shell", run)
    configurations = [{"allowed_env_vars": "FIRST_TOKEN"}, {"allowed_env_vars": ""}, {"allowed_env_vars": "LAST_API_KEY"}]
    tools = [next(tool for tool in coding.get_tools(FakeSession(), config)
                  if isinstance(tool, (coding.ShellTool, coding.AsyncShellTool))) for config in configurations]
    async def execute(tool):
        if isinstance(tool, coding.AsyncShellTool):
            return await tool.execute("echo audit")
        return tool.execute("echo audit")
    assert [await execute(tool) for tool in [*tools, tools[0]]] == ["FIRST_TOKEN", "", "LAST_API_KEY", "FIRST_TOKEN"]
    if asynchronous:
        assert await asyncio.gather(*(execute(tool) for tool in tools)) == ["FIRST_TOKEN", "", "LAST_API_KEY"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("tool_name", [
    "read_file", "write_file", "edit_file", "search_replace", "glob_files", "grep_files",
])
async def test_factory_protected_directories_isolated(tmp_path, monkeypatch, asynchronous, tool_name):
    """真实工厂的保护配置在交错和并发调用中保持独立, 拒绝时不修改文件"""
    import asyncio
    from satrap.edictum import SimpleSession, AsyncSimpleSession

    base = AsyncSimpleSession if asynchronous else SimpleSession

    class FakeSession(base):
        def __init__(self):
            self._wf = SimpleNamespace(llm=None, tools_manager=None)
            self.coding_workspace_root = tmp_path
            self.coding_sandbox_root = tmp_path

    def state(session, root):
        return {"engine": PermissionEngine(rules_file=tmp_path / "rules.json",
                                          log_file=tmp_path / "log.jsonl"), "todos": {}}

    monkeypatch.setattr(coding, "get_plugin_state", state)
    configurations = [{"protected_dirs": " FIRST_PRIVATE, "},
                      {"protected_dirs": "second_private"}, {"protected_dirs": ""}, None]
    tools = [{tool.tool_name: tool for tool in coding.get_tools(FakeSession(), config)}
             for config in configurations]
    directories = ["first_private", "second_private", ".satrap", ".git", "node_modules"]
    for directory in directories:
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "sample.txt").write_text("original-marker", encoding="utf-8")

    async def check(index):
        tool = tools[index][tool_name]
        for directory in directories:
            target = tmp_path / directory / f"case-{index}.txt"
            target.write_text("original-marker", encoding="utf-8")
            denied = directory in directories[2:] or (index < 2 and directory == directories[index])
            relative = f"{directory}/{target.name}"
            arguments = {
                "read_file": {"path": relative},
                "write_file": {"path": relative, "content": "changed-marker"},
                "edit_file": {"path": relative, "old": "original", "new": "changed"},
                "search_replace": {"path": relative, "replacements": [{"old": "original", "new": "changed"}]},
                "glob_files": {"pattern": f"{directory}/*.txt"},
                "grep_files": {"pattern": "marker", "path": directory},
            }[tool_name]
            result = await tool.execute(**arguments) if asynchronous else tool.execute(**arguments)
            if tool_name in {"glob_files", "grep_files"}:
                assert ("sample.txt" not in result) == denied, (index, directory, result)
            elif tool_name == "read_file":
                assert ("拒绝读取" in result) == denied, (index, directory, result)
                assert ("original-marker" not in result) == denied
            else:
                assert ("拒绝" in result) == denied, (index, directory, result)
                assert target.read_text(encoding="utf-8") == ("original-marker" if denied else "changed-marker")

    for index in [0, 1, 2, 3, 0]:
        await check(index)
    if asynchronous:
        await asyncio.gather(*(check(index) for index in range(4)))
