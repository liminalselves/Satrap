from satrap.core.utils.TCBuilder import Tool, create_tool_defined, ToolsManager
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.utils.context import ContextManager
from satrap.core.type import LLMCallResponse
from typing import Optional

from satrap import logger

class ModelWorkflowFramework:
    """模型工作流框架"""
    def __init__(self, llm: LLM, context_id: str, tools_manager: ToolsManager, system_prompt: str | None = None):
        """
        模型工作流框架, 负责管理模型的调用和工作流的执行
        这是 Satrap 的核心组件之一, 负责协调单个模型实例调用, 以实现复杂的任务处理和自动化流程;

        任何工作流都应该继承这个类, 并实现自己的工作流逻辑, 以便被调用和执行;
        工作流应当覆写 `forward` 方法, 并在其中实现模型的调用和工作流的逻辑

        并在初始化进行 `super().__init__(llm, context_id, tools_manager, system_prompt)`

        参数:
        - model: 模型实例
        - context_id: 上下文 ID
        - tools_manager: 工具管理器实例
        - system_prompt: 系统提示词; 如果填写, 会重置上下文的系统提示词

        使用示例
        ``` python
        class MyWorkflow(ModelWorkflowFramework):
            def __init__(self, llm, context_id, tools_manager, system_prompt=None):
                super().__init__(llm, context_id, tools_manager, system_prompt)

            def forward(self, user_input: str) -> str:
                self.ctx.add_user_message(user_input)
                response = self.llm.call(self.ctx.get_context(), tools=self.tools_manager.get_tools_definitions())
                
                if not response:
                    return "模型调用失败"
                
                # 处理可能的工具调用
                msg, has_tool = self.agent_executor(response)
                if has_tool:
                    res = self.llm.call(msg, tools=self.tools_manager.get_tools_definitions())
                    return self.final_response(res)
                return self.final_response(response)

        # 2. 初始化并调用工作流
        workflow = MyWorkflow(llm, "conversation_id", tools_manager, "You are a helper")
        result = workflow("北京今天天气怎么样？")
        print(result)

        # 或者集成进 `Session` 类中, 以实现复杂多模型 Agent 与会话管理
        ```
        """
        self.llm = llm
        self.ctx = ContextManager(context_id)
        self.tools_manager = tools_manager

        self.ctx.load_context()
        if system_prompt:
            self.ctx.reset_system_prompt(system_prompt)

    def agent_executor(self, model_response: LLMCallResponse) -> tuple[list[dict[str, str | list]], bool]:
        """智能体执行器, 用于执行智能体调用流程

        参数:
        - model_response: 模型调用响应

        返回:
        - 元组(list, bool): 上下文消息列表和是否有工具调用
        """
        try:
            if model_response.type == "tools_call" and model_response.tool_calls is not None:
                tool_messages = []
                tool_results = []

                for tool_call in model_response.tool_calls:   # 处理所有工具调用
                    tool_message, tool_result = self.tools_manager.execute_tool_call(tool_call)
                    tool_messages.append(tool_message)
                    tool_results.append(tool_result)

                self.ctx.add_tool_call_flow(model_response.content, tool_messages, tool_results)
                messages = self.ctx.get_context()
                return messages, True

            else:
                self.ctx.add_bot_message(model_response.content)   # 添加模型回复到上下文
                messages = self.ctx.get_context()   # 获取最新上下文
                return messages, False

        except Exception as e:
            logger.error(f"智能体执行器错误: {e}")
            return [], False

    @staticmethod
    def final_response(messages: LLMCallResponse | bool) -> str:
        """获取最终回复"""
        if isinstance(messages, bool):
            return "模型调用失败, 请查看日志以获得更多信息"
        else:
            return messages.content

    def forward(self):
        """执行工作流; 调用模型并返回结果"""
        return None

    def __call__(self, *input, **kwargs):
        result = self.forward(*input, **kwargs)
        return result

class Session:
    """会话类, 用于管理多个模型工作流的会话"""
    def __init__(self, session_id: str):
        """会话框架, 用于管理多个模型工作流的会话
        任何依赖多模型的复杂 Agent 都应当继承自该类, 并实现 `forward` 方法

        并在初始化时进行 `super().__init__(session_id)`

        参数:
        - session_id: 会话 ID
        """
        self.session_ctx = ContextManager(session_id)
        """会话共享上下文"""

        self.session_ctx.load_context()
        self.session_id = session_id
        self.wf_list = []

    def run(self):
        """执行会话; 调用模型并返回结果"""
        return None
    
    def workflow_id_assign(self, wf_id: str):
        """为会话分配工作流 ID

        返回:
        - 工作流 ID (str) (session_id + "_" + wf_id)
        """
        workflow_id = self.session_id + "_" + wf_id
        self.wf_list.append(workflow_id)
        return workflow_id
    
    def clear_memory(self):
        """清除会话内存"""
        try:
            self.session_ctx.del_context()
            for wf_id in self.wf_list:
                wf_ctx = ContextManager(wf_id)
                wf_ctx.del_context()

            logger.info(f"[会话管理器] 清除工作流上下文完成, 工作流ID: {wf_id}")

        except Exception as e:
            logger.error(f"[会话管理器] 清除会话上下文错误: {e}")                

    def __call__(self, *input, **kwargs):
        result = self.run(*input, **kwargs)
        return result
