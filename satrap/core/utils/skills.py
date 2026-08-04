"""
技能 (Skill) 扩展

Skill = 一段指令文本 (instructions) + 一组关联工具 + 可选自带工具 / MCP 客户端,
装配到 workflow 时:
1. 指令注入系统提示词 (ContextManager / AsyncContextManager)
2. 关联工具注册并启用 (ToolsManager / AsyncToolsManager)
3. 自带的 MCP 客户端 (若有) 自动连接并将远端工具注册进工具管理器

技能以文件夹为单位组织, 每个技能一个目录, 位于技能扫描目录下:

``` text
skills/
└── coding_agent/          # 技能文件夹, 文件夹名即技能名 (若 skill.md 未声明 name)
    ├── skill.md           # 技能指令 (Markdown, 支持 YAML front matter)
    ├── tools.py           # 可选: 自带工具与 MCP 客户端 (约定见下)
    └── meta.yaml          # 可选: 作者, 版本等信息
```

`skill.md` 的 front matter:
``` markdown
---
name: coding_agent
description: 代码生成与调试助手
tools:
  - code_sandbox
  - search
---
<技能指令正文...>
```

`tools.py` (可选) 约定:
- `get_tools()`: 返回工具实例列表 (构造函数需要参数的场景)
- `get_mcp_clients()`: 返回 MCPClient 实例列表 (激活时自动连接并注册)
- 未定义 get_tools 时, 模块内定义的 Tool / AsyncTool 子类会被自动实例化并收集
- 自带工具名会自动加入技能工具列表, 无需在 front matter 中重复声明

也兼容旧式单文件技能: 直接把 .md 文件放在扫描目录下即可。

用法示例:
``` python
from satrap import SkillsManager   # 或 from satrap.core.utils.skills import SkillsManager

skills = SkillsManager(skills_dir=".satrap/skills")
skills.scan()
skills.activate("coding_agent", workflow)          # 同步 workflow
await skills.activate_async("coding_agent", workflow)   # 异步 workflow (含 MCP 连接)
skills.deactivate("coding_agent", workflow)        # 取消激活
```
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml

from satrap.core.log import logger
from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.utils.TCBuilder import AsyncTool, Tool, ToolsManager, AsyncToolsManager

SKILLS_PRESET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "expend",
    "skills",
)
"""内置技能示例目录 (satrap/expend/skills)"""

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SKILL_TOOLS_MODULE_COUNTER = 0


class Skill:
    """技能; 由名称, 指令文本, 关联工具与可选自带工具组成"""

    def __init__(
        self,
        name: str,
        instructions: str,
        tool_names: Optional[List[str]] = None,
        description: str = "",
        source: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Any]] = None,
        mcp_clients: Optional[List[Any]] = None,
    ):
        """
        参数:
        - name: 技能名称
        - instructions: 技能指令文本, 激活时注入系统提示词
        - tool_names: 关联工具名列表, 激活时在 ToolsManager 中启用
        - description: 技能简介
        - source: 技能来源路径 (md 文件或文件夹, 可选)
        - meta: 元信息 (作者, 版本等, 来自 meta.yaml)
        - tools: 自带工具实例列表 (来自 tools.py)
        - mcp_clients: 自带 MCP 客户端实例列表 (来自 tools.py)
        """
        self.name = name
        self.instructions = instructions
        self.tool_names = list(tool_names or [])
        self.description = description
        self.source = source
        self.meta: Dict[str, Any] = dict(meta or {})
        self.tools: List[Union[Tool, AsyncTool]] = list(tools or [])
        self.mcp_clients: List[Any] = list(mcp_clients or [])

    @classmethod
    def from_file(cls, file_path: str, default_name: Optional[str] = None) -> "Skill":
        """从 Markdown 技能文件加载技能 (支持 YAML front matter)

        参数:
        - file_path: 技能 md 文件路径
        - default_name: front matter 未声明 name 时使用的名称, 默认取文件名
        """
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        meta, body = _parse_front_matter(text)
        name = str(
            meta.get("name")
            or default_name
            or os.path.splitext(os.path.basename(file_path))[0]
        )
        tools = meta.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        description = str(meta.get("description") or "")
        return cls(
            name=name,
            instructions=body.strip(),
            tool_names=[str(t) for t in tools],
            description=description,
            source=file_path,
        )

    def to_text(self) -> str:
        """生成可注入系统提示词的指令块 (带技能标记, 便于反激活时剥离)"""
        lines = [f"<skill:{self.name}>"]
        if self.description:
            lines.append(f"描述: {self.description}")
        if self.instructions:
            lines.append(self.instructions)
        if self.tool_names:
            lines.append(f"可用工具: {', '.join(self.tool_names)}")
        lines.append(f"</skill:{self.name}>")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Skill(name={self.name!r}, tools={self.tool_names!r})"


def _parse_front_matter(text: str):
    """解析 Markdown 文本的 YAML front matter, 返回 (元数据字典, 正文)"""
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception as e:
        logger.warning(f"[技能] front matter 解析失败: {e}")
        meta = {}
    return meta, text[match.end():]


def _load_skill_tools(tools_path: str) -> Tuple[List[Any], List[Any]]:
    """从技能的 tools.py 加载自带工具与 MCP 客户端

    支持:
    - `get_tools()` 工厂函数, 返回工具实例列表
    - `get_mcp_clients()` 工厂函数, 返回 MCPClient 实例列表
    - 模块内定义的 Tool / AsyncTool 子类 (无参构造函数自动实例化)

    返回:
    - (工具实例列表, MCPClient 实例列表)
    """
    global _SKILL_TOOLS_MODULE_COUNTER
    _SKILL_TOOLS_MODULE_COUNTER += 1
    module_name = f"_satrap_skill_tools_{_SKILL_TOOLS_MODULE_COUNTER}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, tools_path)
        if spec is None or spec.loader is None:
            return [], []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        logger.error(f"[技能管理] 加载自带工具失败: {tools_path}: {e}")
        return [], []

    tools: List[Any] = []
    get_tools = getattr(module, "get_tools", None)
    if callable(get_tools):
        try:
            result = get_tools()
            tools.extend(result if isinstance(result, (list, tuple)) else [result])
        except Exception as e:
            logger.error(f"[技能管理] get_tools() 执行失败: {e}")
    else:
        for attr in vars(module).values():
            if not (isinstance(attr, type) and getattr(attr, "__module__", None) == module.__name__):
                continue
            if issubclass(attr, (Tool, AsyncTool)):
                try:
                    tools.append(attr())
                except TypeError:
                    pass   # 构造函数需要参数, 应通过 get_tools() 提供

    mcp_clients: List[Any] = []
    get_mcp_clients = getattr(module, "get_mcp_clients", None)
    if callable(get_mcp_clients):
        try:
            result = get_mcp_clients()
            mcp_clients.extend(result if isinstance(result, (list, tuple)) else [result])
        except Exception as e:
            logger.error(f"[技能管理] get_mcp_clients() 执行失败: {e}")

    return tools, mcp_clients


class SkillsManager:
    """技能管理器; 负责扫描, 加载技能, 并将其装配到 workflow"""

    def __init__(self, skills_dir: Optional[str] = None):
        """
        参数:
        - skills_dir: 技能扫描目录, 默认 None (不自动扫描, 需调用 scan 指定)
        """
        self.skills_dir = skills_dir
        self.skills: Dict[str, Skill] = {}
        self._active: Dict[int, str] = {}            # workflow id -> skill name, 防止重复注入
        self._active_mcp: Dict[int, List[Any]] = {}  # workflow id -> 已连接的 MCP 客户端

    def scan(self, skills_dir: Optional[str] = None) -> List[Skill]:
        """扫描技能目录并加载全部技能

        支持两种结构:
        - 文件夹式: 目录下每个子文件夹含 skill.md (推荐, 可带 tools.py / meta.yaml)
        - 单文件式: 目录下的 .md / .markdown 文件

        参数:
        - skills_dir: 扫描目录, 默认使用构造时的 skills_dir

        返回:
        - 加载的技能列表
        """
        base = skills_dir or self.skills_dir
        if not base or not os.path.isdir(base):
            logger.warning(f"[技能管理] 技能目录不存在: {base}")
            return []
        found: List[Skill] = []
        for entry in sorted(os.listdir(base)):
            entry_path = os.path.join(base, entry)
            try:
                if os.path.isdir(entry_path):
                    skill = self._load_skill_dir(entry_path)
                    if skill is not None:
                        self.skills[skill.name] = skill
                        found.append(skill)
                        logger.info(f"[技能管理] 已加载技能: {skill.name} <- {entry}/")
                elif entry.endswith((".md", ".markdown")):
                    skill = Skill.from_file(entry_path)
                    self.skills[skill.name] = skill
                    found.append(skill)
                    logger.info(f"[技能管理] 已加载技能: {skill.name} <- {entry}")
            except Exception as e:
                logger.error(f"[技能管理] 加载技能 {entry} 失败: {e}")
        return found

    def _load_skill_dir(self, skill_dir: str) -> Optional[Skill]:
        """从技能文件夹加载技能: skill.md + meta.yaml + tools.py"""
        md_path = os.path.join(skill_dir, "skill.md")
        if not os.path.isfile(md_path):
            return None
        skill = Skill.from_file(md_path, default_name=os.path.basename(skill_dir))
        skill.source = skill_dir

        meta_path = os.path.join(skill_dir, "meta.yaml")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = yaml.safe_load(f) or {}
                if isinstance(meta, dict):
                    skill.meta.update(meta)
            except Exception as e:
                logger.warning(f"[技能管理] meta.yaml 解析失败: {meta_path}: {e}")

        tools_path = os.path.join(skill_dir, "tools.py")
        if os.path.isfile(tools_path):
            tools, mcp_clients = _load_skill_tools(tools_path)
            skill.tools = tools
            skill.mcp_clients = mcp_clients
            for tool in tools:
                name = tool.get_tool_name()
                if name not in skill.tool_names:
                    skill.tool_names.append(name)
        return skill

    def load_skill(self, name: str, file_path: str) -> Skill:
        """从指定文件加载单个技能并登记"""
        skill = Skill.from_file(file_path)
        if name and name != skill.name:
            skill.name = name
        self.skills[skill.name] = skill
        return skill

    def get_skill(self, name: str) -> Optional[Skill]:
        """按名称获取技能"""
        return self.skills.get(name)

    def has_skill(self, name: str) -> bool:
        """检查技能是否存在"""
        return name in self.skills

    def list_skills(self) -> List[str]:
        """获取所有已加载技能的名称列表"""
        return list(self.skills.keys())

    # ================= 装配到 workflow =================

    def activate(self, skill_name: str, workflow) -> bool:
        """将技能装配进 workflow: 指令注入系统提示词 + 注册自带工具 + 启用关联工具 (同步版)

        参数:
        - skill_name: 技能名称
        - workflow: 含 `ctx` (ContextManager) 与 `tools_manager` (ToolsManager) 的工作流

        返回:
        - bool: 是否激活成功
        """
        skill = self.skills.get(skill_name)
        if skill is None:
            logger.warning(f"[技能管理] 技能 {skill_name} 不存在")
            return False
        wf_id = id(workflow)
        if self._active.get(wf_id) == skill_name:
            return True

        workflow.ctx.add_at_system_end(skill.to_text(), separator="\n\n")
        self._register_bundled_tools(workflow, skill)
        enabled = self._apply_tools(workflow, skill.tool_names, enable=True)
        if skill.mcp_clients:
            logger.warning(
                f"[技能管理] 技能 {skill_name} 含 MCP 客户端, 请使用 activate_async 激活以自动连接"
            )
        self._active[wf_id] = skill_name
        logger.info(f"[技能管理] 技能 {skill_name} 已激活 (启用工具 {enabled}/{len(skill.tool_names)})")
        return True

    async def activate_async(self, skill_name: str, workflow) -> bool:
        """将技能装配进 workflow (异步版): 额外自动连接并注册技能自带的 MCP 客户端"""
        skill = self.skills.get(skill_name)
        if skill is None:
            logger.warning(f"[技能管理] 技能 {skill_name} 不存在")
            return False
        wf_id = id(workflow)
        if self._active.get(wf_id) == skill_name:
            return True

        await workflow.ctx.add_at_system_end(skill.to_text(), separator="\n\n")
        self._register_bundled_tools(workflow, skill)
        enabled = self._apply_tools(workflow, skill.tool_names, enable=True)

        tools_manager = getattr(workflow, "tools_manager", None)
        connected: List[Any] = []
        if tools_manager is not None:
            for client in skill.mcp_clients:
                try:
                    await client.register_tools(tools_manager)
                    connected.append(client)
                except Exception as e:
                    logger.error(f"[技能管理] 技能 {skill_name} 的 MCP 客户端连接失败: {e}")
        if connected:
            self._active_mcp[wf_id] = connected

        self._active[wf_id] = skill_name
        logger.info(f"[技能管理] 技能 {skill_name} 已激活 (启用工具 {enabled}/{len(skill.tool_names)})")
        return True

    def deactivate(self, skill_name: str, workflow) -> bool:
        """取消技能激活: 从系统提示词剥离指令块 + 禁用关联工具 (同步版)"""
        skill = self.skills.get(skill_name)
        if skill is None:
            logger.warning(f"[技能管理] 技能 {skill_name} 不存在")
            return False
        wf_id = id(workflow)
        if self._active.get(wf_id) != skill_name:
            return True

        self._strip_from_system(workflow, skill_name)
        self._sync_context(workflow)
        self._apply_tools(workflow, skill.tool_names, enable=False)
        self._active.pop(wf_id, None)
        logger.info(f"[技能管理] 技能 {skill_name} 已取消激活")
        return True

    async def deactivate_async(self, skill_name: str, workflow) -> bool:
        """取消技能激活 (异步版): 额外关闭技能自带的 MCP 客户端连接"""
        skill = self.skills.get(skill_name)
        if skill is None:
            logger.warning(f"[技能管理] 技能 {skill_name} 不存在")
            return False
        wf_id = id(workflow)
        if self._active.get(wf_id) != skill_name:
            return True

        self._strip_from_system(workflow, skill_name)
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
        logger.info(f"[技能管理] 技能 {skill_name} 已取消激活")
        return True

    # ================= 内部方法 =================

    def _register_bundled_tools(self, workflow, skill: Skill) -> int:
        """注册技能自带的工具实例 (已注册的同名工具跳过), 返回新注册数"""
        tools_manager: Union[ToolsManager, AsyncToolsManager, None] = getattr(workflow, "tools_manager", None)
        if tools_manager is None or not skill.tools:
            return 0

        registered = 0
        for tool in skill.tools:
            name = tool.get_tool_name()
            if name not in getattr(tools_manager, "tools", {}):
                tools_manager.register_tool(tool)   # type: ignore
                registered += 1

        return registered

    def _apply_tools(self, workflow, tool_names: List[str], enable: bool) -> int:
        """启用或禁用关联工具, 返回实际生效的工具数"""
        tools_manager: Union[ToolsManager, AsyncToolsManager, None] = getattr(workflow, "tools_manager", None)
        if tools_manager is None or not tool_names:
            return 0
        applied = 0
        for name in tool_names:
            if name not in getattr(tools_manager, "tools", {}):
                logger.warning(f"[技能管理] 工具 {name} 未注册, 已跳过")
                continue
            if enable:
                tools_manager.enable_tool(name)
            else:
                tools_manager.disable_tool(name)
            applied += 1
        return applied

    @staticmethod
    def _strip_from_system(workflow: Union[ModelWorkflowFramework, AsyncModelWorkflowFramework], skill_name: str):
        """从系统提示词中剥离 <skill:name>...</skill:name> 指令块"""
        pattern = re.compile(
            rf"\n*<skill:{re.escape(skill_name)}>.*?</skill:{re.escape(skill_name)}>",
            re.DOTALL,
        )
        for msg in workflow.ctx._messages:
            content = msg.get("content")
            if msg.get("role") == "system" and isinstance(content, str):
                msg["content"] = pattern.sub("", content)

    @staticmethod
    def _sync_context(workflow):
        """触发上下文落库, 返回 _sync() 的结果 (异步管理器返回协程)"""
        sync = getattr(workflow.ctx, "_sync", None)
        if sync is not None:
            return sync()
        return None


class SkillTool(AsyncTool):
    """技能加载工具; 模型可按需调用以获取技能指令 (动态技能加载路线)

    注册进 AsyncToolsManager 后, 模型在需要特定专业能力时会调用 `load_skill` 获取指令。
    """

    tool_name = "load_skill"
    description = "加载指定技能, 返回技能指令与可用工具列表; 当任务需要特定专业能力时调用"
    params_dict = {
        "skill": ("string", "技能名称"),
        "task": ("string", "要执行的任务描述, 可选"),
    }

    def __init__(self, skills_manager: SkillsManager):
        self.skills_manager = skills_manager
        super().__init__()

    def get_tool_defined(self) -> Dict[str, Any]:
        """动态生成工具定义, 描述中包含当前可用技能列表"""
        if not self.assert_tool():
            return {}
        available = ", ".join(self.skills_manager.list_skills()) or "无"
        from satrap.core.utils.TCBuilder import create_tool_defined
        return create_tool_defined(
            self.tool_name,
            f"加载指定技能, 返回技能指令与可用工具列表; 当任务需要特定专业能力时调用。可用技能: {available}",
            self.params_dict,
        )

    async def execute(self, skill: str, task: str = "") -> str:
        """返回指定技能的指令文本"""
        s = self.skills_manager.get_skill(skill)
        if s is None:
            available = ", ".join(self.skills_manager.list_skills()) or "无"
            return f"技能 {skill} 不存在, 可用技能: {available}"
        text = s.to_text()
        if task:
            text = f"{text}\n\n任务: {task}"
        return text


__all__ = [
    "Skill",
    "SkillsManager",
    "SkillTool",
    "SKILLS_PRESET_DIR",
]
