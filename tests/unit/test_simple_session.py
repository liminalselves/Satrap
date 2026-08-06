"""edictum SimpleSession / AsyncSimpleSession 单元测试

覆盖:
- run 基本流程 (React 范式, 用户消息只落一次库) + 多模态 img_urls
- 工具: add/remove/enable/disable/list
- 命令: add/remove/enable/disable/list + /help 行为
- skill: add/remove/enable/disable/list (临时技能目录)
- 插件: 优先级排序 / 链式改写 / 透传 / 启停 / 优先级调整 / 删除
- checkpoint: 启用可用, 未启用抛 ValueError
- 模型: set_llm / set_model_parameters / set_stream_mode
- 异步版: run / MCP 接入与移除 / 异步插件回调
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterator, cast

import pytest

from satrap.edictum import AsyncSimpleSession, SessionPlugin, SimpleSession
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.utils.skills import SkillsManager


class _EchoTool(Tool):
    """同步测试工具"""

    tool_name = "echo"
    description = "回显"
    params_dict: dict[str, tuple[str, str]] = {}


class _AsyncEchoTool(AsyncTool):
    """异步测试工具"""

    tool_name = "async_echo"
    description = "异步回显"
    params_dict: dict[str, tuple[str, str]] = {}

    async def execute(self, text: str = "") -> str:
        return text or "ok"


class _FakeLLM(LLM):
    """记录调用参数的同步 fake LLM"""

    def __init__(self, response: LLMCallResponse | None = None):
        self.calls: list[dict[str, Any]] = []
        self.params: dict[str, Any] = {}
        self.response = response or LLMCallResponse(type="answer", content="回复")

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        self.calls.append({
            "messages": messages, "tools": tools, "img_urls": img_urls, "thinking": thinking,
        })
        return self.response

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        self.calls.append({
            "messages": messages, "tools": tools, "img_urls": img_urls,
            "thinking": thinking, "stream": True,
        })
        yield LLMCallStreamEvent(kind="content_delta", delta="流")
        yield LLMCallStreamEvent(kind="content_delta", delta="式")
        yield LLMCallStreamEvent(
            kind="done", response=LLMCallResponse(type="answer", content="流式回复"),
        )

    def set_parameters(self, **kwargs: Any) -> None:
        self.params.update(kwargs)


class _FakeAsyncLLM(AsyncLLM):
    """记录调用参数的异步 fake LLM"""

    def __init__(self, response: LLMCallResponse | None = None):
        self.calls: list[dict[str, Any]] = []
        self.params: dict[str, Any] = {}
        self.response = response or LLMCallResponse(type="answer", content="异步回复")

    async def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        self.calls.append({
            "messages": messages, "tools": tools, "img_urls": img_urls, "thinking": thinking,
        })
        return self.response

    async def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Any:
        self.calls.append({
            "messages": messages, "tools": tools, "img_urls": img_urls,
            "thinking": thinking, "stream": True,
        })
        yield LLMCallStreamEvent(kind="content_delta", delta="异")
        yield LLMCallStreamEvent(
            kind="done", response=LLMCallResponse(type="answer", content="异步流式"),
        )

    def set_parameters(self, **kwargs: Any) -> None:
        self.params.update(kwargs)


class _FakeMCPClient:
    """fake MCPClient: register_tools 返回适配器, close 记录"""

    def __init__(self, adapters: list[Any] | None = None):
        self.adapters = adapters or [_AsyncEchoTool()]
        self.registered_to: list[Any] = []
        self.closed = False

    async def register_tools(self, tools_manager: Any, name_prefix: str | None = None):
        self.registered_to.append(tools_manager)
        for adapter in self.adapters:
            tools_manager.register_tool(adapter)
        return list(self.adapters)

    async def close(self):
        self.closed = True


def _make_session(tmp_path: Path, llm: LLM | None = None) -> SimpleSession:
    return SimpleSession(
        "conv-1", llm or _FakeLLM(),
        db_path=str(tmp_path / "chat.db"),
        enable_checkpoint=True,
    )


def _write_demo_skill(tmp_path: Path) -> Path:
    """创建最小演示技能目录, 返回目录路径"""
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(
        "---\nname: demo\n---\n\n# Demo\n演示技能说明\n", encoding="utf-8",
    )
    return skill_dir


# ================= 基本流程 =================


def test_run_basic_flow(tmp_path: Path):
    """run 返回模型回复, 用户消息只落一次库 (React 范式)"""
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    result = session.run("你好")
    assert result == "回复"

    assert len(llm.calls) == 1
    call = llm.calls[0]
    user_msgs = [m for m in call["messages"] if m.get("role") == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "你好"
    assert call["tools"] == []


def test_call_delegates_to_run(tmp_path: Path):
    """__call__ 委托 run"""
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    assert session("你好") == "回复"


def test_run_multimodal_img_urls(tmp_path: Path):
    """多模态: img_urls 透传到 llm.call"""
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    session.run("看图", img_urls=["http://x/a.png"])
    assert llm.calls[0]["img_urls"] == ["http://x/a.png"]


# ================= 工具管理 =================


def test_add_remove_enable_tools(tmp_path: Path):
    """工具: 注入后进入 llm.call 的 tools, 启停/删除/列表生效"""
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    tool = _EchoTool()

    session.add_tool(tool)
    assert session.list_tools() == ["echo"]
    assert session.is_tool_enabled("echo") is True

    session.run("hi")
    tools_def = llm.calls[0]["tools"]
    assert tools_def and tools_def[0]["function"]["name"] == "echo"

    session.disable_tool("echo")
    assert session.is_tool_enabled("echo") is False
    session.enable_tool("echo")
    assert session.is_tool_enabled("echo") is True

    assert session.remove_tool("echo") is True
    assert session.list_tools() == []
    assert session.remove_tool("echo") is False


def test_add_tools_batch(tmp_path: Path):
    """批量注入工具"""
    session = _make_session(tmp_path)
    session.add_tools(_EchoTool(), _EchoTool())
    assert session.list_tools() == ["echo"]


# ================= 命令管理 =================


def test_command_lifecycle(tmp_path: Path):
    """命令: 添加/列表/执行/停用/删除"""
    session = _make_session(tmp_path)

    def ping(args: str) -> str:
        return f"pong:{args}"

    session.add_command("ping", ping, intro="测试命令")
    assert "ping" in session.list_commands()
    assert session.is_command_enabled("ping") is True

    result, is_cmd = session.cmd_handler.process_message("/ping hello")
    assert is_cmd is True
    assert result == "pong:hello"

    session.disable_command("ping")
    assert session.is_command_enabled("ping") is False
    result, is_cmd = session.cmd_handler.process_message("/ping hello")
    assert is_cmd is False

    session.enable_command("ping")
    assert session.is_command_enabled("ping") is True

    assert session.remove_command("ping") is True
    assert "ping" not in session.list_commands()
    assert session.remove_command("ping") is False


def test_default_help_command(tmp_path: Path):
    """默认 /help 存在且在列表中"""
    session = _make_session(tmp_path)
    assert "help" in session.list_commands()
    result, is_cmd = session.cmd_handler.process_message("/help")
    assert is_cmd is True
    assert "help" in str(result)


# ================= skill 管理 =================


def test_skill_lifecycle(tmp_path: Path):
    """skill: 激活后系统提示变化, 停用/删除/列表生效"""
    skill_dir = _write_demo_skill(tmp_path)
    mgr = SkillsManager(skills_dir=str(skill_dir.parent), include_preset=False)
    mgr.scan(str(skill_dir.parent))

    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    assert session.add_skill("demo", skills_manager=mgr) is True
    assert "demo" in session.list_skills()
    session.run("hi")
    system_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "system"]
    assert system_msgs and "Demo" in str(system_msgs[0]["content"])

    assert session.disable_skill("demo") is True
    assert session.enable_skill("demo") is True
    assert session.remove_skill("demo") is True
    assert "demo" not in session.list_skills()
    assert session.remove_skill("demo") is False


def test_skill_missing_returns_false(tmp_path: Path):
    """不存在的技能激活返回 False"""
    session = _make_session(tmp_path)
    assert session.add_skill("not-exist") is False


# ================= 插件管理 =================


def test_plugins_execute_in_priority_order(tmp_path: Path):
    """插件按优先级升序执行, before_user_send 链式改写"""
    order: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def low(text: str) -> str | None:
        order.append("low")
        return text + "b"

    def high(text: str) -> str | None:
        order.append("high")
        return text + "a"

    session.add_plugin(SessionPlugin(name="low", priority=200, before_user_send=low))
    session.add_plugin(SessionPlugin(name="high", priority=100, before_user_send=high))

    session.run("x")
    assert order == ["high", "low"]
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[0]["content"] == "xab"


def test_plugin_none_passthrough(tmp_path: Path):
    """before_user_send 返回 None 不改写"""
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.add_plugin(SessionPlugin(name="p", before_user_send=lambda t: None))

    session.run("原样")
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[0]["content"] == "原样"


def test_plugin_after_callbacks_receive_values(tmp_path: Path):
    """after 系列收到改写后输入与最终回复"""
    seen: dict[str, Any] = {}
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def after_send(text: str) -> None:
        seen["user"] = text

    def before_reply() -> None:
        seen["before"] = True

    def after_reply(text: str) -> None:
        seen["reply"] = text

    session.add_plugin(SessionPlugin(
        name="p",
        before_user_send=lambda t: t + "!",
        after_user_send=after_send,
        before_model_reply=before_reply,
        after_model_reply=after_reply,
    ))

    result = session.run("hi")
    assert seen == {"user": "hi!", "before": True, "reply": "回复"}
    assert result == "回复"


def test_plugin_enable_disable_and_remove(tmp_path: Path):
    """插件: 停用不执行, 启用恢复, 删除移除"""
    calls: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def fn(text: str) -> str | None:
        calls.append("called")
        return None

    session.add_plugin(SessionPlugin(name="p", before_user_send=fn))
    session.run("a")
    assert calls == ["called"]

    assert session.disable_plugin("p") is True
    session.run("b")
    assert calls == ["called"]

    assert session.enable_plugin("p") is True
    session.run("c")
    assert calls == ["called", "called"]

    assert session.remove_plugin("p") is True
    session.run("d")
    assert calls == ["called", "called"]
    assert session.remove_plugin("p") is False
    assert session.disable_plugin("missing") is False


def test_plugin_priority_adjust(tmp_path: Path):
    """set_plugin_priority 调整执行顺序"""
    order: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    session.add_plugin(SessionPlugin(
        name="a", priority=100, before_user_send=lambda t: order.append("a") or None,
    ))
    session.add_plugin(SessionPlugin(
        name="b", priority=200, before_user_send=lambda t: order.append("b") or None,
    ))

    session.run("x")
    assert order == ["a", "b"]

    order.clear()
    assert session.set_plugin_priority("b", 50) is True
    session.run("x")
    assert order == ["b", "a"]
    assert session.set_plugin_priority("missing", 1) is False


def test_plugin_duplicate_name_overwrites(tmp_path: Path):
    """同名插件覆盖"""
    calls: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def first(t: str) -> str | None:
        calls.append("first")
        return None

    def second(t: str) -> str | None:
        calls.append("second")
        return None

    session.add_plugin(SessionPlugin(name="p", before_user_send=first))
    session.add_plugin(SessionPlugin(name="p", before_user_send=second))
    session.run("x")
    assert calls == ["second"]
    assert len(session.list_plugins()) == 1


def test_plugin_empty_name_raises(tmp_path: Path):
    """空插件名抛 ValueError"""
    session = _make_session(tmp_path)
    with pytest.raises(ValueError):
        session.add_plugin(SessionPlugin(name=""))


# ================= checkpoint =================


def test_checkpoint_available_when_enabled(tmp_path: Path):
    """启用检查点后 create/list 可用"""
    session = _make_session(tmp_path)
    session.run("你好")

    batch_id = session.create_checkpoint(name="t1")
    assert batch_id
    checkpoints = session.list_checkpoints()
    assert len(checkpoints) >= 1


def test_checkpoint_raises_when_disabled(tmp_path: Path):
    """未启用检查点时抛 ValueError"""
    session = SimpleSession(
        "conv-2", _FakeLLM(), db_path=str(tmp_path / "chat.db"),
        enable_checkpoint=False,
    )
    with pytest.raises(ValueError):
        session.create_checkpoint()


# ================= 模型与流式 =================


def test_set_llm_and_parameters(tmp_path: Path):
    """set_llm 替换模型, set_model_parameters 透传"""
    llm1 = _FakeLLM()
    session = _make_session(tmp_path, llm1)
    assert session.llm is llm1

    llm2 = _FakeLLM()
    session.set_llm(llm2)
    assert session.llm is llm2

    session.set_model_parameters(temperature=0.5)
    assert llm2.params == {"temperature": 0.5}


def test_stream_mode_switches_to_stream_call(tmp_path: Path):
    """流式模式走 stream_call 并返回完整文本"""
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    assert session.stream is False

    session.set_stream_mode(True)
    assert session.stream is True
    result = session.run("hi")
    assert result == "流式回复"
    assert llm.calls[0].get("stream") is True


# ================= 异步版 =================


@pytest.mark.asyncio
async def test_async_run_basic(tmp_path: Path):
    """异步 run 基本流程 (自动初始化)"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    result = await session.run("你好")
    assert result == "异步回复"
    assert len(llm.calls) == 1
    assert session.llm is llm
    assert session.list_tools() == []


