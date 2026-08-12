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
└── coding-agent/        # 技能文件夹, 文件夹名即技能名 (若 skill.md 未声明 name)
    ├── skill.md           # 技能指令 (Markdown, 支持 YAML front matter)
    ├── tools.py           # 可选: 自带工具与 MCP 客户端 (约定见下)
    └── meta.yaml          # 可选: 作者, 版本, satrap-skill-id 等
```

扫描目录区分两层:
- 官方预设目录 `satrap/expend/skills` (只读基线)
- 用户技能目录 `skills_dir` (默认 `.satrap/skills`, 用户自添加, 同名无 id 时官方优先)

`meta.yaml` 的 `satrap-skill-id` 是技能身份识别符 (小写字母/数字/连字符):
同名技能携带不同 id 时共存不冲突; 无 id 的旧式技能同名时官方优先。

`skill.md` 的 front matter:
``` markdown
---
name: coding-agent
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

也兼容旧式单文件技能: 直接把 .md 文件放在扫描目录下即可

用法示例:
``` python
from satrap import SkillsManager   # 或 from satrap.core.utils.skills import SkillsManager

skills = SkillsManager()           # 用户技能目录默认 .satrap/skills
skills.scan()                      # 扫描官方预设 + 用户目录 (同名无 id 时官方优先)
skills.activate("coding-agent", workflow)          # 同步 workflow
await skills.activate_async("coding-agent", workflow)   # 异步 workflow (含 MCP 连接)
skills.deactivate("coding-agent", workflow)        # 取消激活
```
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union, cast

import yaml

from satrap.core.log import logger
from satrap.core.type import safe_getattr, safe_getattr_callable, safe_getattr_dict
from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.utils.TCBuilder import AsyncTool, Tool, ToolsManager, AsyncToolsManager

SKILLS_PRESET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "expend",
    "skills",
)
"""官方预设技能目录 (satrap/expend/skills), 只读基线"""

