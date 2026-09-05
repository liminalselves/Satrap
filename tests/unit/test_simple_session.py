"""
edictum SimpleSession / AsyncSimpleSession 单元测试

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

import importlib
import threading
import asyncio
import logging
from pathlib import Path
import pytest
from typing import Any, Iterator, Protocol, cast
import time

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.utils.skills import SkillsManager
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import (
    AsyncSimpleSession,
    HandlerAbortError,
    HandlerConfig,
    HandlerContext,
    HandlerResult,
    SessionHandler,
    SimpleSession,
)


class _SessionAwareTool(Protocol):
    """插件工厂动态挂载 session_id 的工具结构"""

    session_id: str


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
        thinking: str = "off", temperature: float | None = None,
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
        thinking: str = "off", temperature: float | None = None,
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
        thinking: str = "off", temperature: float | None = None,
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
        thinking: str = "off", temperature: float | None = None,
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
    """
    创建最小演示技能目录, 返回目录路径

    参数:
    - tmp_path: tmp路径

    返回:
    - Path: 目录路径
    """
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(
        "---\nname: demo\n---\n\n# Demo\n演示技能说明\n", encoding="utf-8",
    )
    return skill_dir


# ================= 基本流程 =================


def test_run_basic_flow(tmp_path: Path):
    """
    run 返回模型回复, 用户消息只落一次库 (React 范式)

    参数:
    - tmp_path: tmp路径
    """
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
    """
    __call__ 委托 run

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    assert session("你好") == "回复"


def test_run_multimodal_img_urls(tmp_path: Path):
    """
    多模态: img_urls 透传到 llm.call

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    session.run("看图", img_urls=["http://x/a.png"])
    assert llm.calls[0]["img_urls"] == ["http://x/a.png"]


# ================= 工具管理 =================


def test_add_remove_enable_tools(tmp_path: Path):
    """
    工具: 注入后进入 llm.call 的 tools, 启停/删除/列表生效

    参数:
    - tmp_path: tmp路径
    """
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
    """
    批量注入工具

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    session.add_tools(_EchoTool(), _EchoTool())
    assert session.list_tools() == ["echo"]


# ================= 命令管理 =================


def test_command_lifecycle(tmp_path: Path):
    """
    命令: 添加/列表/执行/停用/删除

    参数:
    - tmp_path: tmp路径
    """
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
    """
    默认 /help 存在且在列表中

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    assert "help" in session.list_commands()
    result, is_cmd = session.cmd_handler.process_message("/help")
    assert is_cmd is True
    assert "help" in str(result)


# ================= skill 管理 =================


def test_skill_lifecycle(tmp_path: Path):
    """
    skill: 激活后系统提示变化, 停用/删除/列表生效

    参数:
    - tmp_path: tmp路径
    """
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
    """
    不存在的技能激活返回 False

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    assert session.add_skill("not-exist") is False


# ================= 插件管理 =================


def test_plugins_execute_in_priority_order(tmp_path: Path):
    """
    插件按优先级升序执行, before_user_send 链式改写

    参数:
    - tmp_path: tmp路径
    """
    order: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def low(text: str, ctx: HandlerContext) -> str | None:
        order.append("low")
        return text + "b"

    def high(text: str, ctx: HandlerContext) -> str | None:
        order.append("high")
        return text + "a"

    session.add_handler(SessionHandler(name="low", priority=200, before_user_send=low))
    session.add_handler(SessionHandler(name="high", priority=100, before_user_send=high))

    session.run("x")
    assert order == ["high", "low"]
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[0]["content"] == "xab"


def test_plugin_none_passthrough(tmp_path: Path):
    """
    before_user_send 返回 None 不改写

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.add_handler(SessionHandler(name="p", before_user_send=lambda t, ctx: None))

    session.run("原样")
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[0]["content"] == "原样"


def test_plugin_after_callbacks_receive_values(tmp_path: Path):
    """
    after 系列收到改写后输入与最终回复

    参数:
    - tmp_path: tmp路径
    """
    seen: dict[str, Any] = {}
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def after_send(text: str, ctx: HandlerContext) -> None:
        seen["user"] = text

    def before_reply(ctx: HandlerContext) -> None:
        seen["before"] = True

    def after_reply(text: str | None, ctx: HandlerContext) -> None:
        seen["reply"] = text

    session.add_handler(SessionHandler(
        name="p",
        before_user_send=lambda t, ctx: t + "!",
        after_user_send=after_send,
        before_model_reply=before_reply,
        after_model_reply=after_reply,
    ))

    result = session.run("hi")
    assert seen == {"user": "hi!", "before": True, "reply": "回复"}
    assert result == "回复"


def test_plugin_enable_disable_and_remove(tmp_path: Path):
    """
    插件: 停用不执行, 启用恢复, 删除移除

    参数:
    - tmp_path: tmp路径
    """
    calls: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def fn(text: str, ctx: HandlerContext) -> str | None:
        calls.append("called")
        return None

    session.add_handler(SessionHandler(name="p", before_user_send=fn))
    session.run("a")
    assert calls == ["called"]

    assert session.disable_handler("p") is True
    session.run("b")
    assert calls == ["called"]

    assert session.enable_handler("p") is True
    session.run("c")
    assert calls == ["called", "called"]

    assert session.remove_handler("p") is True
    session.run("d")
    assert calls == ["called", "called"]
    assert session.remove_handler("p") is False
    assert session.disable_handler("missing") is False


def test_plugin_priority_adjust(tmp_path: Path):
    """
    set_plugin_priority 调整执行顺序

    参数:
    - tmp_path: tmp路径
    """
    order: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    session.add_handler(SessionHandler(
        name="a", priority=100, before_user_send=lambda t, ctx: order.append("a") or None,
    ))
    session.add_handler(SessionHandler(
        name="b", priority=200, before_user_send=lambda t, ctx: order.append("b") or None,
    ))

    session.run("x")
    assert order == ["a", "b"]

    order.clear()
    assert session.set_handler_priority("b", 50) is True
    session.run("x")
    assert order == ["b", "a"]
    assert session.set_handler_priority("missing", 1) is False


def test_plugin_duplicate_name_conflicts_and_replace(tmp_path: Path):
    """
    同名处理器: 默认 raise; replace=True 覆盖且旧对象 close 被调

    参数:
    - tmp_path: tmp路径
    """
    calls: list[str] = []
    closed: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def first(t: str, ctx: HandlerContext) -> str | None:
        calls.append("first")
        return None

    def second(t: str, ctx: HandlerContext) -> str | None:
        calls.append("second")
        return None

    class _ClosingHandler(SessionHandler):
        def close(self) -> None:
            closed.append(self.name)

    session.add_handler(_ClosingHandler(name="p", before_user_send=first))
    with pytest.raises(ValueError, match="已存在"):
        session.add_handler(SessionHandler(name="p", before_user_send=second))
    assert session.add_handler(
        SessionHandler(name="p", before_user_send=second), replace=True,
    ) is True
    session.run("x")
    assert calls == ["second"]
    assert len(session.list_handlers()) == 1
    assert closed == ["p"]


def test_plugin_empty_name_raises(tmp_path: Path):
    """
    空插件名抛 ValueError

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    with pytest.raises(ValueError):
        session.add_handler(SessionHandler(name=""))


# ================= checkpoint 测试 =================


