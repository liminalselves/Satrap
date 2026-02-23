import os
import sys
import json
from typing import Dict, Any, Tuple, List

# 假设以下导入路径正确，可根据实际环境调整
from satrap.core.log import logger
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.context import ContextManager
from satrap.core.utils.TCBuilder import Tool, ToolsManager, create_tool_defined
from satrap.core.framework import ModelWorkflowFramework  # 需确保存在

# 修复原CodeSandbox中_safe_join无返回值的问题
from satrap.core.utils.sandbox import CodeSandbox

from satrap.core.framework import ModelWorkflowFramework, Session
from satrap.core.utils.TCBuilder import Tool, ToolsManager
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.context import ContextManager
from satrap.core.utils.sandbox import CodeSandbox
from satrap.core.log import logger
import re
from typing import Dict, Any

# ---------- 工作流2：根据要求生成代码并执行 ----------
class Workflow2(ModelWorkflowFramework):
    """工作流2：接收代码要求，生成代码并在沙箱中执行，返回执行结果"""
    def __init__(self, llm: LLM, context_id: str, sandbox: CodeSandbox):
        super().__init__(llm, context_id, ToolsManager())  # wf2 不使用工具
        self.sandbox = sandbox

    def forward(self, requirement: str) -> Dict[str, Any]:
        """执行工作流2：生成代码并运行"""
        prompt = f"请根据以下要求生成可直接运行的 Python 代码，只返回代码本身，不要包含任何解释或额外内容：\n{requirement}"
        self.ctx.add_user_message(prompt)
        response = self.llm.call(self.ctx.get_context())
        if not response:
            return {"error": "代码生成失败"}

        # 提取代码块（假设模型可能返回 ```python ... ``` 或纯代码）
        code = response.content
        code_pattern = r"```(?:python)?\s*([\s\S]*?)```"
        match = re.search(code_pattern, code)
        if match:
            code = match.group(1).strip()
        else:
            # 如果没有代码块标记，直接使用返回内容（假设就是代码）
            code = code.strip()

        logger.info(f"[Workflow2] 生成的代码：\n{code}")

        # 在沙箱中执行代码
        result = self.sandbox.run(code)
        logger.info(f"[Workflow2] 执行结果：{result}")
        return result


# ---------- 工具：请求工作流2生成代码 ----------
class RequestCodeTool(Tool):
    """工具：向工作流2请求代码生成并执行"""
    def __init__(self, wf2: Workflow2):
        super().__init__(
            tool_name="request_code",
            description="当需要编写并执行代码时，调用此工具。输入一个清晰的代码要求，返回代码执行结果。",
            params_dict={
                "requirement": ("string", "详细的代码要求，例如“计算1到100的和”")
            }
        )
        self.wf2 = wf2

    def execute(self, requirement: str) -> Dict[str, Any]:
        """执行工具：调用工作流2"""
        logger.info(f"[RequestCodeTool] 收到代码要求：{requirement}")
        try:
            result = self.wf2.forward(requirement)
            return result
        except Exception as e:
            logger.error(f"[RequestCodeTool] 调用工作流2失败：{e}")
            return {"error": f"代码执行失败：{str(e)}"}


# ---------- 工作流1：生成意见，可调用工具 ----------
class Workflow1(ModelWorkflowFramework):
    """工作流1：根据用户输入生成意见，必要时调用工具请求代码"""
    def __init__(self, llm: LLM, context_id: str, wf2: Workflow2):
        tools_manager = ToolsManager()
        # 注册请求代码的工具
        tools_manager.register_tool(RequestCodeTool(wf2))
        super().__init__(llm, context_id, tools_manager)
        # 可设置系统提示，引导模型使用工具
        self.ctx.reset_system_prompt(
            "你是一个智能助手，负责分析用户问题并给出处理意见。如果需要编写代码，请使用 request_code 工具。"
        )

    def forward(self, user_input: str) -> str:
        """执行工作流1：生成意见"""
        self.ctx.add_user_message(user_input)
        # 第一次调用模型
        response = self.llm.call(self.ctx.get_context(), tools=self.tools_manager.get_tools_definitions())
        if not response:
            return "模型调用失败"

        # 处理可能的工具调用
        messages, has_tool = self.agent_executor(response)
        if has_tool:
            # 如果有工具调用，再次调用模型生成最终意见
            response2 = self.llm.call(messages, tools=self.tools_manager.get_tools_definitions())
            return self.final_response(response2)
        else:
            return self.final_response(response)


# ---------- 工作流3：生成最终回答 ----------
class Workflow3(ModelWorkflowFramework):
    """工作流3：结合用户输入和工作流1的意见生成最终回答"""
    def __init__(self, llm: LLM, context_id: str):
        super().__init__(llm, context_id, ToolsManager())  # wf3 不使用工具

    def forward(self, user_input: str, wf1_output: str) -> str:
        """执行工作流3：生成回答"""
        prompt = f"用户问题：{user_input}\n工作流1给出的处理意见：{wf1_output}\n请根据以上信息生成最终回答。"
        self.ctx.add_user_message(prompt)
        response = self.llm.call(self.ctx.get_context())
        return self.final_response(response)


# ---------- 会话管理器 ----------
class CodeGenSession(Session):
    """会话类，管理三个工作流的协作"""
    def __init__(self, session_id: str, llm: LLM, sandbox: CodeSandbox):
        super().__init__(session_id)
        self.llm = llm
        self.sandbox = sandbox

        # 初始化工作流，分配独立的上下文ID
        wf2_id = self.workflow_id_assign("wf2")
        self.wf2 = Workflow2(llm, wf2_id, sandbox)

        wf1_id = self.workflow_id_assign("wf1")
        self.wf1 = Workflow1(llm, wf1_id, self.wf2)

        wf3_id = self.workflow_id_assign("wf3")
        self.wf3 = Workflow3(llm, wf3_id)

    def run(self, user_input: str) -> str:
        """执行会话流程"""
        logger.info(f"[CodeGenSession] 收到用户输入：{user_input}")

        # 步骤1：运行工作流1，获得意见
        wf1_output = self.wf1(user_input)
        logger.info(f"[CodeGenSession] 工作流1输出：{wf1_output}")

        # 步骤2：运行工作流3，获得最终回答
        final_answer = self.wf3(user_input, wf1_output)
        logger.info(f"[CodeGenSession] 最终回答：{final_answer}")

        # 将会话中的最终回答保存到会话上下文（可选）
        self.session_ctx.add_bot_message(final_answer)   # type: ignore

        return final_answer   # type: ignore


# ---------- 使用示例 ----------
if __name__ == "__main__":
    # 初始化 LLM（请替换为实际 API 密钥和地址）
    api_key = ""
    base_url = "https://api.deepseek.com/v1"
    model = "deepseek-chat"
    llm = LLM(api_key=api_key, base_url=base_url, model=model, temperature=0.7, max_tokens=2000)

    # 初始化代码沙箱（指定一个临时目录和 Python 解释器路径）
    sandbox = CodeSandbox(sandbox_path="./sandbox_temp", env="E:\\conda\\envs\\code\\python.exe")

    # 创建会话
    session = CodeGenSession(session_id="test_session_00335", llm=llm, sandbox=sandbox)

    # 用户输入
    user_input = "请帮我计算 12345 乘以 6789 的结果"
    result = session(user_input)
    print(result)

    # 清理会话内存（可选）
    session.clear_memory()