@pytest.mark.asyncio
async def test_async_add_tool_before_initialize(tmp_path: Path):
    """初始化前注入的工具在 initialize 时生效"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_tool(_AsyncEchoTool())
    await session.run("hi")
    assert session.list_tools() == ["async_echo"]
    tools_def = llm.calls[0]["tools"]
    assert tools_def and tools_def[0]["function"]["name"] == "async_echo"


@pytest.mark.asyncio
async def test_async_mcp_lifecycle(tmp_path: Path):
    """MCP: 接入后工具进 manager, 移除后注销并断开"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    client = _FakeMCPClient()

    adapters = await session.add_mcp("fs", client)
    assert len(adapters) == 1
    assert session.list_mcp() == ["fs"]
    assert session.list_tools() == ["async_echo"]

    await session.run("hi")
    assert llm.calls[0]["tools"][0]["function"]["name"] == "async_echo"

    # 启停
    assert session.disable_mcp("fs") is True
    assert session.is_tool_enabled("async_echo") is False
    assert session.enable_mcp("fs") is True
    assert session.is_tool_enabled("async_echo") is True

    # 移除
    assert await session.remove_mcp("fs") is True
    assert client.closed is True
    assert session.list_mcp() == []
    assert session.list_tools() == []
    assert await session.remove_mcp("fs") is False


