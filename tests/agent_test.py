from satrap.core.framework import ModelWorkflowFramework
import json
from typing import Dict, Any, Tuple, List

from satrap.core.utils.TCBuilder import Tool, create_tool_defined, ToolsManager
from satrap.core.APICall.LLMCall import LLM, parse_call_response
from satrap.core.utils.context import ContextManager

from satrap import logger

class WeatherTool(Tool):
    """天气查询工具示例"""
    
    def __init__(self):
        super().__init__(
            tool_name="get_weather",
            description="获取指定城市的天气信息",
            params_dict={
                "city": ("string", "要查询天气的城市名称, 使用英文或拼音"),
                "unit": ("string", "温度单位，可选 'celsius' 或 'fahrenheit'")
            }
        )
    
    def execute(self, city: str, unit: str = "celsius") -> Dict[str, Any]:
        """
        执行天气查询（模拟实现）
        
        实际应用中，这里应该调用真实的天气 API
        """
        # 模拟天气数据
        weather_data = {
            "beijing": {"temp": 25, "condition": "晴天", "humidity": 45},
            "shanghai": {"temp": 28, "condition": "多云", "humidity": 60},
            "guangzhou": {"temp": 32, "condition": "雷阵雨", "humidity": 80},
        }

        city_lower = city.lower()
        if city_lower in weather_data:
            data = weather_data[city_lower]
            if unit == "fahrenheit":
                data["temp"] = data["temp"] * 9 / 5 + 32
            return {
                "city": city,
                "temperature": data["temp"],
                "unit": unit,
                "condition": data["condition"],
                "humidity": data["humidity"]
            }
        else:
            return {"error": f"未找到城市 {city} 的天气信息"}


class CalculatorTool(Tool):
    """计算器工具示例"""
    
    def __init__(self):
        super().__init__(
            tool_name="calculate",
            description="执行基本的数学计算",
            params_dict={
                "expression": ("string", "要计算的数学表达式，如 '2 + 3 * 4'")
            }
        )
    
    def execute(self, expression: str) -> Dict[str, Any]:
        """执行数学计算"""
        try:
            # 注意：实际应用中应使用更安全的表达式解析方式
            result = eval(expression)
            return {
                "expression": expression,
                "result": result
            }
        except Exception as e:
            return {
                "expression": expression,
                "error": str(e)
            }

conversation_id = "agent_test_0"
api_key = ""
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"

system_prompt = "你是一个智能助手，可以使用工具来帮助用户完成任务。,可用'get_weather'进行函数调用获得天气"

llm = LLM(
    api_key=api_key,
    base_url=base_url,
    model=model,
    temperature=0.7,
    max_tokens=1000
)

tools_manager = ToolsManager()
tools_manager.register_tool(WeatherTool())
tools_manager.register_tool(CalculatorTool())

class TestWorkflow(ModelWorkflowFramework):
    def __init__(self, llm: LLM, context_id: str, tools_manager: ToolsManager, system_prompt: str = ""):
        super().__init__(llm, context_id, tools_manager, system_prompt)

    def forward(self, user_input: str) -> str:
        self.ctx.add_user_message(user_input)   # 添加用户消息到上下文
        model_response = self.llm.call(self.ctx.get_context(), tools=self.tools_manager.get_tools_definitions())
        # 获得模型回复

        if not model_response:
            return "模型调用失败"

        msg, has_tool_call = self.agent_executor(model_response)   # Agent 执行器处理模型回复   # type: ignore

        if has_tool_call:   # 如果模型回复包含函数调用, 再返还给模型处理
            res = self.llm.call(msg, tools=self.tools_manager.get_tools_definitions())
            logger.info("存在函数调用")
            return self.final_response(res)

        else:   # 如果模型回复不包含函数调用, 直接返回模型回复
            logger.info("不存在函数调用")
            return self.final_response(model_response)

wf = TestWorkflow(llm, conversation_id, tools_manager, system_prompt)

user_message = "北京今天天气怎么样,摄氏度？"
msg = wf(user_message)
print(msg)


