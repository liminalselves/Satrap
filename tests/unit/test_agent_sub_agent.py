from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any, cast

import pytest

from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.type import LLMCallResponse
from satrap.core.utils.TCBuilder import ToolsManager, AsyncToolsManager
from satrap.expend.tools.agent import AsyncSubAgent, AsyncSubAgentModel, SubAgent, SubAgentModel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLKIT_PATH = PROJECT_ROOT / ".toolkit" / "apikey.txt"


def _load_deepseek_config() -> dict[str, str]:
    """
    解析 .toolkit/apikey.txt 并返回 DeepSeek 配置块

    文件包含多个由空行分隔的配置块
    每个配置块由一组 `key: value` 键值对组成, 返回 base_url 包含 'deepseek.com' 的配置块

    返回:
    - dict[str, str]:  DeepSeek 配置块
    """
    if not TOOLKIT_PATH.exists():
        return {}
    text = TOOLKIT_PATH.read_text(encoding="utf-8")
    blocks = text.strip().split("\n\n")
    for block in blocks:
        cfg: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            cfg[key.strip().lower()] = value.strip()
        if "deepseek.com" in cfg.get("base url", ""):
            return cfg.copy()
    return {}


def _build_deepseek_llm() -> tuple[LLM | None, AsyncLLM | None]:
    """
    使用 .toolkit 中的 DeepSeek 配置创建同步和异步 LLM 实例

    返回:
    - tuple[LLM | None, AsyncLLM | None]: 使用 .toolkit 中的 DeepSeek 配置创建同步和异步 LLM 实例
    """
    cfg = _load_deepseek_config()
    api_key = cfg.get("api key", "")
    base_url = cfg.get("base url", "")
    model = cfg.get("model", "")
    if not api_key or not model or not base_url:
        print("[SKIP] DeepSeek API config not found in .toolkit/apikey.txt")
        return None, None
    if not model.startswith("deepseek"):
        print(f"[SKIP] model '{model}' is not a DeepSeek model")
        return None, None
    print(f"  Using DeepSeek API: {model} @ {base_url}")
    llm = LLM(api_key=api_key, base_url=base_url, model=model, temperature=0.7, max_tokens=4096)
    async_llm = AsyncLLM(api_key=api_key, base_url=base_url, model=model, temperature=0.7, max_tokens=4096)
    return llm, async_llm


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
        """
        阻塞足够长时间以触发子进程硬超时

        参数:
        - messages: 对话消息
        - tools: 可选工具定义
        - img_urls: 可选图片 URL

        返回:
        - 不应在测试超时前返回的模型响应
        """
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
        """
        首次调用请求 echo 工具, 后续调用返回最终结果

        参数:
        - messages: 对话消息
        - tools: 可选工具定义
        - img_urls: 可选图片 URL

        返回:
        - 工具调用或最终模型响应
        """
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
        """
        记录并执行工具调用

        参数:
        - call_info: 工具调用信息

        返回:
        - 工具消息与工具结果
        """
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
# 同步 SubAgent 测试 (使用 fake LLM 的单元测试)
# ================================================================


def test_sub_agent_parses_json_array_task():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)   # type: ignore[arg-type]

    result = agent.execute('["task one", "task two"]')
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "task one" in result
    assert "task two" in result


def test_sub_agent_parses_single_string_task():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)   # type: ignore[arg-type]

    result = agent.execute("single task description")
    assert "子代理1执行任务" in result
    assert "single task description" in result


def test_sub_agent_handles_empty_task_list():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)   # type: ignore[arg-type]

    result = agent.execute("[]")
    assert result == "未收到任何子任务"


def test_sub_agent_rejects_excessive_task_count():
    """同步子代理拒绝超过固定上限的批量任务"""
    agent = SubAgent(_FakeLLM(), _FakeToolsManager())   # type: ignore[arg-type]
    result = agent.execute(json.dumps([f"task-{index}" for index in range(17)]))
    assert "不能超过 16" in result


def test_sub_agent_handles_malformed_json():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)   # type: ignore[arg-type]

    result = agent.execute("{bad json}")
    assert "子代理1执行任务" in result
    assert "{bad json}" in result


def test_sub_agent_preserves_task_order():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)   # type: ignore[arg-type]

    result = agent.execute('["first", "second", "third"]')
    first_pos = result.index("first")
    second_pos = result.index("second")
    third_pos = result.index("third")
    assert first_pos < second_pos < third_pos


def test_sub_agent_hard_timeout_terminates_worker_process() -> None:
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


def test_sub_agent_does_not_serialize_tools_manager_runtime_state() -> None:
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


