"""Shell 逐次授权, 计划模式和受保护文件边界回归"""
import pytest
import shutil
from types import SimpleNamespace
import os

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
    tool._bind(session)
    if asynchronous:
        return await tool.execute(command, timeout=10, shell=shell)
    return tool.execute(command, timeout=10, shell=shell)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shell", ["powershell", "cmd"])
async def test_shell_harness_read_control(shell_env, asynchronous, shell):
    root, engine, session = shell_env
    (root / "control.txt").write_text("audit-control", encoding="utf-8")
    reader = coding.AsyncReadFileTool() if asynchronous else coding.ReadFileTool()
    reader._bind(session)
    output = await reader.execute("control.txt") if asynchronous else reader.execute("control.txt")
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
    reader._bind(session)
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
    engine.add_persistent_rule("sandbox_escape", RiskLevel.HIGH)
    engine.approve("shell", RiskLevel.HIGH)
    calls = []
    approvals = []

    def provider(question):
        approvals.append(question)
        return answer

    session = SimpleNamespace(coding_workspace_root=tmp_path, user_input_provider=provider if answer else None)
    monkeypatch.setattr(coding, "_run_shell", lambda *args: calls.append(args) or "executed")
    tool = coding.AsyncShellTool(engine) if asynchronous else coding.ShellTool(engine)
    tool._bind(session)
    for _ in range(2):
        result = await tool.execute("echo harmless") if asynchronous else tool.execute("echo harmless")
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
    monkeypatch.setattr(coding, "_run_shell", lambda *args: pytest.fail("已失效授权不能执行"))
    tool = coding.AsyncShellTool(engine) if asynchronous else coding.ShellTool(engine)
    tool._bind(session)
    result = await tool.execute("echo audit") if asynchronous else tool.execute("echo audit")
    assert "执行已取消" in result
