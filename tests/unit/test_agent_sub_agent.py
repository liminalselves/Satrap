from __future__ import annotations

import satrap.expend.tools.agent as agent_module
from collections.abc import Callable
import threading
import pytest
from typing import Any, cast
import queue
import json
import sys
import time
from pathlib import Path

from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import ToolsManager
from satrap.expend.tools.agent import AsyncSubAgent, SubAgent
from satrap.core.type import LLMCallResponse
from support.casts import _as_async_llm, _as_async_tools_manager, _as_llm, _as_tools_manager


class _ThreadProcess:
    """使用线程承载 worker 的测试进程替身"""

    def __init__(
        self,
        target: Callable[..., None],
        args: tuple[object, ...],
    ) -> None:
        """初始化线程承载的 worker"""
        self._thread = threading.Thread(target=target, args=args, daemon=True)

    def start(self) -> None:
        """启动 worker 线程"""
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        """等待 worker 线程结束"""
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        """检查 worker 线程是否仍在运行"""
        return self._thread.is_alive()

    def terminate(self) -> None:
        """禁止普通逻辑测试误用无法模拟的进程终止能力"""
        raise AssertionError("线程进程替身不支持 terminate")

    def kill(self) -> None:
        """禁止普通逻辑测试误用无法模拟的进程强杀能力"""
        raise AssertionError("线程进程替身不支持 kill")


class _ThreadConnection:
    """使用队列模拟独立生命周期的双向 Pipe 端点"""

    def __init__(
        self,
        incoming: queue.Queue[object],
        outgoing: queue.Queue[object],
    ) -> None:
        """初始化队列连接端点"""
        self._incoming = incoming
        self._outgoing = outgoing

    def send(self, value: object) -> None:
        """向对端发送消息"""
        self._outgoing.put(value)

    def recv(self) -> object:
        """接收对端消息"""
        return self._incoming.get()

    def poll(self) -> bool:
        """检查是否存在待接收消息"""
        return not self._incoming.empty()

    def close(self) -> None:
        """关闭当前测试端点副本而不影响线程持有的端点"""


class _ThreadProcessContext:
    """提供队列 Pipe 和线程 Process 的测试上下文"""

    def Pipe(self, duplex: bool = True) -> tuple[_ThreadConnection, _ThreadConnection]:
        """创建双向队列管道"""
        assert duplex is True
        parent_incoming: queue.Queue[object] = queue.Queue()
        child_incoming: queue.Queue[object] = queue.Queue()
        return (
            _ThreadConnection(parent_incoming, child_incoming),
            _ThreadConnection(child_incoming, parent_incoming),
        )

    def Process(
        self,
        *,
        target: Callable[..., None],
        args: tuple[object, ...],
    ) -> _ThreadProcess:
        """创建线程承载的测试进程"""
        return _ThreadProcess(target, args)


class _ThreadMultiprocessing:
    """替换 agent 模块使用的 multiprocessing 入口"""

    def __init__(self) -> None:
        """初始化线程进程上下文"""
        self._context = _ThreadProcessContext()

    def get_context(self, method: str) -> _ThreadProcessContext:
        """获取线程进程上下文"""
        assert method == "spawn"
        return self._context


@pytest.fixture
def threaded_sub_agent_process_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """将同步子代理切换到线程承载的测试进程上下文"""
    monkeypatch.setattr(agent_module, "multiprocessing", _ThreadMultiprocessing())


@pytest.fixture
def isolated_spawn_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """子进程继承父进程 sys.path; 其它测试追加的插件临时目录含 mcp.py 会遮蔽标准库 mcp 包"""
    monkeypatch.setattr(
        sys,
        "path",
        [entry for entry in sys.path if not (entry and (Path(entry) / "mcp.py").is_file())],
    )


class _FakeTool:
    def get_tool_defined(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "echo input",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            },
        }

    def execute(self, text: str) -> dict[str, Any]:
        return {"result": f"echo: {text}"}


class _FakeToolsManager:
    def get_tools_definitions(self) -> list[dict[str, Any]]:
        return [_FakeTool().get_tool_defined()]

    def execute_tool_call(self, call_info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        name = call_info.get("name", "")
        args_str = call_info.get("arguments", "{}")
        if isinstance(args_str, str):
            args = json.loads(args_str)
        else:
            args = args_str
        result = _FakeTool().execute(**args)
        msg: dict[str, Any] = {"role": "tool", "content": json.dumps(result, ensure_ascii=False), "tool_call_id": call_info.get("id", "")}
        return msg, result


class _FakeLLM:
    def __init__(self):
        self.call_count = 0

    def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, img_urls: list[str] | None = None) -> LLMCallResponse | bool:
        self.call_count += 1
        return LLMCallResponse(type="message", content=f"fake reply #{self.call_count}")


class _SlowLLM:
    """持续阻塞的同步 LLM 替身"""

    def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        img_urls: list[str] | None = None,
    ) -> LLMCallResponse | bool:
        """阻塞足够长时间以触发子进程硬超时"""
        time.sleep(10)
        return LLMCallResponse(type="message", content="late")


class _ToolCallingLLM:
    """先请求工具再返回最终结果的同步 LLM 替身"""

    def __init__(self) -> None:
        """初始化调用计数"""
        self.call_count = 0

    def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        img_urls: list[str] | None = None,
    ) -> LLMCallResponse | bool:
        """首次调用请求 echo 工具, 后续调用返回最终结果"""
        self.call_count += 1
        if self.call_count == 1:
            return LLMCallResponse(
                type="tools_call",
                content="",
                tool_calls=[{
                    "name": "echo",
                    "id": "call-proxy",
                    "arguments": {"text": "from child"},
                }],
            )
        return LLMCallResponse(type="message", content="proxy complete")