@pytest.mark.asyncio
async def test_async_plugin_async_callbacks(tmp_path: Path):
    """异步插件回调 (async 函数) 生效"""
    seen: list[str] = []
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    async def before(text: str) -> str | None:
        seen.append("before")
        return text + "!"

    async def after(text: str) -> None:
        seen.append(f"after:{text}")

    session.add_plugin(SessionPlugin(
        name="p", before_user_send=before, after_model_reply=after,
    ))

    result = await session.run("hi")
    assert result == "异步回复"
    assert seen == ["before", "after:异步回复"]
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[0]["content"] == "hi!"


@pytest.mark.asyncio
async def test_async_missing_wf_raises(tmp_path: Path):
    """未初始化时访问工具管理器抛 RuntimeError"""
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"),
    )
    with pytest.raises(RuntimeError):
        session.list_tools()


# ================= 补充分支覆盖 =================


def test_sync_constructor_tools_and_proxies(tmp_path: Path):
    """构造传初始工具 + 属性代理 (llm setter / ctx / tools_manager)"""
    llm = _FakeLLM()
    session = SimpleSession(
        "conv-3", llm, tools=[_EchoTool()], db_path=str(tmp_path / "chat.db"),
    )
    assert session.list_tools() == ["echo"]

    llm2 = _FakeLLM()
    session.llm = llm2
    assert session.llm is llm2
    assert session.ctx is not None
    assert session.tools_manager is session._wf.tools_manager