def test_checkpoint_available_when_enabled(tmp_path: Path):
    """
    启用检查点后 create/list 可用

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    session.run("你好")

    batch_id = session.create_checkpoint(name="t1")
    assert batch_id
    checkpoints = session.list_checkpoints()
    assert len(checkpoints) >= 1


def test_checkpoint_raises_when_disabled(tmp_path: Path):
    """
    未启用检查点时抛 ValueError

    参数:
    - tmp_path: tmp路径
    """
    session = SimpleSession(
        "conv-2", _FakeLLM(), db_path=str(tmp_path / "chat.db"),
        enable_checkpoint=False,
    )
    with pytest.raises(ValueError):
        session.create_checkpoint()


# ================= 模型与流式 =================


def test_set_llm_and_parameters(tmp_path: Path):
    """
    set_llm 替换模型, set_model_parameters 透传

    参数:
    - tmp_path: tmp路径
    """
    llm1 = _FakeLLM()
    session = _make_session(tmp_path, llm1)
    assert session.llm is llm1

    llm2 = _FakeLLM()
    session.set_llm(llm2)
    assert session.llm is llm2

    session.set_model_parameters(temperature=0.5)
    assert llm2.params == {"temperature": 0.5}


def test_stream_mode_switches_to_stream_call(tmp_path: Path):
    """
    流式模式走 stream_call 并返回完整文本

    参数:
    - tmp_path: tmp路径
    """
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
    """
    异步 run 基本流程 (自动初始化)

    参数:
    - tmp_path: tmp路径
    """
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
    """
    初始化前注入的工具在 initialize 时生效

    参数:
    - tmp_path: tmp路径
    """
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
    """
    MCP: 接入后工具进 manager, 移除后注销并断开

    参数:
    - tmp_path: tmp路径
    """
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

    assert session.disable_mcp("fs") is True
    # 启停
    assert session.is_tool_enabled("async_echo") is False
    assert session.enable_mcp("fs") is True
    assert session.is_tool_enabled("async_echo") is True

    assert await session.remove_mcp("fs") is True
    # 移除
    assert client.closed is True
    assert session.list_mcp() == []
    assert session.list_tools() == []
    assert await session.remove_mcp("fs") is False


@pytest.mark.asyncio
async def test_async_plugin_sync_callbacks(tmp_path: Path):
    """
    异步会话同步回调 (全同步协议) 经 to_thread 生效

    参数:
    - tmp_path: tmp路径
    """
    seen: list[str] = []
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    def before(text: str, ctx: HandlerContext) -> str | None:
        seen.append("before")
        return text + "!"

    def after(text: str | None, ctx: HandlerContext) -> str | None:
        seen.append(f"after:{text}")
        return None

    session.add_handler(SessionHandler(
        name="p", before_user_send=before, after_model_reply=after,
    ))

    result = await session.run("hi")
    assert result == "异步回复"
    assert seen == ["before", "after:异步回复"]
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[0]["content"] == "hi!"


@pytest.mark.asyncio
async def test_async_missing_wf_raises(tmp_path: Path):
    """
    未初始化时访问工具管理器抛 RuntimeError

    参数:
    - tmp_path: tmp路径
    """
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"),
    )
    with pytest.raises(RuntimeError):
        session.list_tools()


# ================= 补充分支覆盖 =================


def test_sync_constructor_tools_and_proxies(tmp_path: Path):
    """
    构造传初始工具 + 属性代理 (llm setter / ctx / tools_manager)

    参数:
    - tmp_path: tmp路径
    """
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
    """
    插件只有 after_user_send 时 before_user_send 分支跳过

    参数:
    - tmp_path: tmp路径
    """
    seen: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.add_handler(SessionHandler(
        name="p", after_user_send=lambda t, ctx: seen.append(t),
    ))
    session.run("hi")
    assert seen == ["hi"]


def test_sync_skill_manager_missing_branches(tmp_path: Path):
    """
    无技能管理器时 remove/disable/list 的兑底分支

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    assert session.remove_skill("demo") is False
    assert session.disable_skill("demo") is False
    assert session.list_skills() == []


def test_sync_plugin_missing_enable(tmp_path: Path):
    """
    enable_plugin 不存在的插件返回 False

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    assert session.enable_handler("missing") is False


def test_sync_reload_llm(tmp_path: Path):
    """
    reload_llm 委托 set_llm

    参数:
    - tmp_path: tmp路径
    """
    llm1 = _FakeLLM()
    session = _make_session(tmp_path, llm1)
    llm2 = _FakeLLM()
    session.reload_llm(llm2)
    assert session.llm is llm2


@pytest.mark.asyncio
async def test_async_proxies_and_model_interfaces(tmp_path: Path):
    """
    异步属性代理与模型接口 (__call__ / ctx / tools_manager / set_llm 等)

    参数:
    - tmp_path: tmp路径
    """
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
    """
    异步流式切换走 stream_full_agent

    参数:
    - tmp_path: tmp路径
    """
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
    """
    异步版同步回调 + 缺省处理点的 continue 分支

    参数:
    - tmp_path: tmp路径
    """
    seen: dict[str, Any] = {}
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    def after_send(text: str, ctx: HandlerContext) -> None:
        seen["user"] = text

    def before_send(text: str, ctx: HandlerContext) -> str | None:
        seen["before"] = True
        return text + "!"

    def before_reply(ctx: HandlerContext) -> None:
        seen["before_reply"] = True

    session.add_handler(SessionHandler(name="a", after_user_send=after_send))
    session.add_handler(SessionHandler(
        name="b", before_user_send=before_send, before_model_reply=before_reply,
    ))

    result = await session.run("hi")
    assert seen == {"before": True, "before_reply": True, "user": "hi!"}
    assert result == "异步回复"


@pytest.mark.asyncio
async def test_async_command_lifecycle(tmp_path: Path):
    """
    异步命令: 添加/列表/执行/停用/删除

    参数:
    - tmp_path: tmp路径
    """
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
    """
    初始化后注入/批量/启停/删除工具

    参数:
    - tmp_path: tmp路径
    """
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
    """
    异步 skill: 初始化前注册延迟生效, 惰性创建, 启停/删除

    参数:
    - tmp_path: tmp路径
    """
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

    session2 = AsyncSimpleSession(
        "conv-b", llm, db_path=str(tmp_path / "chat2.db"), enable_checkpoint=True,
    )
    # 无管理器兑底 + 惰性创建
    assert await session2.remove_skill("demo") is False
    await session2.run("hi")
    assert await session2.add_skill("not-exist") is False


@pytest.mark.asyncio
async def test_async_mcp_missing_branches(tmp_path: Path):
    """
    MCP 不存在的连接启停返回 False

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    await session.run("hi")
    assert session.enable_mcp("missing") is False
    assert session.disable_mcp("missing") is False


@pytest.mark.asyncio
async def test_async_plugin_management(tmp_path: Path):
    """
    异步插件管理全套 + 空名抛错

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    with pytest.raises(ValueError):
        session.add_handler(SessionHandler(name=""))

    session.add_handler(SessionHandler(name="p", priority=100))
    assert len(session.list_handlers()) == 1
    assert session.set_handler_priority("p", 50) is True
    assert session.set_handler_priority("missing", 1) is False

    assert session.disable_handler("p") is True
    assert session.enable_handler("p") is True
    assert session.disable_handler("missing") is False
    assert session.enable_handler("missing") is False
    assert session.remove_handler("p") is True
    assert session.remove_handler("p") is False


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
        thinking: str = "off", temperature: float | None = None,
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
    """
    H1 回归: 默认 db_path (正斜杠) 构造不再抛 ValueError

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    - tmp_path: tmp路径
    """
    monkeypatch.chdir(tmp_path)
    session = SimpleSession("conv-default", _FakeLLM())
    assert session.run("hi") == "回复"


@pytest.mark.asyncio
async def test_async_default_db_path_initializes_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    H1 回归: 异步版默认 db_path 初始化不再抛 ValueError

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    - tmp_path: tmp路径
    """
    monkeypatch.chdir(tmp_path)
    session = AsyncSimpleSession("conv-default", _FakeAsyncLLM())
    assert await session.run("hi") == "异步回复"


def test_plugin_empty_string_rewrite(tmp_path: Path):
    """
    H2 修复: before_user_send 返回空串 = 拦截/清空输入

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.add_handler(SessionHandler(name="p", before_user_send=lambda t, ctx: ""))
    session.run("你好")
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == ""


@pytest.mark.asyncio
async def test_async_plugin_empty_string_rewrite(tmp_path: Path):
    """
    H2 修复: 异步版空串改写生效

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_handler(SessionHandler(name="p", before_user_send=lambda t, ctx: ""))
    await session.run("hi")
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == ""


def test_plugin_non_str_return_ignored_sync(tmp_path: Path):
    """
    非 str/HandlerResult 返回值被忽略 (不改写, 不中断)

    参数:
    - tmp_path: tmp路径
    """
    def bad(t: str, ctx: HandlerContext) -> bool:
        return False

    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.add_handler(SessionHandler(name="p", before_user_send=cast(Any, bad)))
    result = session.run("hi")
    assert result == "回复"
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi"   # False 被忽略, 未改写


@pytest.mark.asyncio
async def test_plugin_non_str_return_ignored_async(tmp_path: Path):
    """
    异步版非 str 返回值被忽略

    参数:
    - tmp_path: tmp路径
    """
    def bad(t: str, ctx: HandlerContext) -> dict[str, int]:
        return {"bad": 1}

    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_handler(SessionHandler(name="p", before_user_send=cast(Any, bad)))
    result = await session.run("hi")
    assert result == "异步回复"
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi"   # dict 被忽略, 未改写


