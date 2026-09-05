from __future__ import annotations

import argparse
import operator
from pathlib import Path
from typing import Any, cast
import ast
import sys

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from satrap import LLM, ModelWorkflowFramework, Tool, ToolsManager
from satrap.core.framework.BackGroundManager import ModelConfigManager


class SafeCalculatorTool(Tool):
    tool_name = "calculate"
    description = "安全计算四则运算和乘方, 不执行任意代码"
    params_dict = {
        "expression": ("string", "只包含数字和 + - * / ** % 以及括号的数学表达式"),
    }

    _binary_ops: dict[type, Any] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: getattr(operator, "truediv"),
        ast.Pow: getattr(operator, "pow"),
        ast.Mod: operator.mod,
    }
    _unary_ops: dict[type, Any] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    def execute(self, expression: str) -> dict[str, Any]:
        try:
            tree = ast.parse(expression, mode="eval")
            result = self._evaluate(tree.body)
            if isinstance(result, float) and not result.is_integer():
                value: int | float = result
            else:
                value = int(result)
            print(f"\n[工具 calculate] {expression} = {value}")
            return {"expression": expression, "result": value}
        except Exception as exc:
            print(f"\n[工具 calculate] 计算失败: {exc}")
            return {"expression": expression, "error": str(exc)}

    @classmethod
    def _evaluate(cls, node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in cls._binary_ops:
            left = cls._evaluate(node.left)
            right = cls._evaluate(node.right)
            return cls._binary_ops[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in cls._unary_ops:
            return cls._unary_ops[type(node.op)](cls._evaluate(node.operand))
        raise ValueError("只允许数字、括号和基本算术运算")


class WeatherTool(Tool):
    tool_name = "get_weather"
    description = "查询演示天气数据, 用于测试 Agent 的工具调用"
    params_dict = {
        "city": ("string", "城市名称, 支持北京、上海、广州、beijing、shanghai、guangzhou"),
        "unit": ("string", "温度单位, 可选 celsius 或 fahrenheit"),
    }

    _weather = {
        "北京": (25, "晴天", 45),
        "beijing": (25, "晴天", 45),
        "上海": (28, "多云", 60),
        "shanghai": (28, "多云", 60),
        "广州": (32, "雷阵雨", 80),
        "guangzhou": (32, "雷阵雨", 80),
    }

    def execute(self, city: str, unit: str = "celsius") -> dict[str, Any]:
        key = city.strip().lower()
        if key not in self._weather:
            result = {"city": city, "error": "演示数据中没有这个城市"}
            print(f"\n[工具 get_weather] {result}")
            return result

        temp, condition, humidity = self._weather[key]
        normalized_unit = unit.strip().lower()
        if normalized_unit == "fahrenheit":
            temp = round(temp * 9 / 5 + 32, 1)
        elif normalized_unit != "celsius":
            result = {"city": city, "error": "unit 只能是 celsius 或 fahrenheit"}
            print(f"\n[工具 get_weather] {result}")
            return result

        result: dict[str, Any] = {
            "city": city,
            "temperature": temp,
            "unit": normalized_unit,
            "condition": condition,
            "humidity": humidity,
        }
        print(f"\n[工具 get_weather] {result}")
        return result


def _is_deepseek_v4_flash(model: str | None) -> bool:
    normalized = (model or "").casefold().replace("-", "").replace("_", "")
    return "deepseekv4flash" in normalized


def load_llm_config(profile: str | None) -> tuple[str, Any]:
    manager = ModelConfigManager()
    if profile:
        config = manager.get_llm_config(profile)
        selected = profile
    else:
        config = manager.get_llm_config("default")
        selected = "default"
        if not _is_deepseek_v4_flash(config.model):
            for name, payload in manager.list_llm_configs(mask_api_key=False).items():
                if _is_deepseek_v4_flash(payload.get("model")):
                    selected = name
                    config = manager.get_llm_config(name)
                    break

    if not config.api_key:
        raise RuntimeError(f"模型配置 {selected!r} 没有 api_key")
    if not config.base_url:
        raise RuntimeError(f"模型配置 {selected!r} 没有 base_url")
    if not config.model:
        raise RuntimeError(f"模型配置 {selected!r} 没有 model")
    return selected, config


def build_agent(
    profile: str | None,
    conversation_id: str,
    content_callback: Any = None,
    thinking_callback: Any = None,
) -> ModelWorkflowFramework:
    selected, config = load_llm_config(profile)
    print(f"[模型] profile={selected}, model={config.model}, base_url={config.base_url}")

    llm = LLM(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        temperature=config.temperature if config.temperature is not None else 0.2,
        top_p=config.top_p if config.top_p is not None else 0.95,
        max_tokens=config.max_tokens if config.max_tokens is not None else 380000,
        timeout=120,
        suppress_error=False,
    )

    tools = ToolsManager()
    tools.register_tool(SafeCalculatorTool())
    tools.register_tool(WeatherTool())

    agent = ModelWorkflowFramework(
        llm=llm,
        context_id=conversation_id,
        tools_manager=tools,
        content_callback=content_callback,
        return_thinking=True,
        thinking_callback=thinking_callback,
        system_prompt=(
            "你是一个简洁可靠的中文助手。"
            "涉及数学计算时必须调用 calculate 工具。"
            "涉及天气时必须调用 get_weather 工具。"
            "只能根据工具返回结果回答, 不要编造工具结果。"
        ),
    )
    agent.ctx.del_context()
    print(f"[上下文] 已清空: {conversation_id}")
    return agent


def run_once(
    agent: ModelWorkflowFramework,
    message: str,
    clear: bool = False,
    stream: bool = True,
) -> str:
    if clear:
        agent.ctx.del_context()
    if stream:
        return agent.stream_full_agent(
            message,
            callback=True,
            max_iterations=5,
            thinking="medium",
        )
    return agent.full_agent(message, callback=False, max_iterations=5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Satrap DeepSeek v4 flash Agent 工具调用演示")
    parser.add_argument("--message", help="单次发送的消息")
    parser.add_argument("--profile", help=".satrap/model_config.json 中的 LLM 配置名")
    parser.add_argument("--conversation-id", default="agent_demo", help="上下文 ID")
    parser.add_argument("--clear", action="store_true", help="再次清空当前上下文, 启动时默认已清空")
    parser.add_argument("--repl", action="store_true", help="进入交互模式")
    parser.add_argument("--no-stream", action="store_true", help="使用非流式 full_agent 对比测试")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        stream = not args.no_stream
        output_state = {"thinking_started": False, "answer_started": False}

        def reset_output_state() -> None:
            output_state["thinking_started"] = False
            output_state["answer_started"] = False

        def content_callback(content: str) -> None:
            if not output_state["answer_started"]:
                if output_state["thinking_started"]:
                    print("\n")
                print("[回答] ", end="", flush=True)
                output_state["answer_started"] = True
            print(content, end="", flush=True)

        def thinking_callback(content: str) -> None:
            if not output_state["thinking_started"]:
                print("[思考] ", end="", flush=True)
                output_state["thinking_started"] = True
            print(content, end="", flush=True)

        agent = build_agent(
            args.profile,
            args.conversation_id,
            content_callback=content_callback,
            thinking_callback=thinking_callback,
        )
        if args.message:
            print(f"[用户] {args.message}")
            if stream:
                reset_output_state()
                print("[助手] ", end="", flush=True)
                answer = run_once(agent, args.message, args.clear, stream=True)
                if not output_state["answer_started"]:
                    print(f"[回答] {answer}", end="", flush=True)
                print()
            else:
                print(f"[助手] {run_once(agent, args.message, args.clear, stream=False)}")
            return 0

        if args.repl or not args.message:
            print("输入问题, 输入 /clear 清空上下文, 输入 /quit 退出")
            first = True
            while True:
                try:
                    message = input("\n你> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if not message:
                    continue
                if message in {"/quit", "/exit"}:
                    return 0
                if message == "/clear":
                    agent.ctx.del_context()
                    print("[上下文] 已清空")
                    continue
                if stream:
                    reset_output_state()
                    print("[助手] ", end="", flush=True)
                    answer = run_once(agent, message, args.clear and first, stream=True)
                    if not output_state["answer_started"]:
                        print(f"[回答] {answer}", end="", flush=True)
                    print()
                else:
                    print(f"[助手] {run_once(agent, message, args.clear and first, stream=False)}")
                first = False
    except Exception as exc:
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
