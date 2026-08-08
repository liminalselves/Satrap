"""edictum 简易单工作流 Agent 系统

SimpleSession / AsyncSimpleSession: 基于 Session 的高可扩展单 workflow Agent 框架
- 单个主 workflow / 单一主模型, 内部直接使用 full_agent (React 范式)
- 命令 / 工具 / MCP / skill / 处理器 / 插件六类能力的注入、删除、启用/停用、查看
- 处理器 (SessionHandler): 函数直注流程 (非 hook), 4 处理点, 优先级排序, before 可改写/拦截
- 插件 (Plugin): 目录化组合包 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py), 双层启停
- checkpoint 全套 (继承 Session 聚合检查点)
- 多模态 (img_urls) + 流式/非流式切换
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import (
    AsyncModelWorkflowFramework,
    AsyncSession,
    ModelWorkflowFramework,
    Session,
)
from satrap.core.log import logger
from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager
from satrap.core.utils.skills import SkillsManager
from satrap.edictum.plugin import (
    Plugin,
    collect_cleanup,
    collect_commands,
    collect_handlers,
    collect_mcp_clients,
    collect_skills,
    collect_tools,
    load_plugin_meta,
)

# 插件回调: 同步版仅调用同步函数; 异步版同步/异步均可 (运行期区分)
BeforeUserSend = Callable[[str], str | None] | Callable[[str], Awaitable[str | None]]
AfterUserSend = Callable[[str], None] | Callable[[str], Awaitable[None]]
BeforeModelReply = Callable[[], None] | Callable[[], Awaitable[None]]
AfterModelReply = Callable[[str], None] | Callable[[str], Awaitable[None]]


def _command_intro(handler: Callable[..., Any]) -> str:
    """从命令函数 docstring 提取简介 (首行)"""
    doc = inspect.getdoc(handler)
    if doc:
        first = doc.strip().splitlines()[0].strip()
        if first:
            return first
    return "None"


def _add_plugin_sys_path(plugin_dir: Path) -> None:
    """将插件目录加入 sys.path, 使插件内私有模块可互相 import (如 core.permission)"""
    plugin_path = str(plugin_dir.resolve())
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)


def _remove_plugin_sys_path(plugin_dir: Path) -> None:
    """卸载时尝试从 sys.path 移除插件目录 (多个插件共用时静默忽略)"""
    plugin_path = str(plugin_dir.resolve())
    try:
        sys.path.remove(plugin_path)
    except ValueError:
        pass


@dataclass
class SessionHandler:
    """处理器: 4 个处理点直接注入流程, 非 hook

    - before_user_send: 用户消息处理前, 返回 str 改写输入, 返回 None 透传
    - after_user_send: 用户消息处理后 (通知, 收到改写后的文本)
    - before_model_reply: 模型调用前 (通知)
    - after_model_reply: 模型回复后 (通知, 收到最终回复文本)
    - priority: 越小越先执行
    - enabled: 停用后不参与流程
    """

    name: str
    priority: int = 100
    enabled: bool = True
    before_user_send: BeforeUserSend | None = None
    after_user_send: AfterUserSend | None = None
    before_model_reply: BeforeModelReply | None = None
    after_model_reply: AfterModelReply | None = None


class SimpleSession(Session):
    """同步简易会话: 单 workflow 单主模型, 直接使用 full_agent (React 范式)

    使用示例:
    ``` python
    session = SimpleSession("conv-1", llm, system_prompt="你是助手")
    session.add_tool(my_tool)
    result = session("你好")
    ```
    """

    def __init__(
        self,
        session_id: str,
        llm: LLM,
        *,
        system_prompt: str | None = None,
        tools: Iterable[Tool] | None = None,
        content_callback: Callable[[str], None] | None = None,
        db_path: str = ".satrap/chat_history.db",
        enable_checkpoint: bool = True,
        stream: bool = False,
        return_thinking: bool = False,
        thinking_callback: Callable[[str], None] | None = None,
    ):
        """初始化简易会话

        参数:
        - session_id: 会话 ID
        - llm: 主模型实例
        - system_prompt: 系统提示词
        - tools: 初始工具列表
        - content_callback: 内容回调 (流式输出与命令输出)
        - db_path: 上下文/检查点数据库路径
        - enable_checkpoint: 是否启用会话级检查点
        - stream: 默认是否流式输出
        - return_thinking: 是否回传思考内容
        - thinking_callback: 思考内容回调
        """
        super().__init__(
            session_id, content_callback, db_path=db_path, enable_checkpoint=enable_checkpoint,
        )
        wf_id = self.workflow_id_assign("main")
        self._wf = ModelWorkflowFramework(
            llm,
            context_id=wf_id,
            tools_manager=ToolsManager(),
            system_prompt=system_prompt,
            content_callback=content_callback,
            return_thinking=return_thinking,
            thinking_callback=thinking_callback,
            db_path=db_path,
        )
        self._track_workflow_context(wf_id, self._wf.ctx)
        self._handlers: dict[str, SessionHandler] = {}
        self._plugins: dict[str, Plugin] = {}
        self._skills_manager: SkillsManager | None = None
        self.user_input_provider: Callable[[str], str] | None = None
        """用户输入通道: 供 ask_user / 审批询问使用 (CLI 可注入 input, Streamlit 可注入 st.text_input)"""
        self.stream = stream
        if tools:
            for tool in tools:
                self._wf.tools_manager.register_tool(tool)

    # ---------------- 属性代理 ----------------

    @property
    def llm(self) -> LLM:
        """主模型实例"""
        return self._wf.llm

    @llm.setter
    def llm(self, value: LLM) -> None:
        self._wf.llm = value

    @property
    def ctx(self):
        """主工作流上下文"""
        return self._wf.ctx

    @property
    def tools_manager(self) -> ToolsManager:
        """主工作流工具管理器"""
        return self._wf.tools_manager

    # ---------------- 调用入口 ----------------

    def run(
        self,
        user_input: str,
        img_urls: list[str] | None = None,
        *,
        thinking: bool = False,
        max_iterations: int = 10,
    ) -> str:
        """执行一轮 Agent 流程 (React 范式), 返回最终模型输出

        参数:
        - user_input: 用户输入
        - img_urls: 图片 URL 列表 (多模态)
        - thinking: 是否要求模型思考
        - max_iterations: 最大工具调用迭代次数
        """
        text = user_input
        for p in self._enabled_handlers():
            if p.before_user_send is None:
                continue
            out = p.before_user_send(text)
            if out is not None:
                assert isinstance(out, str), f"插件 {p.name}.before_user_send 必须返回 str 或 None"
                text = out

        for p in self._enabled_handlers():
            if p.after_user_send is None:
                continue
            p.after_user_send(text)

        for p in self._enabled_handlers():
            if p.before_model_reply is None:
                continue
            p.before_model_reply()

        if self.stream:
            result = self._wf.stream_full_agent(
                text, img_urls=img_urls, thinking=thinking, max_iterations=max_iterations,
            )
        else:
            if thinking:
                raise NotImplementedError("thinking 参数仅在流式模式 (stream=True) 下生效")
            result = self._wf.full_agent(
                text, img_urls=img_urls, max_iterations=max_iterations,
            )

        for p in self._enabled_handlers():
            if p.after_model_reply is None:
                continue
            p.after_model_reply(result)

        return result

    def __call__(self, user_input: str, **kwargs: Any) -> str:
        return self.run(user_input, **kwargs)

    # ---------------- 命令管理 ----------------

    def add_command(self, name: str, handler: Callable[..., Any], intro: str = "None"):
        """注册命令"""
        self.cmd_handler.register_command(name, handler, intro=intro)

    def remove_command(self, name: str) -> bool:
        """注销命令"""
        return self.cmd_handler.unregister_command(name)

    def enable_command(self, name: str) -> bool:
        """启用命令"""
        return self.cmd_handler.enable_command(name)

    def disable_command(self, name: str) -> bool:
        """停用命令 (停用后消息不再按命令处理)"""
        return self.cmd_handler.disable_command(name)

    def is_command_enabled(self, name: str) -> bool:
        """检查命令是否启用"""
        return self.cmd_handler.is_command_enabled(name)

    def list_commands(self) -> dict[str, str]:
        """列出已注册命令及其简介"""
        return self.cmd_handler.list_commands()

    # ---------------- 工具管理 ----------------

    def add_tool(self, tool: Tool):
        """注册工具 (任意时刻可注入, 立即生效)"""
        self._wf.tools_manager.register_tool(tool)

    def add_tools(self, *tools: Tool):
        """批量注册工具"""
        for tool in tools:
            self._wf.tools_manager.register_tool(tool)

    def remove_tool(self, name: str) -> bool:
        """注销工具"""
        return self._wf.tools_manager.unregister_tool(name)

    def enable_tool(self, name: str) -> bool:
        """启用工具"""
        return self._wf.tools_manager.enable_tool(name)

    def disable_tool(self, name: str) -> bool:
        """停用工具"""
        return self._wf.tools_manager.disable_tool(name)

    def is_tool_enabled(self, name: str) -> bool:
        """检查工具是否启用"""
        return self._wf.tools_manager.is_tool_enabled(name)

    def list_tools(self) -> list[str]:
        """列出已注册工具名"""
        return list(self._wf.tools_manager.tools.keys())

    # ---------------- skill 管理 ----------------

    def _get_skills_manager(self) -> SkillsManager:
        """获取技能管理器 (惰性创建)"""
        if self._skills_manager is None:
            self._skills_manager = SkillsManager()
        return self._skills_manager

    def add_skill(self, skill_name: str, skills_manager: SkillsManager | None = None) -> bool:
        """加载并激活技能到主工作流"""
        mgr = skills_manager or self._get_skills_manager()
        if skills_manager is not None:
            self._skills_manager = skills_manager
        return mgr.activate(skill_name, self._wf)

    def remove_skill(self, skill_name: str) -> bool:
        """从工作流卸载并从管理器移除技能定义"""
        mgr = self._skills_manager
        if mgr is None:
            return False
        removed = mgr.deactivate(skill_name, self._wf)
        mgr.unregister_skill(skill_name)
        return removed

    def enable_skill(self, skill_name: str) -> bool:
        """重新激活技能"""
        return self.add_skill(skill_name)

    def disable_skill(self, skill_name: str) -> bool:
        """停用技能 (从工作流卸载, 保留注册)"""
        if self._skills_manager is None:
            return False
        return self._skills_manager.deactivate(skill_name, self._wf)

    def list_skills(self) -> list[str]:
        """列出技能管理器已加载的技能"""
        if self._skills_manager is None:
            return []
        return self._skills_manager.list_skills()

    # ---------------- 处理器管理 ----------------

    def add_handler(self, handler: SessionHandler):
        """注册处理器 (同名校覆盖)"""
        if not handler.name:
            raise ValueError("处理器 name 不能为空")
        self._handlers[handler.name] = handler

    def remove_handler(self, name: str) -> bool:
        """注销处理器"""
        return self._handlers.pop(name, None) is not None

    def enable_handler(self, name: str) -> bool:
        """启用处理器"""
        handler = self._handlers.get(name)
        if handler is None:
            return False
        handler.enabled = True
        return True

    def disable_handler(self, name: str) -> bool:
        """停用处理器"""
        handler = self._handlers.get(name)
        if handler is None:
            return False
        handler.enabled = False
        return True

    def list_handlers(self) -> list[SessionHandler]:
        """列出处理器 (按优先级升序)"""
        return sorted(self._handlers.values(), key=lambda p: p.priority)

    def set_handler_priority(self, name: str, priority: int) -> bool:
        """调整处理器优先级 (越小越先执行)"""
        handler = self._handlers.get(name)
        if handler is None:
            return False
        handler.priority = priority
        return True

    def _enabled_handlers(self) -> list[SessionHandler]:
        """返回启用中的处理器 (按优先级升序)"""
        return sorted(
            (p for p in self._handlers.values() if p.enabled),
            key=lambda p: p.priority,
        )

    # ---------------- 插件管理 (目录插件包) ----------------

    def install_plugin(self, path: str) -> Plugin:
        """安装目录插件 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py)

        同步版跳过 mcp.py (工具为 AsyncTool, 仅异步版支持), 其余能力照常注册;
        任一步失败时回滚已注册能力与 sys.path, 不留孤儿
        """
        plugin_dir = Path(path)
        _add_plugin_sys_path(plugin_dir)
        tool_states: dict[str, bool] = {}
        skill_states: dict[str, bool] = {}
        handler_states: dict[str, bool] = {}
        command_states: dict[str, bool] = {}
        try:
            meta = load_plugin_meta(plugin_dir)
            name = str(meta.get("name") or "").strip()
            if not name:
                raise ValueError(f"插件 {path} 的 meta.yaml 缺少 name")
            if name in self._plugins:
                raise ValueError(f"插件 {name} 已安装")

            for t in collect_tools(plugin_dir, name, Tool, self):
                tname = t.get_tool_name()
                if tname in self._wf.tools_manager.tools or tname in tool_states:
                    raise ValueError(f"插件 {name} 的工具 {tname} 与已注册工具冲突")
                self._wf.tools_manager.register_tool(t)
                tool_states[tname] = True

            mgr = self._get_skills_manager()
            for s in collect_skills(plugin_dir, name):
                key = s.skill_id or s.name
                if key in mgr.skills or key in skill_states:
                    raise ValueError(f"插件 {name} 的技能 {key} 与已加载技能冲突")
                mgr._register_skill(s)
                skill_states[key] = True

            for h in collect_handlers(plugin_dir, name, self):
                if h.name in self._handlers or h.name in handler_states:
                    raise ValueError(f"插件 {name} 的处理器 {h.name} 与已注册处理器冲突")
                self._handlers[h.name] = h
                handler_states[h.name] = True

            sync_commands, _async_commands = collect_commands(plugin_dir, name, self)
            for cname, chandler in sync_commands.items():
                if cname in self.cmd_handler.commands or cname in command_states:
                    raise ValueError(f"插件 {name} 的命令 {cname} 与已注册命令冲突")
                self.cmd_handler.register_command(cname, chandler, intro=_command_intro(chandler))
                command_states[cname] = True

            if (plugin_dir / "mcp.py").is_file():
                logger.warning(
                    f"[edictum] 插件 {name} 含 mcp.py, 仅异步版 (AsyncSimpleSession) 支持, 已跳过"
                )

            plugin = Plugin(
                name=name,
                version=str(meta.get("version") or ""),
                author=str(meta.get("author") or ""),
                repo=str(meta.get("repo") or ""),
                description=str(meta.get("description") or ""),
                path=str(plugin_dir),
            )
            plugin._session = self
            plugin._cleanup = collect_cleanup(plugin_dir, name, self)
            plugin.tools = tool_states
            plugin.skills = skill_states
            plugin.handlers = handler_states
            plugin.commands = command_states
            self._plugins[name] = plugin
            logger.info(f"[edictum] 插件 {name} 已安装")
            return plugin
        except Exception:
            _remove_plugin_sys_path(plugin_dir)
            for tname in tool_states:
                self._wf.tools_manager.unregister_tool(tname)
            mgr = self._skills_manager
            if mgr is not None:
                for key in skill_states:
                    mgr.unregister_skill(key)
            for hname in handler_states:
                self._handlers.pop(hname, None)
            for cname in command_states:
                self.cmd_handler.unregister_command(cname)
            raise

    def uninstall_plugin(self, name: str) -> bool:
        """卸载插件: 回收其全部能力 (含清理回调), 不留孤儿"""
        plugin = self._plugins.pop(name, None)
        if plugin is None:
            return False
        _remove_plugin_sys_path(Path(plugin.path))
        for tname in plugin.tools:
            self._wf.tools_manager.unregister_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for sname in plugin.skills:
                mgr.deactivate(sname, self._wf)
                mgr.unregister_skill(sname)
        for hname in plugin.handlers:
            self._handlers.pop(hname, None)
        for cname in plugin.commands:
            self.cmd_handler.unregister_command(cname)
        cleanup = plugin._cleanup
        if cleanup is not None:
            try:
                cleanup(self)
            except Exception as e:
                logger.warning(f"[edictum] 插件 {name} 清理回调失败: {e}")
        logger.info(f"[edictum] 插件 {name} 已卸载")
        return True

    def enable_plugin(self, name: str) -> bool:
        """启用插件 (按独立状态恢复名下能力, 独立停用的保持停用)"""
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if plugin.enabled:
            return True
        plugin.enabled = True
        for tname, st in plugin.tools.items():
            if st:
                self._wf.tools_manager.enable_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for sname, st in plugin.skills.items():
                if st:
                    mgr.activate(sname, self._wf)
        for hname, st in plugin.handlers.items():
            if st and hname in self._handlers:
                self._handlers[hname].enabled = True
        for cname, st in plugin.commands.items():
            if st:
                self.cmd_handler.enable_command(cname)
        return True

    def disable_plugin(self, name: str) -> bool:
        """停用插件 (压制名下全部能力, 不改独立状态)"""
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if not plugin.enabled:
            return True
        plugin.enabled = False
        for tname in plugin.tools:
            self._wf.tools_manager.disable_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for sname in plugin.skills:
                mgr.deactivate(sname, self._wf)
        for hname in plugin.handlers:
            if hname in self._handlers:
                self._handlers[hname].enabled = False
        for cname in plugin.commands:
            self.cmd_handler.disable_command(cname)
        return True

    def list_plugins(self) -> list[Plugin]:
        """列出已安装插件"""
        return list(self._plugins.values())

    def _activate_plugin_skill(self, name: str) -> bool:
        """插件技能独立启用钩子 (同步版)"""
        return self._get_skills_manager().activate(name, self._wf)

    def _deactivate_plugin_skill(self, name: str) -> bool:
        """插件技能独立停用钩子 (同步版)"""
        return self._get_skills_manager().deactivate(name, self._wf)

    # ---------------- 模型与流式 ----------------

    def set_llm(self, llm: LLM):
        """替换主模型"""
        self._wf.llm = llm

    def set_model_parameters(self, **kwargs: Any):
        """调整主模型调用参数 (temperature / top_p / max_tokens 等)"""
        self._wf.llm.set_parameters(**kwargs)

    def set_stream_mode(self, stream: bool):
        """切换流式 / 非流式输出"""
        self.stream = bool(stream)

    def reload_llm(self, llm: LLM):
        """重载 LLM 实例 (Session 兼容接口)"""
        self.set_llm(llm)


class AsyncSimpleSession(AsyncSession):
    """异步简易会话: 单 workflow 单主模型, 直接使用 full_agent (React 范式)

    - 支持 MCP 工具注入 (add_mcp / remove_mcp)
    - 插件回调支持同步与异步函数
    - 使用前需 `await session.initialize()` (或直接调用 run, 会自动初始化)
    """

    def __init__(
        self,
        session_id: str,
        llm: AsyncLLM,
        *,
        system_prompt: str | None = None,
        tools: Iterable[AsyncTool] | None = None,
        content_callback: Callable[[str], Any] | None = None,
        db_path: str = ".satrap/chat_history.db",
        enable_checkpoint: bool = True,
        stream: bool = False,
        return_thinking: bool = False,
        thinking_callback: Callable[[str], Any] | None = None,
    ):
        """初始化异步简易会话 (工作流在 initialize 中构建)"""
        super().__init__(
            session_id, content_callback, db_path=db_path, enable_checkpoint=enable_checkpoint,
        )
        self._init_llm = llm
        self._init_system_prompt = system_prompt
        self._init_content_callback = content_callback
        self._init_return_thinking = return_thinking
        self._init_thinking_callback = thinking_callback
        self._init_tools: list[AsyncTool] = list(tools or [])
        self._init_skills: list[str] = []
        self._init_model_params: dict[str, Any] = {}
        self._db_path = db_path
        self._wf: AsyncModelWorkflowFramework | None = None
        self._handlers: dict[str, SessionHandler] = {}
        self._plugins: dict[str, Plugin] = {}
        self._skills_manager: SkillsManager | None = None
        self._mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
        self._init_lock = asyncio.Lock()
        self.user_input_provider: Callable[[str], str | Awaitable[str]] | None = None
        """用户输入通道: 供 ask_user / 审批询问使用 (CLI 可注入 input, Streamlit 可注入 st.text_input)"""
        self.stream = stream

    async def _async_init(self):
        """构建主工作流 (AsyncSession.initialize 自动调用, run 前就绪, 并发安全)"""
        if self._wf is not None:
            return
        async with self._init_lock:
            if self._wf is not None:
                return
            wf_id = self.workflow_id_assign("main")
            wf = AsyncModelWorkflowFramework(
                self._init_llm,
                context_id=wf_id,
                tools_manager=AsyncToolsManager(),
                system_prompt=self._init_system_prompt,
                content_callback=self._init_content_callback,
                return_thinking=self._init_return_thinking,
                thinking_callback=self._init_thinking_callback,
                db_path=self._db_path,
            )
            await wf.initialize()
            self._track_workflow_context(wf_id, wf.ctx)
            self._wf = wf
            if self._init_model_params:
                wf.llm.set_parameters(**self._init_model_params)
            try:
                for tool in self._init_tools:
                    wf.tools_manager.register_tool(tool)
                for skill_name in self._init_skills:
                    if not await self._get_skills_manager().activate_async(skill_name, wf):
                        logger.warning(f"[edictum] 初始化时技能 {skill_name} 激活失败")
            finally:
                self._init_tools = []
                self._init_skills = []

    def _require_wf(self) -> AsyncModelWorkflowFramework:
        """获取主工作流, 未初始化时抛错"""
        if self._wf is None:
            raise RuntimeError("工作流未初始化, 请先 await session.initialize()")
        return self._wf

    # ---------------- 属性代理 ----------------

    @property
    def llm(self) -> AsyncLLM:
        """主模型实例"""
        return self._require_wf().llm

    @llm.setter
    def llm(self, value: AsyncLLM) -> None:
        if self._wf is None:
            self._init_llm = value
            return
        self._wf.llm = value

    @property
    def ctx(self):
        """主工作流上下文"""
        return self._require_wf().ctx

    @property
    def tools_manager(self) -> AsyncToolsManager:
        """主工作流工具管理器"""
        return self._require_wf().tools_manager

    # ---------------- 调用入口 ----------------

    async def run(
        self,
        user_input: str,
        img_urls: list[str] | None = None,
        *,
        thinking: bool = False,
        max_iterations: int = 10,
    ) -> str:
        """执行一轮 Agent 流程 (React 范式), 返回最终模型输出

        参数:
        - user_input: 用户输入
        - img_urls: 图片 URL 列表 (多模态)
        - thinking: 是否要求模型思考
        - max_iterations: 最大工具调用迭代次数
        """
        wf = self._require_wf()
        text = user_input
        for p in self._enabled_handlers():
            if p.before_user_send is None:
                continue
            out = await self._call_plugin(p.before_user_send, text)
            if out is not None:
                if not isinstance(out, str):
                    raise TypeError(
                        f"插件 {p.name}.before_user_send 必须返回 str 或 None, 实际为 {type(out).__name__}"
                    )
                text = out

        for p in self._enabled_handlers():
            if p.after_user_send is None:
                continue
            await self._call_plugin(p.after_user_send, text)

        for p in self._enabled_handlers():
            if p.before_model_reply is None:
                continue
            await self._call_plugin(p.before_model_reply)

        if self.stream:
            result = await wf.stream_full_agent(
                text, img_urls=img_urls, thinking=thinking, max_iterations=max_iterations,
            )
        else:
            if thinking:
                raise NotImplementedError("thinking 参数仅在流式模式 (stream=True) 下生效")
            result = await wf.full_agent(
                text, img_urls=img_urls, max_iterations=max_iterations,
            )

        for p in self._enabled_handlers():
            if p.after_model_reply is None:
                continue
            await self._call_plugin(p.after_model_reply, result)

        return result

    async def __call__(self, user_input: str, **kwargs: Any) -> str:
        return await self.run(user_input, **kwargs)

    @staticmethod
    async def _call_plugin(fn: Callable[..., Any], *args: Any) -> Any:
        """调用插件函数, 兼容同步与异步"""
        result = fn(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    # ---------------- 命令管理 ----------------

    def add_command(self, name: str, handler: Callable[..., Any], intro: str = "None"):
        """注册命令"""
        self.command_handler.register_command(name, handler, intro=intro)

    def remove_command(self, name: str) -> bool:
        """注销命令"""
        return self.command_handler.unregister_command(name)

    def enable_command(self, name: str) -> bool:
        """启用命令"""
        return self.command_handler.enable_command(name)

    def disable_command(self, name: str) -> bool:
        """停用命令"""
        return self.command_handler.disable_command(name)

    def is_command_enabled(self, name: str) -> bool:
        """检查命令是否启用"""
        return self.command_handler.is_command_enabled(name)

    def list_commands(self) -> dict[str, str]:
        """列出已注册命令及其简介"""
        return self.command_handler.list_commands()

    # ---------------- 工具管理 ----------------

    def add_tool(self, tool: AsyncTool):
        """注册工具 (初始化前注册会延迟到 initialize 时生效)"""
        if self._wf is None:
            self._init_tools.append(tool)
        else:
            self._wf.tools_manager.register_tool(tool)

    def add_tools(self, *tools: AsyncTool):
        """批量注册工具"""
        for tool in tools:
            self.add_tool(tool)

    def remove_tool(self, name: str) -> bool:
        """注销工具"""
        return self._require_wf().tools_manager.unregister_tool(name)

    def enable_tool(self, name: str) -> bool:
        """启用工具"""
        return self._require_wf().tools_manager.enable_tool(name)

    def disable_tool(self, name: str) -> bool:
        """停用工具"""
        return self._require_wf().tools_manager.disable_tool(name)

    def is_tool_enabled(self, name: str) -> bool:
        """检查工具是否启用"""
        return self._require_wf().tools_manager.is_tool_enabled(name)

    def list_tools(self) -> list[str]:
        """列出已注册工具名"""
        return list(self._require_wf().tools_manager.tools.keys())

    # ---------------- skill 管理 ----------------

    def _get_skills_manager(self) -> SkillsManager:
        """获取技能管理器 (惰性创建)"""
        if self._skills_manager is None:
            self._skills_manager = SkillsManager()
        return self._skills_manager

    async def add_skill(self, skill_name: str, skills_manager: SkillsManager | None = None) -> bool:
        """加载并激活技能到主工作流 (初始化前注册会延迟到 initialize 时生效)"""
        mgr = skills_manager or self._get_skills_manager()
        if skills_manager is not None:
            self._skills_manager = skills_manager
        if self._wf is None:
            if mgr.get_skill(skill_name) is None:
                return False
            self._init_skills.append(skill_name)
            return True
        return await mgr.activate_async(skill_name, self._require_wf())

    async def remove_skill(self, skill_name: str) -> bool:
        """从工作流卸载并从管理器移除技能定义"""
        mgr = self._skills_manager
        if mgr is None:
            return False
        removed = await mgr.deactivate_async(skill_name, self._require_wf())
        mgr.unregister_skill(skill_name)
        return removed

    async def enable_skill(self, skill_name: str) -> bool:
        """重新激活技能"""
        return await self.add_skill(skill_name)

    async def disable_skill(self, skill_name: str) -> bool:
        """停用技能 (从工作流卸载, 保留注册)"""
        if self._skills_manager is None:
            return False
        return await self._skills_manager.deactivate_async(skill_name, self._require_wf())

    def list_skills(self) -> list[str]:
        """列出技能管理器已加载的技能"""
        if self._skills_manager is None:
            return []
        return self._skills_manager.list_skills()

    # ---------------- MCP 管理 ----------------

    async def add_mcp(self, name: str, client: Any, name_prefix: str | None = None) -> list[Any]:
        """接入 MCP Server, 将远程工具注册到主工作流

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
            adapters = await client.register_tools(wf.tools_manager, name_prefix=name_prefix)
        except Exception:
            close = getattr(client, "close", None)
            if close is not None:
                await close()
            raise
        self._mcp_clients[name] = (client, list(adapters))
        return list(adapters)

    async def remove_mcp(self, name: str) -> bool:
        """移除 MCP 连接: 注销其全部工具并断开连接"""
        entry = self._mcp_clients.pop(name, None)
        if entry is None:
            return False
        client, adapters = entry
        wf = self._require_wf()
        for adapter in adapters:
            wf.tools_manager.unregister_tool(adapter.get_tool_name())
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as e:
                logger.warning(f"[edictum] MCP {name} 断开失败: {e}")
        return True

    def enable_mcp(self, name: str) -> bool:
        """启用 MCP 连接的全部工具"""
        entry = self._mcp_clients.get(name)
        if entry is None:
            return False
        wf = self._require_wf()
        for adapter in entry[1]:
            wf.tools_manager.enable_tool(adapter.get_tool_name())
        return True

    def disable_mcp(self, name: str) -> bool:
        """停用 MCP 连接的全部工具"""
        entry = self._mcp_clients.get(name)
        if entry is None:
            return False
        wf = self._require_wf()
        for adapter in entry[1]:
            wf.tools_manager.disable_tool(adapter.get_tool_name())
        return True

    def list_mcp(self) -> list[str]:
        """列出已接入的 MCP 连接名"""
        return list(self._mcp_clients.keys())

    # ---------------- 处理器管理 ----------------

    def add_handler(self, handler: SessionHandler):
        """注册处理器 (同名校覆盖)"""
        if not handler.name:
            raise ValueError("处理器 name 不能为空")
        self._handlers[handler.name] = handler

    def remove_handler(self, name: str) -> bool:
        """注销处理器"""
        return self._handlers.pop(name, None) is not None

    def enable_handler(self, name: str) -> bool:
        """启用处理器"""
        handler = self._handlers.get(name)
        if handler is None:
            return False
        handler.enabled = True
        return True

    def disable_handler(self, name: str) -> bool:
        """停用处理器"""
        handler = self._handlers.get(name)
        if handler is None:
            return False
        handler.enabled = False
        return True

    def list_handlers(self) -> list[SessionHandler]:
        """列出处理器 (按优先级升序)"""
        return sorted(self._handlers.values(), key=lambda p: p.priority)

    def set_handler_priority(self, name: str, priority: int) -> bool:
        """调整处理器优先级 (越小越先执行)"""
        handler = self._handlers.get(name)
        if handler is None:
            return False
        handler.priority = priority
        return True

    def _enabled_handlers(self) -> list[SessionHandler]:
        """返回启用中的处理器 (按优先级升序)"""
        return sorted(
            (p for p in self._handlers.values() if p.enabled),
            key=lambda p: p.priority,
        )

    # ---------------- 插件管理 (目录插件包) ----------------

    async def install_plugin(self, path: str) -> Plugin:
        """安装目录插件 (异步版支持 mcp.py, 自动接入 MCP 客户端)

        任一步失败时回滚已注册能力/MCP 连接与 sys.path, 不留孤儿
        """
        if self._wf is None:
            await self.initialize()
        plugin_dir = Path(path)
        _add_plugin_sys_path(plugin_dir)
        tool_states: dict[str, bool] = {}
        skill_states: dict[str, bool] = {}
        handler_states: dict[str, bool] = {}
        command_states: dict[str, bool] = {}
        mcp_states: dict[str, bool] = {}
        mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
        try:
            meta = load_plugin_meta(plugin_dir)
            name = str(meta.get("name") or "").strip()
            if not name:
                raise ValueError(f"插件 {path} 的 meta.yaml 缺少 name")
            if name in self._plugins:
                raise ValueError(f"插件 {name} 已安装")
            wf = self._require_wf()

            for t in collect_tools(plugin_dir, name, AsyncTool, self):
                tname = t.get_tool_name()
                if tname in wf.tools_manager.tools or tname in tool_states:
                    raise ValueError(f"插件 {name} 的工具 {tname} 与已注册工具冲突")
                wf.tools_manager.register_tool(t)
                tool_states[tname] = True

            mgr = self._get_skills_manager()
            for s in collect_skills(plugin_dir, name):
                key = s.skill_id or s.name
                if key in mgr.skills or key in skill_states:
                    raise ValueError(f"插件 {name} 的技能 {key} 与已加载技能冲突")
                mgr._register_skill(s)
                skill_states[key] = True

            for h in collect_handlers(plugin_dir, name, self):
                if h.name in self._handlers or h.name in handler_states:
                    raise ValueError(f"插件 {name} 的处理器 {h.name} 与已注册处理器冲突")
                self._handlers[h.name] = h
                handler_states[h.name] = True

            _sync_commands, async_commands = collect_commands(plugin_dir, name, self)
            for cname, chandler in async_commands.items():
                if cname in self.command_handler.commands or cname in command_states:
                    raise ValueError(f"插件 {name} 的命令 {cname} 与已注册命令冲突")
                self.command_handler.register_command(cname, chandler, intro=_command_intro(chandler))
                command_states[cname] = True

            for mcp_name, client in collect_mcp_clients(plugin_dir, name).items():
                if mcp_name in self._mcp_clients or mcp_name in mcp_states:
                    raise ValueError(f"插件 {name} 的 MCP 连接 {mcp_name} 与已接入连接冲突")
                try:
                    adapters = await client.register_tools(wf.tools_manager, name_prefix=name)
                except Exception:
                    close = getattr(client, "close", None)
                    if close is not None:
                        await close()
                    raise
                mcp_clients[mcp_name] = (client, list(adapters))
                mcp_states[mcp_name] = True

            plugin = Plugin(
                name=name,
                version=str(meta.get("version") or ""),
                author=str(meta.get("author") or ""),
                repo=str(meta.get("repo") or ""),
                description=str(meta.get("description") or ""),
                path=str(plugin_dir),
            )
            plugin._session = self
            plugin._cleanup = collect_cleanup(plugin_dir, name, self)
            plugin.tools = tool_states
            plugin.skills = skill_states
            plugin.mcp = mcp_states
            plugin._mcp_clients = mcp_clients
            plugin.handlers = handler_states
            plugin.commands = command_states
            self._plugins[name] = plugin
            logger.info(f"[edictum] 插件 {name} 已安装")
            return plugin
        except Exception:
            _remove_plugin_sys_path(plugin_dir)
            wf = self._wf
            if wf is not None:
                for tname in tool_states:
                    wf.tools_manager.unregister_tool(tname)
            mgr = self._skills_manager
            if mgr is not None:
                for key in skill_states:
                    mgr.unregister_skill(key)
            for hname in handler_states:
                self._handlers.pop(hname, None)
            for cname in command_states:
                self.command_handler.unregister_command(cname)
            for mcp_name, (client, adapters) in mcp_clients.items():
                if wf is not None:
                    for adapter in adapters:
                        wf.tools_manager.unregister_tool(adapter.get_tool_name())
                close = getattr(client, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        pass
            raise

    async def uninstall_plugin(self, name: str) -> bool:
        """卸载插件: 回收其全部能力 (含断开 MCP 连接), 不留孤儿"""
        plugin = self._plugins.pop(name, None)
        if plugin is None:
            return False
        _remove_plugin_sys_path(Path(plugin.path))
        wf = self._require_wf()
        for tname in plugin.tools:
            wf.tools_manager.unregister_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for sname in plugin.skills:
                await mgr.deactivate_async(sname, wf)
                mgr.unregister_skill(sname)
        for mcp_name, (client, adapters) in plugin._mcp_clients.items():
            for adapter in adapters:
                wf.tools_manager.unregister_tool(adapter.get_tool_name())
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception as e:
                    logger.warning(f"[edictum] 插件 {name} 的 MCP {mcp_name} 断开失败: {e}")
        for hname in plugin.handlers:
            self._handlers.pop(hname, None)
        for cname in plugin.commands:
            self.command_handler.unregister_command(cname)
        cleanup = plugin._cleanup
        if cleanup is not None:
            try:
                cleanup(self)
            except Exception as e:
                logger.warning(f"[edictum] 插件 {name} 清理回调失败: {e}")
        logger.info(f"[edictum] 插件 {name} 已卸载")
        return True

    async def enable_plugin(self, name: str) -> bool:
        """启用插件 (按独立状态恢复名下能力, 独立停用的保持停用)"""
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if plugin.enabled:
            return True
        plugin.enabled = True
        wf = self._require_wf()
        for tname, st in plugin.tools.items():
            if st:
                wf.tools_manager.enable_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for sname, st in plugin.skills.items():
                if st:
                    await mgr.activate_async(sname, wf)
        for mcp_name, st in plugin.mcp.items():
            if st:
                for adapter in plugin._mcp_clients.get(mcp_name, (None, []))[1]:
                    wf.tools_manager.enable_tool(adapter.get_tool_name())
        for hname, st in plugin.handlers.items():
            if st and hname in self._handlers:
                self._handlers[hname].enabled = True
        for cname, st in plugin.commands.items():
            if st:
                self.command_handler.enable_command(cname)
        return True

    async def disable_plugin(self, name: str) -> bool:
        """停用插件 (压制名下全部能力, 不改独立状态)"""
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if not plugin.enabled:
            return True
        plugin.enabled = False
        wf = self._require_wf()
        for tname in plugin.tools:
            wf.tools_manager.disable_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for sname in plugin.skills:
                await mgr.deactivate_async(sname, wf)
        for mcp_name in plugin.mcp:
            for adapter in plugin._mcp_clients.get(mcp_name, (None, []))[1]:
                wf.tools_manager.disable_tool(adapter.get_tool_name())
        for hname in plugin.handlers:
            if hname in self._handlers:
                self._handlers[hname].enabled = False
        for cname in plugin.commands:
            self.command_handler.disable_command(cname)
        return True

    def list_plugins(self) -> list[Plugin]:
        """列出已安装插件"""
        return list(self._plugins.values())

    async def _activate_plugin_skill(self, name: str) -> bool:
        """插件技能独立启用钩子 (异步版)"""
        return await self._get_skills_manager().activate_async(name, self._require_wf())

    async def _deactivate_plugin_skill(self, name: str) -> bool:
        """插件技能独立停用钩子 (异步版)"""
        return await self._get_skills_manager().deactivate_async(name, self._require_wf())

    # ---------------- 模型与流式 ----------------

    def set_llm(self, llm: AsyncLLM):
        """替换主模型 (未初始化时延迟到 initialize 生效)"""
        if self._wf is None:
            self._init_llm = llm
            return
        self._wf.llm = llm

    def set_model_parameters(self, **kwargs: Any):
        """调整主模型调用参数 (未初始化时延迟到 initialize 生效)"""
        if self._wf is None:
            self._init_model_params.update(kwargs)
            return
        self._wf.llm.set_parameters(**kwargs)

    def set_stream_mode(self, stream: bool):
        """切换流式 / 非流式输出"""
        self.stream = bool(stream)

    def reload_llm(self, llm: AsyncLLM):
        """重载 LLM 实例 (AsyncSession 兼容接口)"""
        self.set_llm(llm)
