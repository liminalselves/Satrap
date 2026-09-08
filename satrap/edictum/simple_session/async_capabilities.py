from __future__ import annotations
from typing import Any, Callable
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.core.utils.skills import SkillsManager
from satrap.core.type import safe_getattr_callable
from satrap.core.log import logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .async_ import AsyncSimpleSession


def add_command(
    self: AsyncSimpleSession,
    name: str,
    handler: Callable[..., Any],
    intro: str = "None",
):
    """
    注册命令

    参数:
    - name: 名称
    - handler: 处理器
    - intro: 简介文本
    """
    self.command_handler.register_command(name, handler, intro=intro)


def remove_command(self: AsyncSimpleSession, name: str) -> bool:
    """
    注销命令

    参数:
    - name: 名称

    返回:
    - bool: 注销命令
    """
    return self.command_handler.unregister_command(name)


def enable_command(self: AsyncSimpleSession, name: str) -> bool:
    """
    启用命令

    参数:
    - name: 名称

    返回:
    - bool: 启用命令
    """
    return self.command_handler.enable_command(name)


def disable_command(self: AsyncSimpleSession, name: str) -> bool:
    """
    停用命令

    参数:
    - name: 名称

    返回:
    - bool: 停用命令
    """
    return self.command_handler.disable_command(name)


def is_command_enabled(self: AsyncSimpleSession, name: str) -> bool:
    """
    检查命令是否启用

    参数:
    - name: 名称

    返回:
    - bool: 检查结果
    """
    return self.command_handler.is_command_enabled(name)


def list_commands(self: AsyncSimpleSession) -> dict[str, str]:
    """
    列出已注册命令及其简介

    返回:
    - dict[str, str]: 列出已注册命令及其简介
    """
    return self.command_handler.list_commands()


def add_tool(self: AsyncSimpleSession, tool: AsyncTool):
    """
    注册工具 (初始化前注册会延迟到 initialize 时生效)

    参数:
    - tool: 工具
    """
    if self._wf is None:
        self._init_tools.append(tool)
    else:
        self._wf.tools_manager.register_tool(tool)


def add_tools(self: AsyncSimpleSession, *tools: AsyncTool):
    """
    批量注册工具

    参数:
    - tools: 工具列表
    """
    for tool in tools:
        self.add_tool(tool)


def remove_tool(self: AsyncSimpleSession, name: str) -> bool:
    """
    注销工具

    参数:
    - name: 名称

    返回:
    - bool: 注销工具
    """
    return self._require_wf().tools_manager.unregister_tool(name)


def enable_tool(self: AsyncSimpleSession, name: str) -> bool:
    """
    启用工具

    参数:
    - name: 名称

    返回:
    - bool: 启用工具
    """
    return self._require_wf().tools_manager.enable_tool(name)


def disable_tool(self: AsyncSimpleSession, name: str) -> bool:
    """
    停用工具

    参数:
    - name: 名称

    返回:
    - bool: 停用工具
    """
    return self._require_wf().tools_manager.disable_tool(name)


def is_tool_enabled(self: AsyncSimpleSession, name: str) -> bool:
    """
    检查工具是否启用

    参数:
    - name: 名称

    返回:
    - bool: 检查结果
    """
    return self._require_wf().tools_manager.is_tool_enabled(name)


def list_tools(self: AsyncSimpleSession) -> list[str]:
    """
    列出已注册工具名

    返回:
    - list[str]: 列出已注册工具名
    """
    return list(self._require_wf().tools_manager.tools.keys())


async def add_skill(
    self: AsyncSimpleSession,
    skill_name: str,
    skills_manager: SkillsManager | None = None,
) -> bool:
    """
    加载并激活技能到主工作流 (初始化前注册会延迟到 initialize 时生效)

    参数:
    - skill_name: skill名称
    - skills_manager: skills管理器

    返回:
    - bool: 加载并激活技能到主工作流 (初始化前注册会延迟到 initialize 时生效)
    """
    mgr = skills_manager or self._get_skills_manager()
    if skills_manager is not None:
        self._skills_manager = skills_manager
    if self._wf is None:
        if mgr.get_skill(skill_name) is None:
            return False
        self._init_skills.append(skill_name)
        return True
    return await mgr.activate_async(skill_name, self._require_wf())


async def remove_skill(self: AsyncSimpleSession, skill_name: str) -> bool:
    """
    从工作流卸载并从管理器移除技能定义

    参数:
    - skill_name: skill名称

    返回:
    - bool: 从工作流卸载并从管理器移除技能定义
    """
    mgr = self._skills_manager
    if mgr is None:
        return False
    removed = await mgr.deactivate_async(skill_name, self._require_wf())
    mgr.unregister_skill(skill_name)
    return removed


