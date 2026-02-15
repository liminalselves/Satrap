"""
测试 Function Call 功能

本测试演示如何使用 TCBuilder、LLMCall 和 ContextManager 进行一次完整的 function call 流程：
1. 使用 TCBuilder 定义工具
2. 使用 ContextManager 管理对话上下文
3. 调用 LLM 并传入工具定义
4. 解析响应并执行工具
"""

import json
from typing import Dict, Any, Tuple, List

from satrap.core.utils.TCBuilder import Tool, create_tool_defined, ToolsManager
from satrap.core.APICall.LLMCall import LLM, parse_call_response
from satrap.core.utils.context import ContextManager


# ==================== 定义工具 ====================

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


# ==================== 测试函数 ====================

def test_function_call():
    """
    测试 Function Call 完整流程
    
    注意：运行此测试需要设置有效的 API Key 和 Base URL
    """
    
    # Step 1: 初始化工具管理器并注册工具
    print("=" * 50)
    print("Step 1: 初始化工具")
    print("=" * 50)
    
    tool_manager = ToolsManager()
    tool_manager.register_tool(WeatherTool())
    tool_manager.register_tool(CalculatorTool())
    
    print(f"已注册工具: {list(tool_manager.tools.keys())}")
    
    # 打印工具定义
    for tool_def in tool_manager.get_tools_definitions():
        print(f"\n工具定义: {tool_def['function']['name']}")
        print(json.dumps(tool_def, ensure_ascii=False, indent=2))
    
    # Step 2: 初始化上下文管理器
    print("\n" + "=" * 50)
    print("Step 2: 初始化上下文管理器")
    print("=" * 50)
    
    ctx = ContextManager(
        conversation_id="test_function_call_001",
        keep_in_memory=True  # 使用内存模式，不写入数据库
    )

    # 添加系统消息
    ctx.add_at_system_start("你是一个智能助手，可以使用工具来帮助用户完成任务。,可用'get_weather'进行函数调用获得天气")
    
    # Step 3: 初始化 LLM（需要配置 API）
    print("\n" + "=" * 50)
    print("Step 3: 初始化 LLM")
    print("=" * 50)
    
    # 注意：请替换为你的 API 配置
    # 这里使用环境变量或直接配置
    import os
    
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
    
    print(f"LLM 模型: {llm.get_model()}")
    print(f"Base URL: {llm.get_base_url()}")
    
    # Step 4: 发送用户消息并调用 LLM
    print("\n" + "=" * 50)
    print("Step 4: 发送用户消息")
    print("=" * 50)
    
    user_message = "北京今天天气怎么样,摄氏度？"
    ctx.add_user_message(user_message)
    print(f"用户: {user_message}")
    
    # Step 5: 调用 LLM 并传入工具定义
    print("\n" + "=" * 50)
    print("Step 5: 调用 LLM (带工具定义)")
    print("=" * 50)
    
    try:
        # 使用 LLM.call() 方法，支持 tools 参数
        result = llm.call(
            messages=ctx.get_context(),
            tools=tool_manager.get_tools_definitions(),
            tool_choice="auto"
        )

        # 检查返回值类型
        if isinstance(result, bool):
            print("调用失败")
            return False
        
        response_type, text_content, call_info = result
        
        print(f"响应类型: {response_type}")
        print(f"文本内容: {text_content}")
        print(f"调用信息: {call_info}")
        
        # Step 6: 如果是工具调用，执行工具并获取结果
        if response_type == "tools_call":
            print("\n" + "=" * 50)
            print("Step 6: 执行工具调用")
            print("=" * 50)

            tool_message, tool_result = tool_manager.execute_tool_call(call_info[0])
            ctx.add_tool_call_flow(text_content, tool_message, tool_result)
            messages = ctx.get_context()
            # 工具调用

            # 再次调用 LLM（不带工具定义，获取最终回复）
            final_result = llm.call(messages=messages)
            if isinstance(final_result, bool):
                print("获取最终回复失败")
                return False
            
            final_type, final_text, _ = final_result
            print(f"最终回复: {final_text}")
            
            # 保存对话
            ctx.add_bot_message(final_text)

        else:
            print("\n模型直接回复（未调用工具）:")
            print(text_content)
            ctx.add_bot_message(text_content)
        
        print("\n" + "=" * 50)
        print("测试完成!")
        print("=" * 50)
        print(f"对话历史: {ctx.static_message()} 条消息")
        
    except Exception as e:
        print(f"\n错误: {e}")
        print("请确保已正确配置 API Key 和 Base URL")
        return False
    
    return True


def test_tool_definition():
    """测试工具定义功能（不需要 API 调用）"""
    print("=" * 50)
    print("测试工具定义功能")
    print("=" * 50)
    
    # 使用函数创建工具定义
    tool_def = create_tool_defined(
        tool_name="search_web",
        description="在网络上搜索信息",
        params_dict={
            "query": ("string", "搜索关键词"),
            "num_results": ("number", "返回结果数量")
        }
    )
    
    print("使用 create_tool_defined 创建的工具定义:")
    print(json.dumps(tool_def, ensure_ascii=False, indent=2))
    
    # 使用 Tool 类创建工具
    class SearchTool(Tool):
        def __init__(self):
            super().__init__(
                tool_name="search_web",
                description="在网络上搜索信息",
                params_dict={
                    "query": ("string", "搜索关键词"),
                    "num_results": ("number", "返回结果数量")
                }
            )
        
        def execute(self, query: str, num_results: int = 5):
            return {"results": [f"结果{i+1}: {query}" for i in range(num_results)]}
    
    search_tool = SearchTool()
    print("\n使用 Tool 类创建的工具定义:")
    print(json.dumps(search_tool.get_tool_defined(), ensure_ascii=False, indent=2))
    
    # 测试工具执行
    print("\n测试工具执行:")
    result = search_tool(query="Python教程", num_results=3)
    print(f"执行结果: {result}")


# ==================== 主函数 ====================

if __name__ == "__main__":
    print("Function Call 测试脚本\n")
    
    # 测试工具定义（不需要 API）
    test_tool_definition()
    
    print("\n")
    
    # 测试完整的 function call 流程（需要 API 配置）
    print("开始测试 Function Call 流程...")
    print("注意: 此测试需要配置有效的 API Key\n")
    
    test_function_call()