def test_thinking_requires_stream_mode_sync(tmp_path: Path):
    """
    M3 修复: 非流式 thinking="medium" 抛 NotImplementedError

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    with pytest.raises(NotImplementedError):
        session.run("hi", thinking="medium")


@pytest.mark.asyncio
async def test_thinking_requires_stream_mode_async(tmp_path: Path):
    """
    M3 修复: 异步非流式 thinking="medium" 抛 NotImplementedError

    参数:
    - tmp_path: tmp路径
    """
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    with pytest.raises(NotImplementedError):
        await session.run("hi", thinking="medium")


@pytest.mark.asyncio
async def test_async_model_setters_before_initialize(tmp_path: Path):
    """
    M4 修复: 未初始化时 set_llm / set_model_parameters 延迟生效

    参数:
    - tmp_path: tmp路径
    """
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
    """
    M2 修复: 初始化前 add_skill 不存在的技能返回 False

    参数:
    - tmp_path: tmp路径
    """
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    assert await session.add_skill("not-exist") is False


@pytest.mark.asyncio
async def test_async_concurrent_init_single_workflow(tmp_path: Path):
    """
    H3 修复: 并发初始化只构建一个工作流, 无孤儿上下文

    参数:
    - tmp_path: tmp路径
    """
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
    """
    L1 修复: 工具循环内每轮 llm.call 都透传 img_urls

    参数:
    - tmp_path: tmp路径
    """
    llm = _ToolLoopLLM()
    session = _make_session(tmp_path, llm)
    session.add_tool(_CalcTool())
    result = session.run("算 2+3", img_urls=["http://x/a.png"])
    assert result == "最终回复"
    assert len(llm.calls) == 2
    assert llm.calls[0]["img_urls"] == ["http://x/a.png"]
    assert llm.calls[1]["img_urls"] == ["http://x/a.png"]


# ================= 目录插件测试 =================


def _write_plugin_dir(tmp_path: Path, name: str = "demo", *, with_mcp: bool = False, with_commands: bool = False) -> Path:
    """
    构造最小插件目录: meta.yaml + tools.py + skills/ + handlers.py (可带 mcp.py / commands.py)

    参数:
    - tmp_path: tmp路径
    - name: 名称
    - with_mcp: 是否包含MCP
    - with_commands: 是否包含命令集合

    返回:
    - Path: 构造最小插件目录: meta.yaml + tools.py + skills/ + handlers.py (可带 mcp.py / commands.py)
    """
    plugin_dir = tmp_path / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text(
        f"name: {name}\nversion: 0.1.0\nauthor: tester\nrepo: https://example.com/{name}\n"
        "description: 测试插件\n",
        encoding="utf-8",
    )
    (plugin_dir / "tools.py").write_text(
        "from satrap.core.utils.TCBuilder import Tool\n\n"
        "class GreetTool(Tool):\n"
        "    tool_name = 'greet'\n"
        "    description = '问候'\n"
        "    params_dict = {'name': ('string', '名字')}\n"
        "    def execute(self, name: str = '') -> str:\n"
        "        return f'hi {name}'\n",
        encoding="utf-8",
    )
    skill_dir = plugin_dir / "skills" / "pskill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(
        "---\nname: pskill\n---\n\n# 插件技能\n插件自带技能指令\n", encoding="utf-8",
    )
    (plugin_dir / "handlers.py").write_text(
        "from satrap.edictum import SessionHandler\n\n"
        "def before_user_send(text: str, ctx):\n"
        "    return text + ' [插件]'\n"
        "handlers = [SessionHandler(name='demo.handler', before_user_send=before_user_send)]\n",
        encoding="utf-8",
    )
    if with_mcp:
        (plugin_dir / "mcp.py").write_text(
            "clients = {}\n", encoding="utf-8",
        )
    if with_commands:
        (plugin_dir / "commands.py").write_text(
            "def cmd_hello(name: str = ''):\n"
            "    \"\"\"打招呼命令\"\"\"\n"
            "    return f'hello {name}'\n",
            encoding="utf-8",
        )
    return plugin_dir


def _write_plugin_with_factory(tmp_path: Path, *, session_arg: bool) -> Path:
    """
    构造 get_tools 工厂插件目录 (带会话注入 / 无参降级两版)

    参数:
    - tmp_path: tmp路径
    - session_arg: 会话arg

    返回:
    - Path: 构造 get_tools 工厂插件目录 (带会话注入 / 无参降级两版)
    """
    plugin_dir = tmp_path / "plugins" / "factory"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: factory\n", encoding="utf-8")
    if session_arg:
        tools_src = (
            "from satrap.core.utils.TCBuilder import Tool\n\n"
            "class ProbeTool(Tool):\n"
            "    tool_name = 'probe'\n"
            "    description = '探针'\n"
            "    params_dict = {}\n"
            "    def execute(self) -> str:\n"
            "        return getattr(self, 'session_id', 'none')\n\n"
            "def get_tools(session):\n"
            "    tool = ProbeTool()\n"
            "    tool.session_id = session.session_id\n"
            "    return [tool]\n"
        )
    else:
        tools_src = (
            "from satrap.core.utils.TCBuilder import Tool\n\n"
            "class ProbeTool(Tool):\n"
            "    tool_name = 'probe'\n"
            "    description = '探针'\n"
            "    params_dict = {}\n"
            "    def execute(self) -> str:\n"
            "        return 'ok'\n\n"
            "def get_tools():\n"
            "    return [ProbeTool()]\n"
        )
    (plugin_dir / "tools.py").write_text(tools_src, encoding="utf-8")
    return plugin_dir


def test_install_plugin_full_package(tmp_path: Path):
    """
    插件: 工具进 manager, skill 注册, handler 生效, meta 信息完整

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    plugin = session.install_plugin(str(plugin_dir))
    assert plugin.name == "demo"
    assert plugin.version == "0.1.0"
    assert plugin.author == "tester"
    assert plugin.repo == "https://example.com/demo"
    assert session.list_plugins() == [plugin]

    assert session.list_tools() == ["greet"]
    # 工具已注册
    # 技能已注册 (未激活)
    assert "pskill" in session.list_skills()
    # handler 生效
    session.run("hi")
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi [插件]"
    # 技能可激活
    assert session.add_skill("pskill") is True
    session.run("再次调用")
    system_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "system"]
    assert system_msgs and "插件技能" in str(system_msgs[0]["content"])


def test_plugin_commands_install_and_execute(tmp_path: Path):
    """
    插件命令: 注册进命令系统, 可执行, intro 取自 docstring 首行

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path, with_commands=True)
    session = _make_session(tmp_path, _FakeLLM())
    plugin = session.install_plugin(str(plugin_dir))

    assert "hello" in plugin.commands
    assert plugin.list_capabilities()["commands"] == [{"name": "hello", "enabled": True, "description": ""}]
    assert session.list_commands()["hello"] == "打招呼命令"
    result, is_cmd = session.cmd_handler.process_message("/hello world")
    assert is_cmd and result == "hello world"


def test_plugin_commands_conflict(tmp_path: Path):
    """
    插件命令与已注册命令冲突: 拒绝安装

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path, with_commands=True)
    session = _make_session(tmp_path, _FakeLLM())
    session.add_command("hello", lambda: "x")
    with pytest.raises(ValueError, match="命令 hello"):
        session.install_plugin(str(plugin_dir))


def test_plugin_commands_uninstall(tmp_path: Path):
    """
    卸载插件: 命令回收, 不留孤儿

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path, with_commands=True)
    session = _make_session(tmp_path, _FakeLLM())
    plugin = session.install_plugin(str(plugin_dir))
    assert session.uninstall_plugin("demo") is True
    assert "hello" not in session.list_commands()
    assert plugin.commands == {"hello": True}


def test_plugin_commands_independent_and_aggregate_toggle(tmp_path: Path):
    """
    插件命令: 独立启停 + 聚合启停 (压制不改变独立状态)

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path, with_commands=True)
    session = _make_session(tmp_path, _FakeLLM())
    plugin = session.install_plugin(str(plugin_dir))

    assert plugin.disable_command("hello") is True
    assert session.is_command_enabled("hello") is False
    assert plugin.enable_command("hello") is True
    assert session.is_command_enabled("hello") is True
    assert plugin.disable_command("nope") is False

    assert session.disable_plugin("demo") is True
    assert session.is_command_enabled("hello") is False
    assert plugin.commands["hello"] is True
    assert plugin.list_capabilities()["commands"] == [{"name": "hello", "enabled": False, "description": ""}]
    assert session.enable_plugin("demo") is True
    assert session.is_command_enabled("hello") is True


def test_plugin_tools_factory_with_session(tmp_path: Path):
    """
    get_tools(session) 工厂: 工具拿到会话依赖

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_with_factory(tmp_path, session_arg=True)
    session = _make_session(tmp_path, _FakeLLM())
    session.install_plugin(str(plugin_dir))

    assert "probe" in session.list_tools()
    tool = cast(_SessionAwareTool, session.tools_manager.tools["probe"])
    assert tool.session_id == session.session_id


def test_plugin_tools_factory_fallback_no_session(tmp_path: Path):
    """
    get_tools() 无参工厂: 传 session 不匹配时降级无参调用

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_with_factory(tmp_path, session_arg=False)
    session = _make_session(tmp_path, _FakeLLM())
    session.install_plugin(str(plugin_dir))

    assert "probe" in session.list_tools()
    assert session.tools_manager.tools["probe"].execute() == "ok"


@pytest.mark.asyncio
async def test_plugin_async_commands(tmp_path: Path):
    """
    异步插件命令: cmd_*_async 约定注册并可执行

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "ademo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: ademo\n", encoding="utf-8")
    (plugin_dir / "commands.py").write_text(
        "async def cmd_ping(text: str = ''):\n"
        "    \"\"\"ping 命令\"\"\"\n"
        "    return f'pong {text}'\n",
        encoding="utf-8",
    )
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    plugin = await session.install_plugin(str(plugin_dir))

    assert "ping" in plugin.commands
    assert session.list_commands()["ping"] == "ping 命令"
    result, is_cmd = await session.command_handler.process_message("/ping hi")
    assert is_cmd and result == "pong hi"
    assert await session.uninstall_plugin("ademo") is True
    assert "ping" not in session.list_commands()


def test_install_plugin_skips_mcp_sync(tmp_path: Path):
    """
    同步版安装含 mcp.py 的插件: 跳过 mcp, 其余能力照常

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path, with_mcp=True)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    plugin = session.install_plugin(str(plugin_dir))
    assert plugin.mcp == {}
    assert session.list_tools() == ["greet"]
    assert session.list_handlers()