class _RecordingToolsManager(_FakeToolsManager):
    """记录父进程工具调用次数的工具管理器"""

    def __init__(self) -> None:
        """初始化调用计数"""
        self.call_count = 0

    def execute_tool_call(
        self,
        call_info: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """记录并执行工具调用"""
        self.call_count += 1
        arguments = call_info.get("arguments", {})
        parsed_arguments = cast(dict[str, object], arguments) if isinstance(arguments, dict) else {}
        result = _FakeTool().execute(text=str(parsed_arguments.get("text", "")))
        return ToolsManager.create_call_message(call_info), result


class _FakeAsyncLLM:
    def __init__(self):
        self.call_count = 0

    async def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, img_urls: list[str] | None = None) -> LLMCallResponse | bool:
        self.call_count += 1
        return LLMCallResponse(type="message", content=f"async fake reply #{self.call_count}")


# ================================================================
# 同步 / 异步 SubAgent 解析族测试 (共用 _parse_tasks 纯函数)
# ================================================================


async def _execute_sub_agent(task_input: str | list[str], asynchronous: bool) -> str:
    """按同步或异步实现执行一次子代理任务"""
    if asynchronous:
        agent = AsyncSubAgent(_as_async_llm(_FakeAsyncLLM()), _as_async_tools_manager(_FakeToolsManager()))
        return await agent.execute(task_input)
    agent = SubAgent(_as_llm(_FakeLLM()), _as_tools_manager(_FakeToolsManager()))
    return agent.execute(task_input)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sub_agent_parses_json_array_task(
    threaded_sub_agent_process_context: None,
    asynchronous: bool,
) -> None:
    result = await _execute_sub_agent('["task one", "task two"]', asynchronous)
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "task one" in result
    assert "task two" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sub_agent_accepts_native_task_array(
    threaded_sub_agent_process_context: None,
    asynchronous: bool,
) -> None:
    result = await _execute_sub_agent(["task one", "task two"], asynchronous)
    assert "子代理1执行任务" in result and "子代理2执行任务" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sub_agent_parses_single_string_task(
    threaded_sub_agent_process_context: None,
    asynchronous: bool,
) -> None:
    result = await _execute_sub_agent("single task description", asynchronous)
    assert "子代理1执行任务" in result
    assert "single task description" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sub_agent_handles_empty_task_list(asynchronous: bool) -> None:
    assert await _execute_sub_agent("[]", asynchronous) == "未收到任何子任务"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sub_agent_rejects_excessive_task_count(asynchronous: bool) -> None:
    """同步与异步子代理拒绝超过固定上限的批量任务"""
    result = await _execute_sub_agent(json.dumps([f"task-{index}" for index in range(17)]), asynchronous)
    assert "不能超过 16" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("task_input", "expected_text"),
    [("{bad json}", "{bad json}"), ('"just a string"', "just a string")],
)
async def test_sub_agent_handles_malformed_or_plain_string_input(
    threaded_sub_agent_process_context: None,
    asynchronous: bool,
    task_input: str,
    expected_text: str,
) -> None:
    """非法 JSON 与裸 JSON 字符串都降级为单任务"""
    result = await _execute_sub_agent(task_input, asynchronous)
    assert "子代理1执行任务" in result
    assert expected_text in result


@pytest.mark.asyncio
async def test_sub_agent_preserves_task_order(
    threaded_sub_agent_process_context: None,
) -> None:
    result = await _execute_sub_agent('["first", "second", "third"]', asynchronous=False)
    first_pos = result.index("first")
    second_pos = result.index("second")
    third_pos = result.index("third")
    assert first_pos < second_pos < third_pos


def test_sub_agent_hard_timeout_terminates_worker_process(isolated_spawn_path: None) -> None:
    """同步子代理超时后应终止工作进程并及时返回"""
    agent = SubAgent(
        cast(LLM, _SlowLLM()),
        cast(ToolsManager, _FakeToolsManager()),
        task_timeout=0.5,
        max_workers=1,
    )
    started_at = time.monotonic()

    result = agent.execute('["slow task"]')

    assert time.monotonic() - started_at < 3
    assert "超过 0.5 秒" in result


def test_sub_agent_does_not_serialize_tools_manager_runtime_state(isolated_spawn_path: None) -> None:
    """进程隔离只传递工具定义, 不要求真实工具管理器可序列化"""
    tools_manager = _FakeToolsManager()
    setattr(tools_manager, "runtime_lock", threading.Lock())
    agent = SubAgent(
        cast(LLM, _FakeLLM()),
        cast(ToolsManager, tools_manager),
    )

    result = agent.execute('["task"]')

    assert "子代理1执行任务" in result
    assert "fake reply" in result


def test_sub_agent_forwards_tool_calls_to_parent_manager(isolated_spawn_path: None) -> None:
    """隔离子进程应把真实工具执行转发回父进程"""
    tools_manager = _RecordingToolsManager()
    agent = SubAgent(
        cast(LLM, _ToolCallingLLM()),
        cast(ToolsManager, tools_manager),
    )

    result = agent.execute('["use tool"]')

    assert tools_manager.call_count == 1
    assert "proxy complete" in result