DEFAULT_USER_SKILLS_DIR = ".satrap/skills"
"""默认用户技能目录 (相对工作目录), 用户自添加技能"""

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
        skill_id: Optional[str] = None,
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
        - skill_id: 技能身份识别符 (来自 meta.yaml 的 satrap-skill-id), 同名区分
        """
        self.name = name
        self.skill_id = skill_id
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
        tools: list[str] | str = meta.get("tools") or []
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
        """生成可注入系统提示词的指令块 (带技能标记, 便于反激活时剥离)

        标记使用注册 key (satrap-skill-id or name), 同名不同 id 的技能注入标记互不相同,
        反激活时按 key 剥离不会误伤同名的其他技能
        """
        key = self.skill_id or self.name
        lines = [f"<skill:{key}>"]
        if self.description:
            lines.append(f"描述: {self.description}")
        if self.instructions:
            lines.append(self.instructions)
        if self.tool_names:
            lines.append(f"可用工具: {', '.join(self.tool_names)}")
        lines.append(f"</skill:{key}>")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Skill(name={self.name!r}, tools={self.tool_names!r})"


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析 Markdown 文本的 YAML front matter, 返回 (元数据字典, 正文)"""
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta: object = yaml.safe_load(match.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
        else:
            meta = cast(dict[str, Any], meta)
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
    get_tools = safe_getattr_callable(module, "get_tools")
    if get_tools is not None:
        try:
            result = get_tools()
            if isinstance(result, (list, tuple)):
                tools.extend(cast(list[Any], result))
            else:
                tools.append(result)
        except Exception as e:
            logger.error(f"[技能管理] get_tools() 执行失败: {e}")
    else:
        for attr in vars(module).values():
            if not (isinstance(attr, type) and safe_getattr(attr, "__module__") == module.__name__):
                continue
            if issubclass(attr, (Tool, AsyncTool)):
                try:
                    tools.append(attr())
                except TypeError:
                    pass   # 构造函数需要参数, 应通过 get_tools() 提供

    mcp_clients: List[Any] = []
    get_mcp_clients = safe_getattr_callable(module, "get_mcp_clients")
    if get_mcp_clients is not None:
        try:
            result = get_mcp_clients()
            if isinstance(result, (list, tuple)):
                mcp_clients.extend(cast(list[Any], result))
            else:
                mcp_clients.append(result)
        except Exception as e:
            logger.error(f"[技能管理] get_mcp_clients() 执行失败: {e}")

    return tools, mcp_clients


class SkillsManager:
    """技能管理器; 负责扫描, 加载技能, 并将其装配到 workflow"""

    def __init__(self, skills_dir: Optional[str] = None, include_preset: bool = True):
        """
        参数:
        - skills_dir: 用户技能扫描目录, 默认 ".satrap/skills" (None 时使用默认值)
        - include_preset: 是否同时扫描官方预设目录 (satrap/expend/skills), 默认 True

        扫描时官方预设目录在前, 用户目录在后; 同名技能无 satrap-skill-id 时官方优先,
        携带不同 id 的同名技能共存不冲突。
        """
        self.skills_dir = skills_dir or DEFAULT_USER_SKILLS_DIR
        self.include_preset = include_preset
        self.skills: Dict[str, Skill] = {}
        self._active: Dict[int, str] = {}            # workflow id -> skill name, 防止重复注入
        self._active_mcp: Dict[int, List[Any]] = {}  # workflow id -> 已连接的 MCP 客户端

    @staticmethod
    def _is_preset(skill: Skill) -> bool:
        """技能是否来自官方预设目录"""
        return bool(skill.source and skill.source.startswith(SKILLS_PRESET_DIR))

    def _register_skill(self, skill: Skill) -> None:
        """登记技能 (key 优先取 satrap-skill-id, 同 key 冲突时官方优先)

        同名技能携带不同 id 时 key 不同, 共存不冲突;
        同 key 冲突 (同名无 id 或同 id) 时: 已有官方版本则保留官方,
        用户想定制官方技能应使用不同的 satrap-skill-id。
        """
        key = skill.skill_id or skill.name
        existing = self.skills.get(key)
        if existing is not None and self._is_preset(existing) and not self._is_preset(skill):
            logger.info(f"[技能管理] 技能 {skill.name} 与官方同标识, 保留官方版本")
            return
        self.skills[key] = skill

    def scan(self, skills_dir: Optional[str] = None) -> List[Skill]:
        """扫描技能目录并加载全部技能

        支持两种结构:
        - 文件夹式: 目录下每个子文件夹含 skill.md (推荐, 可带 tools.py / meta.yaml)
        - 单文件式: 目录下的 .md / .markdown 文件

        参数:
        - skills_dir: 显式指定扫描目录时只扫描该目录; 否则扫描官方预设 + 构造时的用户目录

        返回:
        - 加载的技能列表
        """
        if skills_dir is not None:
            dirs: List[str] = [skills_dir]
        else:
            dirs = [SKILLS_PRESET_DIR] if self.include_preset else []
            dirs.append(self.skills_dir)

        found: List[Skill] = []
        for base in dirs:
            if not os.path.isdir(base):
                if base == self.skills_dir:
                    logger.info(f"[技能管理] 用户技能目录不存在, 已跳过: {base}")
                else:
                    logger.warning(f"[技能管理] 技能目录不存在: {base}")
                continue
            for entry in sorted(os.listdir(base)):
                entry_path = os.path.join(base, entry)
                try:
                    if os.path.isdir(entry_path):
                        skill = self._load_skill_dir(entry_path)
                        if skill is not None:
                            self._register_skill(skill)
                            found.append(skill)
                            logger.info(f"[技能管理] 已加载技能: {skill.name} <- {entry}/")
                    elif entry.endswith((".md", ".markdown")):
                        skill = Skill.from_file(entry_path)
                        self._register_skill(skill)
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
                    meta: dict[str, Any] = yaml.safe_load(f) or {}
                if isinstance(meta, dict):
                    skill.meta.update(cast(dict[str, Any], meta))
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
        # satrap-skill-id: 技能身份识别符 (同名技能区分)
        raw_id = str(skill.meta.get("satrap-skill-id") or "").strip()
        if raw_id:
            skill.skill_id = raw_id
        return skill

    def load_skill(self, name: str, file_path: str) -> Skill:
        """从指定文件加载单个技能并登记"""
        skill = Skill.from_file(file_path)
        if name and name != skill.name:
            skill.name = name
        self._register_skill(skill)
        return skill

    def get_skill(self, name: str) -> Optional[Skill]:
        """按名称或 satrap-skill-id 获取技能 (同名多个时返回官方优先) """
        skill = self.skills.get(name)
        if skill is not None:
            return skill
        for s in self.skills.values():
            if s.name == name:
                return s
        return None

    def has_skill(self, name: str) -> bool:
        """检查技能是否存在 (名称或 satrap-skill-id) """
        return self.get_skill(name) is not None

    def list_skills(self) -> List[str]:
        """获取所有已加载技能的可识别标识列表 (名称或 satrap-skill-id) """
        return list(self.skills.keys())

    def unregister_skill(self, name: str) -> bool:
        """从管理器移除已加载技能 (不影响其他 workflow 已激活的副本)

        参数:
        - name: 技能名称

        返回:
        - bool: 是否存在并已移除
        """
        if name not in self.skills:
            return False
        del self.skills[name]
        return True

    # ================= 装配到 workflow =================

    def activate(self, skill_name: str, workflow: Union[ModelWorkflowFramework, AsyncModelWorkflowFramework]) -> bool:
        """将技能装配进 workflow: 指令注入系统提示词 + 注册自带工具 + 启用关联工具 (同步版)

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
        logger.info(f"[技能管理] 技能 {skill.name} 已激活 (启用工具 {enabled}/{len(skill.tool_names)})")
        return True

    async def activate_async(self, skill_name: str, workflow: Union[ModelWorkflowFramework, AsyncModelWorkflowFramework]) -> bool:
        """将技能装配进 workflow (异步版): 额外自动连接并注册技能自带的 MCP 客户端"""
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
        logger.info(f"[技能管理] 技能 {skill.name} 已激活 (启用工具 {enabled}/{len(skill.tool_names)})")
        return True

    def deactivate(self, skill_name: str, workflow: Union[ModelWorkflowFramework, AsyncModelWorkflowFramework]) -> bool:
        """取消技能激活: 从系统提示词剥离指令块 + 禁用关联工具 (同步版)"""
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

    async def deactivate_async(self, skill_name: str, workflow: Union[ModelWorkflowFramework, AsyncModelWorkflowFramework]) -> bool:
        """取消技能激活 (异步版): 额外关闭技能自带的 MCP 客户端连接"""
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

    # ================= 内部方法 =================

    def _register_bundled_tools(self, workflow: Union[ModelWorkflowFramework, AsyncModelWorkflowFramework], skill: Skill) -> int:
        """注册技能自带的工具实例 (已注册的同名工具跳过), 返回新注册数"""
        tools_manager: Union[ToolsManager, AsyncToolsManager, None] = safe_getattr(workflow, "tools_manager")
        if tools_manager is None or not skill.tools:
            return 0

        registered = 0
        for tool in skill.tools:
            name = tool.get_tool_name()
            if name not in safe_getattr_dict(tools_manager, "tools"):
                tools_manager.register_tool(tool)   # type: ignore
                registered += 1

        return registered

    def _apply_tools(self, workflow: Union[ModelWorkflowFramework, AsyncModelWorkflowFramework], tool_names: List[str], enable: bool) -> int:
        """启用或禁用关联工具, 返回实际生效的工具数"""
        tools_manager: Union[ToolsManager, AsyncToolsManager, None] = safe_getattr(workflow, "tools_manager")
        if tools_manager is None or not tool_names:
            return 0
        applied = 0
        for name in tool_names:
            if name not in safe_getattr_dict(tools_manager, "tools"):
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
    def _sync_context(workflow: Union[ModelWorkflowFramework, AsyncModelWorkflowFramework]):
        """触发上下文落库, 返回 _sync() 的结果 (异步管理器返回协程)

        技能指令直接改写上下文中的系统消息内容, 属于外部编辑,
        落库前需标记 dirty 以触发全量重写
        """
        ctx = safe_getattr(workflow, "ctx")
        if ctx is not None:
            mark = safe_getattr_callable(ctx, "_mark_dirty")
            if mark is not None:
                mark()
        sync = safe_getattr_callable(workflow.ctx, "_sync")
        if sync is not None:
            return sync()
        return None


class SkillTool(AsyncTool):
    """技能加载工具; 模型可按需调用以获取技能指令 (动态技能加载路线)

    注册进 AsyncToolsManager 后, 模型在需要特定专业能力时会调用 `load_skill` 获取指令
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
