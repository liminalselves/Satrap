from __future__ import annotations
from typing import Any, Dict
from satrap.core.utils.TCBuilder import AsyncTool, create_tool_defined
from .manager import SkillsManager


class SkillTool(AsyncTool):
    """
    技能加载工具; 模型可按需调用以获取技能指令 (动态技能加载路线)

    注册进 AsyncToolsManager 后, 模型在需要特定专业能力时会调用 `load_skill` 获取指令
    """

    tool_name = "load_skill"
    recovery_policy = "retry"  # 只返回技能文本, 不执行技能中的指令
    description = (
        "加载指定技能, 返回技能指令与可用工具列表; 当任务需要特定专业能力时调用"
    )
    params_dict = {
        "skill": ("string", "技能名称"),
        "task": ("string", "要执行的任务描述, 可选"),
    }

    def __init__(self, skills_manager: SkillsManager):
        """
        初始化 SkillTool

        参数:
        - skills_manager: 技能列表管理器
        """
        self.skills_manager = skills_manager
        super().__init__()

    def get_tool_defined(self) -> Dict[str, Any]:
        """
        动态生成工具定义, 描述中包含当前可用技能列表

        返回:
        - Dict[str, Any]: 动态生成工具定义, 描述中包含当前可用技能列表
        """
        if not self.assert_tool():
            return {}
        available = ", ".join(self.skills_manager.list_skills()) or "无"
        return create_tool_defined(
            self.tool_name,
            f"加载指定技能, 返回技能指令与可用工具列表; 当任务需要特定专业能力时调用。可用技能: {available}",
            self.params_dict,
        )

    async def execute(self, skill: str, task: str = "") -> str:
        """
        返回指定技能的指令文本

        参数:
        - skill: 技能实例
        - task: 任务描述

        返回:
        - str: 指定技能的指令文本
        """
        s = self.skills_manager.get_skill(skill)
        if s is None:
            available = ", ".join(self.skills_manager.list_skills()) or "无"
            return f"技能 {skill} 不存在, 可用技能: {available}"
        text = s.to_text()
        if task:
            text = f"{text}\n\n任务: {task}"
        return text