def test_plugin_aggregate_enable_disable(tmp_path: Path):
    """
    插件聚合启停: disable 压制全部能力, enable 按独立状态恢复

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    plugin = session.install_plugin(str(plugin_dir))
    session.add_skill("pskill")

    session.disable_plugin("demo")
    assert plugin.enabled is False
    session.run("hi")
    tools_def = llm.calls[-1]["tools"]
    # 定义列表按独立位过滤, 工具仍可见; 执行路径合成 (effectiveness_guard) 拒绝执行
    assert any(t["function"]["name"] == "greet" for t in tools_def)
    err = session.tools_manager.execute_tool("greet", {"name": "x"})
    assert err["ok"] is False and err["error_type"] == "disabled"
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi"

    assert session.enable_plugin("demo") is True
    assert plugin.enabled is True
    session.run("hi")
    tools_def = llm.calls[-1]["tools"]
    assert any(t["function"]["name"] == "greet" for t in tools_def)
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi [插件]"

    assert session.disable_plugin("missing") is False


def test_plugin_independent_enable_disable(tmp_path: Path):
    """
    插件内能力独立启停: 聚合恢复不覆盖独立停用

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    plugin = session.install_plugin(str(plugin_dir))

    assert plugin.disable_tool("greet") is True
    # 独立停用工具
    assert plugin.disable_tool("missing") is False
    session.run("hi")
    tools_def = llm.calls[-1]["tools"]
    assert not any(t["function"]["name"] == "greet" for t in tools_def)

    session.disable_plugin("demo")
    # 聚合停用再启用: 独立停用的工具保持停用
    session.enable_plugin("demo")
    session.run("hi")
    tools_def = llm.calls[-1]["tools"]
    assert not any(t["function"]["name"] == "greet" for t in tools_def)

    assert plugin.enable_tool("greet") is True
    # 独立恢复
    session.run("hi")
    tools_def = llm.calls[-1]["tools"]
    assert any(t["function"]["name"] == "greet" for t in tools_def)

    caps = {c["name"]: c for c in plugin.list_capabilities()["tools"]}
    assert caps["greet"]["enabled"] is True


def test_plugin_uninstall_reclaims(tmp_path: Path):
    """
    插件卸载: 工具注销, skill 移除, handler 移除, 不留孤儿

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.install_plugin(str(plugin_dir))
    session.add_skill("pskill")

    assert session.uninstall_plugin("demo") is True
    assert session.list_plugins() == []
    assert session.list_tools() == []
    assert session.list_skills() == []
    assert session.list_handlers() == []
    assert session.uninstall_plugin("demo") is False


def test_install_plugin_errors(tmp_path: Path):
    """
    插件安装校验: 缺 meta.yaml / 重名 / 工具冲突

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)

    bad_dir = tmp_path / "bad"
    # 缺 meta.yaml
    bad_dir.mkdir()
    with pytest.raises(ValueError):
        session.install_plugin(str(bad_dir))

    plugin_dir = _write_plugin_dir(tmp_path)
    # 重名
    session.install_plugin(str(plugin_dir))
    with pytest.raises(ValueError):
        session.install_plugin(str(plugin_dir))

    session2 = _make_session(tmp_path)
    # 工具冲突
    session2.add_tool(_EchoTool())
    conflict_dir = tmp_path / "plugins" / "conflict"
    conflict_dir.mkdir(parents=True)
    (conflict_dir / "meta.yaml").write_text("name: conflict\n", encoding="utf-8")
    (conflict_dir / "tools.py").write_text(
        "from satrap.core.utils.TCBuilder import Tool\n"
        "class EchoTool(Tool):\n"
        "    tool_name = 'echo'\n"
        "    description = 'x'\n"
        "    params_dict = {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        session2.install_plugin(str(conflict_dir))


def test_plugin_handlers_convention_functions(tmp_path: Path):
    """
    handlers.py 用 4 约定函数 (不导出 handlers 列表) 自动构建处理器

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "conv"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: conv\n", encoding="utf-8")
    (plugin_dir / "handlers.py").write_text(
        "def before_user_send(text: str, ctx):\n"
        "    return text.upper()\n",
        encoding="utf-8",
    )
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    session.install_plugin(str(plugin_dir))
    session.run("hi")
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "HI"


@pytest.mark.asyncio
async def test_async_install_plugin_with_mcp(tmp_path: Path):
    """
    异步插件: mcp.py 客户端自动接入, 卸载时断开连接

    参数:
    - tmp_path: tmp路径
    """
    mark = tmp_path / "closed.flag"
    plugin_dir = _write_plugin_dir(tmp_path, name="ademo", with_mcp=True)
    (plugin_dir / "mcp.py").write_text(
        "from satrap.core.utils.TCBuilder import AsyncTool\n\n"
        "class _T(AsyncTool):\n"
        "    tool_name = 'mcp_greet'\n"
        "    description = 'x'\n"
        "    params_dict = {}\n"
        "    async def execute(self) -> str:\n"
        "        return 'ok'\n"
        "class _FakeClient:\n"
        "    def __init__(self, mark):\n"
        "        self.mark = mark\n"
        "        self.adapters = [_T()]\n"
        "    async def register_tools(self, tm, name_prefix=None):\n"
        "        for a in self.adapters:\n"
        "            tm.register_tool(a)\n"
        "        return list(self.adapters)\n"
        "    async def close(self):\n"
        "        open(self.mark, 'w', encoding='utf-8').write('closed')\n"
        f"clients = {{'fs': _FakeClient({str(mark)!r})}}\n",
        encoding="utf-8",
    )
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    plugin = await session.install_plugin(str(plugin_dir))
    assert plugin.name == "ademo"
    assert plugin.mcp == {"fs": True}
    assert session.list_tools() == ["mcp_greet"]

    assert await session.uninstall_plugin("ademo") is True
    assert mark.is_file()
    assert session.list_tools() == []
    assert session.list_plugins() == []
    assert await session.uninstall_plugin("ademo") is False


@pytest.mark.asyncio
async def test_async_plugin_aggregate_enable_disable(tmp_path: Path):
    """
    异步插件聚合启停: MCP 工具随插件启停

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path, name="ademo", with_mcp=True)
    (plugin_dir / "mcp.py").write_text(
        "from satrap.core.utils.TCBuilder import AsyncTool\n\n"
        "class _T(AsyncTool):\n"
        "    tool_name = 'mcp_greet'\n"
        "    description = 'x'\n"
        "    params_dict = {}\n"
        "    async def execute(self) -> str:\n"
        "        return 'ok'\n"
        "class _FakeClient:\n"
        "    def __init__(self):\n"
        "        self.adapters = [_T()]\n"
        "    async def register_tools(self, tm, name_prefix=None):\n"
        "        for a in self.adapters:\n"
        "            tm.register_tool(a)\n"
        "        return list(self.adapters)\n"
        "    async def close(self):\n"
        "        pass\n"
        "clients = {'fs': _FakeClient()}\n",
        encoding="utf-8",
    )
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    await session.install_plugin(str(plugin_dir))
    assert session.is_tool_enabled("mcp_greet") is True

    assert await session.disable_plugin("ademo") is True
    assert session.is_tool_enabled("mcp_greet") is True   # 独立位未变
    err = await session.tools_manager.execute_tool("mcp_greet", {})
    assert err["ok"] is False and err["error_type"] == "disabled"   # 执行路径合成

    assert await session.enable_plugin("ademo") is True
    assert session.is_tool_enabled("mcp_greet") is True
    assert await session.disable_plugin("missing") is False


# ================= 目录插件补充分支测试 =================


def test_plugin_meta_not_dict_raises(tmp_path: Path):
    """
    meta.yaml 非字典格式抛 ValueError

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "badmeta"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("- a\n- b\n", encoding="utf-8")
    session = _make_session(tmp_path)
    with pytest.raises(ValueError):
        session.install_plugin(str(plugin_dir))


def test_plugin_minimal_and_loose_files(tmp_path: Path):
    """
    最小插件: 无 handlers/skills/mcp 分支; skills/ 下非目录文件跳过

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "minimal"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: minimal\n", encoding="utf-8")
    (plugin_dir / "tools.py").write_text(
        "from satrap.core.utils.TCBuilder import Tool\n"
        "class MiniTool(Tool):\n"
        "    tool_name = 'mini'\n"
        "    description = 'x'\n"
        "    params_dict = {}\n",
        encoding="utf-8",
    )
    skills_root = plugin_dir / "skills"
    skills_root.mkdir()
    (skills_root / "loose.md").write_text("# 松散文件\n", encoding="utf-8")   # 非目录, 跳过
    (plugin_dir / "handlers.py").write_text("x = 1\n", encoding="utf-8")   # 无约定内容 -> 空

    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    plugin = session.install_plugin(str(plugin_dir))
    assert session.list_tools() == ["mini"]
    assert plugin.handlers == {}
    assert plugin.skills == {}
    assert session.list_handlers() == []


