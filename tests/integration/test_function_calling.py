"""真实 LLM Function Call 集成测试

需要设置 TEST_LLM_API_KEY (可选 TEST_LLM_BASE_URL / TEST_LLM_MODEL) 后运行:
pytest tests/integration/test_function_calling.py --run-integration
"""

from __future__ import annotations

import ast
import operator
import os

import pytest

from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallResponse
from satrap.core.utils.TCBuilder import Tool, ToolsManager
from satrap.core.utils.context import ContextManager

pytestmark = [pytest.mark.integration, pytest.mark.requires_api]


class WeatherTool(Tool):
    def __init__(self):
        super().__init__(
            tool_name="get_weather",
            description="获取指定城市的天气信息",
            params_dict={
                "city": ("string", "要查询的城市名称"),
                "unit": ("string", "温度单位"),
            },
        )

    def execute(self, city: str, unit: str = "celsius"):
        return {"city": city, "unit": unit, "condition": "晴天", "temperature": 25}


class CalculatorTool(Tool):
    def __init__(self):
        super().__init__(
            tool_name="calculate",
            description="执行基本的数学计算",
            params_dict={"expression": ("string", "数学表达式")},
        )

    def execute(self, expression: str):
        try:
            operators = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
            }
            tree = ast.parse(expression, mode="eval")

            def evaluate(node):
                if isinstance(node, ast.Expression):
                    return evaluate(node.body)
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                    return node.value
                if isinstance(node, ast.BinOp) and type(node.op) in operators:
                    return operators[type(node.op)](evaluate(node.left), evaluate(node.right))
                raise ValueError("不支持的表达式")

            result = evaluate(tree)
            return {"expression": expression, "result": result}
        except Exception as exc:
            return {"expression": expression, "error": str(exc)}


def test_function_call_with_real_llm():
    api_key = os.getenv("TEST_LLM_API_KEY")
    base_url = os.getenv("TEST_LLM_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("TEST_LLM_MODEL", "deepseek-chat")
    if not api_key:
        pytest.skip("设置 TEST_LLM_API_KEY 后运行真实 Function Call 测试")

    manager = ToolsManager()
    manager.register_tool(WeatherTool())
    manager.register_tool(CalculatorTool())
    context = ContextManager(
        conversation_id="test_function_call_001",
        keep_in_memory=True,
    )
    context.reset_system_prompt("你是一个可以调用天气和计算工具的助手")
    context.add_user_message("请调用 calculate 计算 2 + 3 * 4")

    llm = LLM(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.7,
        max_tokens=1000,
        suppress_error=False,
    )
    response = llm.call(
        context.get_context(),
        tools=manager.get_tools_definitions(),
        tool_choice="auto",
    )

    assert isinstance(response, LLMCallResponse)
    if response.type == "tools_call":
        tool_messages = []
        tool_results = []
        for call in response.tool_calls or []:
            message, result = manager.execute_tool_call(call)
            tool_messages.append(message)
            tool_results.append(result)
        context.add_tool_call_flow(response.content, tool_messages, tool_results)
        final_response = llm.call(context.get_context())
        assert isinstance(final_response, LLMCallResponse)
        assert final_response.content.strip()
    else:
        assert response.content.strip()