def test_sync_plugin_missing_before_user_send(tmp_path: Path):
    """插件只有 after_user_send 时 before_user_send 分支跳过"""
    seen: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.add_plugin(SessionPlugin(
        name="p", after_user_send=lambda t: seen.append(t),
    ))
    session.run("hi")
    assert seen == ["hi"]


def test_sync_skill_manager_missing_branches(tmp_path: Path):
    """无技能管理器时 remove/disable/list 的兑底分支"""
    session = _make_session(tmp_path)
    assert session.remove_skill("demo") is False
    assert session.disable_skill("demo") is False
    assert session.list_skills() == []


def test_sync_plugin_missing_enable(tmp_path: Path):
    """enable_plugin 不存在的插件返回 False"""
    session = _make_session(tmp_path)
    assert session.enable_plugin("missing") is False


def test_sync_reload_llm(tmp_path: Path):
    """reload_llm 委托 set_llm"""
    llm1 = _FakeLLM()
    session = _make_session(tmp_path, llm1)
    llm2 = _FakeLLM()
    session.reload_llm(llm2)
    assert session.llm is llm2


@pytest.mark.asyncio
async def test_async_proxies_and_model_interfaces(tmp_path: Path):
    """异步属性代理与模型接口 (__call__ / ctx / tools_manager / set_llm 等)"""
    llm1 = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm1, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    assert await session("hi") == "异步回复"
    assert session.ctx is not None
    assert session.tools_manager is session._require_wf().tools_manager

    llm2 = _FakeAsyncLLM()
    session.llm = llm2
    assert session.llm is llm2
    session.reload_llm(llm1)
    assert session.llm is llm1
    session.set_model_parameters(temperature=0.5)
    assert llm1.params == {"temperature": 0.5}
    session.set_stream_mode(False)
    assert session.stream is False


