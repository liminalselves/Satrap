"""业务链路场景与集中边界契约"""
from __future__ import annotations

from dataclasses import replace
from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot
from satrap.expend.plugins.satrap_coding.core.approval import ApprovalRequest, ApprovalResult

from pathlib import Path
from contextlib import closing
import pytest
from typing import Any
from satrap.expend.plugins.satrap_coding.core.command_gate import classify_command
from satrap.expend.plugins.satrap_coding.core.goal_state import GoalState
from satrap.expend.plugins.satrap_coding.core.permission import PermissionDecision, PermissionEngine, RiskLevel
from satrap.core.memory.store import MemoryStore
import tracemalloc
from typing import Iterator, Callable, cast
from satrap.expend.plugins.satrap_coding.tools import get_tools
from satrap.expend.plugins.satrap_coding import tools as tools_mod
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import AsyncSimpleSession, SimpleSession
from unittest.mock import AsyncMock, Mock
from satrap.expend.plugins.satrap_coding.tools import async_interaction, async_write, sync_interaction, sync_write
from satrap.core.utils.TCBuilder import AsyncTool, Tool
import json
from satrap.core.utils.outbound import OutboundHTTPResponse, OutboundResponseTooLargeError, UnsafeOutboundURLError
from satrap.expend.tools.search import async_ as async_search, sync as sync_search
from satrap.expend.tools import search

class TestClassifyCommand:
    def test_empty_and_read(self):
        """空串与只读命令: READ 且非越界, 引号包裹与大小写归一"""
        assert classify_command("") == (RiskLevel.READ, False)
        assert classify_command("   ") == (RiskLevel.READ, False)
        assert classify_command("ls -la") == (RiskLevel.READ, False)
        assert classify_command("git status") == (RiskLevel.READ, False)
        assert classify_command("Get-ChildItem") == (RiskLevel.READ, False)
        assert classify_command('"Git" status') == (RiskLevel.READ, False)

    def test_write_and_high(self):
        """常规写, 高危, 未知保守归 WRITE 与重定向升级"""
        assert classify_command("mkdir newdir") == (RiskLevel.WRITE, False)
        assert classify_command("git commit -m x") == (RiskLevel.WRITE, False)
        assert classify_command("mycustomtool do-thing") == (RiskLevel.WRITE, False)
        assert classify_command("echo hi > f.txt")[0] == RiskLevel.WRITE
        assert classify_command("type a.txt >> b.txt")[0] == RiskLevel.WRITE
        assert classify_command("rm file.txt") == (RiskLevel.HIGH, False)
        assert classify_command("git push") == (RiskLevel.HIGH, False)
        assert classify_command("git reset HEAD~1") == (RiskLevel.HIGH, False)
        assert classify_command("git clean -fd") == (RiskLevel.FORBIDDEN, False)   # clean -f 命中黑名单

    def test_forbidden_patterns(self):
        """黑名单模式: 系统根删除 / 格式化 / git 破坏性操作"""
        assert classify_command("rm -rf /tmp/x") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("del /s C:\\windows") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("format C:") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("git reset --hard HEAD") == (RiskLevel.FORBIDDEN, False)

    def test_env_modify_is_escape(self):
        """环境修改命令 (pip/npm install 等): 高危 + 越界"""
        assert classify_command("pip install requests") == (RiskLevel.HIGH, True)
        assert classify_command("pip3 install requests") == (RiskLevel.HIGH, True)
        assert classify_command("npm i lodash") == (RiskLevel.HIGH, True)
        assert classify_command("npm install -g vite") == (RiskLevel.HIGH, True)
        assert classify_command("uv add pytest") == (RiskLevel.HIGH, True)
        assert classify_command("pip list") == (RiskLevel.WRITE, False)   # 无子命令不越界
        assert classify_command("pip -V") == (RiskLevel.WRITE, False)

    def test_segment_join_bypass_blocked(self):
        """H1: 拼接命令逐段分类, 只读首词 + 破坏性尾段不再放行"""
        assert classify_command("dir && del /f /q C:\\temp\\*")[0] >= RiskLevel.HIGH
        assert classify_command("cd /d D:\\ && rmdir /s /q D:\\data")[0] >= RiskLevel.HIGH
        assert classify_command("Get-Content C:\\x | Out-File y")[0] >= RiskLevel.WRITE
        assert classify_command("echo hi ; format C:")[0] == RiskLevel.FORBIDDEN
        assert classify_command("ls || rm -rf /tmp/x")[0] >= RiskLevel.HIGH
        # 引号内分隔符不切分
        assert classify_command('echo "a;b"')[0] == RiskLevel.READ

    def test_drive_root_deletion_forbidden(self):
        """L1: 盘根删除与 Remove-Item 参数顺序均补入黑名单"""
        assert classify_command("rm -rf C:\\") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("rm -rf D:\\x") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("del /f /s /q C:\\windows") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("rd /s /q D:\\data") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("Remove-Item -Recurse -Force E:\\") == (RiskLevel.FORBIDDEN, False)
        assert classify_command(r"Remove-Item -Path C:\temp\x -Recurse -Force")[0] == RiskLevel.FORBIDDEN
        assert classify_command(r"Remove-Item -Recurse -Path C:\temp\x -Force")[0] == RiskLevel.FORBIDDEN

    def test_git_fine_grained(self):
        """git 子命令细粒度: config 写值/branch 删除/tag 创建为写"""
        assert classify_command("git config --list")[0] == RiskLevel.READ
        assert classify_command("git config user.email")[0] == RiskLevel.READ
        assert classify_command("git config user.email x@y.com")[0] == RiskLevel.WRITE
        assert classify_command("git stash")[0] == RiskLevel.WRITE
        assert classify_command("git stash push -m x")[0] == RiskLevel.WRITE
        assert classify_command("git branch")[0] == RiskLevel.READ
        assert classify_command("git branch -d old")[0] == RiskLevel.WRITE
        assert classify_command("git tag")[0] == RiskLevel.READ
        assert classify_command("git tag v1.0")[0] == RiskLevel.WRITE
        assert classify_command("git remote add origin x")[0] == RiskLevel.WRITE
        assert classify_command("sc query")[0] == RiskLevel.READ
        assert classify_command("sc stop svc")[0] == RiskLevel.HIGH

    def test_reg_fine_grained(self):
        """reg 仅允许 query 作为只读操作"""
        assert classify_command(r"reg query HKCU\Software")[0] == RiskLevel.READ
        assert classify_command(r"reg add HKCU\Software\Satrap /v x /d y")[0] == RiskLevel.HIGH
        assert classify_command(r"reg delete HKCU\Software\Satrap /f")[0] == RiskLevel.HIGH

    def test_nested_shell_and_encoded_command(self):
        """包装器递归分类并拒绝不可审计的编码 PowerShell 载荷"""
        assert classify_command(r"cmd /c reg add HKCU\Software\Satrap /v x /d y")[0] == RiskLevel.HIGH
        assert classify_command(
            r'powershell -NoProfile -Command "reg add HKCU\Software\Satrap /v x /d y"'
        )[0] == RiskLevel.HIGH
        assert classify_command("powershell -EncodedCommand ZQBjAGgAbwAgAGgAaQA=")[0] == RiskLevel.FORBIDDEN
        assert classify_command("pwsh -enc ZQBjAGgAbwAgAGgAaQA=")[0] == RiskLevel.FORBIDDEN
        assert classify_command("powershell -e ZQBjAGgAbwAgAGgAaQA=")[0] == RiskLevel.FORBIDDEN

