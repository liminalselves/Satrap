import ast
import json
import operator
from typing import Any, cast

import pytest

from satrap.core.utils.TCBuilder import Tool, ToolsManager, create_tool_defined


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

    def execute(self, city: str, unit: str = "celsius") -> dict[str, Any]:
        return {"city": city, "unit": unit, "condition": "晴天", "temperature": 25}


class CalculatorTool(Tool):
    def __init__(self):
        super().__init__(
            tool_name="calculate",
            description="执行基本的数学计算",
            params_dict={"expression": ("string", "数学表达式")},
        )

    def execute(self, expression: str) -> dict[str, Any]:
        try:
            operators: dict[type, Any] = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: getattr(operator, "truediv"),
            }
            tree = ast.parse(expression, mode="eval")

            def evaluate(node: ast.AST) -> int | float:
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