def test_plugin_skills_py_declared(tmp_path: Path):
    """
    skills.py 导出 skills 列表注册技能

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "skpy"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: skpy\n", encoding="utf-8")
    (plugin_dir / "skills.py").write_text(
        "from satrap.core.utils.skills import Skill\n"
        "skills = [Skill(name='skpy-skill', instructions='# 声明技能\\n声明式技能指令')]\n",
        encoding="utf-8",
    )
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.install_plugin(str(plugin_dir))
    assert "skpy-skill" in session.list_skills()
    assert session.add_skill("skpy-skill") is True
    session.run("hi")
    system_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "system"]
    assert system_msgs and "声明式技能指令" in str(system_msgs[0]["content"])


def test_plugin_skill_independent_ops(tmp_path: Path):
    """
    插件技能独立启停 (同步版)

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    plugin = session.install_plugin(str(plugin_dir))

    assert plugin.enable_skill("pskill") is True
    session.run("hi")
    system_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "system"]
    assert system_msgs and "插件技能" in str(system_msgs[0]["content"])

    assert plugin.disable_skill("pskill") is True
    session.run("hi")
    system_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "system"]
    assert not any("插件技能" in str(m["content"]) for m in system_msgs)

    assert plugin.enable_skill("missing") is False
    assert plugin.disable_skill("missing") is False


def test_plugin_handler_independent_ops(tmp_path: Path):
    """
    插件处理器独立启停

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    plugin = session.install_plugin(str(plugin_dir))

    assert plugin.disable_handler("demo.handler") is True
    session.run("hi")
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi"

    assert plugin.enable_handler("demo.handler") is True
    session.run("hi")
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi [插件]"

    assert plugin.disable_handler("missing") is False
    assert plugin.enable_handler("missing") is False
    assert plugin.enable_tool("missing") is False


@pytest.mark.asyncio
async def test_async_plugin_without_mcp(tmp_path: Path):
    """
    异步安装无 mcp.py 的插件: 正常注册其余能力

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path, name="ademo")
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    plugin = await session.install_plugin(str(plugin_dir))
    assert plugin.mcp == {}
    assert session.list_skills() == ["pskill"]
    assert session.list_handlers()


@pytest.mark.asyncio
async def test_async_plugin_mcp_independent_and_build_clients(tmp_path: Path):
    """
    异步插件: build_clients 工厂 + MCP 连接独立启停

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "bmcp"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: bmcp\n", encoding="utf-8")
    (plugin_dir / "mcp.py").write_text(
        "from satrap.core.utils.TCBuilder import AsyncTool\n\n"
        "class _T(AsyncTool):\n"
        "    tool_name = 'bmcp_tool'\n"
        "    description = 'x'\n"
        "    params_dict = {}\n"
        "    async def execute(self) -> str:\n"
        "        return 'ok'\n"
        "class _FakeClient:\n"
        "    def __init__(self):\n"
        "        self.adapters = [_T()]\n"
        "    async def register_tools(self, tm, name_prefix=None):\n"
        "        for a in self.adapters:\n"
        "            tm.register_tool(a)\n"
        "        return list(self.adapters)\n"
        "    async def close(self):\n"
        "        pass\n"
        "def build_clients():\n"
        "    return {'fs': _FakeClient()}\n",
        encoding="utf-8",
    )
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    plugin = await session.install_plugin(str(plugin_dir))
    assert plugin.mcp == {"fs": True}
    assert session.list_tools() == ["bmcp_tool"]

    assert plugin.disable_mcp("fs") is True
    assert session.is_tool_enabled("bmcp_tool") is False
    assert plugin.enable_mcp("fs") is True
    assert session.is_tool_enabled("bmcp_tool") is True
    assert plugin.disable_mcp("missing") is False
    assert plugin.enable_mcp("missing") is False

    plugin.disable_mcp("fs")
    # 聚合停用再启用: 独立停用的 MCP 保持停用
    await session.disable_plugin("bmcp")
    await session.enable_plugin("bmcp")
    assert session.is_tool_enabled("bmcp_tool") is False


def test_plugin_skill_ops_while_disabled(tmp_path: Path):
    """
    插件停用期间技能独立操作: 只改状态不激活

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    plugin = session.install_plugin(str(plugin_dir))

    session.disable_plugin("demo")
    assert plugin.enable_skill("pskill") is True   # 停用中: 只改状态
    session.run("hi")
    system_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "system"]
    assert not any("插件技能" in str(m["content"]) for m in system_msgs)

    session.enable_plugin("demo")
    session.run("hi")
    system_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "system"]
    assert system_msgs and "插件技能" in str(system_msgs[0]["content"])


@pytest.mark.asyncio
async def test_async_plugin_mcp_build_clients_not_dict(tmp_path: Path):
    """
    mcp.py build_clients 返回非 dict: 忽略不报错

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "bd"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: bd\n", encoding="utf-8")
    (plugin_dir / "mcp.py").write_text(
        "def build_clients():\n"
        "    return ['not-a-dict']\n",
        encoding="utf-8",
    )
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    plugin = await session.install_plugin(str(plugin_dir))
    assert plugin.mcp == {}
    assert session.list_tools() == []


# ================= Handler 协议升级新增测试 =================


def test_handler_result_continue_rewrites(tmp_path: Path):
    """
    HandlerResult.continue_with 等价 str 改写, 继续链式执行后续 handler

    参数:
    - tmp_path: tmp路径
    """
    order: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def first(text: str, ctx: HandlerContext) -> HandlerResult:
        order.append("first")
        return HandlerResult.continue_with(text + "a")

    def second(text: str, ctx: HandlerContext) -> str | None:
        order.append("second")
        return text + "b"

    session.add_handler(SessionHandler(name="first", priority=1, before_user_send=first))
    session.add_handler(SessionHandler(name="second", priority=2, before_user_send=second))
    session.run("x")
    assert order == ["first", "second"]
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "xab"


def test_handler_result_respond_short_circuits(tmp_path: Path):
    """
    respond 跳过模型直接返回; after_model_reply 仍执行 (finally)

    参数:
    - tmp_path: tmp路径
    """
    seen: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def respond(text: str, ctx: HandlerContext) -> HandlerResult:
        return HandlerResult.respond("已响应")

    def after_reply(result: str | None, ctx: HandlerContext) -> None:
        seen.append(result or "")

    session.add_handler(SessionHandler(
        name="r", before_user_send=respond, after_model_reply=after_reply,
    ))
    result = session.run("hi")
    assert isinstance(result, str) and result == "已响应"
    assert seen == ["已响应"]
    assert llm.calls == []   # 模型未调用


def test_handler_result_reject_returns_reason(tmp_path: Path):
    """
    reject 业务拒绝, reason 作为 run 的返回文本 (正常返回, 不抛异常)

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def reject(text: str, ctx: HandlerContext) -> HandlerResult:
        return HandlerResult.reject("拒绝: 无权限")

    session.add_handler(SessionHandler(name="p", before_user_send=reject))
    result = session.run("hi")
    assert isinstance(result, str) and result == "拒绝: 无权限"
    assert llm.calls == []


def test_handler_result_abort_raises(tmp_path: Path):
    """
    abort 故障终止, 抛 HandlerAbortError

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path, _FakeLLM())

    def abort(text: str, ctx: HandlerContext) -> HandlerResult:
        return HandlerResult.abort("系统故障")

    session.add_handler(SessionHandler(name="p", before_user_send=abort))
    with pytest.raises(HandlerAbortError, match="系统故障"):
        session.run("hi")


@pytest.mark.asyncio
async def test_async_handler_result_respond_short_circuits(tmp_path: Path):
    """
    异步版 respond 短路同样生效

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    def respond(text: str, ctx: HandlerContext) -> HandlerResult:
        return HandlerResult.respond("异步已响应")

    session.add_handler(SessionHandler(name="r", before_user_send=respond))
    assert await session.run("hi") == "异步已响应"
    assert llm.calls == []


def test_after_model_reply_rewrite_result(tmp_path: Path):
    """
    after_model_reply 返回 str 修改最终结果

    参数:
    - tmp_path: tmp路径
    """
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def after_reply(result: str | None, ctx: HandlerContext) -> str:
        return f"改写:{result}"

    session.add_handler(SessionHandler(name="p", after_model_reply=after_reply))
    assert session.run("hi") == "改写:回复"


def test_after_model_reply_rewrite_ignored_on_error(tmp_path: Path):
    """
    模型抛异常时 after_model_reply 返回的 str 被忽略, 异常照常 re-raise

    参数:
    - tmp_path: tmp路径
    """
    class _BoomLLM(_FakeLLM):
        def call(self, *args: Any, **kwargs: Any) -> LLMCallResponse:   # noqa: ARG002
            raise RuntimeError("boom")

    def after_reply(result: str | None, ctx: HandlerContext) -> str:
        return "不应生效"

    session = _make_session(tmp_path, _BoomLLM())
    session.add_handler(SessionHandler(name="p", after_model_reply=after_reply))
    with pytest.raises(RuntimeError, match="boom"):
        session.run("hi")


