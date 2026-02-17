from satrap.core.framework import ModelWorkflowFramework, Session
from satrap.core.utils.TCBuilder import ToolsManager
from satrap.core.APICall.LLMCall import LLM
from typing import Dict, Tuple, Any, Union, List
from satrap.core.utils.context import ContextManager
from satrap.core.utils.TCBuilder import Tool
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
class WeatherWorkflow(ModelWorkflowFramework):
    """第一个工作流：查询天气"""
    def forward(self, user_input: str, session_data: dict) -> tuple[str, dict]:
        # 添加用户消息到自己的上下文
        self.ctx.add_user_message(user_input)
        
        # 调用 LLM，传入工具定义
        response = self.llm.call(
            self.ctx.get_context(),
            tools=self.tools_manager.get_tools_definitions()
        )
        if not response:
            return "模型调用失败", session_data
        
        # 使用 agent_executor 处理可能的工具调用
        messages, has_tool = self.agent_executor(response)   # type: ignore
        if has_tool:
            # 如果有工具调用，再次调用 LLM 获取最终回复
            final_resp = self.llm.call(messages, tools=self.tools_manager.get_tools_definitions())
            final_text = self.final_response(final_resp)
        else:
            final_text = self.final_response(response)
        
        # 将天气信息存入共享数据（这里简化：直接将最终回复作为天气信息）
        updated_data = session_data.copy()
        updated_data["weather_info"] = final_text
        
        return final_text, updated_data


class RecommendWorkflow(ModelWorkflowFramework):
    """第二个工作流：根据天气推荐活动"""
    def forward(self, user_input: str, session_data: dict) -> tuple[str, dict]:
        # 从共享数据中获取天气信息
        weather_info = session_data.get("weather_info", "未知天气")
        
        # 构造提示词
        prompt = f"根据以下天气信息推荐适合的活动：{weather_info}\n请给出一些建议。"
        self.ctx.add_user_message(prompt)
        
        # 调用 LLM（无需工具）
        response = self.llm.call(self.ctx.get_context())
        if not response:
            return "模型调用失败", session_data
        
        final_text = self.final_response(response)
        return final_text, session_data  # 不更新共享数据


class MySession(Session):
    """自定义会话类，管理两个工作流"""
    def __init__(self, session_id: str, llm: LLM, tools_manager: ToolsManager):
        super().__init__(session_id)
        self.llm = llm
        self.tools_manager = tools_manager
        self.session_data = {}  # 跨工作流共享的数据

    def run(self, user_input: str) -> str:
        # 为每个工作流生成唯一 ID
        wf1_id = self.workflow_id_assign("weather")
        wf2_id = self.workflow_id_assign("recommend")

        # 创建工作流实例（注意 context_id 使用复合 ID 确保独立上下文）
        wf1 = WeatherWorkflow(
            llm=self.llm,
            context_id=wf1_id,  # 复合 ID，工作流内部会用此 ID 创建独立的 ContextManager
            tools_manager=self.tools_manager,
            system_prompt="你是一个天气查询助手，可以调用 get_weather 工具。"
        )
        wf2 = RecommendWorkflow(
            llm=self.llm,
            context_id=wf2_id,
            tools_manager=self.tools_manager,
            system_prompt="你是一个活动推荐助手，根据天气信息推荐活动。"
        )

        # 执行第一个工作流
        response1, updated_data = wf1.forward(user_input, self.session_data)
        self.session_data.update(updated_data)

        # 执行第二个工作流，将第一个的输出作为输入
        response2, updated_data2 = wf2.forward(response1, self.session_data)
        self.session_data.update(updated_data2)

        # 可选：将会话消息保存到全局上下文（session_ctx）
        self.session_ctx.add_user_message(user_input)
        self.session_ctx.add_bot_message(response2)

        return response2


if __name__ == "__main__":
    # 配置参数（请替换为有效 API Key）
    conversation_id = "agent_test_multi"
    api_key = ""
    base_url = "https://api.deepseek.com/v1"
    model = "deepseek-chat"

    llm = LLM(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.7,
        max_tokens=1000
    )

    tools_manager = ToolsManager()
    tools_manager.register_tool(WeatherTool())
    tools_manager.register_tool(CalculatorTool())  # 可选的额外工具

    # 创建会话并运行
    session = MySession(conversation_id, llm, tools_manager)
    user_message = "北京今天天气怎么样,摄氏度？"
    result = session.run(user_message)
    print("最终结果:", result)

    # 可选：清理所有工作流和会话的上下文
    session.clear_memory()