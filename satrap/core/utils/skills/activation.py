from __future__ import annotations
import inspect
from typing import Any, List
from satrap.core.log import logger
from satrap.core.type import safe_getattr
from .utils import SkillWorkflowProtocol
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import SkillsManager


def activate(
    self: SkillsManager, skill_name: str, workflow: SkillWorkflowProtocol
) -> bool:
    """
    将技能装配进 workflow: 指令注入系统提示词 + 注册自带工具 + 启用关联工具 (同步版)

    参数:
    - skill_name: 技能名称
    - workflow: 含 `ctx` (ContextManager) 与 `tools_manager` (ToolsManager) 的工作流

    返回:
    - bool: 是否激活成功
    """
    skill = self.get_skill(skill_name)
    if skill is None:
        logger.warning(f"[技能管理] 技能 {skill_name} 不存在")
        return False
    wf_id = id(workflow)
    key = skill.skill_id or skill.name
    if self._active.get(wf_id) == key:
        return True

    workflow.ctx.add_at_system_end(skill.to_text(), separator="\n\n")
    self._register_bundled_tools(workflow, skill)
    enabled = self._apply_tools(workflow, skill.tool_names, enable=True)
    if skill.mcp_clients:
        logger.warning(
            f"[技能管理] 技能 {skill.name} 含 MCP 客户端, 请使用 activate_async 激活以自动连接"
        )
    self._active[wf_id] = key
    logger.info(
        f"[技能管理] 技能 {skill.name} 已激活 (启用工具 {enabled}/{len(skill.tool_names)})"
    )
    return True


async def activate_async(
    self: SkillsManager, skill_name: str, workflow: SkillWorkflowProtocol
) -> bool:
    """
    将技能装配进 workflow (异步版): 额外自动连接并注册技能自带的 MCP 客户端

    参数:
    - skill_name: skill名称
    - workflow: 工作流实例

    返回:
    - bool: 将技能装配进 workflow (异步版): 额外自动连接并注册技能自带的 MCP 客户端
    """
    skill = self.get_skill(skill_name)
    if skill is None:
        logger.warning(f"[技能管理] 技能 {skill_name} 不存在")
        return False
    wf_id = id(workflow)
    key = skill.skill_id or skill.name
    if self._active.get(wf_id) == key:
        return True

    result = workflow.ctx.add_at_system_end(skill.to_text(), separator="\n\n")
    if inspect.isawaitable(result):
        await result
    self._register_bundled_tools(workflow, skill)
    enabled = self._apply_tools(workflow, skill.tool_names, enable=True)

    tools_manager = safe_getattr(workflow, "tools_manager")
    connected: List[Any] = []
    if tools_manager is not None:
        for client in skill.mcp_clients:
            try:
                await client.register_tools(tools_manager)
                connected.append(client)
            except Exception as e:
                logger.error(f"[技能管理] 技能 {skill.name} 的 MCP 客户端连接失败: {e}")
    if connected:
        self._active_mcp[wf_id] = connected

    self._active[wf_id] = key
    logger.info(
        f"[技能管理] 技能 {skill.name} 已激活 (启用工具 {enabled}/{len(skill.tool_names)})"
    )
    return True


def deactivate(
    self: SkillsManager, skill_name: str, workflow: SkillWorkflowProtocol
) -> bool:
    """
    取消技能激活: 从系统提示词剥离指令块 + 禁用关联工具 (同步版)

    参数:
    - skill_name: skill名称
    - workflow: 工作流实例

    返回:
    - bool: 取消技能激活: 从系统提示词剥离指令块 + 禁用关联工具 (同步版)
    """
    skill = self.get_skill(skill_name)
    if skill is None:
        logger.warning(f"[技能管理] 技能 {skill_name} 不存在")
        return False
    wf_id = id(workflow)
    key = skill.skill_id or skill.name
    if self._active.get(wf_id) != key:
        return True

    self._strip_from_system(workflow, key)
    self._sync_context(workflow)
    self._apply_tools(workflow, skill.tool_names, enable=False)
    self._active.pop(wf_id, None)
    logger.info(f"[技能管理] 技能 {skill.name} 已取消激活")
    return True


async def deactivate_async(
    self: SkillsManager, skill_name: str, workflow: SkillWorkflowProtocol
) -> bool:
    """
    取消技能激活 (异步版): 额外关闭技能自带的 MCP 客户端连接

    参数:
    - skill_name: skill名称
    - workflow: 工作流实例

    返回:
    - bool: 取消技能激活 (异步版): 额外关闭技能自带的 MCP 客户端连接
    """
    skill = self.get_skill(skill_name)
    if skill is None:
        logger.warning(f"[技能管理] 技能 {skill_name} 不存在")
        return False
    wf_id = id(workflow)
    key = skill.skill_id or skill.name
    if self._active.get(wf_id) != key:
        return True

    self._strip_from_system(workflow, key)
    sync_result = self._sync_context(workflow)
    if inspect.isawaitable(sync_result):
        await sync_result
    self._apply_tools(workflow, skill.tool_names, enable=False)

    for client in self._active_mcp.pop(wf_id, []):
        try:
            await client.close()
        except Exception as e:
            logger.error(f"[技能管理] MCP 客户端关闭失败: {e}")
    self._active.pop(wf_id, None)
    logger.info(f"[技能管理] 技能 {skill.name} 已取消激活")
    return True
