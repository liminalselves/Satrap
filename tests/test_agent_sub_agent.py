from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.type import LLMCallResponse
from satrap.core.utils.TCBuilder import ToolsManager, AsyncToolsManager
from satrap.expend.agent import AsyncSubAgent, AsyncSubAgentModel, SubAgent, SubAgentModel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLKIT_PATH = PROJECT_ROOT / ".toolkit" / "apikey.txt"


def _load_deepseek_config() -> dict[str, str]:
    """Parse .toolkit/apikey.txt and return the DeepSeek config block.

    The file contains multiple config blocks separated by blank lines.
    Each block is a set of `key: value` pairs. Returns the block whose
    base_url contains 'deepseek.com'.
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
    """Create sync/async LLM instances using the DeepSeek config from .toolkit"""
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
    def get_tool_defined(self):
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
    def get_tools_definitions(self):
        return [_FakeTool().get_tool_defined()]

    def execute_tool_call(self, call_info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        name = call_info.get("name", "")
        args_str = call_info.get("arguments", "{}")
        if isinstance(args_str, str):
            args = json.loads(args_str)
        else:
            args = args_str
        result = _FakeTool().execute(**args)
        msg = {"role": "tool", "content": json.dumps(result, ensure_ascii=False), "tool_call_id": call_info.get("id", "")}
        return msg, result


class _FakeLLM:
    def __init__(self):
        self.call_count = 0

    def call(self, messages, tools=None) -> LLMCallResponse | bool:
        self.call_count += 1
        return LLMCallResponse(type="message", content=f"fake reply #{self.call_count}")


class _FakeAsyncLLM:
    def __init__(self):
        self.call_count = 0

    async def call(self, messages, tools=None) -> LLMCallResponse | bool:
        self.call_count += 1
        return LLMCallResponse(type="message", content=f"async fake reply #{self.call_count}")


# ===============================================================
# Sync SubAgent tests (unit tests with fake LLM)
# ===============================================================


def test_sub_agent_parses_json_array_task():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)  # type: ignore[arg-type]

    result = agent.execute('["task one", "task two"]')
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "task one" in result
    assert "task two" in result


def test_sub_agent_parses_single_string_task():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)  # type: ignore[arg-type]

    result = agent.execute("single task description")
    assert "子代理1执行任务" in result
    assert "single task description" in result


def test_sub_agent_handles_empty_task_list():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)  # type: ignore[arg-type]

    result = agent.execute("[]")
    assert result == "未收到任何子任务"


def test_sub_agent_handles_malformed_json():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)  # type: ignore[arg-type]

    result = agent.execute("{bad json}")
    assert "子代理1执行任务" in result
    assert "{bad json}" in result


def test_sub_agent_preserves_task_order():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    agent = SubAgent(llm, tools_manager)  # type: ignore[arg-type]

    result = agent.execute('["first", "second", "third"]')
    first_pos = result.index("first")
    second_pos = result.index("second")
    third_pos = result.index("third")
    assert first_pos < second_pos < third_pos


# ===============================================================
# Async SubAgent tests (unit tests with fake async LLM)
# ===============================================================


async def _run_async_sub_agent(task_input: str) -> str:
    llm = _FakeAsyncLLM()
    tools_manager = _FakeToolsManager()
    agent = AsyncSubAgent(llm, tools_manager)  # type: ignore[arg-type]
    return await agent.execute(task_input)


async def test_async_sub_agent_parses_json_array_task():
    result = await _run_async_sub_agent('["async task a", "async task b"]')
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "async task a" in result
    assert "async task b" in result


async def test_async_sub_agent_parses_single_string():
    result = await _run_async_sub_agent("single async task")
    assert "子代理1执行任务" in result
    assert "single async task" in result


async def test_async_sub_agent_empty_list():
    result = await _run_async_sub_agent("[]")
    assert result == "未收到任何子任务"


async def test_async_sub_agent_malformed_json():
    result = await _run_async_sub_agent("{bad json}")
    assert "子代理1执行任务" in result
    assert "{bad json}" in result


async def test_async_sub_agent_invalid_type():
    result = await _run_async_sub_agent('"just a string"')
    assert "子代理1执行任务" in result
    assert "just a string" in result


# ===============================================================
# Integration tests with DeepSeek API (skip if toolkit config missing)
# ===============================================================


def test_sub_agent_with_deepseek():
    llm, _ = _build_deepseek_llm()
    if llm is None:
        return

    tools_mgr = ToolsManager()
    agent = SubAgent(llm, tools_mgr)
    result = agent.execute('["What is 2+2?", "What is the capital of France?"]')
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "2+2" in result or "4" in result or "capital" in result or "Paris" in result


async def test_async_sub_agent_with_deepseek():
    _, async_llm = _build_deepseek_llm()
    if async_llm is None:
        return

    tools_mgr = AsyncToolsManager()
    agent = AsyncSubAgent(async_llm, tools_mgr)
    result = await agent.execute('["What is 2+2?", "What is the capital of France?"]')
    assert "子代理1执行任务" in result
    assert "子代理2执行任务" in result
    assert "2+2" in result or "4" in result or "capital" in result or "Paris" in result


async def test_async_sub_agent_parallelism_with_deepseek():
    _, async_llm = _build_deepseek_llm()
    if async_llm is None:
        return

    tools_mgr = AsyncToolsManager()
    agent = AsyncSubAgent(async_llm, tools_mgr)
    tasks = json.dumps([f"What is {i} + {i}?" for i in range(1, 5)])
    result = await agent.execute(tasks)
    for i in range(1, 5):
        assert f"子代理{i}" in result
        assert str(i) in result


# ===============================================================
# Run all tests
# ===============================================================


def test_sub_agent_model_forward_returns_string():
    llm = _FakeLLM()
    tools_manager = _FakeToolsManager()
    model = SubAgentModel(llm, "test-id", tools_manager)  # type: ignore[arg-type]
    result = model.forward("some task")
    assert isinstance(result, str)
    assert len(result) > 0


async def test_async_sub_agent_model_forward_returns_string():
    llm = _FakeAsyncLLM()
    tools_manager = _FakeToolsManager()
    model = await AsyncSubAgentModel.create(llm, "test-async-id", tools_manager)  # type: ignore[arg-type]
    result = await model.forward("some async task")
    assert isinstance(result, str)
    assert len(result) > 0


async def run_all_unit_tests():
    """Run all unit tests"""
    print("=" * 60)
    print("Sync SubAgent unit tests")
    print("=" * 60)
    test_sub_agent_parses_json_array_task()
    print("  PASS: test_sub_agent_parses_json_array_task")
    test_sub_agent_parses_single_string_task()
    print("  PASS: test_sub_agent_parses_single_string_task")
    test_sub_agent_handles_empty_task_list()
    print("  PASS: test_sub_agent_handles_empty_task_list")
    test_sub_agent_handles_malformed_json()
    print("  PASS: test_sub_agent_handles_malformed_json")
    test_sub_agent_preserves_task_order()
    print("  PASS: test_sub_agent_preserves_task_order")
    test_sub_agent_model_forward_returns_string()
    print("  PASS: test_sub_agent_model_forward_returns_string")

    print("\n" + "=" * 60)
    print("Async SubAgent unit tests")
    print("=" * 60)
    await test_async_sub_agent_parses_json_array_task()
    print("  PASS: test_async_sub_agent_parses_json_array_task")
    await test_async_sub_agent_parses_single_string()
    print("  PASS: test_async_sub_agent_parses_single_string")
    await test_async_sub_agent_empty_list()
    print("  PASS: test_async_sub_agent_empty_list")
    await test_async_sub_agent_malformed_json()
    print("  PASS: test_async_sub_agent_malformed_json")
    await test_async_sub_agent_invalid_type()
    print("  PASS: test_async_sub_agent_invalid_type")
    await test_async_sub_agent_model_forward_returns_string()
    print("  PASS: test_async_sub_agent_model_forward_returns_string")


async def run_integration_tests():
    """Run integration tests with DeepSeek API (skipped if toolkit config missing)"""
    llm, async_llm = _build_deepseek_llm()
    if llm is None or async_llm is None:
        print("\n[SKIP] DeepSeek config not found in .toolkit/apikey.txt")
        return

    print("\n" + "=" * 60)
    print("Integration tests with DeepSeek API")
    print("=" * 60)
    test_sub_agent_with_deepseek()
    print("  PASS: test_sub_agent_with_deepseek")
    await test_async_sub_agent_with_deepseek()
    print("  PASS: test_async_sub_agent_with_deepseek")
    await test_async_sub_agent_parallelism_with_deepseek()
    print("  PASS: test_async_sub_agent_parallelism_with_deepseek")


async def main():
    await run_all_unit_tests()
    await run_integration_tests()
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
