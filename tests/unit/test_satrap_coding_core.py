"""satrap_coding 插件 core 模块单元测试: 权限引擎 / 命令闸门 / 记忆库 / 目标状态"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from satrap.expend.plugins.satrap_coding.core.command_gate import classify_command
from satrap.expend.plugins.satrap_coding.core.goal_state import GoalState
from satrap.expend.tools.memory_store import MemoryStore
from satrap.expend.plugins.satrap_coding.core.permission import (
    PermissionDecision,
    PermissionEngine,
    RiskLevel,
)


# ================= command_gate: 命令风险分级 =================


class TestClassifyCommand:
    def test_empty_and_read(self):
        """空串与只读命令: READ 且非越界"""
        assert classify_command("") == (RiskLevel.READ, False)
        assert classify_command("   ") == (RiskLevel.READ, False)
        assert classify_command("ls -la") == (RiskLevel.READ, False)
        assert classify_command("git status") == (RiskLevel.READ, False)
        assert classify_command("Get-ChildItem") == (RiskLevel.READ, False)

    def test_write_and_high(self):
        """常规写与高危命令分级"""
        assert classify_command("mkdir newdir") == (RiskLevel.WRITE, False)
        assert classify_command("git commit -m x") == (RiskLevel.WRITE, False)
        assert classify_command("rm file.txt") == (RiskLevel.HIGH, False)
        assert classify_command("git push") == (RiskLevel.HIGH, False)
        assert classify_command("git reset HEAD~1") == (RiskLevel.HIGH, False)
        assert classify_command("git clean -fd") == (RiskLevel.FORBIDDEN, False)  # clean -f 命中黑名单

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
        assert classify_command("pip list") == (RiskLevel.WRITE, False)  # 无子命令不越界
        assert classify_command("pip -V") == (RiskLevel.WRITE, False)

    def test_unknown_conservative(self):
        """未匹配命令保守归 WRITE"""
        assert classify_command("mycustomtool do-thing") == (RiskLevel.WRITE, False)

    def test_quoted_head_and_case(self):
        """引号包裹与大小写归一"""
        assert classify_command('"Git" status') == (RiskLevel.READ, False)

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
        """L1: 盘根删除补入黑名单"""
        assert classify_command("rm -rf C:\\") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("rm -rf D:\\x") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("del /f /s /q C:\\windows") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("rd /s /q D:\\data") == (RiskLevel.FORBIDDEN, False)
        assert classify_command("Remove-Item -Recurse -Force E:\\") == (RiskLevel.FORBIDDEN, False)

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

    def test_redirect_upgrades_read(self):
        """只读命令带重定向符 = 写文件, 升级为 WRITE"""
        assert classify_command("echo hi > f.txt")[0] == RiskLevel.WRITE
        assert classify_command("type a.txt >> b.txt")[0] == RiskLevel.WRITE


# ================= permission: 审批引擎 =================


@pytest.fixture
def engine(tmp_path: Path) -> PermissionEngine:
    return PermissionEngine(
        rules_file=tmp_path / "permissions.json",
        log_file=tmp_path / "approval_log.jsonl",
    )


class TestPermissionEngine:
    def test_read_always_allowed(self, engine: PermissionEngine):
        """只读操作直接放行 (无论策略)"""
        assert engine.evaluate("read", RiskLevel.READ) == PermissionDecision.ALLOW

    def test_user_mode_asks_write(self, engine: PermissionEngine):
        """user 策略下常规写操作返回 ASK"""
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ASK

    def test_full_mode_allows(self, engine: PermissionEngine):
        """full 策略全部放行"""
        engine.set_mode("full")
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ALLOW

    def test_forbidden_always_denied(self, engine: PermissionEngine):
        """黑名单级操作永远拒绝"""
        assert engine.evaluate("shell", RiskLevel.FORBIDDEN) == PermissionDecision.DENY

    def test_plan_mode_blocks_writes(self, engine: PermissionEngine):
        """计划模式: 写类操作拒绝, 只读放行"""
        engine.set_plan_mode(True)
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.DENY
        assert engine.evaluate("shell", RiskLevel.HIGH) == PermissionDecision.DENY
        assert engine.evaluate("read", RiskLevel.READ) == PermissionDecision.ALLOW
        engine.set_plan_mode(False)
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ASK

    def test_session_rule_after_approve(self, engine: PermissionEngine):
        """批准一次后会话内同类操作放行"""
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ASK
        engine.approve("file_write", RiskLevel.WRITE)
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ALLOW
        engine.clear_session_rules()
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ASK

    def test_persistent_rule_lifecycle(self, engine: PermissionEngine, tmp_path: Path):
        """持久规则: 添加生效 -> 落盘 -> 新引擎重载 -> 移除"""
        engine.add_persistent_rule("file_write", RiskLevel.WRITE)
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ALLOW

        reloaded = PermissionEngine(
            rules_file=tmp_path / "permissions.json",
            log_file=tmp_path / "approval_log.jsonl",
        )
        assert reloaded.list_persistent_rules() == {"file_write": 1}
        assert reloaded.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ALLOW

        assert reloaded.remove_persistent_rule("file_write") is True
        assert reloaded.remove_persistent_rule("file_write") is False
        assert reloaded.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ASK

    def test_auto_agent_judge(self, engine: PermissionEngine):
        """auto-agent 策略按 judge 判定 allow/deny/ask"""
        engine.set_mode("auto-agent")

        def judge_allow(operation: str, risk: RiskLevel, description: str) -> str:
            return "allow"

        def judge_deny(operation: str, risk: RiskLevel, description: str) -> str:
            return "deny"

        assert engine.evaluate("file_write", RiskLevel.WRITE, judge=judge_allow) == PermissionDecision.ALLOW
        assert engine.evaluate("file_write", RiskLevel.WRITE, judge=judge_deny) == PermissionDecision.DENY

        def judge_maybe(operation: str, risk: RiskLevel, description: str) -> str:
            return "maybe"

        assert engine.evaluate("file_write", RiskLevel.WRITE, judge=judge_maybe) == PermissionDecision.ASK
        assert engine.evaluate("file_write", RiskLevel.WRITE) == PermissionDecision.ASK

    @pytest.mark.asyncio
    async def test_evaluate_async_with_async_judge(self, engine: PermissionEngine, tmp_path: Path):
        """异步评估支持异步 judge"""
        engine = PermissionEngine(
            rules_file=tmp_path / "permissions.json",
            log_file=tmp_path / "approval_log.jsonl",
            mode="auto-agent",
        )

        async def async_judge(op: str, risk: RiskLevel, desc: str) -> str:
            return "allow"

        assert await engine.evaluate_async(
            "file_write", RiskLevel.WRITE, judge=async_judge,
        ) == PermissionDecision.ALLOW
        assert await engine.evaluate_async(
            "file_write", RiskLevel.WRITE, judge=lambda op, risk, desc: "deny",
        ) == PermissionDecision.DENY
        assert await engine.evaluate_async(
            "file_write", RiskLevel.WRITE,
        ) == PermissionDecision.ASK

    def test_set_mode_invalid(self, engine: PermissionEngine):
        """非法策略名抛 ValueError"""
        with pytest.raises(ValueError):
            engine.set_mode("root")

    def test_deny_logs_entry(self, engine: PermissionEngine, tmp_path: Path):
        """拒绝操作写入审批日志"""
        engine.deny("shell", RiskLevel.HIGH, "危险命令")
        log = (tmp_path / "approval_log.jsonl").read_text(encoding="utf-8")
        assert "shell" in log and "DENY" in log

    def test_broken_rules_file_ignored(self, tmp_path: Path):
        """损坏的规则文件加载为空, 不崩溃"""
        bad = tmp_path / "permissions.json"
        bad.write_text("{ not json", encoding="utf-8")
        engine = PermissionEngine(rules_file=bad, log_file=tmp_path / "log.jsonl")
        assert engine.list_persistent_rules() == {}

    def test_mode_persisted_and_restored(self, tmp_path: Path):
        """审批策略随规则文件持久化, 新引擎恢复"""
        rules_file = tmp_path / "permissions.json"
        PermissionEngine(rules_file=rules_file, log_file=tmp_path / "log.jsonl").set_mode("full")
        engine = PermissionEngine(rules_file=rules_file, log_file=tmp_path / "log.jsonl")
        assert engine.mode == "full"

    def test_multi_engine_rule_merge(self, tmp_path: Path):
        """M5: 多实例交错添加持久规则不丢失更新"""
        rules_file = tmp_path / "permissions.json"
        a = PermissionEngine(rules_file=rules_file, log_file=tmp_path / "log.jsonl")
        b = PermissionEngine(rules_file=rules_file, log_file=tmp_path / "log.jsonl")
        a.add_persistent_rule("file_write", RiskLevel.WRITE)
        b.add_persistent_rule("shell", RiskLevel.WRITE)
        a.add_persistent_rule("memory_write", RiskLevel.WRITE)
        assert a.list_persistent_rules() == {"file_write": 1, "shell": 1, "memory_write": 1}
        # 删除不复活: b 移除 shell 后, 磁盘不再有 shell
        b.remove_persistent_rule("shell")
        assert PermissionEngine(
            rules_file=rules_file, log_file=tmp_path / "log.jsonl",
        ).list_persistent_rules() == {"file_write": 1, "memory_write": 1}


# ================= memory_store: 长期记忆 =================


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

        assert store.count() == 1
        assert store.list_all()[0]["id"] == memory_id
        assert store.delete(memory_id)["ok"] is True
        assert store.count() == 0

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
        assert s1.count() == 1 and s2.count() == 0
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
        assert store.to_context_block() != ""  # base 只读仍注入
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
        assert "high" in block and "mid" in block and "low" not in block  # max_entries=2 截断
        assert block.index("high") < block.index("mid")
        assert block.count("<long-term-memory>") == 1
        assert store.to_context_block(scope="nobody") == ""

    def test_empty_content_rejected(self, store: MemoryStore):
        """空 title/content 拒绝添加"""
        assert store.add("", "x")["ok"] is False
        assert store.add("x", "")["ok"] is False


# ================= goal_state: 持续目标状态机 =================


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