@pytest.fixture
def engine(tmp_path: Path) -> PermissionEngine:
    return PermissionEngine(
        rules_file=tmp_path / "permissions.json",
        log_file=tmp_path / "approval_log.jsonl",
    )

class TestPermissionEngine:
    @pytest.mark.parametrize("mode", ["user", "auto", "full-access"])
    def test_mode_policy_plan_persistence(self, tmp_path, mode, monkeypatch):
        engine = PermissionEngine(rules_file=tmp_path / "permissions.json", log_file=tmp_path / "log.jsonl")
        engine.set_mode(mode)
        for operation, risk in (("file_write", RiskLevel.WRITE), ("shell", RiskLevel.READ)):
            assert engine.evaluate(operation, risk) == (PermissionDecision.ALLOW if mode == "full-access" else PermissionDecision.ASK)
        assert engine.evaluate("read", RiskLevel.READ) == PermissionDecision.ALLOW
        assert engine.evaluate("shell", RiskLevel.FORBIDDEN) == PermissionDecision.DENY
        engine.set_plan_mode(True)
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.DENY
        assert engine.evaluate("shell", RiskLevel.READ) == PermissionDecision.DENY
        assert engine.evaluate("read", RiskLevel.READ) == PermissionDecision.ALLOW
        reloaded = PermissionEngine(rules_file=engine.rules_file, log_file=engine.log_file)
        assert reloaded.mode == mode and not reloaded.plan_mode
        original, revision = engine.rules_file.read_bytes(), engine.revision
        monkeypatch.setattr(engine, "_save", Mock(side_effect=OSError("写入失败")))
        with pytest.raises(OSError):
            engine.set_mode("user" if mode != "user" else "full-access")
        assert engine.mode == mode and engine.revision == revision
        assert engine.rules_file.read_bytes() == original

    @pytest.mark.parametrize("legacy", ["user", "auto-agent", "full"])
    def test_legacy_state_backup_without_escalation(self, tmp_path, legacy):
        path = tmp_path / "permissions.json"
        original = json.dumps({"mode": legacy, "rules": {"file_write": 2, "shell": 2}}, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode("utf-8")
        path.write_bytes(original)
        engine = PermissionEngine(rules_file=path, log_file=tmp_path / "log.jsonl")
        assert engine.mode == "user" and engine.migration_notice
        assert path.with_suffix(".legacy-v1.json").read_bytes() == original
        assert json.loads(path.read_text(encoding="utf-8")) == {"version": 2, "mode": "user"}
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ASK
        for old in ("auto-agent", "full", "root"):
            with pytest.raises(ValueError):
                engine.set_mode(old)
        path.write_text("{ broken", encoding="utf-8")
        assert PermissionEngine(rules_file=path).mode == "user"

@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(db_path=tmp_path / "memory.db", scope="u1")

class TestMemoryStore:
    def test_crud_lifecycle(self, store: MemoryStore):
        """add/get/update/list/delete 完整流转"""
        added = store.add("约定", "使用 pytest", ["code"], 2)
        assert added["ok"] is True
        memory_id = str(added["memory_id"])

        got = store.get(memory_id)
        assert got["title"] == "约定" and got["content"] == "使用 pytest"
        assert got["tags"] == ["code"] and got["importance"] == 2

        updated = store.update(memory_id, content="改用 pytest-asyncio")
        assert updated["ok"] is True and updated["content"] == "改用 pytest-asyncio"

        assert [item["id"] for item in store.list_all()] == [memory_id]
        assert store.delete(memory_id)["ok"] is True
        assert store.list_all() == []

    def test_prefix_resolution(self, store: MemoryStore, tmp_path: Path):
        """唯一前缀可定位, 多匹配返回错误"""
        a = str(store.add("a", "x")["memory_id"])
        b = str(store.add("b", "y")["memory_id"])
        assert store.get(a[:8])["ok"] is True
        # 强制两条记忆共享 8 位前缀, 触发多匹配
        import sqlite3

        shared = a[:8]
        with sqlite3.connect(tmp_path / "memory.db") as conn:
            conn.execute("UPDATE memories SET id=? WHERE id=?", (shared + "0" * 16, a))
            conn.execute("UPDATE memories SET id=? WHERE id=?", (shared + "1" * 16, b))
        assert store.get(shared)["ok"] is False
        assert store.delete(shared)["ok"] is False

    def test_scope_isolation(self, tmp_path: Path):
        """不同作用域记忆互相隔离"""
        s1 = MemoryStore(db_path=tmp_path / "m.db", scope="u1")
        s2 = MemoryStore(db_path=tmp_path / "m.db", scope="u2")
        s1.add("秘密", "u1 的数据")
        assert [item["title"] for item in s1.list_all()] == ["秘密"] and s2.list_all() == []
        assert s2.get(s1.list_all()[0]["id"])["ok"] is False
        assert s2.clear() == 0

    def test_modes(self, store: MemoryStore):
        """disabled/base/full 模式行为"""
        store.add("m", "c")
        store.set_mode("base")
        assert store.can_write() is False
        assert store.add("m2", "c2")["ok"] is False
        assert store.delete(store.list_all()[0]["id"])["ok"] is False
        assert store.clear() == 0
        assert store.to_context_block() != ""   # base 只读仍注入
        store.set_mode("disabled")
        assert store.to_context_block() == ""
        with pytest.raises(ValueError):
            store.set_mode("evil")

    def test_context_block_ordering_and_limit(self, tmp_path: Path):
        """注入块: importance 降序 + max_entries 截断"""
        store = MemoryStore(db_path=tmp_path / "m.db", scope="u1", max_entries=2)
        store.add("低", "low", importance=1)
        store.add("高", "high", importance=5)
        store.add("中", "mid", importance=3)
        block = store.to_context_block()
        assert "high" in block and "mid" in block and "low" not in block   # max_entries=2 截断
        assert block.index("high") < block.index("mid")
        assert block.count("<long-term-memory>") == 1
        other = MemoryStore(db_path=tmp_path / "m.db", scope="nobody")
        assert other.to_context_block() == ""

    def test_context_block_sql_limit_matches_full_slice(self, tmp_path: Path):
        """SQL 侧 LIMIT 截断与逐行切片在重要度和时间相同时保持同一结果"""
        store = MemoryStore(db_path=tmp_path / "m.db", scope="u1", max_entries=3)
        for index in range(5):
            store.add(f"标题{index}", f"正文{index}", importance=2)
        with closing(store._connect()) as conn, conn:
            conn.execute("UPDATE memories SET updated_at='2026-01-01T00:00:00'")
        # 参照实现即改动前的 list_all() 切片, 用于对照 SQL LIMIT 的取行与顺序
        assert store.to_context_block() == store._render_block(store.list_all()[:3])

    def test_empty_content_rejected(self, store: MemoryStore):
        """空 title/content 拒绝添加"""
        assert store.add("", "x")["ok"] is False
        assert store.add("x", "")["ok"] is False

@pytest.fixture
def goals(tmp_path: Path) -> GoalState:
    return GoalState(file_path=tmp_path / "goal.json")

class TestGoalState:
    def test_goal_lifecycle(self, goals: GoalState):
        """set/get/complete/clear 完整流转"""
        goal = goals.set_goal("s1", "写一个插件")
        assert goal["status"] == "active"
        assert goals.is_active("s1") is True
        assert goals.complete_goal("s1") is True
        assert goals.is_active("s1") is False
        done = goals.get_goal("s1")
        assert done is not None and done["status"] == "done"
        assert goals.clear_goal("s1") is True
        assert goals.clear_goal("s1") is False
        assert goals.complete_goal("s1") is False

    def test_empty_goal_rejected(self, goals: GoalState):
        with pytest.raises(ValueError):
            goals.set_goal("s1", "   ")

    def test_todo_lifecycle(self, goals: GoalState):
        """子任务添加/完成/边界"""
        goals.set_goal("s1", "写插件")
        assert goals.add_todo("s1", "写权限引擎") is True
        assert goals.add_todo("s1", "   ") is False
        assert goals.complete_todo("s1", 0) is True
        assert goals.complete_todo("s1", 5) is False
        assert goals.complete_todo("nobody", 0) is False
        goals.complete_goal("s1")
        assert goals.add_todo("s1", "完成后的子任务") is False

    def test_session_isolation(self, tmp_path: Path):
        """不同会话目标互相隔离"""
        g = GoalState(file_path=tmp_path / "g.json")
        g.set_goal("s1", "A")
        g.set_goal("s2", "B")
        a = g.get_goal("s1")
        b = g.get_goal("s2")
        assert a is not None and a["text"] == "A"
        assert b is not None and b["text"] == "B"
        g.clear_goal("s1")
        assert g.get_goal("s1") is None and g.get_goal("s2") is not None

    def test_context_and_status_format(self, goals: GoalState):
        """注入块与状态文本"""
        assert goals.to_context_block("s1") == ""
        goals.set_goal("s1", "目标A")
        block = goals.to_context_block("s1")
        assert "<active-goal>目标A</active-goal>" in block
        assert "每次回复都应围绕该目标推进" in block

        goals.add_todo("s1", "任务1")
        assert "[ ] 任务1" in goals.to_context_block("s1")
        status = goals.format_status("s1")
        assert "目标: 目标A" in status and "active" in status
        assert "当前没有设置目标" in goals.format_status("nobody")

    def test_persistence_reload(self, tmp_path: Path):
        """状态落盘后新实例重载"""
        file_path = tmp_path / "g.json"
        GoalState(file_path=file_path).set_goal("s1", "持久目标")
        reloaded = GoalState(file_path=file_path)
        goal = reloaded.get_goal("s1")
        assert goal is not None and goal["text"] == "持久目标"
        file_path.write_text("{ broken", encoding="utf-8")
        assert GoalState(file_path=file_path).get_goal("s1") is None

    def test_multi_instance_merge(self, tmp_path: Path):
        """M5: 多实例交错设置目标不互相覆盖"""
        file_path = tmp_path / "g.json"
        a = GoalState(file_path=file_path)
        b = GoalState(file_path=file_path)
        a.set_goal("s1", "目标A")
        b.set_goal("s2", "目标B")
        a.set_goal("s3", "目标C")
        a1 = a.get_goal("s1")
        b2 = b.get_goal("s2")
        a3 = a.get_goal("s3")
        assert a1 is not None and a1["text"] == "目标A"
        assert b2 is not None and b2["text"] == "目标B"
        assert a3 is not None and a3["text"] == "目标C"
        b.clear_goal("s2")
        # 磁盘为准: 新实例看不到已删除的 s2, s1/s3 保留
        reloaded = GoalState(file_path=file_path)
        s1 = reloaded.get_goal("s1")
        s3 = reloaded.get_goal("s3")
        assert reloaded.get_goal("s2") is None
        assert s1 is not None and s1["text"] == "目标A"
        assert s3 is not None and s3["text"] == "目标C"

def _reply(value: str) -> Callable[[str], str]:
    """创建忽略提示文本并返回固定答案的输入函数"""
    def answer(_: str) -> str:
        """返回固定答案"""
        return value

    return answer

class _FakeAsyncLLM(AsyncLLM):
    """记录调用参数的异步 fake LLM"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        self.calls.append({"messages": messages, "tools": tools})
        return LLMCallResponse(type="answer", content="异步回复")

    async def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Any:
        self.calls.append({"messages": messages, "tools": tools})
        yield LLMCallStreamEvent(kind="content_delta", delta="流")

@pytest.fixture
def workspace(tmp_path: Any, monkeypatch: Any) -> Any:
    """把工作区根与插件数据目录重定向到临时目录, 使文件工具/数据落盘隔离"""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(tools_mod, "WORKSPACE_ROOT", root)
    return root

class _FakeLLM(LLM):
    """记录调用参数的同步 fake LLM"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        self.calls.append({"messages": messages, "tools": tools})
        return LLMCallResponse(type="answer", content="回复")

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        self.calls.append({"messages": messages, "tools": tools})
        yield LLMCallStreamEvent(kind="content_delta", delta="流")

def _make_session(tmp_path: Any) -> SimpleSession:
    return SimpleSession(
        "conv-1", _FakeLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

def _install_tools(session: SimpleSession) -> dict[str, Any]:
    """安装全部工具并返回名称 -> 实例映射"""
    tools = get_tools(session)
    names: set[str] = set()
    for tool in tools:
        name = tool.get_tool_name()
        assert name not in names, f"工具重名: {name}"
        names.add(name)
        session.add_tool(tool)
    return {t.get_tool_name(): t for t in tools}

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_factory_tool_set(tmp_path: Any, asynchronous: bool):
    """同步与异步会话均注册 11 个无重名工具, 异步集全部为 AsyncTool"""
    expected = {
        "ask_user", "shell", "subagent", "read_file", "write_file",
        "edit_file", "search_replace", "todo_write", "list_dir", "glob_files", "grep_files",
    }
    if asynchronous:
        session = AsyncSimpleSession(
            "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
        )
        await session.initialize()
        tools = get_tools(session)
        assert all(isinstance(tool, AsyncTool) for tool in tools)
        names = {tool.get_tool_name() for tool in tools}
    else:
        session = _make_session(tmp_path)
        tools = _install_tools(session)
        assert set(session.list_tools()) == expected
        names = set(tools)
    assert names == expected

def test_file_tools_read_list_glob_grep(tmp_path: Any, workspace: Any):
    """只读文件工具: 读/列表/glob/grep"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    target = workspace / "src" / "demo.py"
    target.parent.mkdir(parents=True)
    target.write_text("line1\nline2 demo\n", encoding="utf-8")

    out = tools["read_file"].execute(str(target))
    assert "line2 demo" in out
    out = tools["read_file"].execute(str(target), offset=1, limit=1)
    assert "line2 demo" in out and "line1" not in out

    out = tools["list_dir"].execute(str(workspace))
    assert "src" in out

    out = tools["glob_files"].execute("**/*.py")
    assert "src/demo.py" in out

    out = tools["grep_files"].execute("demo", path=str(workspace))
    assert "demo.py:2:" in out

@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"offset": "bad"}, "offset 必须是整数"),
        ({"offset": -1}, "offset 必须在"),
        ({"limit": 0}, "limit 必须在"),
        ({"limit": 2001}, "limit 必须在"),
    ],
)
def test_read_file_rejects_invalid_pagination(
    tmp_path: Path,
    workspace: Path,
    kwargs: dict[str, object],
    expected: str,
):
    """read_file 对错误类型和越界分页参数返回稳定错误"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    target = workspace / "page.txt"
    target.write_text("line\n", encoding="utf-8")

    execute = cast(Callable[..., str], tools["read_file"].execute)
    out = execute(str(target), **kwargs)

    assert expected in out

def test_glob_cannot_escape_workspace(tmp_path: Any, workspace: Any):
    """L3: glob 的 ../ 模式不能枚举工作区外文件"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    outside = workspace.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    (workspace / "inside.txt").write_text("y", encoding="utf-8")

    out = tools["glob_files"].execute("../*.txt")
    assert "outside.txt" not in out   # ../ 越界结果被过滤
    out = tools["glob_files"].execute("*.txt")
    assert "inside.txt" in out   # 工作区内正常匹配

@pytest.mark.asyncio
async def test_grep_does_not_follow_symlink_outside_workspace(tmp_path: Path, workspace: Path):
    """同步和异步 grep 均不得读取工作区外的符号链接目标"""
    outside = workspace.parent / "outside-secret.txt"
    outside.write_text("external-secret", encoding="utf-8")
    link = workspace / "linked-secret.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"当前环境无法创建文件符号链接: {error}")

    sync_session = _make_session(tmp_path)
    sync_tools = _install_tools(sync_session)
    assert "external-secret" not in sync_tools["grep_files"].execute("external-secret")

    async_session = await _make_async_session(tmp_path)
    async_tools = await _async_tools(async_session)
    assert "external-secret" not in await async_tools["grep_files"].execute("external-secret")

def test_file_tools_path_boundary_and_protection(tmp_path: Any, workspace: Any):
    """文件工具: 工作区外拒绝, 受保护路径拒绝"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    outside = workspace.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    out = tools["read_file"].execute(str(outside))
    assert "越出工作区" in out

    out = tools["read_file"].execute("no-such-file-xyz.txt")
    assert "不存在" in out

    (workspace / ".satrap").mkdir()
    out = tools["write_file"].execute(".satrap/secret.txt", "x")
    assert "受保护" in out
    out = tools["read_file"].execute(".satrap/config.yaml")
    assert "受保护" in out or "拒绝" in out
    out = tools["read_file"].execute("api-token")
    assert "受保护" in out or "不存在" in out

def test_write_file_requires_approval(tmp_path: Any, workspace: Any):
    """文件写入要求单次结构化批准, 普通 y/all 不授予权限"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    target = workspace / "out.txt"

    out = tools["write_file"].execute(str(target), "hello")
    assert "approval_required" in out
    assert not target.exists()

    session.user_input_provider = _reply("y")
    session.coding_approval_provider = lambda request: ApprovalResult.for_request(request, "allow", "用户批准", "user")
    # y 仅批准本次: 写入成功, 但下次同类操作仍需询问
    out = tools["write_file"].execute(str(target), "hello")
    assert "已写入" in out
    assert target.read_text(encoding="utf-8") == "hello"

    session.coding_approval_provider = lambda request: ApprovalResult.for_request(request, "deny", "用户拒绝", "user")
    assert "approval_denied" in tools["write_file"].execute(str(target), "again", append=True)
    session.coding_approval_provider = None
    session.user_input_provider = _reply("all")
    assert "approval_required" in tools["write_file"].execute(str(target), "again", append=True)
    assert target.read_text(encoding="utf-8") == "hello"

def test_edit_file_tool(tmp_path: Any, workspace: Any):
    """编辑文件: 精确替换 + 未匹配报错"""
    session = _make_session(tmp_path)
    session.user_input_provider = _reply("y")
    session.coding_approval_provider = lambda request: ApprovalResult.for_request(request, "allow", "用户批准", "user")
    tools = _install_tools(session)
    target = workspace / "edit.txt"
    target.write_text("aaa bbb aaa", encoding="utf-8")

    out = tools["edit_file"].execute(str(target), "bbb", "ccc")
    assert "已编辑" in out
    assert target.read_text(encoding="utf-8") == "aaa ccc aaa"

    out = tools["edit_file"].execute(str(target), "zzz", "x")
    assert "未找到匹配" in out

def test_search_replace_tool(tmp_path: Any, workspace: Any):
    """批量替换: 多对 old->new, replace_all 独立控制, 全量预校验"""
    session = _make_session(tmp_path)
    session.user_input_provider = _reply("y")
    session.coding_approval_provider = lambda request: ApprovalResult.for_request(request, "allow", "用户批准", "user")
    tools = _install_tools(session)
    target = workspace / "sr.txt"
    target.write_text("x a x b x a", encoding="utf-8")

    out = tools["search_replace"].execute(
        str(target),
        [{"old": "x", "new": "X", "replace_all": True}, {"old": "b", "new": "B"}],
    )
    assert "已批量替换" in out and "'x' 3 处" in out
    assert target.read_text(encoding="utf-8") == "X a X B X a"

    out = tools["search_replace"].execute(
        str(target), [{"old": "X", "new": "Y"}, {"old": "zzz", "new": "y"}],
    )
    # 任一 old 不匹配: 全部不执行 (预校验)
    assert "未找到匹配" in out
    assert target.read_text(encoding="utf-8") == "X a X B X a"

    out = tools["search_replace"].execute(str(target), [{"new": "y"}])
    # 格式无效的替换项
    assert "格式无效" in out

def test_todo_write_tool(tmp_path: Any):
    """任务清单: add / list / done / clear 全链路 (会话级状态)"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    todo = tools["todo_write"]

    out = todo.execute("add", item="第一步")
    assert "任务已添加" in out
    out = todo.execute("add", item="第二步")
    assert "任务已添加 (2)" in out

    out = todo.execute("list")
    assert "1. [ ] 第一步" in out and "2. [ ] 第二步" in out

    out = todo.execute("done", index=1)
    assert "任务 1 已完成" in out
    out = todo.execute("list")
    assert "1. [x] 第一步" in out

    out = todo.execute("done", index=9)
    assert "序号无效" in out
    out = todo.execute("done", index="bad")
    assert "序号无效" in out
    out = todo.execute("clear")
    assert "已清空" in out
    out = todo.execute("list")
    assert "任务清单为空" in out

def test_ask_user_tool(tmp_path: Any):
    """询问用户: 有通道返回回复, 无通道返回提示, 推荐选项编号展示"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    out = tools["ask_user"].execute("继续吗?")
    assert "需要用户回复" in out

    session.user_input_provider = _reply("继续")
    out = tools["ask_user"].execute("继续吗?")
    assert "继续" in out

    captured: list[str] = []

    def capture_prompt(prompt: str) -> str:
        """记录用户提示并返回选项"""
        captured.append(prompt)
        return "2"

    session.user_input_provider = capture_prompt
    out = tools["ask_user"].execute("继续吗?", ["方案A", "方案B"])
    assert "1. 方案A" in captured[0] and "2. 方案B" in captured[0]
    assert "用户回复: 2" in out

    structured: list[tuple[str, list[str] | None]] = []

    def structured_provider(question: str, options: list[str] | None = None) -> str:
        structured.append((question, options))
        return "方案B"

    session.user_input_provider = structured_provider
    out = tools["ask_user"].execute("继续吗?", ["方案A", "方案B"])
    assert structured == [("继续吗?", ["方案A", "方案B"])]
    assert "用户回复: 方案B" in out

@pytest.mark.parametrize(
    ("timeout", "expected"),
    [
        ("bad", "timeout 必须是整数"),
        (0, "timeout 必须在"),
        (3601, "timeout 必须在"),
    ],
)
def test_shell_rejects_invalid_timeout(
    tmp_path: Path,
    workspace: Path,
    timeout: int | str,
    expected: str,
) -> None:
    """shell 对错误类型和越界超时返回稳定错误"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    output = tools["shell"].execute("echo never-runs", timeout=timeout)

    assert expected in output

def test_subagent_tool_constructs(tmp_path: Any):
    """子代理工具: 可构造, 独立 manager 白名单过滤"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    sub = tools["subagent"]
    assert sub.llm is session.llm

    from satrap.expend.plugins.satrap_coding.tools import _CodingSubAgent

    agent = _CodingSubAgent(session.llm, session.tools_manager, "你是子代理", ["read_file"])
    assert agent.tools_manager.tools.keys() == {"read_file"}
    assert agent.context_id.startswith("coding_sub_")

@pytest.mark.parametrize(
    ("max_turns", "expected"),
    [
        ("bad", "max_turns 必须是整数"),
        (0, "max_turns 必须在"),
        (101, "max_turns 必须在"),
    ],
)
def test_subagent_rejects_invalid_max_turns(
    tmp_path: Path,
    max_turns: int | str,
    expected: str,
) -> None:
    """子代理对错误类型和越界轮数返回稳定错误"""
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    output = tools["subagent"].execute("never-runs", max_turns=max_turns)

    assert expected in output

async def _make_async_session(tmp_path: Any) -> AsyncSimpleSession:
    """构造异步会话并安装全部异步工具"""
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    await session.initialize()
    for tool in get_tools(session):
        session.add_tool(tool)
    return session

async def _async_tools(session: AsyncSimpleSession) -> dict[str, Any]:
    return {t.get_tool_name(): t for t in session.tools_manager.tools.values()}

@pytest.mark.asyncio
async def test_async_file_tools(tmp_path: Any, workspace: Any):
    """异步文件工具: 读写/编辑/列表/glob/grep 主路径"""
    session = await _make_async_session(tmp_path)
    session.user_input_provider = _reply("y")
    session.coding_approval_provider = lambda request: ApprovalResult.for_request(request, "allow", "用户批准", "user")
    tools = await _async_tools(session)

    target = workspace / "a.txt"
    out = await tools["write_file"].execute(str(target), "hello")
    assert "已写入" in out
    out = await tools["read_file"].execute(str(target))
    assert "hello" in out
    assert "offset 必须是整数" in await tools["read_file"].execute(str(target), offset="bad")
    assert "limit 必须在" in await tools["read_file"].execute(str(target), limit=0)

    out = await tools["edit_file"].execute(str(target), "hello", "world")
    assert "已编辑" in out
    assert (await tools["read_file"].execute(str(target))).endswith("world")
    out = await tools["edit_file"].execute(str(target), "zzz", "x")
    assert "未找到匹配" in out

    out = await tools["list_dir"].execute(str(workspace))
    assert "a.txt" in out
    out = await tools["glob_files"].execute("**/*.txt")
    assert "a.txt" in out
    out = await tools["grep_files"].execute("world", path=str(workspace))
    assert "a.txt:1:" in out

@pytest.mark.asyncio
async def test_async_search_replace_and_todo(tmp_path: Any, workspace: Any):
    """异步 search_replace / todo_write 主路径"""
    session = await _make_async_session(tmp_path)
    session.user_input_provider = _reply("y")
    session.coding_approval_provider = lambda request: ApprovalResult.for_request(request, "allow", "用户批准", "user")
    tools = await _async_tools(session)

    target = workspace / "a.txt"
    target.write_text("x x y", encoding="utf-8")
    out = await tools["search_replace"].execute(
        str(target), [{"old": "x", "new": "X", "replace_all": True}],
    )
    assert "已批量替换" in out
    assert target.read_text(encoding="utf-8") == "X X y"

    out = await tools["todo_write"].execute("add", item="异步任务")
    assert "任务已添加" in out
    out = await tools["todo_write"].execute("list")
    assert "异步任务" in out
    out = await tools["todo_write"].execute("done", index="bad")
    assert "序号无效" in out

@pytest.mark.asyncio
async def test_async_ask_shell(tmp_path: Any, workspace: Any):
    """异步 ask_user / shell 主路径"""
    session = await _make_async_session(tmp_path)
    session.user_input_provider = _reply("y")
    session.coding_approval_provider = lambda request: ApprovalResult.for_request(request, "allow", "用户批准", "user")
    tools = await _async_tools(session)

    out = await tools["ask_user"].execute("继续?")
    assert "用户回复: y" in out

    structured: list[tuple[str, list[str] | None]] = []

    async def structured_provider(question: str, options: list[str] | None = None) -> str:
        structured.append((question, options))
        return "继续"

    session.user_input_provider = structured_provider
    out = await tools["ask_user"].execute("下一步?", ["继续", "停止"])
    assert structured == [("下一步?", ["继续", "停止"])]
    assert "用户回复: 继续" in out

    out = await tools["shell"].execute("echo async-ok")
    assert "async-ok" in out
    out = await tools["shell"].execute("rm -rf /")
    assert "黑名单" in out

    session.user_input_provider = None
    session.coding_approval_provider = None
    out = await tools["shell"].execute("git push")
    assert "approval_required" in out
    out = await tools["shell"].execute("type ../outside.txt")
    assert "approval_required" in out

@pytest.mark.asyncio
async def test_async_shell_and_subagent_reject_invalid_integer_arguments(
    tmp_path: Path,
) -> None:
    """异步 shell 和子代理在执行前拒绝非法整数参数"""
    session = await _make_async_session(tmp_path)
    tools = await _async_tools(session)

    timeout_error = await tools["shell"].execute("echo never-runs", timeout="bad")
    turns_error = await tools["subagent"].execute("never-runs", max_turns="bad")

    assert "timeout 必须是整数" in timeout_error
    assert "max_turns 必须是整数" in turns_error

@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\x1c", "\u2028"])
def test_file_page_matches_splitlines(tmp_path, separator):
    path = tmp_path / "text.txt"
    text = ("中文" + separator + separator + "尾行" + separator) * 10000 + "末尾"
    path.write_bytes(text.encode("utf-8"))
    for start in (0, 21845, 30000, 40000):
        lines = text.splitlines()
        expected = f"{path} 行 {start}-{min(len(lines), start + 20)}/{len(lines)}:\n" + "\n".join(lines[start:start + 20])
        assert tools_mod._read_file_page(path, start, 20) == expected

def test_file_page_bounds_long_line_memory(tmp_path):
    path = tmp_path / "long.txt"
    with path.open("wb") as stream:
        for _ in range(512):
            stream.write(b"x" * 65536)
        stream.write(b"\nlast\n")
    tracemalloc.start()
    try:
        result = tools_mod._read_file_page(path, 0, 20)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert "已截断" in result and "\nlast\n" in result
    assert "行 0-2/2" in result

@pytest.mark.asyncio
async def test_all_unbound_tools_fail_before_other_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证五对工具在未绑定时优先报告会话错误"""
    engine = PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl")
    calls: list[tuple[Tool, AsyncTool, tuple[object, ...]]] = [
        (sync_write.WriteFileTool(engine), async_write.AsyncWriteFileTool(engine), ("file", "content")),
        (sync_write.EditFileTool(engine), async_write.AsyncEditFileTool(engine), ("file", "old", "new")),
        (sync_write.SearchReplaceTool(engine), async_write.AsyncSearchReplaceTool(engine), ("file", [])),
        (sync_interaction.AskUserTool(), async_interaction.AsyncAskUserTool(), ("question",)),
        (sync_interaction.ShellTool(engine), async_interaction.AsyncShellTool(engine), ("unused",)),
    ]
    for module in (sync_write, async_write, sync_interaction, async_interaction):
        monkeypatch.setattr(module, "_tool_root", Mock(side_effect=AssertionError("不得访问工作区")))
    for sync, async_tool, args in calls:
        with pytest.raises(RuntimeError, match=f"^{sync.tool_name} 未绑定会话$"):
            sync.execute(*args)
        with pytest.raises(RuntimeError, match=f"^{async_tool.tool_name} 未绑定会话$"):
            await async_tool.execute(*args)

@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"old": "missing", "new": "x"}, {"old": "", "new": "x"}])
async def test_invalid_later_replacement_never_approves_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: dict[str, Any]
) -> None:
    """验证后续替换项错误不会导致审批或部分写入"""
    path = tmp_path / "file.txt"
    path.write_text("first second", encoding="utf-8")
    engine = PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl")
    sync, async_tool = sync_write.SearchReplaceTool(engine), async_write.AsyncSearchReplaceTool(engine)
    sync._bind(SimpleSession.__new__(SimpleSession))
    bind_environment(sync._session, replace(environment_snapshot(sync._session), workspace_root=Path(tmp_path)))
    async_tool._bind(AsyncSimpleSession.__new__(AsyncSimpleSession))
    bind_environment(async_tool._session, replace(environment_snapshot(async_tool._session), workspace_root=Path(tmp_path)))
    for module in (sync_write, async_write):
        monkeypatch.setattr(module, "_tool_root", lambda tool: tmp_path)
    sync_approve, async_approve = Mock(), AsyncMock()
    monkeypatch.setattr(sync_write, "_approve_sync", sync_approve)
    monkeypatch.setattr(async_write, "_approve_async", async_approve)
    replacements = [{"old": "first", "new": "changed"}, invalid]
    result = sync.execute(str(path), replacements)
    assert result == await async_tool.execute(str(path), replacements)
    assert "第 2 个" in result
    assert path.read_text(encoding="utf-8") == "first second"
    sync_approve.assert_not_called()
    async_approve.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_replacements_preserve_order_and_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    """验证已绑定的两版工具保留替换顺序并遵守审批结果"""
    path = tmp_path / "file.txt"
    engine = PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl")
    sync, async_tool = sync_write.SearchReplaceTool(engine), async_write.AsyncSearchReplaceTool(engine)
    sync._bind(SimpleSession.__new__(SimpleSession))
    bind_environment(sync._session, replace(environment_snapshot(sync._session), workspace_root=Path(tmp_path)))
    async_tool._bind(AsyncSimpleSession.__new__(AsyncSimpleSession))
    bind_environment(async_tool._session, replace(environment_snapshot(async_tool._session), workspace_root=Path(tmp_path)))
    for module in (sync_write, async_write):
        monkeypatch.setattr(module, "_tool_root", lambda tool: tmp_path)
    monkeypatch.setattr(sync_write, "_approve_sync", Mock(return_value=(allowed, "拒绝")))
    monkeypatch.setattr(async_write, "_approve_async", AsyncMock(return_value=(allowed, "拒绝")))
    replacements = [{"old": "first", "new": "second"}, {"old": "second", "new": "third", "replace_all": True}]
    path.write_text("first second", encoding="utf-8")
    sync_result = sync.execute(str(path), replacements)
    sync_content = path.read_text(encoding="utf-8")
    path.write_text("first second", encoding="utf-8")
    async_result = await async_tool.execute(str(path), replacements)
    assert sync_result == async_result
    assert sync_content == path.read_text(encoding="utf-8") == ("third third" if allowed else "first second")
    if not allowed:
        assert sync_result == "拒绝"

