import asyncio
from typing import Any
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.utils.TCBuilder import AsyncToolsManager
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.core.framework.Base import AsyncModelWorkflowFramework
from .utils import (
    SUB_AGENT_SYSTEM_PROMPT,
    MAX_SUB_AGENT_TASKS,
    DEFAULT_SUB_AGENT_TIMEOUT,
    DEFAULT_SUB_AGENT_WORKERS,
)
from .utils import _parse_tasks


class AsyncSubAgentModel(AsyncModelWorkflowFramework):
    def __init__(
        self, llm: AsyncLLM, context_id: str, tools_manager: AsyncToolsManager
    ):
        """
        初始化异步子代理模型

        参数:
        - llm: AsyncLLM 模型实例
        - context_id: 子代理上下文 ID
        - tools_manager: 异步工具管理器实例
        """
        super().__init__(
            llm,
            context_id=f"sub_agent_{context_id}",
            tools_manager=tools_manager,
            system_prompt=SUB_AGENT_SYSTEM_PROMPT,
        )

    def forward(self, task: str):
        """
        运行异步子代理任务

        参数:
        - task: 子代理要执行的任务描述

        返回:
        - 运行异步子代理任务
        """
        return self.tools_agent(task)


class AsyncSubAgent(AsyncTool):
    def __init__(
        self,
        llm: AsyncLLM,
        tools_manager: AsyncToolsManager,
        task_timeout: float = DEFAULT_SUB_AGENT_TIMEOUT,
        max_workers: int = DEFAULT_SUB_AGENT_WORKERS,
    ):
        """
        初始化异步子代理工具

        参数:
        - llm: AsyncLLM 模型实例
        - tools_manager: 异步工具管理器实例
        - task_timeout: 单个子任务超时秒数
        - max_workers: 最大并行子任务数
        """
        super().__init__(
            tool_name="sub_agent",
            description="异步子代理，运行在独立上下文中的异步子代理，用于高效处理特定任务",
            params_dict={
                "task": (
                    "array",
                    "要执行的任务描述数组，形式为: [sub_task1, sub_task2, sub_task3]",
                )
            },
        )  # 初始化父类工具

        self.llm = llm
        self.tools_manager = tools_manager
        self.task_timeout = max(0.1, float(task_timeout))
        self.max_workers = max(1, min(MAX_SUB_AGENT_TASKS, int(max_workers)))

    async def execute(self, task: str) -> str:
        """
        执行异步子代理任务

        参数:
        - task: 子代理要执行的任务描述数组

        返回:
        - str: 执行异步子代理任务
        """
        # Step.1 安全解析 (处理模型可能传字符串或数组的情况)
        task_list, error = _parse_tasks(task, "AsyncSubAgent")
        if error is not None:
            return error

        # Step.2 定义单个子任务的执行函数
        async def run_single(index: int, sub_task: str) -> dict[str, Any]:
            sub_agent = await AsyncSubAgentModel.create(
                self.llm, index, self.tools_manager
            )
            result = await sub_agent.forward(sub_task)
            return {
                "index": index,
                "sub_task": sub_task,
                "result": result,
            }  # 返回包含索引, 子任务和结果的字典

        # Step.3 并行执行
        semaphore = asyncio.Semaphore(self.max_workers)

        async def run_limited(index: int, sub_task: str) -> dict[str, object]:
            """
            在并发与超时上限内执行单个子任务

            参数:
            - index: 子任务序号
            - sub_task: 子任务说明

            返回:
            - 包含任务结果或超时错误的结构化记录
            """
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        run_single(index, sub_task),
                        timeout=self.task_timeout,
                    )
                except TimeoutError:
                    return {
                        "index": index,
                        "sub_task": sub_task,
                        "error": f"超过 {self.task_timeout} 秒",
                    }

        coros = [
            run_limited(i, sub_task) for i, sub_task in enumerate(task_list, start=1)
        ]
        # 创建所有协程任务

        results = await asyncio.gather(*coros, return_exceptions=True)
        # 使用 gather 并发执行, return_exceptions=True 可防止某个子任务崩溃影响整体

        output_lines: list[str] = []
        for res in results:
            if isinstance(res, dict) and "index" in res and "error" not in res:
                output_lines.append(
                    f"子代理{res['index']}执行任务: {res['sub_task']}，结果: {res['result']}"
                )  # 正常结果

            elif isinstance(res, dict) and "index" in res:
                output_lines.append(
                    f"子代理{res['index']}执行失败: {res.get('error', '未知错误')}"
                )

            else:
                output_lines.append(f"子代理执行失败: {str(res)}")
                # 异常或其他意外类型
        return "\n".join(output_lines)
