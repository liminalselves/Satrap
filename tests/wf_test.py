import os
import json
from typing import Optional, Callable, List, Dict, Any

# 假设这些模块已按之前提供的内容实现
from satrap.core.APICall.LLMCall import LLM  # 之前的 LLM 类
from satrap.core.utils.TCBuilder import Tool, ToolsManager  # 之前的工具基类和管理器
from satrap.core.utils.context import ContextManager
from satrap.core.type import LLMCallResponse
from satrap.core.framework import ModelWorkflowFramework  # 我们修改后的工作流基类
from satrap.core.log import logger

# ========== 1. 定义工具 ==========
class GetDateTool(Tool):
    tool_name = "get_date"
    description = "获取当前日期"
    params_dict = {}  # 无参数

    def execute(self) -> str:
        # 模拟返回一个固定日期（实际应调用真实API）
        return "2025-12-01"

class GetWeatherTool(Tool):
    tool_name = "get_weather"
    description = "获取某地某日的天气"
    params_dict = {
        "location": ("string", "城市名称"),
        "date": ("string", "日期，格式 YYYY-mm-dd")
    }

    def execute(self, location: str, date: str) -> str:
        # 模拟天气数据（实际应调用天气API）
        return f"{location} {date} 天气：多云 7~13°C"

# ========== 2. 创建工作流 ==========
class WeatherWorkflow(ModelWorkflowFramework):
    """天气查询工作流，自动处理多轮工具调用"""
    def __init__(
        self,
        llm: LLM,
        context_id: str,
        tools_manager: ToolsManager,
        system_prompt: Optional[str] = None,
        content_callback: Optional[Callable[[str], None]] = None
    ):
        super().__init__(llm, context_id, tools_manager, system_prompt, content_callback)

    def forward(self, user_input: str) -> str:
        # 1. 添加用户消息到上下文
        self.ctx.add_user_message(user_input)

        # 2. 首次调用模型
        response = self.llm.call(
            self.ctx.get_context(),
            tools=self.tools_manager.get_tools_definitions(),
            thinking=True  # 启用思考，但思考内容不会存入上下文
        )
        if not response:
            return "模型调用失败"

        # 3. 使用 agent_executor 自动处理多轮工具调用
        #    注意：agent_executor 内部会循环调用模型直到无工具调用，
        #    并且中间思考内容不会进入上下文。
        final_context = self.agent_executor(response)

        # 4. 获取最终回答（最后一条 assistant 消息的内容）
        for msg in reversed(final_context):
            if msg.get("role") == "assistant":
                return msg.get("content", "")   # type: ignore
        return "未找到模型回复"

# ========== 3. 主程序 ==========
def main():
    # 配置 LLM（请根据实际情况设置环境变量）
    api_key = ""
    base_url = "https://api.deepseek.com/v1"
    if not api_key or not base_url:
        logger.error("请设置 DEEPSEEK_API_KEY 和 DEEPSEEK_BASE_URL 环境变量")
        return

    llm = LLM(
        api_key=api_key,
        base_url=base_url,
        model="deepseek-reasoner",
        temperature=0.7,
        max_tokens=2000
    )

    # 创建工具管理器并注册工具
    tools_manager = ToolsManager()
    tools_manager.register_tool(GetDateTool())
    tools_manager.register_tool(GetWeatherTool())

    # 可选：设置系统提示词
    system_prompt = "你是一个天气助手，可以帮助用户查询天气。如果需要获取当前日期，请调用 get_date 工具。"

    # 创建工作流
    workflow = WeatherWorkflow(
        llm=llm,
        context_id="weather_session_003",
        tools_manager=tools_manager,
        system_prompt=system_prompt
    )

    # 测试查询
    user_query = "杭州明天天气怎么样？"
    print(f"用户：{user_query}")
    result = workflow(user_query)
    print(f"助手：{result}")

    # 可选：打印完整上下文，验证思考内容未存入
    print("\n=== 上下文内容（不含思考内容）===")
    for idx, msg in enumerate(workflow.ctx.get_context()):
        print(f"{idx}: {msg}")

if __name__ == "__main__":
    main()