@pytest.mark.asyncio
async def test_async_stream_mode(tmp_path: Path):
    """异步流式切换走 stream_full_agent"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.set_stream_mode(True)
    result = await session.run("hi")
    assert result == "异步流式"
    assert llm.calls[0].get("stream") is True


@pytest.mark.asyncio
async def test_async_plugin_sync_callbacks_and_continue(tmp_path: Path):
    """异步版同步回调 + 缺省处理点的 continue 分支"""
    seen: dict[str, Any] = {}
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    def after_send(text: str) -> None:
        seen["user"] = text

    def before_send(text: str) -> str | None:
        seen["before"] = True
        return text + "!"

    def before_reply() -> None:
        seen["before_reply"] = True

    session.add_plugin(SessionPlugin(name="a", after_user_send=after_send))
    session.add_plugin(SessionPlugin(
        name="b", before_user_send=before_send, before_model_reply=before_reply,
    ))

    result = await session.run("hi")
    assert seen == {"before": True, "before_reply": True, "user": "hi!"}
    assert result == "异步回复"


@pytest.mark.asyncio
async def test_async_command_lifecycle(tmp_path: Path):
    """异步命令: 添加/列表/执行/停用/删除"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    async def ping(args: str) -> str:
        return f"pong:{args}"

    session.add_command("ping", ping, intro="测试")
    assert "ping" in session.list_commands()
    assert session.is_command_enabled("ping") is True

    result, is_cmd = await session.command_handler.process_message("/ping hello")
    assert is_cmd is True
    assert result == "pong:hello"

    session.disable_command("ping")
    assert session.is_command_enabled("ping") is False
    session.enable_command("ping")
    assert session.is_command_enabled("ping") is True
    assert session.remove_command("ping") is True
    assert session.remove_command("ping") is False