def test_handler_exception_isolated(tmp_path: Path):
    """
    handler 抛异常不中断 run, 后续 handler 继续执行

    参数:
    - tmp_path: tmp路径
    """
    order: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def boom(text: str, ctx: HandlerContext) -> None:
        raise RuntimeError("handler boom")

    def after(text: str, ctx: HandlerContext) -> None:
        order.append("after")

    session.add_handler(SessionHandler(name="boom", after_user_send=boom))
    session.add_handler(SessionHandler(name="after", after_user_send=after))
    assert session.run("hi") == "回复"
    assert order == ["after"]


def test_after_model_reply_runs_in_finally_on_error(tmp_path: Path):
    """
    模型抛异常: after_model_reply 在 finally 被调, ctx.error 有值, result 为 None

    参数:
    - tmp_path: tmp路径
    """
    seen: dict[str, Any] = {}

    class _BoomLLM(_FakeLLM):
        def call(self, *args: Any, **kwargs: Any) -> LLMCallResponse:   # noqa: ARG002
            raise RuntimeError("boom")

    def after_reply(result: str | None, ctx: HandlerContext) -> None:
        seen["result"] = result
        seen["error"] = ctx.error

    session = _make_session(tmp_path, _BoomLLM())
    session.add_handler(SessionHandler(name="p", after_model_reply=after_reply))
    with pytest.raises(RuntimeError, match="boom"):
        session.run("hi")
    assert seen["result"] is None
    assert isinstance(seen["error"], RuntimeError)


@pytest.mark.asyncio
async def test_async_handler_timeout_isolated(tmp_path: Path):
    """
    同步 handler 阻塞超过 timeout -> wait_for 放弃等待, 隔离继续 (to_thread 线程仍跑完)

    参数:
    - tmp_path: tmp路径
    """
    seen: list[str] = []
    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    def slow(text: str, ctx: HandlerContext) -> str | None:
        time.sleep(0.5)
        seen.append("slow-done")
        return None

    session.add_handler(SessionHandler(name="slow", timeout=0.05, before_user_send=slow))
    assert await session.run("hi") == "异步回复"
    assert seen == []   # 超时放弃等待, 隔离继续


@pytest.mark.asyncio
async def test_async_handler_close_after_timeout(tmp_path: Path):
    """
    超时放弃等待后再 remove_handler 触发 close, close 不抛异常 (幂等容忍超时态)

    参数:
    - tmp_path: tmp路径
    """
    closed: list[str] = []

    def slow(text: str, ctx: HandlerContext) -> str | None:
        time.sleep(0.5)
        return None

    class _H(SessionHandler):
        def close(self) -> None:
            closed.append(self.name)

    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    h = _H(name="slow", timeout=0.05, before_user_send=slow)
    session.add_handler(h)
    await session.run("hi")   # 回调被超时取消
    assert session.remove_handler("slow") is True
    assert closed == ["slow"]
    # close 幂等: 直接重复调用无害
    h.close()
    h.close()
    assert closed == ["slow", "slow", "slow"]
    assert session.remove_handler("slow") is False


def test_handler_state_composition_with_plugin(tmp_path: Path):
    """
    状态合成: 插件禁用后 enable_handler 执行层不生效; 启用恢复; list_capabilities 与执行一致

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    plugin = session.install_plugin(str(plugin_dir))

    assert session.disable_plugin("demo") is True
    assert plugin.enable_handler("demo.handler") is True   # 独立位更新
    session.run("hi")
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi"   # 执行路径仍过滤
    caps = {c["name"]: c for c in plugin.list_capabilities()["handlers"]}
    assert caps["demo.handler"]["enabled"] is False   # 合成值 False

    assert session.enable_plugin("demo") is True
    session.run("hi")
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi [插件]"
    caps = {c["name"]: c for c in plugin.list_capabilities()["handlers"]}
    assert caps["demo.handler"]["enabled"] is True


def test_tool_bypass_closed_when_plugin_disabled(tmp_path: Path):
    """
    工具旁路: 插件禁用后 execute_tool 返回禁用; enable_tool/enable_all_tools 不生效

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    session = _make_session(tmp_path, _FakeLLM())
    session.install_plugin(str(plugin_dir))

    assert session.disable_plugin("demo") is True
    err = session.tools_manager.execute_tool("greet", {"name": "x"})
    assert err["ok"] is False and err["error_type"] == "disabled"

    assert session.enable_tool("greet") is True
    # enable_tool / enable_all_tools 只改独立位, 执行路径仍过滤
    assert session.tools_manager.enable_all_tools() is True
    err = session.tools_manager.execute_tool("greet", {"name": "x"})
    assert err["ok"] is False and err["error_type"] == "disabled"

    assert session.enable_plugin("demo") is True
    assert session.tools_manager.execute_tool("greet", {"name": "x"}) == "hi x"


def test_replace_inherits_owner_plugin(tmp_path: Path):
    """
    replace=True 覆盖插件 handler: 新 handler 继承 owner_plugin, 插件禁用仍过滤

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = _write_plugin_dir(tmp_path)
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)
    session.install_plugin(str(plugin_dir))

    def custom(text: str, ctx: HandlerContext) -> str | None:
        return text + " [custom]"

    assert session.add_handler(
        SessionHandler(name="demo.handler", before_user_send=custom), replace=True,
    ) is True
    session.run("hi")
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi [custom]"

    assert session.disable_plugin("demo") is True
    session.run("hi")
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi"   # 继承归属 -> 插件禁用仍过滤

    assert session.enable_plugin("demo") is True
    session.run("hi")
    user_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "hi [custom]"


def test_handler_close_on_remove_idempotent(tmp_path: Path):
    """
    remove_handler 触发 close; close 幂等 (重复调用无害)

    参数:
    - tmp_path: tmp路径
    """
    closed: list[str] = []

    class _H(SessionHandler):
        def close(self) -> None:
            closed.append(self.name)

    session = _make_session(tmp_path)
    h = _H(name="p")
    session.add_handler(h)
    assert session.remove_handler("p") is True
    assert closed == ["p"]
    h.close()
    h.close()
    assert closed == ["p", "p", "p"]   # 幂等: 重复调用不抛异常
    assert session.remove_handler("p") is False


def test_install_rollback_closes_handlers(tmp_path: Path):
    """
    插件安装中途失败 (命令冲突) 时已注册 handler 的 close 被调

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "rb"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: rb\n", encoding="utf-8")
    (plugin_dir / "handlers.py").write_text(
        "from satrap.edictum import SessionHandler\n\n"
        "closed = []\n"
        "class _H(SessionHandler):\n"
        "    def close(self):\n"
        "        closed.append(self.name)\n"
        "handlers = [_H(name='rb.h')]\n",
        encoding="utf-8",
    )
    (plugin_dir / "commands.py").write_text(
        "def cmd_hello():\n"
        "    return 'x'\n",
        encoding="utf-8",
    )
    session = _make_session(tmp_path)
    session.add_command("hello", lambda: "x")
    with pytest.raises(ValueError, match="命令 hello"):
        session.install_plugin(str(plugin_dir))
    assert session.list_handlers() == []   # 回滚注销
    mod = importlib.import_module("rb.handlers")
    assert mod.closed == ["rb.h"]   # 回滚 close 被调


def test_sync_add_handler_rejects_async_callback(tmp_path: Path):
    """
    协议级拒绝: add_handler 拒绝异步回调 (全同步协议)

    参数:
    - tmp_path: tmp路径
    """
    async def bad(text: str, ctx: HandlerContext) -> str | None:
        return None

    session = _make_session(tmp_path)
    with pytest.raises(TypeError, match="异步回调"):
        session.add_handler(SessionHandler(
            name="p", before_user_send=cast(Any, bad),   # 负向用例: 故意传入异步回调验证拒绝
        ))


def test_handler_context_fields_passthrough(tmp_path: Path):
    """
    HandlerContext 字段透传 (call_id 唯一, error 初始为 None); respond/reject 返回 str

    参数:
    - tmp_path: tmp路径
    """
    seen: dict[str, Any] = {}
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def probe(text: str, ctx: HandlerContext) -> str | None:
        seen["ctx"] = ctx
        return None

    session.add_handler(SessionHandler(name="p", before_user_send=probe))
    session.run("你好", img_urls=["http://x/a.png"], max_iterations=5)
    ctx = seen["ctx"]
    assert ctx.config.original_input == "你好"
    assert ctx.text == "你好"
    assert ctx.config.img_urls == ["http://x/a.png"]
    assert ctx.config.thinking == "off"
    assert ctx.config.max_iterations == 5
    assert ctx.config.call_id
    assert ctx.error is None
    assert ctx.outcome is None   # 正常路径无短路语义

    seen.clear()
    session.run("再来")
    assert seen["ctx"].config.call_id != ctx.config.call_id   # 每轮 call_id 唯一


