"""
同步与异步工作流的按需技能读取工具

以普通工具结果提供有效技能正文, 在当前任务内去重,
不激活工具权限, 不修改审批策略或持久化系统指令
"""
from __future__ import annotations

from typing import cast

from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.call_context import current_tool_messages
from .manager import SkillsManager
from .utils import SkillWorkflowProtocol


class _SkillReader:
    """同步与异步加载器共用的只读技能访问"""

    tool_name = "load_skill"
    recovery_policy = "retry"
    description = "读取可用技能的完整说明; 任务匹配或用户指定技能时先调用, 不改变工具权限"
    params_dict = {"skill": ("string", "技能目录中的精确标识")}

    def __init__(self, skills_manager: SkillsManager, workflow: SkillWorkflowProtocol | None = None):
        """
        绑定技能注册表及实际调用工作流

        参数:
        - skills_manager: 技能注册表
        - workflow: 所属工作流, 独立读取时可省略
        """
        self.skills_manager = skills_manager
        self.workflow = workflow
        super().__init__()

    def read(self, skill: str) -> str:
        """
        核验技能状态后返回正文

        参数:
        - skill: 精确技能标识

        返回:
        - 技能全文或明确的不可用提示
        """
        try:
            text = self.skills_manager.read_skill(skill, self.workflow)
        except ValueError as error:
            return str(error)
        messages = current_tool_messages(self.workflow) if self.workflow is not None else []
        loads: set[str] = set()
        for row in messages:
            if row.get("role") == "assistant":
                calls = row.get("tool_calls")
                for value in calls if isinstance(calls, list) else []:
                    if not isinstance(value, dict):
                        continue
                    call = cast(dict[str, object], value)
                    function = call.get("function")
                    if isinstance(function, dict) and function.get("name") == "load_skill":
                        loads.add(str(call.get("id", "")))
            elif row.get("role") == "tool" and str(row.get("tool_call_id")) in loads:
                content = row.get("content")
                if isinstance(content, str) and content.startswith(f"<skill:{skill}>") and content.endswith(f"</skill:{skill}>"):
                    return f"本次任务已加载技能 {skill}, 请沿用前面的完整说明"
        return text


class SkillTool(_SkillReader, AsyncTool):
    """异步工作流的按需技能加载工具"""

    async def execute(self, skill: str) -> str:
        """
        异步返回技能说明

        参数:
        - skill: 精确技能标识

        返回:
        - 技能全文或不可用提示
        """
        return self.read(skill)


class SyncSkillTool(_SkillReader, Tool):
    """同步工作流的按需技能加载工具"""

    def execute(self, skill: str) -> str:
        """
        同步返回技能说明

        参数:
        - skill: 精确技能标识

        返回:
        - 技能全文或不可用提示
        """
        return self.read(skill)
