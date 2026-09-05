import operator
from typing import Any
import json
import ast

from satrap.core.utils.TCBuilder import Tool, ToolsManager, create_tool_defined
from satrap.core.utils import safe_parse_arguments


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


# ================= safe_parse_arguments 容错解析 =================


def test_safe_parse_arguments_standard_json():
    """标准 JSON 字符串直接解析"""
    assert safe_parse_arguments('{"path": "a.md", "content": "hi"}') == {
        "path": "a.md", "content": "hi",
    }


def test_safe_parse_arguments_unescaped_newline_in_value():
    """值内裸换行 (模型常见输出, 如多行 markdown) -> 修复"""
    raw = '{"path": "log.md", "content": "第一行\n第二行```markdown\n代码块```"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"path": "log.md", "content": "第一行\n第二行```markdown\n代码块```"}


def test_safe_parse_arguments_unescaped_backslash_path():
    """Windows 路径未转义反斜杠 (如 F 盘 work 目录) -> 修复"""
    raw = '{"path": "F:\\work\\Satrap\\a.txt", "content": "x"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"path": "F:\\work\\Satrap\\a.txt", "content": "x"}


def test_safe_parse_arguments_backslash_escape_like_dir_names():
    """路径段首字母是转义符字母 (C:\new, C:\bin) -> 按字面路径修复, 不误解码为控制字符"""
    raw = '{"path": "C:\\new\\bin\\x", "content": "y"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"path": "C:\\new\\bin\\x", "content": "y"}


def test_safe_parse_arguments_unescaped_quote_in_value():
    """值内裸引号 (如他说"好的") -> 修复; 边界: 内容以裸引号结尾时尾引号可能丢失 (信息歧义)"""
    raw = '{"content": "他说"好的"然后走了", "path": "a.md"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"content": "他说\"好的\"然后走了", "path": "a.md"}


def test_safe_parse_arguments_code_field_still_repaired():
    """原 code 字段修复场景回归 (含未转义引号的代码块)"""
    raw = '{"code": "print(\"hi\")\nprint(1)", "name": "demo"}'
    parsed = safe_parse_arguments(raw)
    assert parsed == {"code": 'print("hi")\nprint(1)', "name": "demo"}


def test_safe_parse_arguments_bool_and_number_values_untouched():
    """非字符串值 (布尔/数字) 不被修复逻辑破坏"""
    raw = '{"append": true, "count": 3, "content": "x"}'
    assert safe_parse_arguments(raw) == {"append": True, "count": 3, "content": "x"}


def test_safe_parse_arguments_unparseable_returns_empty():
    """完全无法解析 -> 空字典 (不抛异常)"""
    assert safe_parse_arguments("这不是 JSON") == {}


def test_safe_parse_arguments_dict_passthrough():
    """dict 输入原样返回"""
    arg = {"path": "a.md"}
    assert safe_parse_arguments(arg) is arg

