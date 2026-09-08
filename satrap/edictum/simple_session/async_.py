from __future__ import annotations
import asyncio
from typing import Any, Callable, Iterable
import time
from uuid import uuid4
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager
from satrap.core.framework.Base import AsyncModelWorkflowFramework, AsyncSession
from satrap.core.utils.skills import SkillsManager
from satrap.core.utils.paths import get_db_path
from satrap.edictum.plugin import Plugin
from satrap.core.type import CommandAction
from satrap.core.log import logger
from .utils import (
    AsyncUserInputProvider,
    HandlerResult,
    HandlerAbortError,
    HandlerConfig,
    HandlerContext,
    HANDLER_TIMEOUT,
    SessionHandler,
)
from .base import _SessionFeatures
from . import async_plugins, async_capabilities


class AsyncSimpleSession(AsyncSession, _SessionFeatures):
    """
    异步简易会话: 单 workflow 单主模型, 直接使用 full_agent (React 范式)

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
        db_path: str = get_db_path(),
        enable_checkpoint: bool = True,
        stream: bool = False,
        return_thinking: bool = False,
        thinking_callback: Callable[[str], Any] | None = None,
    ):
        """
        初始化异步简易会话 (工作流在 initialize 中构建)

        参数:
        - session_id: 会话 ID
        - llm: 模型实例
        - system_prompt: 系统提示词
        - tools: 工具列表
        - content_callback: 内容回调函数
        - db_path: 数据库路径
        - enable_checkpoint: 是否enable检查点
        - stream: 是否使用流式调用
        - return_thinking: 返回思考内容
        - thinking_callback: 思考内容回调函数
        """
        super().__init__(
            session_id,
            content_callback,
            db_path=db_path,
            enable_checkpoint=enable_checkpoint,
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
        self._init_handler_registry()
        self._skills_manager: SkillsManager | None = None
        self._mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
        self._init_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        """run 串行化锁: 同一会话不支持并发 run (第二个 run 排队等待)"""
        self.user_input_provider: AsyncUserInputProvider | None = None
        """用户输入通道: 供 ask_user / 审批询问使用 (CLI 或 Web 前端均可注入)"""
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
            wf.tools_manager.effectiveness_guard = self._tool_effective
            if self._init_model_params:
                wf.llm.set_parameters(**self._init_model_params)
            try:
                for tool in self._init_tools:
                    wf.tools_manager.register_tool(tool)
                for skill_name in self._init_skills:
                    if not await self._get_skills_manager().activate_async(
                        skill_name, wf
                    ):
                        logger.warning(f"[edictum] 初始化时技能 {skill_name} 激活失败")
            finally:
                self._init_tools = []
                self._init_skills = []

    def _require_wf(self) -> AsyncModelWorkflowFramework:
        """
        获取主工作流, 未初始化时抛错

        返回:
        - AsyncModelWorkflowFramework: 主工作流, 未初始化时抛错
        """
        if self._wf is None:
            raise RuntimeError("工作流未初始化, 请先 await session.initialize()")
        return self._wf

    # ---------- 属性代理 ----------

    @property
    def llm(self) -> AsyncLLM:
        """
        主模型实例

        返回:
        - AsyncLLM: 主模型实例
        """
        return self._require_wf().llm

    @llm.setter
    def llm(self, value: AsyncLLM) -> None:
        """
        执行 `llm` 操作

        参数:
        - value: 输入值
        """
        if self._wf is None:
            self._init_llm = value
            return
        self._wf.llm = value

    @property
    def ctx(self):
        """
        主工作流上下文

        返回:
        - 主工作流上下文
        """
        return self._require_wf().ctx

    @property
    def tools_manager(self) -> AsyncToolsManager:
        """
        主工作流工具管理器

        返回:
        - AsyncToolsManager: 主工作流工具管理器
        """
        return self._require_wf().tools_manager

    def get_context_stats(self) -> dict[str, Any] | None:
        """
        返回最近一轮模型调用的上下文统计

        返回:
        - dict[str, Any] | None: 没有模型请求时为 None
        """
        return self._require_wf().get_context_stats()

    # ---------- 调用入口 ----------

    async def run(
        self,
        user_input: str,
        img_urls: list[str] | None = None,
        *,
        thinking: str = "off",
        max_iterations: int = 10,
    ) -> str | CommandAction:
        """
        执行一轮 Agent 流程 (React 范式), 返回最终模型输出

        参数:
        - user_input: 用户输入
        - img_urls: 图片 URL 列表 (多模态)
        - thinking: 是否要求模型思考
        - max_iterations: 最大工具调用迭代次数

        处理器语义 (与同步版一致): 短路/改写/隔离/超时/finally, 见 SimpleSession.run

        并发: 同一会话的 run 经 _run_lock 串行执行 (不支持并发 run, 第二个 run 排队等待)

        返回:
        - str | CommandAction: 命令结果或最终模型输出
        """
        self._require_wf().reset_context_stats()
        command_result, is_command = await self.cmd_process(user_input)
        if is_command:
            if isinstance(command_result, CommandAction):
                return command_result
            return "" if command_result is None else str(command_result)

        async with self._run_lock:
            wf = self._require_wf()
            with self._registry_lock:
                self._active_runs += 1
                handlers = self._enabled_handlers()
            try:
                ctx = HandlerContext(
                    config=HandlerConfig(
                        original_input=user_input,
                        img_urls=img_urls,
                        thinking=thinking,
                        max_iterations=max_iterations,
                        call_id=uuid4().hex,
                    ),
                    text=user_input,
                )
                text = user_input
                final_override: str | None = None
                for p in handlers:
                    if p.before_user_send is None:
                        continue
                    out = await self._invoke_handler_async(
                        p, "before_user_send", p.before_user_send, text, ctx
                    )
                    if isinstance(out, HandlerResult):
                        if out.action == "continue":
                            text = out.text
                            ctx.text = text
                            continue
                        if out.action == "abort":
                            ctx.outcome = "abort"
                            raise HandlerAbortError(out.text)
                        # respond / reject: 短路, 跳过模型调用
                        final_override = out.text
                        ctx.outcome = out.action
                        logger.info(
                            f"[edictum] 处理器 {p.name}.before_user_send 短路 ({out.action})"
                        )
                        break
                    if isinstance(out, str):
                        text = out
                        ctx.text = text
                    elif out is not None:
                        logger.warning(
                            f"[edictum] 处理器 {p.name}.before_user_send 返回不支持的类型 {type(out).__name__}, 已忽略"
                        )

                if final_override is None:
                    for p in handlers:
                        if p.after_user_send is None:
                            continue
                        await self._invoke_handler_async(
                            p, "after_user_send", p.after_user_send, text, ctx
                        )

                    for p in handlers:
                        if p.before_model_reply is None:
                            continue
                        await self._invoke_handler_async(
                            p, "before_model_reply", p.before_model_reply, ctx
                        )

                result: str | None = None
                ctx.error = None
                try:
                    if final_override is not None:
                        result = final_override
                    elif self.stream:
                        result = await wf.stream_full_agent(
                            text,
                            img_urls=img_urls,
                            thinking=thinking,
                            max_iterations=max_iterations,
                        )
                    else:
                        if thinking != "off":
                            raise NotImplementedError(
                                "thinking 参数仅在流式模式 (stream=True) 下生效"
                            )
                        result = await wf.full_agent(
                            text,
                            img_urls=img_urls,
                            max_iterations=max_iterations,
                        )
                except BaseException as e:
                    ctx.error = e
                    raise
                finally:
                    for p in handlers:
                        if p.after_model_reply is None:
                            continue
                        out = await self._invoke_handler_async(
                            p, "after_model_reply", p.after_model_reply, result, ctx
                        )
                        if ctx.error is None and isinstance(out, str):
                            result = out

                return result if result is not None else ""
            finally:
                pending: list[SessionHandler] = []
                with self._registry_lock:
                    self._active_runs -= 1
                    if self._active_runs == 0:
                        pending, self._pending_close = self._pending_close, []
                for h in pending:
                    await asyncio.to_thread(self._close_handler, h.name, h)

    async def _invoke_handler_async(
        self,
        handler: SessionHandler,
        stage: str,
        fn: Callable[..., Any],
        *args: Any,
    ) -> Any:
        """
        调用单个处理器回调 (异步): 同步回调投入工作线程 + wait_for 超时, 异常/超时隔离 + 耗时观测

        参数:
        - handler: 处理器
        - stage: 处理阶段
        - fn: 待调用函数
        - args: 额外位置参数

        超时 = "放弃等待"而非"终止执行": 超时后框架不再等结果, 但底层 to_thread
        工作线程仍跑完 -- handler 应避免无限阻塞, 网络调用自设超时

        返回:
        - Any: 调用单个处理器回调 (异步): 同步回调投入工作线程 + wait_for 超时, 异常/超时隔离 + 耗时观测
        """
        t0 = time.perf_counter()
        timeout = HANDLER_TIMEOUT if handler.timeout is None else handler.timeout
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)
        except HandlerAbortError:
            raise
        except Exception as e:  # 含 asyncio.TimeoutError
            if handler.error_policy == "abort":
                raise HandlerAbortError(
                    f"处理器 {handler.name}.{stage} 异常: {e}"
                ) from e
            logger.error(f"[edictum] 处理器 {handler.name}.{stage} 异常/超时: {e}")
            return None
        finally:
            logger.debug(
                f"[edictum] 处理器 {handler.name}.{stage} 耗时 {time.perf_counter() - t0:.3f}s"
            )

    async def __call__(self, user_input: str, **kwargs: Any) -> str | CommandAction:
        return await self.run(user_input, **kwargs)

    # ---------- 命令管理 ----------

    def add_command(self, name: str, handler: Callable[..., Any], intro: str = "None"):
        """注册命令"""
        return async_capabilities.add_command(self, name, handler, intro)

    def remove_command(self, name: str) -> bool:
        """注销命令"""
        return async_capabilities.remove_command(self, name)

    def enable_command(self, name: str) -> bool:
        """启用命令"""
        return async_capabilities.enable_command(self, name)

    def disable_command(self, name: str) -> bool:
        """停用命令"""
        return async_capabilities.disable_command(self, name)

    def is_command_enabled(self, name: str) -> bool:
        """检查命令是否启用"""
        return async_capabilities.is_command_enabled(self, name)

    def list_commands(self) -> dict[str, str]:
        """列出已注册命令及其简介"""
        return async_capabilities.list_commands(self)

    # ---------- 工具管理 ----------

    def add_tool(self, tool: AsyncTool):
        """注册工具 (初始化前注册会延迟到 initialize 时生效)"""
        return async_capabilities.add_tool(self, tool)

    def add_tools(self, *tools: AsyncTool):
        """批量注册工具"""
        return async_capabilities.add_tools(self, *tools)

    def remove_tool(self, name: str) -> bool:
        """注销工具"""
        return async_capabilities.remove_tool(self, name)

    def enable_tool(self, name: str) -> bool:
        """启用工具"""
        return async_capabilities.enable_tool(self, name)

    def disable_tool(self, name: str) -> bool:
        """停用工具"""
        return async_capabilities.disable_tool(self, name)

    def is_tool_enabled(self, name: str) -> bool:
        """检查工具是否启用"""
        return async_capabilities.is_tool_enabled(self, name)

    def list_tools(self) -> list[str]:
        """列出已注册工具名"""
        return async_capabilities.list_tools(self)

    # ---------- skill 管理 ----------

    async def add_skill(
        self, skill_name: str, skills_manager: SkillsManager | None = None
    ) -> bool:
        """加载并激活技能到主工作流 (初始化前注册会延迟到 initialize 时生效)"""
        return await async_capabilities.add_skill(self, skill_name, skills_manager)

    async def remove_skill(self, skill_name: str) -> bool:
        """从工作流卸载并从管理器移除技能定义"""
        return await async_capabilities.remove_skill(self, skill_name)

    async def enable_skill(self, skill_name: str) -> bool:
        """重新激活技能"""
        return await async_capabilities.enable_skill(self, skill_name)

    async def disable_skill(self, skill_name: str) -> bool:
        """停用技能 (从工作流卸载, 保留注册)"""
        return await async_capabilities.disable_skill(self, skill_name)

    # ---------- MCP 管理 ----------

    async def add_mcp(
        self, name: str, client: Any, name_prefix: str | None = None
    ) -> list[Any]:
        """接入 MCP Server, 将远程工具注册到主工作流"""
        return await async_capabilities.add_mcp(self, name, client, name_prefix)

    async def remove_mcp(self, name: str) -> bool:
        """移除 MCP 连接: 注销其全部工具并断开连接"""
        return await async_capabilities.remove_mcp(self, name)

    def enable_mcp(self, name: str) -> bool:
        """启用 MCP 连接的全部工具"""
        return async_capabilities.enable_mcp(self, name)

    def disable_mcp(self, name: str) -> bool:
        """停用 MCP 连接的全部工具"""
        return async_capabilities.disable_mcp(self, name)

    def list_mcp(self) -> list[str]:
        """列出已接入的 MCP 连接名"""
        return async_capabilities.list_mcp(self)

    # ---------- 处理器管理 (注册表实现在 _HandlerRegistryMixin) ----------

    def _tool_effective(self, tool_name: str) -> bool:
        """工具生效过滤 (执行路径合成): 所属插件缺失或启用时生效"""
        return async_capabilities._tool_effective(self, tool_name)

    # ---------- 插件管理 (目录插件包) ----------

    async def install_plugin(
        self, path: str, config: dict[str, Any] | None = None
    ) -> Plugin:
        """安装目录插件 (异步版支持 mcp.py, 自动接入 MCP 客户端)"""
        return await async_plugins.install_plugin(self, path, config)

    async def uninstall_plugin(self, name: str) -> bool:
        """卸载插件: 回收其全部能力 (含断开 MCP 连接), 不留孤儿"""
        return await async_plugins.uninstall_plugin(self, name)

    async def enable_plugin(self, name: str) -> bool:
        """启用插件 (按独立状态恢复名下能力, 独立停用的保持停用)"""
        return await async_plugins.enable_plugin(self, name)

    async def disable_plugin(self, name: str) -> bool:
        """停用插件 (压制名下全部能力, 不改独立状态; 工具/处理器由执行路径合成压制)"""
        return await async_plugins.disable_plugin(self, name)

    async def _activate_plugin_skill(self, name: str) -> bool:
        """插件技能独立启用钩子 (异步版)"""
        return await async_plugins._activate_plugin_skill(self, name)

    async def _deactivate_plugin_skill(self, name: str) -> bool:
        """插件技能独立停用钩子 (异步版)"""
        return await async_plugins._deactivate_plugin_skill(self, name)

    # ---------- 模型与流式 ----------

    def set_llm(self, llm: AsyncLLM):
        """替换主模型 (未初始化时延迟到 initialize 生效)"""
        return async_capabilities.set_llm(self, llm)

    def set_model_parameters(self, **kwargs: Any):
        """调整主模型调用参数 (未初始化时延迟到 initialize 生效)"""
        return async_capabilities.set_model_parameters(self, **kwargs)

    def reload_llm(self, llm: AsyncLLM):
        """重载 LLM 实例 (AsyncSession 兼容接口)"""
        return async_capabilities.reload_llm(self, llm)
