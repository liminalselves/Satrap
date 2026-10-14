from __future__ import annotations
import inspect
from typing import Any, List, cast
from satrap.core.log import logger
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
    if key in self._active.get(wf_id, {}):
        return True
    self._strip_from_system(workflow, key)
    workflow.ctx.add_at_system_end(skill.to_text(), separator="\n\n")
    before = set(workflow.tools_manager.tools) if workflow.tools_manager is not None else set()
    previous_tools = [(tool, tool.is_enabled()) for tool in workflow.tools_manager.tools.values()] if workflow.tools_manager is not None else []
    self._active.setdefault(wf_id, {})[key] = skill
    connected: List[tuple[Any, List[str]]] = []
    self._active_mcp.setdefault(wf_id, {})[key] = connected
    try:
        self._register_bundled_tools(workflow, skill)
        enabled = self._apply_tools(workflow, [*skill.tool_names, *(tool.get_tool_name() for tool in skill.tools)], enable=True)
        if workflow.tools_manager is not None:
            for client in skill.mcp_clients:
                register = getattr(client, "sync_register_tools", None)
                if not callable(register):
                    raise TypeError(f"技能 {skill.name} 的 MCP 客户端缺少同步注册接口")
                connected.append((client, []))
                adapters = cast(List[Any], register(workflow.tools_manager)) or []
                connected[-1] = (client, [adapter.get_tool_name() for adapter in adapters])
    except BaseException:
        if workflow.tools_manager is not None:
            self._owned_tools.setdefault(wf_id, set()).update(set(workflow.tools_manager.tools) - before)
        deactivate(self, skill_name, workflow)
        for tool, was_enabled in previous_tools:
            tool.enable() if was_enabled else tool.disable()   # 激活失败时恢复已有工具的启用状态
        raise
    if workflow.tools_manager is not None:
        self._owned_tools.setdefault(wf_id, set()).update(set(workflow.tools_manager.tools) - before)
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
    if key in self._active.get(wf_id, {}):
        return True
    self._strip_from_system(workflow, key)
    result = workflow.ctx.add_at_system_end(skill.to_text(), separator="\n\n")
    if inspect.isawaitable(result):
        await result
    before = set(workflow.tools_manager.tools) if workflow.tools_manager is not None else set()
    tools_manager = workflow.tools_manager
    previous_tools = [(tool, tool.is_enabled()) for tool in tools_manager.tools.values()] if tools_manager is not None else []
    connected: List[tuple[Any, List[str]]] = []
    self._active.setdefault(wf_id, {})[key] = skill
    self._active_mcp.setdefault(wf_id, {})[key] = connected
    try:
        self._register_bundled_tools(workflow, skill)
        enabled = self._apply_tools(workflow, [*skill.tool_names, *(tool.get_tool_name() for tool in skill.tools)], enable=True)
        if tools_manager is not None:
            for client in skill.mcp_clients:
                connected.append((client, []))
                adapters = await client.register_tools(tools_manager) or []
                connected[-1] = (client, [adapter.get_tool_name() for adapter in adapters])
    except BaseException:
        if tools_manager is not None:
            self._owned_tools.setdefault(wf_id, set()).update(set(tools_manager.tools) - before)
        await deactivate_async(self, skill_name, workflow)
        for tool, was_enabled in previous_tools:
            tool.enable() if was_enabled else tool.disable()   # 激活失败时恢复已有工具的启用状态
        raise
    if tools_manager is not None:
        self._owned_tools.setdefault(wf_id, set()).update(set(tools_manager.tools) - before)
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
    self._strip_from_system(workflow, key)
    self._sync_context(workflow)
    active = self._active.get(wf_id, {})
    was_active = active.pop(key, None) is not None
    clients = self._active_mcp.get(wf_id, {})
    for client, _ in clients.pop(key, []):
        try:
            client.sync_close()
        except Exception as error:
            logger.error(f"[技能管理] MCP 客户端关闭失败: {error}")
    if was_active:
        self._release_skill_tools(skill, workflow)
    if not active:
        self._active.pop(wf_id, None)
    if not clients:
        self._active_mcp.pop(wf_id, None)
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
    self._strip_from_system(workflow, key)
    sync_result = self._sync_context(workflow)
    if inspect.isawaitable(sync_result):
        await sync_result
    active = self._active.get(wf_id, {})
    was_active = active.pop(key, None) is not None
    clients = self._active_mcp.get(wf_id, {})
    for client, _ in clients.pop(key, []):
        try:
            await client.close()
        except Exception as e:
            logger.error(f"[技能管理] MCP 客户端关闭失败: {e}")
    if was_active:
        self._release_skill_tools(skill, workflow)
    if not active:
        self._active.pop(wf_id, None)
    if not clients:
        self._active_mcp.pop(wf_id, None)
    logger.info(f"[技能管理] 技能 {skill.name} 已取消激活")
    return True