@pytest.mark.asyncio
async def test_async_tools_after_initialize(tmp_path: Path):
    """初始化后注入/批量/启停/删除工具"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    await session.run("hi")

    session.add_tool(_AsyncEchoTool())
    assert session.list_tools() == ["async_echo"]
    session.add_tools(_AsyncEchoTool())
    assert session.list_tools() == ["async_echo"]

    assert session.disable_tool("async_echo") is True
    assert session.is_tool_enabled("async_echo") is False
    assert session.enable_tool("async_echo") is True
    assert session.is_tool_enabled("async_echo") is True
    assert session.remove_tool("async_echo") is True
    assert session.remove_tool("async_echo") is False


@pytest.mark.asyncio
async def test_async_skill_lifecycle(tmp_path: Path):
    """异步 skill: 初始化前注册延迟生效, 惰性创建, 启停/删除"""
    skill_dir = _write_demo_skill(tmp_path)
    mgr = SkillsManager(skills_dir=str(skill_dir.parent), include_preset=False)
    mgr.scan(str(skill_dir.parent))

    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    assert await session.disable_skill("demo") is False
    assert session.list_skills() == []

    assert await session.add_skill("demo", skills_manager=mgr) is True
    assert "demo" in session.list_skills()
    await session.run("hi")
    system_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "system"]
    assert system_msgs and "Demo" in str(system_msgs[0]["content"])

    assert await session.enable_skill("demo") is True
    assert await session.disable_skill("demo") is True
    assert await session.remove_skill("demo") is True
    assert "demo" not in session.list_skills()

    # 无管理器兑底 + 惰性创建
    session2 = AsyncSimpleSession(
        "conv-b", llm, db_path=str(tmp_path / "chat2.db"), enable_checkpoint=True,
    )
    assert await session2.remove_skill("demo") is False
    await session2.run("hi")
    assert await session2.add_skill("not-exist") is False


@pytest.mark.asyncio
async def test_async_mcp_missing_branches(tmp_path: Path):
    """MCP 不存在的连接启停返回 False"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    await session.run("hi")
    assert session.enable_mcp("missing") is False
    assert session.disable_mcp("missing") is False


