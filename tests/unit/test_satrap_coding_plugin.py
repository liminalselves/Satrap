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


@pytest.fixture
def session(tmp_path: Any, monkeypatch: Any) -> SimpleSession:
    """隔离插件数据目录的会话 + 安装 satrap_coding 插件 (经 config 指定工作区/数据目录)"""
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))
    (tmp_path / "workspace").mkdir()
    s = SimpleSession(
        "conv-1", _FakeLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    s.install_plugin(str(PLUGIN_DIR), config={
        "workspace_root": str(tmp_path / "workspace"),
        "data_root": str(tmp_path / "coding"),
    })
    return s


def _user_text(s: SimpleSession, index: int = -1) -> str:
    """取第 index 次模型调用的最后一条用户消息"""
    calls = cast(list[dict[str, Any]], getattr(s.llm, "calls"))
    messages = calls[index]["messages"]
    return str([m for m in messages if m.get("role") == "user"][-1]["content"])


def test_plugin_install_full_capabilities(session: SimpleSession):
    """安装: 11 工具 + 3 命令 + 2 技能 + 1 处理器 (search/memory 已移交 base_take)"""
    plugin = session.list_plugins()[0]
    assert plugin.name == "satrap_coding"
    assert len(plugin.tools) == 11
    assert set(plugin.commands) == {"goal", "plan", "approve"}
    assert set(plugin.skills) == {"goal", "plan"}
    assert set(plugin.handlers) == {"satrap_coding.inject"}

    assert len(session.list_tools()) == 11
    assert set(session.list_commands()) >= {"goal", "plan", "approve"}
    assert "goal" in session.list_skills()


def test_goal_command_lifecycle_and_injection(session: SimpleSession):
    """/goal: 设置 -> 注入模型输入 -> status -> done 后不再注入"""
    result, is_cmd = session.cmd_handler.process_message("/goal 实现一个编码助手")
    assert is_cmd and "目标已设置" in str(result)

    session.run("帮我看看这个文件")
    text = _user_text(session)
    assert "实现一个编码助手" in text
    assert "【持续目标】" in text

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
    """/plan: 进入后全部写类能力被拒 (文件/shell), 退出恢复"""
    write_tool = session.tools_manager.tools["write_file"]
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

    # 工作区内 shell 写命令在计划模式下同样被拒
    shell_tool = session.tools_manager.tools["shell"]
    out = shell_tool.execute("echo x > f2.txt")
    assert "计划模式" in out

    result, _ = session.cmd_handler.process_message("/plan off")
    assert "退出" in str(result)
    assert "已写入" in write_tool.execute(str(target), "again")


def test_uninstall_resets_plugin_state(session: SimpleSession):
    """M2: 卸载清理插件状态, 重装后 plan_mode 不残留"""
    session.cmd_handler.process_message("/plan on")

    assert session.uninstall_plugin("satrap_coding") is True
    session.install_plugin(str(PLUGIN_DIR))

    # 重装后计划模式已重置: 写文件需要批准 (不再是 plan 拒绝, 而是正常审批流)
    write_tool = session.tools_manager.tools["write_file"]
    target = tools_mod.WORKSPACE_ROOT / "y.txt"
    out = write_tool.execute(str(target), "hi")
    assert "需要用户批准" in out  # user 策略无通道 = 需要批准, 而非计划模式拒绝
    assert "拒绝: 计划模式" not in out


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


def test_goal_injection(session: SimpleSession):
    """目标注入: 设置目标后下一轮自动带上, 清除后不再注入"""
    session.cmd_handler.process_message("/goal 重构插件系统")
    session.run("第一轮")
    text = _user_text(session)
    assert "重构插件系统" in text

    result, _ = session.cmd_handler.process_message("/goal clear")
    assert "已清除" in str(result)

    session.run("第二轮")
    assert "重构插件系统" not in _user_text(session)


def test_uninstall_plugin_cleanup(session: SimpleSession):
    """卸载: 工具/命令/处理器全部回收"""
    assert session.uninstall_plugin("satrap_coding") is True
    assert session.list_tools() == []
    assert "goal" not in session.list_commands()
    assert "memory" not in session.list_commands()
    assert session.list_handlers() == []
    assert session.list_skills() == []
    assert session.list_plugins() == []


# ---------------- meta.yaml 能力声明 ----------------


def test_capability_descriptions_loaded(session: SimpleSession):
    """meta.yaml 能力声明读入 plugin.capability_descriptions (五类)"""
    plugin = session.list_plugins()[0]
    desc = plugin.capability_descriptions
    assert desc["tools"]["read_file"]
    assert desc["tools"]["shell"]
    assert desc["skills"]["goal"]
    assert desc["handlers"]["satrap_coding.inject"]
    assert desc["commands"]["approve"]
    # 未声明的类别为空或缺省
    assert desc.get("mcp", {}) == {}


def test_list_capabilities_with_description(session: SimpleSession):
    """list_capabilities 每项带 name / enabled / description"""
    plugin = session.list_plugins()[0]
    caps = plugin.list_capabilities()
    for kind in ("tools", "skills", "handlers", "commands"):
        assert caps[kind], f"{kind} 应非空"
        for item in caps[kind]:
            assert "name" in item and "enabled" in item and "description" in item
    tools = {t["name"]: t for t in caps["tools"]}
    assert tools["read_file"]["description"]
    assert tools["read_file"]["enabled"] is True


def test_parse_capability_descriptions_unit():
    """parse_capability_descriptions: 字典解析 / 非 dict 跳过 / 未识别键忽略"""
    from satrap.edictum.plugin import parse_capability_descriptions

    meta: dict[str, Any] = {
        "name": "p",
        "tools": {"a": "描述a", "b": 2},
        "skills": {"s": "技能s"},
        "handlers": "not-a-dict",   # 非 dict -> 跳过
        "unknown": {"x": "y"},       # 未识别键 -> 忽略
    }
    desc = parse_capability_descriptions(meta)
    assert desc["tools"] == {"a": "描述a", "b": "2"}   # 值统一 str()
    assert desc["skills"] == {"s": "技能s"}
    assert "handlers" not in desc
    assert "unknown" not in desc


def test_undeclared_capability_warns_only(tmp_path: Any, monkeypatch: Any, caplog: Any):
    """声明了但扫描不到的能力仅警告, 不改变安装行为"""
    import logging
    from satrap.edictum.simple_session import _warn_undeclared_capabilities

    descriptions = {"tools": {"ghost": "不存在的工具", "real": "真实工具"}}
    scanned = {"tools": {"real": True}}
    with caplog.at_level(logging.WARNING):
        _warn_undeclared_capabilities("p", descriptions, scanned)
    assert any("ghost" in r.message for r in caplog.records)
    assert not any("real" in r.message and "未在插件中发现" in r.message for r in caplog.records)
