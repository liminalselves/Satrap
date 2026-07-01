from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.utils.TCBuilder import ToolsManager, AsyncToolsManager
from concurrent.futures import ThreadPoolExecutor, as_completed
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from uuid import uuid4
import asyncio
import json
import time

from satrap.core.log import logger


SUB_AGENT_SYSTEM_PROMPT = """
你是一个子代理，你需要根据任务描述，合理使用工具来完成任务并返回结果
"""

class SubAgentModel(ModelWorkflowFramework):
    def __init__(self, llm: LLM, context_id: str, tools_manager: ToolsManager):
        """
        初始化子代理模型

        参数:
        llm: LLM模型实例
        """
        super().__init__(llm, context_id = f"sub_agent_{context_id}", tools_manager = tools_manager, system_prompt = SUB_AGENT_SYSTEM_PROMPT)

    def forward(self, task: str):
        """
        运行子代理任务

        参数:
        task: 子代理要执行的任务描述
        """
        return self.tools_agent(task)
    

class AsyncSubAgentModel(AsyncModelWorkflowFramework):
    def __init__(self, llm: AsyncLLM, context_id: str, tools_manager: AsyncToolsManager):
        """
        初始化异步子代理模型

        参数:
        llm: AsyncLLM模型实例
        """
        super().__init__(llm, context_id = f"sub_agent_{context_id}", tools_manager = tools_manager, system_prompt = SUB_AGENT_SYSTEM_PROMPT)

    def forward(self, task: str):
        """
        运行异步子代理任务

        参数:
        task: 子代理要执行的任务描述
        """
        return self.tools_agent(task)


class SubAgent(Tool):
    def __init__(self, llm: LLM, tools_manager: ToolsManager):
        """
        初始化子代理工具

        参数:
        llm: LLM模型实例
        tools_manager: ToolsManager模型实例
        """
        super().__init__(
            tool_name = "sub_agent",
            description = "子代理，运行在独立上下文中的子代理，用于高效处理特定任务",
            params_dict = {"task": ("array", "要执行的任务描述数组，形式为: [sub_task1, sub_task2, sub_task3]")},
        )   # 初始化父类工具

        self.llm = llm
        self.tools_manager = tools_manager

    def execute(self, task: str):
        """
        执行子代理任务

        参数:
        task: 子代理要执行的任务描述数组
        """
        # 1. 安全解析 (处理模型可能传字符串或数组的情况)
        try:
            task_list = json.loads(task)
            if isinstance(task_list, str):
                task_list = [task_list]
            elif not isinstance(task_list, list):
                return f"错误：传入的task不是数组，而是 {type(task_list)}"

        except json.JSONDecodeError:
            logger.warning(f"[SubAgent] 警告：传入的task不是JSON数组，尝试当作单个任务处理")
            task_list = [task]   # 解析失败就当单个任务处理

        # 2. 如果没有任务, 直接返回
        if not task_list:
            return "未收到任何子任务"
        
        # 3. 定义单个子任务的执行函数
        def run_single(index, sub_task):
            sub_agent = SubAgentModel(self.llm, index, self.tools_manager)
            result = sub_agent.forward(sub_task)
            return index, sub_task, result   # 返回 (索引, 子任务, 结果) 以便后续按序拼接

        # 4. 并行执行
        results_dict = {}
        with ThreadPoolExecutor(max_workers=16) as executor:   # max_workers 建议根据 API 限流调整 
            futures = {
                executor.submit(run_single, i, sub_task): i 
                for i, sub_task in enumerate(task_list, start=1)
            }   # 提交所有任务

            for future in as_completed(futures):   # 收集结果
                idx, sub_task, result = future.result()
                results_dict[idx] = f"子代理{idx}执行任务: {sub_task}，结果: {result}\n"

        # 5. 按原始顺序拼接输出
        final_results = ""
        for i in range(1, len(task_list) + 1):
            final_results += results_dict.get(i, f"子代理{i}执行失败: 未返回结果\n")

        return final_results
    

class AsyncSubAgent(AsyncTool):
    def __init__(self, llm: AsyncLLM, tools_manager: AsyncToolsManager):
        """
        初始化异步子代理工具

        参数:
        llm: AsyncLLM模型实例
        tools_manager: AsyncToolsManager模型实例
        """
        super().__init__(
            tool_name = "sub_agent",
            description = "异步子代理，运行在独立上下文中的异步子代理，用于高效处理特定任务",
            params_dict = {"task": ("array", "要执行的任务描述数组，形式为: [sub_task1, sub_task2, sub_task3]")},
        )   # 初始化父类工具

        self.llm = llm
        self.tools_manager = tools_manager

    async def execute(self, task: str):
        """
        执行异步子代理任务

        参数:
        task: 子代理要执行的任务描述数组
        """
        # 1. 安全解析 (处理模型可能传字符串或数组的情况)
        try:
            task_list = json.loads(task)
            if isinstance(task_list, str):
                task_list = [task_list]
            elif not isinstance(task_list, list):
                return f"错误：传入的 task 不是数组，而是 {type(task_list)}"

        except json.JSONDecodeError:   # 解析失败时当作单个任务处理
            logger.warning(f"[AsyncSubAgent] 警告：传入的task不是JSON数组，尝试当作单个任务处理")
            task_list = [task]

        if not task_list:
            return "未收到任何子任务"

        # 2. 定义单个子任务的执行函数
        async def run_single(index, sub_task):
            sub_agent = await AsyncSubAgentModel.create(self.llm, index, self.tools_manager)
            result = await sub_agent.forward(sub_task)
            return {
                "index": index,
                "sub_task": sub_task,
                "result": result
            }   # 返回包含索引, 子任务和结果的字典
        

        # 3. 并行执行
        coros = [run_single(i, sub_task) for i, sub_task in enumerate(task_list, start=1)]
        # 创建所有协程任务

        results = await asyncio.gather(*coros, return_exceptions=True)
        # 使用 gather 并发执行, return_exceptions=True 可防止某个子任务崩溃影响整体

        output_lines = []
        for res in results:
            if isinstance(res, dict) and "index" in res:
                output_lines.append(
                    f"子代理{res['index']}执行任务: {res['sub_task']}，结果: {res['result']}"
                )   # 正常结果

            else:
                output_lines.append(f"子代理执行失败: {str(res)}")
                # 异常或其他意外类型
        return "\n".join(output_lines)
