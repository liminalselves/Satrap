from __future__ import annotations
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, cast
import os
import re
from satrap.core.log import logger
from satrap.core.type import safe_getattr, safe_getattr_callable, safe_getattr_dict
from satrap.core.utils.TCBuilder import AsyncTool, Tool, ToolsManager, AsyncToolsManager
from .utils import (
    _yaml_loader,
    SkillWorkflowProtocol,
    SKILLS_PRESET_DIR,
    DEFAULT_USER_SKILLS_DIR,
    _load_skill_tools,
)
from .skill import Skill
from . import activation


class SkillsManager:
    """技能管理器; 负责扫描, 加载技能, 并将其装配到 workflow"""

    def __init__(
        self,
        skills_dir: Optional[str] = None,
        include_preset: bool = True,
        trusted_code_roots: Iterable[str | Path] | None = None,
    ):
        """
        参数:
        - skills_dir: 用户技能扫描目录, 默认 ".satrap/skills" (None 时使用默认值)
        - include_preset: 是否同时扫描官方预设目录 (satrap/expend/skills), 默认 True
        - trusted_code_roots: 允许执行 tools.py 的额外可信代码根

        扫描时官方预设目录在前, 用户目录在后; 同名技能无 satrap-skill-id 时官方优先,
        携带不同 id 的同名技能共存不冲突
        """
        self.skills_dir = skills_dir or DEFAULT_USER_SKILLS_DIR
        self.include_preset = include_preset
        roots = [Path(SKILLS_PRESET_DIR).resolve()]
        roots.extend(Path(root).resolve() for root in (trusted_code_roots or ()))
        self._trusted_code_roots = tuple(dict.fromkeys(roots))
        self.skills: Dict[str, Skill] = {}
        self._active: Dict[int, str] = {}  # workflow id -> skill name, 防止重复注入
        self._active_mcp: Dict[int, List[Any]] = (
            {}
        )  # workflow id -> 已连接的 MCP 客户端

    @staticmethod
    def _is_preset(skill: Skill) -> bool:
        """
        技能是否来自官方预设目录

        参数:
        - skill: 技能实例

        返回:
        - bool: 技能是否来自官方预设目录
        """
        if not skill.source:
            return False
        try:
            return (
                Path(skill.source)
                .resolve()
                .is_relative_to(Path(SKILLS_PRESET_DIR).resolve())
            )
        except OSError:
            return False

    def _is_trusted_code_file(self, file_path: str | Path) -> bool:
        """
        判断可执行技能代码是否位于可信代码根内

        参数:
        - file_path: 待校验的代码文件

        返回:
        - bool: 是否允许执行
        """
        try:
            resolved = Path(file_path).resolve(strict=True)
        except OSError:
            return False
        return any(resolved.is_relative_to(root) for root in self._trusted_code_roots)

    def _register_skill(self, skill: Skill) -> None:
        """
        登记技能 (key 优先取 satrap-skill-id, 同 key 冲突时官方优先)

        参数:
        - skill: 技能实例

        同名技能携带不同 id 时 key 不同, 共存不冲突;
        同 key 冲突 (同名无 id 或同 id) 时: 已有官方版本则保留官方,
        用户想定制官方技能应使用不同的 satrap-skill-id
        """
        key = skill.skill_id or skill.name
        existing = self.skills.get(key)
        if (
            existing is not None
            and self._is_preset(existing)
            and not self._is_preset(skill)
        ):
            logger.info(f"[技能管理] 技能 {skill.name} 与官方同标识, 保留官方版本")
            return
        self.skills[key] = skill

    def scan(self, skills_dir: Optional[str] = None) -> List[Skill]:
        """
        扫描技能目录并加载全部技能

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
                            logger.info(
                                f"[技能管理] 已加载技能: {skill.name} <- {entry}/"
                            )
                    elif entry.endswith((".md", ".markdown")):
                        skill = Skill.from_file(entry_path)
                        self._register_skill(skill)
                        found.append(skill)
                        logger.info(f"[技能管理] 已加载技能: {skill.name} <- {entry}")
                except Exception as e:
                    logger.error(f"[技能管理] 加载技能 {entry} 失败: {e}")
        return found

    def _load_skill_dir(self, skill_dir: str) -> Optional[Skill]:
        """
        从技能文件夹加载技能: skill.md + meta.yaml + tools.py

        参数:
        - skill_dir: skill目录

        返回:
        - Optional[Skill]: 从技能文件夹加载技能: skill.md + meta.yaml + tools.py
        """
        md_path = os.path.join(skill_dir, "skill.md")
        if not os.path.isfile(md_path):
            return None
        skill = Skill.from_file(md_path, default_name=os.path.basename(skill_dir))
        skill.source = skill_dir

        meta_path = os.path.join(skill_dir, "meta.yaml")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta: object = _yaml_loader.safe_load(f) or {}
                if isinstance(meta, dict):
                    skill.meta.update(cast(dict[str, Any], meta))
            except Exception as e:
                logger.warning(f"[技能管理] meta.yaml 解析失败: {meta_path}: {e}")

        tools_path = os.path.join(skill_dir, "tools.py")
        if os.path.isfile(tools_path):
            if self._is_trusted_code_file(tools_path):
                tools, mcp_clients = _load_skill_tools(tools_path)
                skill.tools = tools
                skill.mcp_clients = mcp_clients
                for tool in tools:
                    name = tool.get_tool_name()
                    if name not in skill.tool_names:
                        skill.tool_names.append(name)
            else:
                logger.warning(
                    f"[技能管理] 已跳过非可信目录中的 tools.py: {tools_path}"
                )
        # satrap-skill-id: 技能身份识别符 (同名技能区分)
        raw_id = str(skill.meta.get("satrap-skill-id") or "").strip()
        if raw_id:
            skill.skill_id = raw_id
        return skill

    def load_skill(self, name: str, file_path: str) -> Skill:
        """
        从指定文件加载单个技能并登记

        参数:
        - name: 名称
        - file_path: 文件路径

        返回:
        - Skill: 从指定文件加载单个技能并登记
        """
        skill = Skill.from_file(file_path)
        if name and name != skill.name:
            skill.name = name
        self._register_skill(skill)
        return skill

    def get_skill(self, name: str) -> Optional[Skill]:
        """
        按名称或 satrap-skill-id 获取技能 (同名多个时返回官方优先)

        参数:
        - name: 名称

        返回:
        - Optional[Skill]: 官方优先)
        """
        skill = self.skills.get(name)
        if skill is not None:
            return skill
        for s in self.skills.values():
            if s.name == name:
                return s
        return None

    def has_skill(self, name: str) -> bool:
        """
        检查技能是否存在 (名称或 satrap-skill-id)

        参数:
        - name: 名称

        返回:
        - bool: 检查结果
        """
        return self.get_skill(name) is not None

    def list_skills(self) -> List[str]:
        """
        获取所有已加载技能的可识别标识列表 (名称或 satrap-skill-id)

        返回:
        - List[str]: 所有已加载技能的可识别标识列表 (名称或 satrap-skill-id)
        """
        return list(self.skills.keys())

    def unregister_skill(self, name: str) -> bool:
        """
        从管理器移除已加载技能 (不影响其他 workflow 已激活的副本)

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

    def activate(self, skill_name: str, workflow: SkillWorkflowProtocol) -> bool:
        """执行技能生命周期业务"""
        return activation.activate(self, skill_name, workflow)

    async def activate_async(
        self, skill_name: str, workflow: SkillWorkflowProtocol
    ) -> bool:
        """执行技能生命周期业务"""
        return await activation.activate_async(self, skill_name, workflow)

    def deactivate(self, skill_name: str, workflow: SkillWorkflowProtocol) -> bool:
        """执行技能生命周期业务"""
        return activation.deactivate(self, skill_name, workflow)

    async def deactivate_async(
        self, skill_name: str, workflow: SkillWorkflowProtocol
    ) -> bool:
        """执行技能生命周期业务"""
        return await activation.deactivate_async(self, skill_name, workflow)

    # ================= 内部方法 =================

    def _register_bundled_tools(
        self, workflow: SkillWorkflowProtocol, skill: Skill
    ) -> int:
        """
        注册技能自带的工具实例 (已注册的同名工具跳过), 返回新注册数

        参数:
        - workflow: 工作流实例
        - skill: 技能实例

        返回:
        - int: 新注册数
        """
        tools_manager: Union[ToolsManager, AsyncToolsManager, None] = safe_getattr(
            workflow, "tools_manager"
        )
        if tools_manager is None or not skill.tools:
            return 0

        registered = 0
        for tool in skill.tools:
            name = tool.get_tool_name()
            if name not in safe_getattr_dict(tools_manager, "tools"):
                # 工具与工具管理器按同步/异步配对提供, 注册行为与配对无关
                if isinstance(tools_manager, AsyncToolsManager):
                    tools_manager.register_tool(cast(AsyncTool, tool))
                else:
                    tools_manager.register_tool(cast(Tool, tool))
                registered += 1

        return registered

    def _apply_tools(
        self, workflow: SkillWorkflowProtocol, tool_names: List[str], enable: bool
    ) -> int:
        """
        启用或禁用关联工具, 返回实际生效的工具数

        参数:
        - workflow: 工作流实例
        - tool_names: 工具names
        - enable: 是否启用

        返回:
        - int: 实际生效的工具数
        """
        tools_manager: Union[ToolsManager, AsyncToolsManager, None] = safe_getattr(
            workflow, "tools_manager"
        )
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
    def _strip_from_system(workflow: SkillWorkflowProtocol, skill_name: str):
        """
        从系统提示词中剥离 <skill:name>...</skill:name> 指令块

        参数:
        - workflow: 工作流实例
        - skill_name: skill名称
        """
        pattern = re.compile(
            rf"\n*<skill:{re.escape(skill_name)}>.*?</skill:{re.escape(skill_name)}>",
            re.DOTALL,
        )
        for msg in workflow.ctx._messages:
            content = msg.get("content")
            if msg.get("role") == "system" and isinstance(content, str):
                msg["content"] = pattern.sub("", content)

    @staticmethod
    def _sync_context(workflow: SkillWorkflowProtocol):
        """
        触发上下文落库, 返回 _sync() 的结果 (异步管理器返回协程)

        参数:
        - workflow: 工作流实例

        技能指令直接改写上下文中的系统消息内容, 属于外部编辑,
        落库前需标记 dirty 以触发全量重写

        返回:
        -  _sync() 的结果 (异步管理器返回协程)
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
