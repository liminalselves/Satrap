"""
技能发现, 有效目录和可信资源生命周期

启用状态只决定技能是否可被发现, 正文由任务通过只读工具加载,
外部 Markdown 导入由 skills 插件提供, 不改写持久系统提示词
"""
from __future__ import annotations
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, cast
import os
from satrap.core.log import logger
from satrap.core.type import safe_getattr, safe_getattr_dict
from satrap.core.type import RequestContextBlock
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
        - skills_dir: 显式 SDK 扫描目录, None 时不自动扫描外部用户技能
        - include_preset: 是否同时扫描官方预设目录 (satrap/expend/skills), 默认 True
        - trusted_code_roots: 允许执行 tools.py 的额外可信代码根

        显式扫描时官方预设目录在前, 用户目录在后; 同名技能无 satrap-skill-id 时官方优先,
        携带不同 id 的同名技能共存不冲突
        """
        self.skills_dir = skills_dir or DEFAULT_USER_SKILLS_DIR
        self._explicit_user_directory = skills_dir is not None
        self.include_preset = include_preset
        roots = [Path(SKILLS_PRESET_DIR).resolve()]
        roots.extend(Path(root).resolve() for root in (trusted_code_roots or ()))
        self._trusted_code_roots = tuple(dict.fromkeys(roots))
        self.skills: Dict[str, Skill] = {}
        self._active: Dict[int, Dict[str, Skill]] = {}   # workflow id -> 独立激活的技能
        self._active_mcp: Dict[int, Dict[str, List[tuple[Any, List[str]]]]] = {}
        self._owned_tools: Dict[int, set[str]] = {}   # 工作流中由技能注册的工具
        self.availability: Callable[[str], bool] | None = None
        self.sources: dict[str, Callable[[], None]] = {}
        self._forced: dict[tuple[str, ...], str] = {}

    def refresh(self) -> None:
        """在任务开始前刷新插件管理的技能, 不在模型循环中更换正文"""
        for source in tuple(self.sources.values()):
            source()

    @staticmethod
    def caller_key() -> tuple[str, ...]:
        """
        获取强制加载待办所属的真实调用者

        返回:
        - 当前平台账号, 对话与发送者组成的键; 本地调用使用独立键
        """
        from satrap.core.call_context import current_call_origin
        origin = current_call_origin()
        return (origin.adapter_id, origin.self_id, origin.conversation_kind or origin.chat_type, origin.chat_id, origin.actor_id) if origin else ("local",)

    def force_next(self, name: str, workflow: SkillWorkflowProtocol) -> None:
        """
        为当前调用者的下一次任务登记技能

        参数:
        - name: 当前可用技能标识
        - workflow: 当前工作流
        """
        self.read_skill(name, workflow)
        self._forced[self.caller_key()] = name

    def take_forced(self, workflow: SkillWorkflowProtocol) -> tuple[RequestContextBlock, ...]:
        """
        消费本人待办并在执行时重新检查可用性

        参数:
        - workflow: 接收任务的工作流

        返回:
        - 本轮技能资料块, 没有待办时为空
        """
        name = self._forced.pop(self.caller_key(), None)
        return (RequestContextBlock("forced_skill", "skills", self.read_skill(name, workflow)),) if name else ()

    def attach(self, workflow: SkillWorkflowProtocol) -> None:
        """
        将目录和只读加载工具绑定到工作流, 不注入技能正文

        参数:
        - workflow: 技能所属工作流
        """
        from .tool import SkillTool, SyncSkillTool
        setattr(workflow, "skill_runtime", self)
        manager = workflow.tools_manager
        existing = manager.tools.get("load_skill") if manager is not None else None
        if existing is not None and getattr(existing, "skills_manager", None) is not self:
            raise ValueError("load_skill 是技能加载器保留名称, 不能被其它工具覆盖")
        if manager is not None and "load_skill" not in manager.tools:
            if isinstance(manager, AsyncToolsManager):
                manager.register_tool(SkillTool(self, workflow))
            else:
                manager.register_tool(SyncSkillTool(self, workflow))

    def available_skills(self, workflow: SkillWorkflowProtocol | None = None) -> list[Skill]:
        """
        过滤当前工作流实际启用且关系有效的技能

        参数:
        - workflow: 工作流, None 用于独立读取已登记技能

        返回:
        - 有效技能列表, 不将停用技能提供给模型
        """
        active = self.skills if workflow is None else self._active.get(id(workflow), {})
        return [skill for key, skill in self.skills.items()
                if key in active and (self.availability is None or self.availability(key))]

    def detach_if_empty(self, workflow: SkillWorkflowProtocol) -> None:
        """
        最后一个技能停用后回收自动注册的加载器

        参数:
        - workflow: 资源所属工作流
        """
        if self._active.get(id(workflow)) or workflow.tools_manager is None:
            return
        tool = workflow.tools_manager.tools.get("load_skill")
        if tool is not None and getattr(tool, "skills_manager", None) is self:
            workflow.tools_manager.unregister_tool("load_skill")

    def catalog(self, workflow: SkillWorkflowProtocol) -> str:
        """
        构造本次请求的精简技能目录

        参数:
        - workflow: 当前工作流

        返回:
        - 名称, 简介和加载规则; 没有可用技能时返回空字符串
        """
        skills = self.available_skills(workflow)
        if not skills:
            return ""
        entries = [f"- {skill.skill_id or skill.name}: {' '.join(skill.description.split()) or '读取说明了解用途'}" for skill in skills]
        return ("可用技能 (目录只用于发现):\n" + "\n".join(entries)
                + "\n任务匹配或用户点名某个技能时, 先调用 load_skill 读取完整说明; 只加载本次需要的技能"
                + "\n技能说明不开放额外工具权限, 也不改变审批规则")

    def read_skill(self, name: str, workflow: SkillWorkflowProtocol | None = None) -> str:
        """
        读取当前可用技能正文, 不启用关联工具或改写系统提示词

        参数:
        - name: 精确登记标识, 同名多来源时不能猜测目标
        - workflow: 当前工作流, 独立读取时可省略

        返回:
        - 技能正文; 未登记或不可用时抛出 ValueError
        """
        skill = self.skills.get(name)
        if skill is None or skill not in self.available_skills(workflow):
            raise ValueError(f"技能不存在或当前不可用: {name}")
        return skill.to_text()

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
        - skills_dir: 显式传入时只扫描该目录; 否则扫描官方预设及构造时显式指定的目录

        返回:
        - 加载的技能列表
        """
        if skills_dir is not None:
            dirs: List[str] = [skills_dir]
        else:
            dirs = [SKILLS_PRESET_DIR] if self.include_preset else []
            if self._explicit_user_directory:
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
        md_path = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(md_path):
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
        """
        同步启用技能目录并装配可信资源

        参数:
        - skill_name: 精确技能标识
        - workflow: 资源所属工作流

        返回:
        - 技能存在且状态更新成功时为 True
        """
        return activation.activate(self, skill_name, workflow)

    async def activate_async(
        self, skill_name: str, workflow: SkillWorkflowProtocol
    ) -> bool:
        """
        异步启用技能目录并装配可信资源

        参数:
        - skill_name: 精确技能标识
        - workflow: 资源所属工作流

        返回:
        - 技能存在且状态更新成功时为 True
        """
        return await activation.activate_async(self, skill_name, workflow)

    def deactivate(self, skill_name: str, workflow: SkillWorkflowProtocol) -> bool:
        """
        同步停用技能并回收其可信资源

        参数:
        - skill_name: 精确技能标识
        - workflow: 资源所属工作流

        返回:
        - 技能存在且状态更新成功时为 True
        """
        return activation.deactivate(self, skill_name, workflow)

    async def deactivate_async(
        self, skill_name: str, workflow: SkillWorkflowProtocol
    ) -> bool:
        """
        异步停用技能并回收其可信资源

        参数:
        - skill_name: 精确技能标识
        - workflow: 资源所属工作流

        返回:
        - 技能存在且状态更新成功时为 True
        """
        return await activation.deactivate_async(self, skill_name, workflow)

    # ================= 内部方法 =================

    def compose_system_prompt(self, prompt: str, workflow: SkillWorkflowProtocol) -> str:
        """
        保留基础提示词兼容入口, 技能目录由模型请求装配层提供

        参数:
        - prompt: 新的基础提示词
        - workflow: 指令所属工作流

        返回:
        - 原始基础提示词, 不追加技能正文
        """
        return prompt

    def _release_skill_tools(self, skill: Skill, workflow: SkillWorkflowProtocol) -> None:
        """
        回收技能独占工具并保留其他技能仍引用的工具

        参数:
        - skill: 已取消激活的技能
        - workflow: 工具所属工作流
        """
        manager = workflow.tools_manager
        if manager is None:
            return
        wf_id = id(workflow)
        needed = {
            name for active in self._active.get(wf_id, {}).values()
            for name in [*active.tool_names, *(tool.get_tool_name() for tool in active.tools)]
        }
        for clients in self._active_mcp.get(wf_id, {}).values():
            for _, names in clients:
                needed.update(names)
        owned = self._owned_tools.get(wf_id, set())
        names = {tool.get_tool_name() for tool in skill.tools} | (owned - needed)
        for name in names - needed:
            if name in owned:
                manager.unregister_tool(name)
                owned.discard(name)
        if not owned:
            self._owned_tools.pop(wf_id, None)

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