def test_sub_agent_forwards_tool_calls_to_parent_manager() -> None:
    """隔离子进程应把真实工具执行转发回父进程"""
    tools_manager = _RecordingToolsManager()
    agent = SubAgent(
        cast(LLM, _ToolCallingLLM()),
        cast(ToolsManager, tools_manager),
    )

    result = agent.execute('["use tool"]')

    assert tools_manager.call_count == 1
    assert "proxy complete" in result


# ================================================================
# 异步 SubAgent 测试 (使用 fake 异步 LLM 的单元测试)
# ================================================================


async def _run_async_sub_agent(task_input: str) -> str:
    llm = _FakeAsyncLLM()
    tools_manager = _FakeToolsManager()
    agent = AsyncSubAgent(llm, tools_manager)   # type: ignore[arg-type]
    return await agent.execute(task_input)


@pytest.mark.asyncio
async def test_async_sub_agent_parses_json_array_task():
    result = await _run_async_sub_agent('["async task a", "async task b"]')
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "async task a" in result
    assert "async task b" in result


@pytest.mark.asyncio
async def test_async_sub_agent_parses_single_string():
    result = await _run_async_sub_agent("single async task")
    assert "子代理1执行任务" in result
    assert "single async task" in result


@pytest.mark.asyncio
async def test_async_sub_agent_empty_list():
    result = await _run_async_sub_agent("[]")
    assert result == "未收到任何子任务"


@pytest.mark.asyncio
async def test_async_sub_agent_rejects_excessive_task_count():
    """异步子代理拒绝超过固定上限的批量任务"""
    agent = AsyncSubAgent(_FakeAsyncLLM(), _FakeToolsManager())   # type: ignore[arg-type]
    result = await agent.execute(json.dumps([f"task-{index}" for index in range(17)]))
    assert "不能超过 16" in result


@pytest.mark.asyncio
async def test_async_sub_agent_malformed_json():
    result = await _run_async_sub_agent("{bad json}")
    assert "子代理1执行任务" in result
    assert "{bad json}" in result


@pytest.mark.asyncio
async def test_async_sub_agent_invalid_type():
    result = await _run_async_sub_agent('"just a string"')
    assert "子代理1执行任务" in result
    assert "just a string" in result


# ================================================================
# DeepSeek API 集成测试 (缺少 toolkit 配置时跳过)
# ================================================================


@pytest.mark.integration
@pytest.mark.requires_api
def test_sub_agent_with_deepseek():
    llm, _ = _build_deepseek_llm()
    if llm is None:
        pytest.skip(".toolkit/apikey.txt 中没有可用的 DeepSeek 配置")

    tools_mgr = ToolsManager()
    agent = SubAgent(llm, tools_mgr)
    result = agent.execute('["What is 2+2?", "What is the capital of France?"]')
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "2+2" in result or "4" in result or "capital" in result or "Paris" in result


@pytest.mark.integration
@pytest.mark.requires_api
@pytest.mark.asyncio
async def test_async_sub_agent_with_deepseek():
    _, async_llm = _build_deepseek_llm()
    if async_llm is None:
        pytest.skip(".toolkit/apikey.txt 中没有可用的 DeepSeek 配置")

    tools_mgr = AsyncToolsManager()
    agent = AsyncSubAgent(async_llm, tools_mgr)
    result = await agent.execute('["What is 2+2?", "What is the capital of France?"]')
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "2+2" in result or "4" in result or "capital" in result or "Paris" in result


@pytest.mark.integration
@pytest.mark.requires_api
@pytest.mark.asyncio
async def test_async_sub_agent_parallelism_with_deepseek():
    _, async_llm = _build_deepseek_llm()
    if async_llm is None:
        pytest.skip(".toolkit/apikey.txt 中没有可用的 DeepSeek 配置")

    tools_mgr = AsyncToolsManager()
    agent = AsyncSubAgent(async_llm, tools_mgr)
    tasks = json.dumps([f"What is {i} + {i}?" for i in range(1, 5)])
    result = await agent.execute(tasks)
    for i in range(1, 5):
        assert f"子代理{i}" in result
        assert str(i) in result


def test_sub_agent_model_forward_returns_string():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    model = SubAgentModel(llm, "test-id", tools_manager)   # type: ignore[arg-type]
    result = model.forward("some task")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_async_sub_agent_model_forward_returns_string():
    llm = _FakeAsyncLLM()
    tools_manager = _FakeToolsManager()
    model = await AsyncSubAgentModel.create(llm, "test-async-id", tools_manager)
    result = await model.forward("some async task")
    assert isinstance(result, str)
    assert len(result) > 0
