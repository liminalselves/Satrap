"""satrap_coding 插件端到端测试: 安装 / 命令 (/goal /plan /memory /approve) / 处理器注入 / 卸载"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, cast

import pytest

from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import SimpleSession
from satrap.expend.plugins.satrap_coding import tools as tools_mod

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "satrap_coding"


class _FakeLLM(LLM):
    """记录调用参数的同步 fake LLM"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        self.calls.append({"messages": messages, "tools": tools})
        return LLMCallResponse(type="answer", content="回复")

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        self.calls.append({"messages": messages, "tools": tools})
        yield LLMCallStreamEvent(kind="content_delta", delta="流")


@pytest.fixture
def session(tmp_path: Any, monkeypatch: Any) -> SimpleSession:
    """隔离插件数据目录的会话 + 安装 satrap_coding 插件"""
    monkeypatch.setattr(tools_mod, "DATA_ROOT", tmp_path / "coding")
    monkeypatch.setattr(tools_mod, "WORKSPACE_ROOT", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    s = SimpleSession(
        "conv-1", _FakeLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    s.install_plugin(str(PLUGIN_DIR))
    return s


def _user_text(s: SimpleSession, index: int = -1) -> str:
    """取第 index 次模型调用的最后一条用户消息"""
    calls = cast(list[dict[str, Any]], getattr(s.llm, "calls"))
    messages = calls[index]["messages"]
    return str([m for m in messages if m.get("role") == "user"][-1]["content"])


def test_plugin_install_full_capabilities(session: SimpleSession):
    """安装: 17 工具 + 4 命令 + 2 技能 + 1 处理器"""
    plugin = session.list_plugins()[0]
    assert plugin.name == "satrap_coding"
    assert len(plugin.tools) == 17
    assert set(plugin.commands) == {"goal", "plan", "memory", "approve"}
    assert set(plugin.skills) == {"goal", "plan"}
    assert set(plugin.handlers) == {"satrap_coding.inject"}

    assert len(session.list_tools()) == 17
    assert set(session.list_commands()) >= {"goal", "plan", "memory", "approve"}
    assert "goal" in session.list_skills()


def test_goal_command_lifecycle_and_injection(session: SimpleSession):
    """/goal: 设置 -> 注入模型输入 -> status -> done 后不再注入"""
    result, is_cmd = session.cmd_handler.process_message("/goal 实现一个编码助手")
    assert is_cmd and "目标已设置" in str(result)

    session.run("帮我看看这个文件")
    text = _user_text(session)
    assert "实现一个编码助手" in text
    assert "【长期记忆与目标】" in text

    result, _ = session.cmd_handler.process_message("/goal status")
    assert "状态: active" in str(result)

    result, _ = session.cmd_handler.process_message("/goal done")
    assert "已标记完成" in str(result)

    session.run("再来一轮")
    text = _user_text(session)
    assert "实现一个编码助手" not in text


def test_goal_todo(session: SimpleSession):
    """/goal todo: 子任务增删"""
    session.cmd_handler.process_message("/goal 写插件")
    result, _ = session.cmd_handler.process_message("/goal todo 写权限引擎")
    assert "子任务已添加" in str(result)
    result, _ = session.cmd_handler.process_message("/goal todo-done 0")
    assert "已标记完成" in str(result)
    result, _ = session.cmd_handler.process_message("/goal status")
    assert "[x]" in str(result)


def test_plan_mode_blocks_writes(session: SimpleSession):
    """/plan: 进入后全部写类能力被拒 (文件/记忆), 退出恢复"""
    write_tool = session.tools_manager.tools["write_file"]
    add_memory = session.tools_manager.tools["add_memory"]
    target = tools_mod.WORKSPACE_ROOT / "x.txt"

    session.user_input_provider = lambda q: "y"
    session.run("占位")  # 触发一次完整流程
    # 直接写可通过 (用户批准)
    assert "已写入" in write_tool.execute(str(target), "hi")

    result, _ = session.cmd_handler.process_message("/plan on")
    assert "计划模式" in str(result)
    out = write_tool.execute(str(target), "blocked")
    assert "拒绝" in out or "计划模式" in out
    assert target.read_text(encoding="utf-8") == "hi"

    # 记忆写在计划模式下同样被拒
    out = add_memory.execute("t", "c")
    assert "拒绝" in out
    result, _ = session.cmd_handler.process_message("/memory add 偏好 计划中不应写记忆")
    assert "拒绝" in str(result)

    # 工作区内 shell 写命令在计划模式下同样被拒
    shell_tool = session.tools_manager.tools["shell"]
    out = shell_tool.execute("echo x > f2.txt")
    assert "计划模式" in out

    result, _ = session.cmd_handler.process_message("/plan off")
    assert "退出" in str(result)
    assert "已写入" in write_tool.execute(str(target), "again")
    assert "记忆已添加" in add_memory.execute("t", "c")


def test_uninstall_resets_plugin_state(session: SimpleSession):
    """M2: 卸载清理插件状态, 重装后 plan_mode 不残留 (记忆数据持久化保留)"""
    session.cmd_handler.process_message("/memory add 约定 旧记忆")
    session.cmd_handler.process_message("/plan on")

    assert session.uninstall_plugin("satrap_coding") is True
    session.install_plugin(str(PLUGIN_DIR))

    # 重装后计划模式已重置: 写文件需要批准 (不再是 plan 拒绝, 而是正常审批流)
    write_tool = session.tools_manager.tools["write_file"]
    target = tools_mod.WORKSPACE_ROOT / "y.txt"
    out = write_tool.execute(str(target), "hi")
    assert "需要用户批准" in out  # user 策略无通道 = 需要批准, 而非计划模式拒绝
    assert "拒绝: 计划模式" not in out

    result, _ = session.cmd_handler.process_message("/memory list")
    # 记忆数据持久化保留 (SQLite 文件未删), 内存状态 (plan mode) 已重置
    assert "旧记忆" in str(result)


def test_memory_command(session: SimpleSession):
    """/memory: 用户侧接口 list/add/del/mode"""
    result, _ = session.cmd_handler.process_message("/memory add 偏好 用户喜欢中文")
    assert "记忆已添加" in str(result)
    result, _ = session.cmd_handler.process_message("/memory list")
    assert "用户喜欢中文" in str(result)

    # 注入模型输入
    session.run("你好")
    assert "用户喜欢中文" in _user_text(session)

    memory_id = _first_memory_id(session)
    result, _ = session.cmd_handler.process_message(f"/memory del {memory_id}")
    assert "已删除" in str(result)

    result, _ = session.cmd_handler.process_message("/memory mode base")
    assert "已切换" in str(result)
    result, _ = session.cmd_handler.process_message("/memory add 偏好2 内容2")
    assert "只读" in str(result)


def test_approve_command(session: SimpleSession):
    """/approve: 策略切换与持久规则"""
    write_tool = session.tools_manager.tools["write_file"]
    target = tools_mod.WORKSPACE_ROOT / "x.txt"

    # user 模式无输入通道: 拒绝
    out = write_tool.execute(str(target), "hi")
    assert "需要用户批准" in out

    result, _ = session.cmd_handler.process_message("/approve mode full")
    assert "已切换: full" in str(result)
    assert "已写入" in write_tool.execute(str(target), "hi")

    result, _ = session.cmd_handler.process_message("/approve rule file_write 1")
    assert "持久规则已添加" in str(result)
    result, _ = session.cmd_handler.process_message("/approve rules")
    assert "file_write" in str(result)


def test_memory_injection_cached_and_invalidated(session: SimpleSession):
    """记忆注入: 写记忆后下一轮自动带上, 删除后不再注入"""
    session.cmd_handler.process_message("/memory add 约定 项目使用 pytest")
    session.run("第一轮")
    text = _user_text(session)
    assert "项目使用 pytest" in text

    session.cmd_handler.process_message("/memory list")
    result, _ = session.cmd_handler.process_message("/memory del " + _first_memory_id(session))
    assert "已删除" in str(result)

    session.run("第二轮")
    assert "项目使用 pytest" not in _user_text(session)


def _first_memory_id(session: SimpleSession) -> str:
    result, _ = session.cmd_handler.process_message("/memory list")
    lines = [ln for ln in str(result).splitlines() if ln.startswith("- ")]
    assert lines, "应有至少一条记忆"
    return lines[0].split(" ")[1]


def test_uninstall_plugin_cleanup(session: SimpleSession):
    """卸载: 工具/命令/处理器全部回收"""
    assert session.uninstall_plugin("satrap_coding") is True
    assert session.list_tools() == []
    assert "goal" not in session.list_commands()
    assert "memory" not in session.list_commands()
    assert session.list_handlers() == []
    assert session.list_skills() == []
    assert session.list_plugins() == []