async def enable_skill(self: AsyncSimpleSession, skill_name: str) -> bool:
    """
    重新激活技能

    参数:
    - skill_name: skill名称

    返回:
    - bool: 重新激活技能
    """
    return await self.add_skill(skill_name)


async def disable_skill(self: AsyncSimpleSession, skill_name: str) -> bool:
    """
    停用技能 (从工作流卸载, 保留注册)

    参数:
    - skill_name: skill名称

    返回:
    - bool: 停用技能 (从工作流卸载, 保留注册)
    """
    if self._skills_manager is None:
        return False
    return await self._skills_manager.deactivate_async(skill_name, self._require_wf())


async def add_mcp(
    self: AsyncSimpleSession, name: str, client: Any, name_prefix: str | None = None
) -> list[Any]:
    """
    接入 MCP Server, 将远程工具注册到主工作流

    参数:
    - name: MCP 连接名 (用于后续启停/删除)
    - client: MCPClient 实例
    - name_prefix: 工具名前缀

    返回:
    - 注册的工具适配器列表
    """
    if self._wf is None:
        await self.initialize()
    wf = self._require_wf()
    try:
        adapters = await client.register_tools(
            wf.tools_manager, name_prefix=name_prefix
        )
    except Exception:
        close = safe_getattr_callable(client, "close")
        if close is not None:
            await close()
        raise
    self._mcp_clients[name] = (client, list(adapters))
    return list(adapters)


async def remove_mcp(self: AsyncSimpleSession, name: str) -> bool:
    """
    移除 MCP 连接: 注销其全部工具并断开连接

    参数:
    - name: 名称

    返回:
    - bool: 移除 MCP 连接: 注销其全部工具并断开连接
    """
    entry = self._mcp_clients.pop(name, None)
    if entry is None:
        return False
    client, adapters = entry
    wf = self._require_wf()
    for adapter in adapters:
        wf.tools_manager.unregister_tool(adapter.get_tool_name())
    close = safe_getattr_callable(client, "close")
    if close is not None:
        try:
            await close()
        except Exception as e:
            logger.warning(f"[edictum] MCP {name} 断开失败: {e}")
    return True


def enable_mcp(self: AsyncSimpleSession, name: str) -> bool:
    """
    启用 MCP 连接的全部工具

    参数:
    - name: 名称

    返回:
    - bool: 启用 MCP 连接的全部工具
    """
    entry = self._mcp_clients.get(name)
    if entry is None:
        return False
    wf = self._require_wf()
    for adapter in entry[1]:
        wf.tools_manager.enable_tool(adapter.get_tool_name())
    return True


def disable_mcp(self: AsyncSimpleSession, name: str) -> bool:
    """
    停用 MCP 连接的全部工具

    参数:
    - name: 名称

    返回:
    - bool: 停用 MCP 连接的全部工具
    """
    entry = self._mcp_clients.get(name)
    if entry is None:
        return False
    wf = self._require_wf()
    for adapter in entry[1]:
        wf.tools_manager.disable_tool(adapter.get_tool_name())
    return True


def list_mcp(self: AsyncSimpleSession) -> list[str]:
    """
    列出已接入的 MCP 连接名

    返回:
    - list[str]: 列出已接入的 MCP 连接名
    """
    return list(self._mcp_clients.keys())


def _tool_effective(self: AsyncSimpleSession, tool_name: str) -> bool:
    """
    工具生效过滤 (执行路径合成): 所属插件缺失或启用时生效

    参数:
    - tool_name: 工具名称

    返回:
    - bool: 工具生效过滤 (执行路径合成): 所属插件缺失或启用时生效
    """
    wf = self._wf
    if wf is None:
        return True
    tool = wf.tools_manager.tools.get(tool_name)
    owner = tool.owner_plugin if tool is not None else None
    if owner is None:
        return True
    with self._registry_lock:
        plugin = self._plugins.get(owner)
    return plugin is not None and plugin.enabled


def set_llm(self: AsyncSimpleSession, llm: AsyncLLM):
    """
    替换主模型 (未初始化时延迟到 initialize 生效)

    参数:
    - llm: 模型实例
    """
    if self._wf is None:
        self._init_llm = llm
        return
    self._wf.llm = llm


def set_model_parameters(self: AsyncSimpleSession, **kwargs: Any):
    """
    调整主模型调用参数 (未初始化时延迟到 initialize 生效)

    参数:
    - kwargs: 额外关键字参数
    """
    if self._wf is None:
        self._init_model_params.update(kwargs)
        return
    self._wf.llm.set_parameters(**kwargs)


def reload_llm(self: AsyncSimpleSession, llm: AsyncLLM):
    """
    重载 LLM 实例 (AsyncSession 兼容接口)

    参数:
    - llm: 模型实例
    """
    self.set_llm(llm)
