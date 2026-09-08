from __future__ import annotations
from typing import Any
import uuid
from satrap.core.framework.Base import (
    AsyncModelWorkflowFramework,
    ModelWorkflowFramework,
)


class _SubAgentCore:
    def __init__(
        self,
        llm: Any,
        tools_manager: Any,
        system_prompt: str,
        tools: list[str] | None = None,
    ):
        """
        初始化 _CodingSubAgent

        参数:
        - llm: 模型实例
        - tools_manager: 工具管理器实例
        - system_prompt: 系统prompt
        - tools: 工具集合
        """
        sub_manager = tools_manager
        if tools is not None:
            sub_manager = type(tools_manager)()
            for name in tools:
                tool = tools_manager.tools.get(name)
                if tool is not None:
                    sub_manager.register_tool(tool)
        self.llm = llm
        self.tools_manager = sub_manager
        self.system_prompt = system_prompt
        self.context_id = f"coding_sub_{uuid.uuid4().hex[:8]}"


class _CodingSubAgent(_SubAgentCore):
    """独立上下文的子代理 (自定义 system prompt / 工具白名单 / 迭代上限)"""

    def forward(self, task: str, max_turns: int = 20) -> str:
        """
        执行 `forward` 操作

        参数:
        - task: 任务描述
        - max_turns: 最大轮数

        返回:
        - str: 执行 `forward` 操作
        """
        sub = ModelWorkflowFramework(
            self.llm,
            context_id=self.context_id,
            tools_manager=self.tools_manager,
            system_prompt=self.system_prompt,
        )
        return sub.tools_agent(task, max_iterations=max_turns)


class _AsyncCodingSubAgent(_SubAgentCore):
    """异步独立上下文的子代理"""

    async def forward(self, task: str, max_turns: int = 20) -> str:
        """
        执行 `forward` 操作

        参数:
        - task: 任务描述
        - max_turns: 最大轮数

        返回:
        - str: 执行 `forward` 操作
        """
        sub = AsyncModelWorkflowFramework(
            self.llm,
            context_id=self.context_id,
            tools_manager=self.tools_manager,
            system_prompt=self.system_prompt,
        )
        return await sub.tools_agent(task, max_iterations=max_turns)