@pytest.mark.asyncio
@pytest.mark.parametrize("error,prefix", [
    (OSError("连接失败"), "请求失败"),
    (TimeoutError("超时"), "请求失败"),
    (OutboundResponseTooLargeError("超限"), "请求失败"),
    (UnsafeOutboundURLError("私网地址"), "请求被安全策略拒绝"),
    (UnsafeOutboundURLError("重定向次数超过限制"), "请求被安全策略拒绝"),
])
async def test_request_errors_match(monkeypatch: pytest.MonkeyPatch, error: Exception, prefix: str) -> None:
    """验证请求阶段的异常分类一致"""
    monkeypatch.setattr(sync_search, "validate_outbound_http_url", lambda url: url)
    monkeypatch.setattr(search, "_requests_get", Mock(side_effect=error))
    monkeypatch.setattr(async_search, "safe_async_get", AsyncMock(side_effect=error))
    url = "https://example.com"
    sync_result = json.loads(search.FetchPageTool().execute(url))
    async_result = json.loads(await search.AsyncFetchPageTool().execute(url))
    assert sync_result == async_result == {"url": url, "error": f"{prefix}: {error}"}

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201, 204, 404, 503])
async def test_response_status_and_html_match(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """验证响应状态与页面正文输出一致"""
    url = "https://example.com"
    response = OutboundHTTPResponse(url=url, status_code=status, headers={}, content='<title>标题</title><p>正文内容</p><script>隐藏</script>'.encode())
    monkeypatch.setattr(sync_search, "validate_outbound_http_url", lambda url: url)
    monkeypatch.setattr(search, "_requests_get", Mock(return_value=response))
    monkeypatch.setattr(async_search, "safe_async_get", AsyncMock(return_value=response))
    sync_result = json.loads(search.FetchPageTool().execute(url, max_length=4))
    async_result = json.loads(await search.AsyncFetchPageTool().execute(url, max_length=4))
    assert sync_result == async_result
    if status != 200:
        assert sync_result == {"url": url, "error": f"HTTP {status}"}
    else:
        assert sync_result["title"] == "标题"
        assert sync_result["content"] == "标题\n正...(内容已截断)"

@pytest.mark.asyncio
async def test_parse_error_is_not_request_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证解析异常独立于请求失败"""
    url = "https://example.com"
    response = OutboundHTTPResponse(url=url, status_code=200, headers={}, content=b"html")
    monkeypatch.setattr(sync_search, "validate_outbound_http_url", lambda url: url)
    monkeypatch.setattr(search, "_requests_get", Mock(return_value=response))
    monkeypatch.setattr(async_search, "safe_async_get", AsyncMock(return_value=response))
    sync, async_tool = search.FetchPageTool(), search.AsyncFetchPageTool()
    sync._extract_text = Mock(side_effect=ValueError("坏页面"))
    async_tool._extract_text = Mock(side_effect=ValueError("坏页面"))
    assert json.loads(sync.execute(url)) == json.loads(await async_tool.execute(url)) == {
        "url": url, "error": "解析失败: 坏页面",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_three_modes_exact_actions_and_separate_user_questions(tmp_path, workspace, monkeypatch, asynchronous):
    """三档模式完整走文件和 Shell, 审批证据包含实际变化, 普通提问不能授权"""
    import inspect
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state
    from satrap.expend.plugins.satrap_coding.tools import approval
    from satrap.core.call_context import bind_tool_workflow
    from types import SimpleNamespace
    session = await _make_async_session(tmp_path) if asynchronous else _make_session(tmp_path)
    tools = await _async_tools(session) if asynchronous else _install_tools(session)
    engine = get_plugin_state(session)["engine"]
    calls, approvals, reviews = [], [], []
    monkeypatch.setattr(tools_mod, "_run_shell", lambda *args, **kwargs: calls.append(args) or "executed")
    def human(request):
        approvals.append(request)
        return ApprovalResult.for_request(request, "allow", "用户批准本次", "user")
    def model(**kwargs):
        reviews.append(kwargs)
        return '{"decision":"allow","reason":"用户已授权本次修改"}'
    async def async_model(**kwargs):
        return model(**kwargs)
    monkeypatch.setattr(session.llm, "chat", async_model if asynchronous else model)
    session.user_input_provider = lambda *args: "all"
    async def execute(name, *args, **kwargs):
        value = tools[name].execute(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    assert "approval_required" in await execute("write_file", "blocked.txt", "not approved")
    assert engine.mode == "user" and not (workspace / "blocked.txt").exists()
    monkeypatch.setattr(session._wf.ctx, "get_context", lambda: [{"role": "user", "content": "用户主会话授权-marker"}])
    child = SimpleNamespace(ctx=SimpleNamespace(get_context=lambda: [
        {"role": "user", "content": "子任务不能冒充用户授权"},
        {"role": "assistant", "content": "准备修改", "reasoning_content": "hidden-secret"}]))
    bind_environment(session, replace(environment_snapshot(session), sandbox_root=Path(workspace / "private")))
    for mode in ("user", "auto", "full-access"):
        engine.set_mode(mode, persist=False)
        session.coding_approval_provider = human
        before_human, before_model = len(approvals), len(reviews)
        assert "已写入" in await execute("write_file", f"private/{mode}.txt", "scratch")
        assert (len(approvals), len(reviews)) == (before_human, before_model)
        with bind_tool_workflow(child):
            assert "已写入" in await execute("write_file", "matrix.txt", "before")
            assert "已编辑" in await execute("edit_file", "matrix.txt", "before", "middle")
            assert "已批量替换" in await execute("search_replace", "matrix.txt", [{"old": "middle", "new": "after"}])
            assert await execute("shell", "echo approved", timeout=13) == "executed"
        assert (workspace / "matrix.txt").read_text(encoding="utf-8") == "after"
        assert len(approvals) - before_human == (4 if mode == "user" else 0)
        assert len(reviews) - before_model == (4 if mode == "auto" else 0)
    change = approvals[1].payload()["action"]
    assert change["before"] == "before" and change["after"] == "middle"
    assert "-before" in change["diff"] and "+middle" in change["diff"]
    shell = approvals[3].payload()["action"]
    assert shell["command"] == "echo approved" and shell["timeout"] == 13
    assert shell["argv"][0] == shell["interpreter"] and shell["cwd"] == str(workspace)
    assert len({request.request_id for request in approvals}) == len(approvals)
    assert all(set(review) == {"messages"} for review in reviews)
    evidence = reviews[0]["messages"][1]["content"]
    assert "main_history" in evidence and "用户主会话授权-marker" in evidence
    assert "executing_history" in evidence and "子任务不能冒充用户授权" in evidence
    assert "hidden-secret" not in evidence and "action_json" not in evidence
    records = [json.loads(line) for line in engine.log_file.read_text(encoding="utf-8").splitlines()]
    assert {"user", "model", "policy"} <= {row["reviewer"] for row in records}
    assert all("before" not in row and "evidence" not in row for row in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_auto_decisions_failures_human_fallback_and_timeout(tmp_path, workspace, monkeypatch, asynchronous):
    """模型拒绝不执行, ask 与格式或调用失败转结构化人工, 超时不能迟到授权"""
    import inspect
    import asyncio
    import time
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state
    from satrap.expend.plugins.satrap_coding.tools import approval
    session = await _make_async_session(tmp_path) if asynchronous else _make_session(tmp_path)
    tools = await _async_tools(session) if asynchronous else _install_tools(session)
    engine = get_plugin_state(session)["engine"]
    engine.set_mode("auto", persist=False)
    monkeypatch.setattr(approval, "_REVIEW_TIMEOUT", 0.01)
    human_calls = []
    def human(request):
        human_calls.append(request)
        return ApprovalResult.for_request(request, "allow", "人工已批准", "user")
    variants = ['{"decision":"deny","reason":"未获用户授权"}',
                '{"decision":"ask","reason":"需要确认目标"}', "allow",
                '{"decision":"allow","reason":"ok","extra":true}', RuntimeError("模型离线"), "timeout"]
    for index, value in enumerate(variants):
        def model(**kwargs):
            if isinstance(value, Exception):
                raise value
            if value == "timeout":
                time.sleep(0.04)
                return '{"decision":"allow","reason":"迟到结果"}'
            return value
        async def async_model(**kwargs):
            if value == "timeout":
                await asyncio.sleep(0.04)
                return '{"decision":"allow","reason":"迟到结果"}'
            return model(**kwargs)
        monkeypatch.setattr(session.llm, "chat", async_model if asynchronous else model)
        for with_human in (False, True):
            session.coding_approval_provider = human if with_human else None
            target = workspace / f"fallback-{index}-{with_human}.txt"
            previous = len(human_calls)
            result = tools["write_file"].execute(str(target), "payload")
            result = await result if inspect.isawaitable(result) else result
            denied = index == 0
            assert target.exists() == (with_human and not denied)
            assert len(human_calls) - previous == int(with_human and not denied)
            if not target.exists():
                assert ("approval_denied" if denied else "approval_required") in result
                if value == "timeout":
                    assert "超时" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("drift", ["file", "created", "mode", "disabled", "sandbox", "replay", "untyped", "turn"])
async def test_approval_cannot_authorize_changed_actions(tmp_path, workspace, asynchronous, drift):
    """文件变化, 权限版本变化, 禁用或审批回放均不得执行"""
    import inspect
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state
    session = await _make_async_session(tmp_path) if asynchronous else _make_session(tmp_path)
    tools = await _async_tools(session) if asynchronous else _install_tools(session)
    engine = get_plugin_state(session)["engine"]
    session.recovery_origin = {"turn_id": 1, "variant_index": 0}
    target = workspace / "drift.txt"
    if drift != "created":
        target.write_text("original", encoding="utf-8")
    tool = tools["write_file"]
    def provider(request):
        if drift in {"file", "created"}:
            target.write_text("external", encoding="utf-8")
        elif drift == "mode":
            engine.set_mode("full-access", persist=False)
            engine.set_mode("user", persist=False)
        elif drift == "disabled":
            tool.disable()
        elif drift == "sandbox":
            bind_environment(session, replace(environment_snapshot(session), sandbox_root=Path(workspace / "other")))
        elif drift == "replay":
            from dataclasses import replace
            return ApprovalResult.for_request(replace(request, request_id="other"), "allow", "重放", "user")
        elif drift == "untyped":
            return "y"
        elif drift == "turn":
            session.recovery_origin = {"turn_id": 2, "variant_index": 0}
        return ApprovalResult.for_request(request, "allow", "本次批准", "user")
    session.coding_approval_provider = provider
    value = tool.execute(str(target), "should-never-write", append=True)
    result = await value if inspect.isawaitable(value) else value
    assert "已写入" not in result and "已追加" not in result
    assert target.read_text(encoding="utf-8") == ("external" if drift in {"file", "created"} else "original")


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_full_access_outside_workspace_and_plan_override(tmp_path, workspace, asynchronous):
    """full-access 文件工具可显式访问工作区外, 保留硬保护和计划模式"""
    import inspect
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state
    session = await _make_async_session(tmp_path) if asynchronous else _make_session(tmp_path)
    tools = await _async_tools(session) if asynchronous else _install_tools(session)
    engine = get_plugin_state(session)["engine"]
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "test.txt"
    async def execute(name, *args):
        value = tools[name].execute(*args)
        return await value if inspect.isawaitable(value) else value
    assert "越出工作区" in await execute("write_file", str(target), "external-content")
    engine.set_mode("full-access", persist=False)
    assert "已写入" in await execute("write_file", str(target), "external-content")
    assert "external-content" in await execute("read_file", str(target))
    assert "test.txt" in await execute("list_dir", str(outside))
    assert "test.txt" in await execute("glob_files", str(outside / "*.txt"))
    assert "external-content" in await execute("grep_files", "external-content", str(outside))
    assert "受保护" in await execute("write_file", str(outside / ".env"), "blocked")
    engine.set_plan_mode(True)
    assert "计划模式" in await execute("write_file", str(target), "blocked")
    assert target.read_text(encoding="utf-8") == "external-content"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_named_approval_model_uses_host_configuration(tmp_path, workspace, monkeypatch, asynchronous):
    """专用审批模型来自宿主模型表, 不把调用转给主 Agent"""
    from types import SimpleNamespace
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state
    from satrap.expend.plugins.satrap_coding.tools import approval
    session = await _make_async_session(tmp_path) if asynchronous else _make_session(tmp_path)
    tools = await _async_tools(session) if asynchronous else _install_tools(session)
    engine = get_plugin_state(session)["engine"]
    engine.set_mode("auto", persist=False)
    engine.approval_model = "reviewer"
    config, seen = object(), []
    def lookup(name):
        assert name == "reviewer"
        return config
    def chat(**kwargs):
        seen.append(kwargs)
        return '{"decision":"allow","reason":"指定审批模型确认"}'
    async def async_chat(**kwargs):
        return chat(**kwargs)
    session.plugin_model_manager = SimpleNamespace(has_config=lambda target, name: (target, name) == ("llm", "reviewer"), get_llm_config=lookup)
    def build(value, *, async_):
        assert value is config and async_ == asynchronous
        return SimpleNamespace(chat=async_chat if asynchronous else chat)
    monkeypatch.setattr(approval, "build_llm_from_config", build)
    value = tools["write_file"].execute("named.txt", "approved")
    result = await value if asynchronous else value
    assert "已写入" in result and len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_subagent_inherits_mode_and_supplies_its_own_evidence(tmp_path, workspace, monkeypatch, asynchronous):
    """真实子代理循环共享同一引擎, 独立审查同时看到主用户授权与子任务"""
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state
    session = await _make_async_session(tmp_path) if asynchronous else _make_session(tmp_path)
    tools = await _async_tools(session) if asynchronous else _install_tools(session)
    engine = get_plugin_state(session)["engine"]
    seen, results = [], []
    stage = [0]
    def call(messages, **kwargs):
        stage[0] += 1
        if stage[0] % 4 == 1:
            return LLMCallResponse(type="tools_call", content="", tool_calls=[
                {"name": "subagent", "id": f"sub-{stage[0]}", "arguments": {"task": "独立子任务-marker", "tools": ["write_file"], "max_turns": 3}}])
        if stage[0] % 4 == 2:
            return LLMCallResponse(type="tools_call", content="", tool_calls=[
                {"name": "write_file", "id": f"call-{stage[0]}", "arguments": {"path": "child.txt", "content": "child-result"}}])
        if stage[0] % 4 == 3:
            results.append(messages[-1]["content"])
            return LLMCallResponse(type="answer", content="子任务结束")
        return LLMCallResponse(type="answer", content="主任务结束")
    def review(**kwargs):
        seen.append(kwargs["messages"][1]["content"])
        return '{"decision":"allow","reason":"主用户已授权"}'
    async def async_call(messages, **kwargs):
        return call(messages, **kwargs)
    async def async_review(**kwargs):
        return review(**kwargs)
    monkeypatch.setattr(session.llm, "call", async_call if asynchronous else call)
    monkeypatch.setattr(session.llm, "chat", async_review if asynchronous else review)
    engine.set_mode("auto", persist=False)
    value = session.run("真实用户授权-marker")
    assert (await value if asynchronous else value) == "主任务结束"
    assert (workspace / "child.txt").read_text(encoding="utf-8") == "child-result"
    assert len(seen) == 1
    assert all(marker in seen[0] for marker in ("main_history", "真实用户授权-marker", "executing_history", "独立子任务-marker")), seen[0]
    engine.set_mode("user", persist=False)
    (workspace / "child.txt").write_text("original", encoding="utf-8")
    value = session.run("真实用户授权-marker")
    assert (await value if asynchronous else value) == "主任务结束"
    assert "approval_required" in str(results[-1])
    assert (workspace / "child.txt").read_text(encoding="utf-8") == "original"