def test_run_snapshot_semantics(tmp_path: Path):
    """
    run 内快照: 中途修改 handler 状态, 当次 run 行为不变, 下次生效

    参数:
    - tmp_path: tmp路径
    """
    calls: list[str] = []
    llm = _FakeLLM()
    session = _make_session(tmp_path, llm)

    def first(text: str, ctx: HandlerContext) -> str | None:
        calls.append("first")
        session.remove_handler("second")   # 中途移除
        return None

    def second(text: str, ctx: HandlerContext) -> str | None:
        calls.append("second")
        return None

    session.add_handler(SessionHandler(name="first", priority=1, before_user_send=first))
    session.add_handler(SessionHandler(name="second", priority=2, before_user_send=second))
    session.run("x")
    assert calls == ["first", "second"]   # 当次 run 快照仍执行 second

    session.run("y")
    assert calls == ["first", "second", "first"]   # 下次 run second 已移除


# ================= 第二轮修复新增测试 (全同步协议 / to_thread / 延迟 close / 并发) =================


def test_sync_install_rejects_async_handler_callbacks(tmp_path: Path):
    """
    同步插件安装路径拒绝异步回调 (与 add_handler 校验统一), 失败后无残留

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "bad"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: bad\n", encoding="utf-8")
    (plugin_dir / "handlers.py").write_text(
        "from satrap.edictum import SessionHandler\n\n"
        "async def before_user_send(text: str, ctx):\n"
        "    return text + '!'\n"
        "handlers = [SessionHandler(name='bad.h', before_user_send=before_user_send)]\n",
        encoding="utf-8",
    )
    session = _make_session(tmp_path)
    with pytest.raises(TypeError, match="异步回调"):
        session.install_plugin(str(plugin_dir))
    assert session.list_handlers() == []   # 回滚无残留
    assert session.list_plugins() == []


@pytest.mark.asyncio
async def test_async_install_rejects_async_handler_callbacks(tmp_path: Path):
    """
    异步插件安装路径同样拒绝异步回调 (全同步协议)

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "bad"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: bad\n", encoding="utf-8")
    (plugin_dir / "handlers.py").write_text(
        "from satrap.edictum import SessionHandler\n\n"
        "async def before_user_send(text: str, ctx):\n"
        "    return text + '!'\n"
        "handlers = [SessionHandler(name='bad.h', before_user_send=before_user_send)]\n",
        encoding="utf-8",
    )
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"),
    )
    with pytest.raises(TypeError, match="异步回调"):
        await session.install_plugin(str(plugin_dir))
    assert session.list_handlers() == []


def test_handler_error_policy_abort(tmp_path: Path):
    """
    error_policy="abort": handler 异常 -> run 抛 HandlerAbortError (与 HandlerResult.abort 同语义)

    参数:
    - tmp_path: tmp路径
    """
    seen: list[str] = []

    def boom(text: str, ctx: HandlerContext) -> str | None:
        raise RuntimeError("boom")

    def after(result: str | None, ctx: HandlerContext) -> str | None:
        seen.append("after")
        return None

    session = _make_session(tmp_path)
    session.add_handler(SessionHandler(name="p", error_policy="abort", before_user_send=boom))
    session.add_handler(SessionHandler(name="q", after_model_reply=after))
    with pytest.raises(HandlerAbortError):
        session.run("x")
    assert seen == []   # abort 在 before 阶段短路, after_model_reply 不执行 (与 HandlerResult.abort 一致)


def test_handler_error_policy_continue_default(tmp_path: Path):
    """
    error_policy 默认 continue: 异常隔离继续 (回归锚点)

    参数:
    - tmp_path: tmp路径
    """
    seen: list[str] = []

    def boom(text: str, ctx: HandlerContext) -> str | None:
        raise RuntimeError("boom")

    def next_h(text: str, ctx: HandlerContext) -> str | None:
        seen.append("next")
        return None

    session = _make_session(tmp_path)
    session.add_handler(SessionHandler(name="p", before_user_send=boom))
    session.add_handler(SessionHandler(name="q", priority=2, before_user_send=next_h))
    result = session.run("x")
    assert result == "回复"
    assert seen == ["next"]   # 隔离后后续 handler 继续


def test_handler_timeout_negative_raises(tmp_path: Path):
    """
    timeout 负数: add_handler 拒绝

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    with pytest.raises(ValueError, match="timeout"):
        session.add_handler(SessionHandler(name="p", timeout=-1))


def test_plugin_install_timeout_negative_raises(tmp_path: Path):
    """
    timeout 负数: 插件安装路径同样拒绝

    参数:
    - tmp_path: tmp路径
    """
    plugin_dir = tmp_path / "plugins" / "bad"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.yaml").write_text("name: bad\n", encoding="utf-8")
    (plugin_dir / "handlers.py").write_text(
        "from satrap.edictum import SessionHandler\n\n"
        "handlers = [SessionHandler(name='bad.h', timeout=-1)]\n",
        encoding="utf-8",
    )
    session = _make_session(tmp_path)
    with pytest.raises(ValueError, match="timeout"):
        session.install_plugin(str(plugin_dir))


def test_handler_result_invalid_action_raises():
    """HandlerResult 非法 action 构造时拒绝"""
    with pytest.raises(ValueError, match="action"):
        HandlerResult("unknown", "text")


def test_handler_invalid_return_value_warns(tmp_path: Path, caplog: Any):
    """
    before_user_send 非法返回值 -> warning 且行为不变

    参数:
    - tmp_path: tmp路径
    - caplog: pytest 日志捕获夹具
    """
    def bad(t: str, ctx: HandlerContext) -> bool:
        return False

    session = _make_session(tmp_path)
    session.add_handler(SessionHandler(name="p", before_user_send=cast(Any, bad)))
    with caplog.at_level(logging.WARNING):
        result = session.run("hi")
    assert result == "回复"
    assert any("不支持的类型" in r.message for r in caplog.records)


def test_handler_outcome_respond_and_reject(tmp_path: Path):
    """
    respond/reject 置位 ctx.outcome, after_model_reply 可见

    参数:
    - tmp_path: tmp路径
    """
    seen: list[str] = []

    def respond(text: str, ctx: HandlerContext) -> HandlerResult:
        return HandlerResult.respond("直接回复")

    def after(result: str | None, ctx: HandlerContext) -> str | None:
        seen.append(str(ctx.outcome))
        return None

    session = _make_session(tmp_path)
    session.add_handler(SessionHandler(
        name="p", before_user_send=respond, after_model_reply=after,
    ))
    assert session.run("x") == "直接回复"
    assert seen == ["respond"]

    def reject(text: str, ctx: HandlerContext) -> HandlerResult:
        return HandlerResult.reject("拒绝")

    seen2: list[str] = []

    def after2(result: str | None, ctx: HandlerContext) -> str | None:
        seen2.append(str(ctx.outcome))
        return None

    session2 = _make_session(tmp_path)
    session2.add_handler(SessionHandler(
        name="p", before_user_send=reject, after_model_reply=after2,
    ))
    assert session2.run("x") == "拒绝"
    assert seen2 == ["reject"]


def test_handler_config_frozen(tmp_path: Path):
    """
    HandlerConfig 冻结: handler 写 config 字段 -> FrozenInstanceError; ctx.text 可变

    参数:
    - tmp_path: tmp路径
    """
    frozen_seen: list[str] = []

    def tamper(text: str, ctx: HandlerContext) -> str | None:
        try:
            # 负向用例: 通过 Any 视图执行正常赋值路径以验证 frozen 拒绝
            cast(Any, ctx.config).original_input = "hacked"
        except Exception as e:   # noqa: BLE001
            frozen_seen.append(type(e).__name__)
        return text

    session = _make_session(tmp_path)
    session.add_handler(SessionHandler(name="p", before_user_send=tamper))
    assert session.run("hi") == "回复"
    assert frozen_seen == ["FrozenInstanceError"]


def test_sync_deferred_close_during_run(tmp_path: Path):
    """
    同步: run 中 remove_handler -> close 延迟到 run 结束冲刷

    参数:
    - tmp_path: tmp路径
    """
    closed: list[str] = []

    class _H(SessionHandler):
        def close(self) -> None:
            closed.append(self.name)

    session = _make_session(tmp_path)

    def remover(text: str, ctx: HandlerContext) -> str | None:
        assert session.remove_handler("victim") is True
        assert closed == []   # 延迟: remove 时未立即 close
        return None

    session.add_handler(SessionHandler(name="remover", priority=1, before_user_send=remover))
    session.add_handler(_H(name="victim", priority=2))
    session.run("x")
    assert closed == ["victim"]   # run 结束冲刷


def test_nested_run_no_pending_name_error(tmp_path: Path):
    """
    嵌套 run: 内层 finally 时 _active_runs 非 0, pending 须已初始化 (回归: NameError)

    参数:
    - tmp_path: tmp路径

    外层 run 的 handler 内触发另一 session 的嵌套 run, 外层 run 进行中 remove -> 延迟 close;
    外层 finally 减到 0 时冲刷, 嵌套路径不得因 pending 未定义而 NameError
    """
    closed: list[str] = []

    class _H(SessionHandler):
        def close(self) -> None:
            closed.append(self.name)

    inner = _make_session(tmp_path / "inner")
    outer = _make_session(tmp_path / "outer")

    def remover(text: str, ctx: HandlerContext) -> str | None:
        inner.run("x")   # 嵌套 run (各自 _active_runs 独立)
        outer.remove_handler("victim")   # 外层 run 进行中 remove -> 延迟 close
        return None

    outer.add_handler(SessionHandler(name="remover", priority=1, before_user_send=remover))
    outer.add_handler(_H(name="victim", priority=2))
    assert outer.run("hi") == "回复"
    assert closed == ["victim"]   # 外层 run 结束冲刷


@pytest.mark.asyncio
async def test_async_deferred_close_during_run(tmp_path: Path):
    """
    异步: run 挂起期间 remove_handler -> close 在 run 结束后执行 (to_thread close)

    参数:
    - tmp_path: tmp路径
    """
    closed: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class _GateLLM(_FakeAsyncLLM):
        async def call(self, *args: Any, **kwargs: Any) -> LLMCallResponse:   # noqa: ARG002
            entered.set()
            await release.wait()
            return self.response

    class _H(SessionHandler):
        def close(self) -> None:
            closed.append(self.name)

    session = AsyncSimpleSession(
        "conv-a", _GateLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_handler(_H(name="victim"))

    task = asyncio.create_task(session.run("hi"))
    await entered.wait()   # run 进入模型调用
    assert session.remove_handler("victim") is True
    assert closed == []   # run 进行中, close 延迟
    release.set()
    assert await task == "异步回复"
    assert closed == ["victim"]   # run 结束后冲刷


@pytest.mark.asyncio
async def test_async_handler_timeout_zero(tmp_path: Path):
    """
    timeout=0: 立即放弃等待, 隔离继续

    参数:
    - tmp_path: tmp路径
    """
    def slow(text: str, ctx: HandlerContext) -> str | None:
        time.sleep(0.2)
        return text

    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_handler(SessionHandler(name="slow", timeout=0, before_user_send=slow))
    assert await session.run("hi") == "异步回复"


@pytest.mark.asyncio
async def test_async_handler_to_thread_does_not_block_loop(tmp_path: Path):
    """
    to_thread: 同步 handler 阻塞不卡事件循环 (问题 6 根治验证)

    参数:
    - tmp_path: tmp路径
    """
    def slow(text: str, ctx: HandlerContext) -> str | None:
        time.sleep(0.3)
        return text

    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_handler(SessionHandler(name="slow", before_user_send=slow))
    loop_alive = asyncio.Event()

    async def tick() -> None:
        await asyncio.sleep(0.1)
        loop_alive.set()

    await asyncio.gather(session.run("hi"), tick())
    assert loop_alive.is_set()   # 事件循环在 handler 阻塞期间仍运转


@pytest.mark.asyncio
async def test_async_handler_timeout_then_next_handler(tmp_path: Path):
    """
    to_thread 超时: wait_for 放弃等待, 后续 handler 照常执行

    参数:
    - tmp_path: tmp路径
    """
    def slow(text: str, ctx: HandlerContext) -> str | None:
        time.sleep(0.5)
        return text

    def next_h(text: str, ctx: HandlerContext) -> str | None:
        return text + "!"

    llm = _FakeAsyncLLM()
    session = AsyncSimpleSession(
        "conv-a", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    session.add_handler(SessionHandler(name="slow", timeout=0.05, before_user_send=slow))
    session.add_handler(SessionHandler(name="next", priority=2, before_user_send=next_h))
    assert await session.run("hi") == "异步回复"
    user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[0]["content"] == "hi!"   # slow 超时隔离, next 改写生效


def test_remove_handler_syncs_plugin_roster(tmp_path: Path):
    """
    remove_handler 后插件名册同步, list_capabilities 不再显示

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    plugin = session.install_plugin(str(_write_plugin_dir(tmp_path)))
    assert "demo.handler" in plugin.handlers
    assert session.remove_handler("demo.handler") is True
    assert "demo.handler" not in plugin.handlers   # 名册同步
    caps = {c["name"] for c in plugin.list_capabilities()["handlers"]}
    assert "demo.handler" not in caps   # 能力表不再残留


