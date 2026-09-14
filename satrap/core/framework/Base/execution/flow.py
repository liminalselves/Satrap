"""
工作流共用的固定 Agent 状态转换

集中定义迭代上限与最终回答协议, 不执行 I/O 或提供插件 Hook
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generator

from satrap.core.utils.context import add_bot_message, add_tools_call_flow, clear_reasoning_content
from .errors import ModelProtocolError


@dataclass(frozen=True)
class ModelStep:
    """模型步骤的固定输入"""

    key: str
    messages: list[dict[str, Any]]
    final: bool = False


@dataclass(frozen=True)
class ToolStep:
    """工具步骤的固定输入"""

    key: str
    call: dict[str, Any]


def agent_flow(user_input: str | list[dict[str, Any]] | None, max_iterations: int) -> Generator[ModelStep | ToolStep, Any, tuple[list[dict[str, Any]], str]]:
    """
    生成普通与可恢复执行共用的模型和工具步骤

    参数:
    - user_input: 本轮用户输入, None 表示兼容执行器的用户消息已在历史中
    - max_iterations: 最大工具调用轮数, 非正数按 1 处理

    返回:
    - 接收步骤结果的生成器, 完成时返回本轮消息和最终回答, 最终阶段仍调用工具时抛出 ModelProtocolError
    """
    messages: list[dict[str, Any]] = [] if user_input is None else [{"role": "user", "content": user_input}]
    for iteration in range(max(1, max_iterations) + 1):
        final = iteration >= max(1, max_iterations)
        if final:
            messages.append({"role": "user", "content": "已达到最大工具调用尝试次数, 请基于已有信息给出最终答案"})
        response = yield ModelStep(f"model:{iteration}", messages, final)
        if response.get("type") != "tools_call" or not response.get("tool_calls"):
            content = response.get("content") or ""
            add_bot_message(messages, content, reasoning=response.get("thinking"))
            clear_reasoning_content(messages)
            return messages, content
        if final:
            raise ModelProtocolError("模型在禁用工具后仍返回工具调用")
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for index, call in enumerate(response["tool_calls"]):
            tool_message, tool_result = yield ToolStep(f"tool:{iteration}:{index}", call)
            calls.append(tool_message)
            results.append(tool_result)
        add_tools_call_flow(messages, response.get("content") or "", calls, results, response.get("thinking"))
    raise RuntimeError("任务未生成最终结果")
