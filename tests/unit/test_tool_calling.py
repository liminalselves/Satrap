import ast
import json
import os
import operator

import pytest

from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallResponse
from satrap.core.utils.TCBuilder import Tool, ToolsManager, create_tool_defined
from satrap.core.utils.context import ContextManager


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


def test_tool_manager_registers_definitions_and_executes_calls():
    manager = ToolsManager()
    manager.register_tool(WeatherTool())
    manager.register_tool(CalculatorTool())

    definitions = manager.get_tools_definitions()
    assert {item["function"]["name"] for item in definitions} == {"get_weather", "calculate"}

    tool_message, result = manager.execute_tool_call(
        {"name": "calculate", "arguments": {"expression": "2 + 3 * 4"}},
    )
    assert tool_message["type"] == "function"
    assert tool_message["function"]["name"] == "calculate"
    assert json.loads(tool_message["function"]["arguments"]) == {"expression": "2 + 3 * 4"}
    assert result["result"] == 14


def test_create_tool_defined_and_tool_call_are_compatible():
    definition = create_tool_defined(
        tool_name="search_web",
        description="搜索信息",
        params_dict={"query": ("string", "搜索关键词")},
    )

    assert definition["function"]["name"] == "search_web"
    assert definition["function"]["parameters"]["required"] == ["query"]


@pytest.mark.integration
@pytest.mark.requires_api
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
    context.add_system_message("你是一个可以调用天气和计算工具的助手")
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
        context.add_tool_call_flow(response.content, tool_messages, tool_results, response.thinking)
        final_response = llm.call(context.get_context())
        assert isinstance(final_response, LLMCallResponse)
        assert final_response.content.strip()
    else:
        assert response.content.strip()