def test_replace_handler_syncs_plugin_roster(tmp_path: Path):
    """
    replace=True 覆盖插件 handler -> 名册同步

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    plugin = session.install_plugin(str(_write_plugin_dir(tmp_path)))
    session.add_handler(
        SessionHandler(name="demo.handler", before_user_send=lambda t, ctx: t),
        replace=True,
    )
    assert "demo.handler" not in plugin.handlers
    assert session.list_handlers()[0].name == "demo.handler"


def test_registry_thread_concurrency(tmp_path: Path):
    """
    注册表线程并发: add/remove/list 不抛 RuntimeError (迭代安全)

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    errors: list[BaseException] = []

    def worker(base: int) -> None:
        try:
            for i in range(40):
                name = f"h{base}_{i}"
                session.add_handler(SessionHandler(name=name))
                session.list_handlers()
                session.remove_handler(name)
        except Exception as e:   # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(b,)) for b in (1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert session.list_handlers() == []


@pytest.mark.asyncio
async def test_async_run_serialized(tmp_path: Path):
    """
    异步 run 串行化: 并发提交的 run 顺序执行 (run_lock 排队, 不支持并发 run)

    参数:
    - tmp_path: tmp路径
    """
    order: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class _GateLLM(_FakeAsyncLLM):
        async def call(self, *args: Any, **kwargs: Any) -> LLMCallResponse:   # noqa: ARG002
            order.append("enter")
            entered.set()
            await release.wait()
            order.append("exit")
            return self.response

    session = AsyncSimpleSession(
        "conv-a", _GateLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    async def run_a() -> None:
        await session.run("a")
        order.append("a_done")

    async def run_b() -> None:
        await session.run("b")
        order.append("b_done")

    task_a = asyncio.create_task(run_a())
    await entered.wait()   # A 进入模型调用
    task_b = asyncio.create_task(run_b())
    await asyncio.sleep(0)   # 让 B 开始排队
    assert "b_done" not in order   # B 被串行化
    release.set()
    await asyncio.gather(task_a, task_b)
    assert order == ["enter", "exit", "a_done", "enter", "exit", "b_done"]


def test_sync_run_serialized_across_threads(tmp_path: Path):
    """
    同步 run 在多线程调用时按会话串行执行

    参数:
    - tmp_path: 临时目录
    """
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    class _GateLLM(_FakeLLM):
        def call(self, *args: object, **kwargs: object) -> LLMCallResponse:   # noqa: ARG002
            """
            阻塞模型调用以验证线程串行化

            参数:
            - args: 未使用的位置参数
            - kwargs: 未使用的关键字参数

            返回:
            - 预设模型响应
            """
            order.append("enter")
            entered.set()
            release.wait(timeout=2)
            order.append("exit")
            return self.response

    session = SimpleSession(
        "conv-sync-lock", _GateLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    def run(value: str) -> None:
        """
        执行一轮会话并记录完成顺序

        参数:
        - value: 当前线程输入
        """
        session.run(value)
        order.append(f"{value}_done")

    first = threading.Thread(target=run, args=("a",))
    second = threading.Thread(target=run, args=("b",))
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    assert "b_done" not in order
    release.set()
    first.join(timeout=3)
    second.join(timeout=3)
    assert order == ["enter", "exit", "a_done", "enter", "exit", "b_done"]


@pytest.mark.asyncio
async def test_async_concurrent_remove_and_run(tmp_path: Path):
    """
    并发 remove + run 竞态 (确定性时序): close 在 A 的 after_model_reply 后, B 执行前发生

    参数:
    - tmp_path: tmp路径
    """
    order: list[str] = []
    closed: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class _GateLLM(_FakeAsyncLLM):
        async def call(self, *args: Any, **kwargs: Any) -> LLMCallResponse:   # noqa: ARG002
            order.append("enter")
            entered.set()
            await release.wait()
            order.append("exit")
            return self.response

    class _H(SessionHandler):
        def close(self) -> None:
            closed.append(self.name)
            order.append("close")

    session = AsyncSimpleSession(
        "conv-a", _GateLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )

    def victim_after(result: str | None, ctx: HandlerContext) -> str | None:
        order.append("victim_after")   # A 的 after_model_reply 执行时 victim 资源仍可用
        return None

    session.add_handler(_H(name="victim", after_model_reply=victim_after))

    async def run_a() -> None:
        await session.run("a")
        order.append("a_done")

    async def run_b() -> None:
        await session.run("b")
        order.append("b_done")

    task_a = asyncio.create_task(run_a())
    await entered.wait()   # A 进入模型调用 (快照含 victim)
    assert session.remove_handler("victim") is True
    assert closed == []   # run 进行中 -> 延迟 close
    task_b = asyncio.create_task(run_b())   # B 在 run_lock 排队
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(task_a, task_b)
    assert closed == ["victim"]
    assert order == [
        "enter", "exit", "victim_after", "close", "a_done",
        "enter", "exit", "b_done",
    ]   # close 在 victim_after 之后 (A 用完资源), B 执行之前