@pytest.mark.asyncio
async def test_async_plugin_management(tmp_path: Path):
    """异步插件管理全套 + 空名抛错"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    with pytest.raises(ValueError):
        session.add_plugin(SessionPlugin(name=""))

    session.add_plugin(SessionPlugin(name="p", priority=100))
    assert len(session.list_plugins()) == 1
    assert session.set_plugin_priority("p", 50) is True
    assert session.set_plugin_priority("missing", 1) is False

    assert session.disable_plugin("p") is True
    assert session.enable_plugin("p") is True
    assert session.disable_plugin("missing") is False
    assert session.enable_plugin("missing") is False
    assert session.remove_plugin("p") is True
    assert session.remove_plugin("p") is False


# ================= 审查问题修复回归测试 =================


class _CalcTool(Tool):
    """带 execute 实现的同步工具 (工具循环测试用)"""

    tool_name = "calc"
    description = "计算"
    params_dict = {"expression": ("string", "算式")}

    def execute(self, expression: str) -> str:
        return "5"


class _ToolLoopLLM(_FakeLLM):
    """第一次返回 tools_call, 之后返回 answer, 记录每轮 img_urls"""

    def __init__(self) -> None:
        super().__init__()
        self._count = 0

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        self.calls.append({
            "messages": messages, "tools": tools, "img_urls": img_urls, "thinking": thinking,
        })
        self._count += 1
        if self._count == 1:
            return LLMCallResponse(
                type="tools_call", content="",
                tool_calls=[{"name": "calc", "id": "call_1", "arguments": {"expression": "2+3"}}],
            )
        return LLMCallResponse(type="answer", content="最终回复")


def test_default_db_path_constructs_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """H1 回归: 默认 db_path (正斜杠) 构造不再抛 ValueError"""
    monkeypatch.chdir(tmp_path)
    session = SimpleSession("conv-default", _FakeLLM())
    assert session.run("hi") == "回复"


@pytest.mark.asyncio
async def test_async_default_db_path_initializes_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """H1 回归: 异步版默认 db_path 初始化不再抛 ValueError"""
    monkeypatch.chdir(tmp_path)
    session = AsyncSimpleSession("conv-default", _FakeAsyncLLM())
    assert await session.run("hi") == "异步回复"


def test_plugin_empty_string_rewrite(tmp_path: Path):
    """H2 修复: before_user_send 返回空串 = 拦截/清空输入"""
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.add_plugin(SessionPlugin(name="p", before_user_send=lambda t: ""))
    session.run("你好")
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == ""


@pytest.mark.asyncio
async def test_async_plugin_empty_string_rewrite(tmp_path: Path):
    """H2 修复: 异步版空串改写生效"""
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_plugin(SessionPlugin(name="p", before_user_send=lambda t: ""))
    await session.run("hi")
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == ""


def test_plugin_non_str_raises_sync(tmp_path: Path):
    """H2 修复: 同步版非 str truthy 返回值抛错"""
    def bad(t: str) -> bool:
        return False

    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.add_plugin(SessionPlugin(name="p", before_user_send=cast(Any, bad)))
    with pytest.raises(AssertionError):
        session.run("hi")


@pytest.mark.asyncio
async def test_plugin_non_str_raises_async(tmp_path: Path):
    """H2 修复: 异步版非 str truthy 返回值抛 TypeError"""
    def bad(t: str) -> dict[str, int]:
        return {"bad": 1}

    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_plugin(SessionPlugin(name="p", before_user_send=cast(Any, bad)))
    with pytest.raises(TypeError):
        await session.run("hi")


def test_thinking_requires_stream_mode_sync(tmp_path: Path):
    """M3 修复: 非流式 thinking=True 抛 NotImplementedError"""
    session = _make_session(tmp_path)
    with pytest.raises(NotImplementedError):
        session.run("hi", thinking=True)


@pytest.mark.asyncio
async def test_thinking_requires_stream_mode_async(tmp_path: Path):
    """M3 修复: 异步非流式 thinking=True 抛 NotImplementedError"""
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    with pytest.raises(NotImplementedError):
        await session.run("hi", thinking=True)


@pytest.mark.asyncio
async def test_async_model_setters_before_initialize(tmp_path: Path):
    """M4 修复: 未初始化时 set_llm / set_model_parameters 延迟生效"""
    llm2 = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.set_llm(llm2)
    session.set_model_parameters(temperature=0.5)
    await session.run("hi")
    assert session.llm is llm2
    assert llm2.params == {"temperature": 0.5}


@pytest.mark.asyncio
async def test_async_add_skill_missing_before_initialize(tmp_path: Path):
    """M2 修复: 初始化前 add_skill 不存在的技能返回 False"""
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    assert await session.add_skill("not-exist") is False


@pytest.mark.asyncio
async def test_async_concurrent_init_single_workflow(tmp_path: Path):
    """H3 修复: 并发初始化只构建一个工作流, 无孤儿上下文"""
    class _Client:
        def __init__(self, name: str) -> None:
            self.name = name

        async def register_tools(self, tm: Any, name_prefix: str | None = None) -> list[Any]:
            await asyncio.sleep(0.05)
            return []

        async def close(self) -> None:
            pass

    session = AsyncSimpleSession(
        "race", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    await asyncio.gather(
        session.add_mcp("m1", _Client("a")),
        session.add_mcp("m2", _Client("b")),
    )
    assert len(session._workflow_contexts) == 1
    assert set(session.list_mcp()) == {"m1", "m2"}


def test_img_urls_passed_in_tool_loop(tmp_path: Path):
    """L1 修复: 工具循环内每轮 llm.call 都透传 img_urls"""
    llm = _ToolLoopLLM()
    session = _make_session(tmp_path, llm)
    session.add_tool(_CalcTool())
    result = session.run("算 2+3", img_urls=["http://x/a.png"])
    assert result == "最终回复"
    assert len(llm.calls) == 2
    assert llm.calls[0]["img_urls"] == ["http://x/a.png"]
    assert llm.calls[1]["img_urls"] == ["http://x/a.png"]
