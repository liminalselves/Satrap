"""独立 memory 插件测试: 工具, 命令, 注入与模式"""
from __future__ import annotations

from pathlib import Path
import pytest
from typing import Any, Callable, Iterator, Protocol, cast

from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import SimpleSession

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "memory"
CODING_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "satrap_coding"



class _FakeLLM(LLM):
    """最小同步 fake LLM"""

    def __init__(self) -> None:
        pass

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content="回复")

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        yield LLMCallStreamEvent(kind="content_delta", delta="流")


@pytest.fixture
def session(tmp_path: Any, monkeypatch: Any) -> SimpleSession:
    """
    隔离数据目录的会话 + 安装 memory 插件

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具

    返回:
    - SimpleSession: 隔离数据目录的会话 + 安装 memory 插件
    """
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    from satrap.expend.plugins.memory import state as state_mod
    from satrap.core.memory import store as ms_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))
    # 隔离: 插件配置目录 + 记忆 db + 沙箱根
    monkeypatch.setattr(ms_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    monkeypatch.setattr(state_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")

    s = SimpleSession("conv-1", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(PLUGIN_DIR))
    return s


def test_memory_mode_config_readonly(tmp_path: Any, monkeypatch: Any):
    """
    memory_mode=base: 写工具被拒绝; disabled: 注入块为空

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    from satrap.expend.plugins.memory import state as state_mod
    from satrap.core.memory import store as ms_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))
    monkeypatch.setattr(ms_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    monkeypatch.setattr(state_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")

    s = SimpleSession("conv-mode", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(PLUGIN_DIR), config={
        "memory_mode": "base",
    })

    from satrap.expend.plugins.memory.state import get_plugin_state
    store = get_plugin_state(s)["store"]
    assert store.mode == "base"
    assert store.can_write() is False

    tm = s._wf.tools_manager
    # base 只读: 写工具返回扁平失败与"只读"提示
    add = tm.tools["add_memory"]
    readonly = add.execute(title="偏好", content="x")
    assert readonly["ok"] is False and readonly["error_type"] == "read_only" and "只读" in readonly["error"]

    store.set_mode("disabled")
    # disabled 模式: 不注入 (注入块为空), 写拒绝文案按实际模式生成
    assert store.to_context_block() == ""
    offline = add.execute(title="偏好", content="x")
    assert offline["ok"] is False and offline["error_type"] == "memory_disabled"
    assert "已禁用" in offline["error"] and "disabled" in offline["error"]


def test_memory_tools_lifecycle(session: SimpleSession):
    """
    memory 工具: 添加 -> 列表 -> 更新 -> 删除

    参数:
    - session: 会话
    """
    tm = session._wf.tools_manager
    add = tm.tools["add_memory"]
    result = add.execute(title="偏好", content="喜欢简洁回答", tags=["style"], importance=3)
    assert "已添加" in result

    list_tool = tm.tools["list_memories"]
    listed = list_tool.execute()
    assert "偏好" in listed and "喜欢简洁回答" in listed

    from satrap.expend.plugins.memory.state import get_plugin_state
    # 取记忆 ID 前缀
    store = get_plugin_state(session)["store"]
    mem_id = store.list_all()[0]["id"]

    update = tm.tools["update_memory"]
    assert "已更新" in update.execute(memory_id=mem_id, content="喜欢极简回答")

    delete = tm.tools["delete_memory"]
    assert "已删除" in delete.execute(memory_id=mem_id)
    assert "没有" in list_tool.execute()


def test_memory_command_lifecycle(session: SimpleSession):
    """
    /memory 命令: 注册 -> list/add/del/mode 全流程

    参数:
    - session: 会话
    """
    cmd = session.cmd_handler.commands["memory"]

    assert "没有长期记忆" in cmd()
    assert "已添加" in cmd("add", "偏好", "喜欢简洁回答")
    listed = cmd()
    assert "偏好" in listed and "喜欢简洁回答" in listed

    mem_id = listed.split("- ")[1].split(" ")[0]
    assert "已删除" in cmd("del", mem_id[:8])   # 支持 ID 前缀

    assert "已切换: base" in cmd("mode", "base")
    assert "只读模式" in cmd("add", "x", "y")
    assert "只读模式" in cmd("clear")
    assert "已切换: full" in cmd("mode", "full")
    assert "用法" in cmd("unknown")


def test_memory_inject_handler(session: SimpleSession):
    """
    记忆注入 handler: 添加记忆后注入到用户输入

    参数:
    - session: 会话
    """
    from satrap.expend.plugins.memory.state import get_plugin_state
    state = get_plugin_state(session)
    store = state["store"]
    store.add("项目约定", "回复用中文", importance=5)

    session.run("你好")
    injected = next(message["content"] for message in reversed(session.ctx.get_context()) if message["role"] == "user")
    assert "长期记忆" in injected
    assert "项目约定" in injected
    assert "回复用中文" in injected

    store.add("新增约定", "回答附上示例", importance=5)
    session.run("再问一次")
    updated = next(message["content"] for message in reversed(session.ctx.get_context()) if message["role"] == "user")
    assert "回答附上示例" in updated

    store.clear()
    session.run("清空后")
    cleared = next(message["content"] for message in reversed(session.ctx.get_context()) if message["role"] == "user")
    assert cleared == "清空后"


def test_memory_writes_not_blocked_by_plan_mode(tmp_path: Any, monkeypatch: Any):
    """
    计划模式限制工作区文件写入和 Shell; 记忆是元信息, 增删改不受拦截

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    from satrap.expend.plugins.memory import state as state_mod
    from satrap.core.memory import store as ms_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))
    monkeypatch.setattr(ms_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    monkeypatch.setattr(state_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    (tmp_path / "workspace").mkdir()

    s = SimpleSession("conv-plan", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(CODING_PLUGIN_DIR), config={
        "workspace_root": str(tmp_path / "workspace"),
        "data_root": str(tmp_path / "coding"),
    })
    s.install_plugin(str(PLUGIN_DIR))

    assert "已进入计划模式" in s.cmd_handler.commands["plan"]("on")

    add = s._wf.tools_manager.tools["add_memory"]
    # 计划模式下记忆写不被拦截: 工具与命令均可写
    assert "已添加" in add.execute(title="决策", content="用 SQLite", tags=[], importance=1)
    assert "已添加" in s.cmd_handler.commands["memory"]("add", "偏好", "中文回复")


def test_memory_tool_failures_use_flat_envelope(session: SimpleSession):
    """
    记忆工具失败统一为框架扁平结果, 宿主失败补上工具名

    参数:
    - session: 会话
    """
    tm = session._wf.tools_manager
    missing = tm.tools["get_memory"].execute(memory_id="不存在的记忆")
    assert missing == {"ok": False, "error": "记忆不存在: 不存在的记忆", "error_type": "not_found", "tool_name": "get_memory"}
    blank = tm.tools["add_memory"].execute(title="  ", content="  ")
    assert blank["ok"] is False and blank["error_type"] == "invalid_argument" and blank["tool_name"] == "add_memory"
    # 群聊宿主结果只补工具名与统一键名, 成功结果与文本原样返回
    from satrap.expend.plugins.memory.tools.sync import ListMemoriesTool
    host = cast(ListMemoriesTool, tm.tools["list_memories"])
    assert host._visible({"ok": False, "error": "记忆操作超时, 已取消等待", "error_type": "unavailable"}) == {
        "ok": False, "error": "记忆操作超时, 已取消等待", "error_type": "unavailable", "tool_name": "list_memories"}
    assert host._visible({"ok": True, "items": []}) == {"ok": True, "items": []}
    assert host._visible("当前没有长期记忆") == "当前没有长期记忆"
