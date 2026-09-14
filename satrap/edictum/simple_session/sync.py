"""
同步简易会话入口

组合主工作流, 插件与会话边界处理器, 支持可选的任务恢复,
通过显式插件环境约束插件适用范围
"""

from __future__ import annotations
import threading
from typing import Any, Callable, Iterable
import time
from uuid import uuid4
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import Tool, ToolsManager
from satrap.core.framework.Base import ModelWorkflowFramework, Session
from satrap.core.utils.skills import SkillsManager
from satrap.core.utils.paths import get_db_path
from satrap.edictum.plugin import Plugin
from satrap.core.type import CommandAction
from satrap.core.log import logger
from .utils import (
    SyncUserInputProvider,
    HandlerResult,
    HandlerAbortError,
    HandlerConfig,
    HandlerContext,
    SessionHandler,
)
from satrap.edictum.plugin_compatibility import PluginEnvironment
from .base import _SessionFeatures
from . import recovery
from . import sync_plugins, sync_capabilities


class SimpleSession(Session, _SessionFeatures):
    """
    同步简易会话: 单 workflow 单主模型, 直接使用 full_agent (React 范式)

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
        db_path: str = get_db_path(),
        enable_checkpoint: bool = True,
        recoverable: bool = False,
        plugin_environment: PluginEnvironment | None = None,
        stream: bool = False,
        return_thinking: bool = False,
        thinking_callback: Callable[[str], None] | None = None,
    ):
        """
        初始化同步简易会话及主工作流

        参数:
        - session_id: 会话 ID
        - llm: 主模型实例
        - system_prompt: 系统提示词, 默认 None 保留已有系统提示词, 提供时在初始化阶段重置
        - tools: 初始工具列表, 默认 None 表示不预注册工具
        - content_callback: 内容回调, 用于模型与命令输出, 默认 None 表示不回调
        - db_path: 上下文, 检查点与执行记录数据库路径, 默认使用 get_db_path() 返回的路径
        - enable_checkpoint: 是否启用会话级检查点, 默认 True, 与 recoverable 独立控制
        - recoverable: 是否启用可恢复 Agent 执行, 默认 False; 为 True 时持久化主工作流的模型与工具步骤
        - plugin_environment: 插件适用环境, 默认 None 使用 embedded; Chat 或平台入口应显式提供对应环境
        - stream: 默认是否流式调用模型, 默认 False
        - return_thinking: 是否回传模型思考内容, 默认 False
        - thinking_callback: 思考内容回调, 默认 None 时复用 content_callback

        恢复边界:
        - 中断任务可通过 resume_run(run_id) 恢复, 已完成步骤复用保存结果
        - 恢复不重放会话输入和输出处理器, 工具结果未知时按恢复策略重试或等待人工确认
        - recoverable 不等同于检查点, 也不表示进程启动后自动恢复任务
        """
        self.recoverable = recoverable
        self.plugin_environment = plugin_environment or PluginEnvironment()
        super().__init__(
            session_id,
            content_callback,
            db_path=db_path,
            enable_checkpoint=enable_checkpoint,
        )
        wf_id = self.workflow_id_assign("main")
        self._wf = ModelWorkflowFramework(
            llm,
            context_id=wf_id,
            tools_manager=ToolsManager(),
            recoverable=recoverable,
            system_prompt=system_prompt,
            content_callback=content_callback,
            return_thinking=return_thinking,
            thinking_callback=thinking_callback,
            db_path=db_path,
        )
        self._track_workflow_context(wf_id, self._wf.ctx)
        self._init_handler_registry()
        self._wf.tools_manager.effectiveness_guard = self._tool_effective
        self._skills_manager: SkillsManager | None = None
        self._mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
        """插件 MCP 连接: 连接名 -> (客户端, 同步适配器列表) (同步版经后台事件循环桥接)"""
        self.user_input_provider: SyncUserInputProvider | None = None
        """用户输入通道: 供 ask_user / 审批询问使用 (CLI 或 Web 前端均可注入)"""
        self._run_lock = threading.RLock()
        """同一同步会话的 run 串行锁"""
        self.stream = stream
        if tools:
            for tool in tools:
                self._wf.tools_manager.register_tool(tool)

    # ---------- 属性代理 ----------

    @property
    def llm(self) -> LLM:
        """
        主模型实例

        返回:
        - LLM: 主模型实例
        """
        return self._wf.llm

    @llm.setter
    def llm(self, value: LLM) -> None:
        """
        执行 `llm` 操作

        参数:
        - value: 输入值
        """
        self._wf.llm = value

    @property
    def ctx(self):
        """
        主工作流上下文

        返回:
        - 主工作流上下文
        """
        return self._wf.ctx

    @property
    def tools_manager(self) -> ToolsManager:
        """
        主工作流工具管理器

        返回:
        - ToolsManager: 主工作流工具管理器
        """
        return self._wf.tools_manager

    def get_context_stats(self) -> dict[str, Any] | None:
        """
        返回最近一轮模型调用的上下文统计

        返回:
        - dict[str, Any] | None: 没有模型请求时为 None
        """
        return self._wf.get_context_stats()

    # ---------- 调用入口 ----------

    def run(
        self,
        user_input: str,
        img_urls: list[str] | None = None,
        *,
        thinking: str = "off",
        max_iterations: int = 10,
    ) -> str | CommandAction:
        """
        串行执行一轮同步 Agent 流程

        参数:
        - user_input: 用户输入
        - img_urls: 附加图片 URL 列表, 默认 None
        - thinking: 模型思考强度, 默认 off, 流式和非流式均支持
        - max_iterations: 最大工具迭代次数, 默认 10

        返回:
        - 最终模型文本或命令动作
        """
        with self._run_lock:
            return self._run_once(
                user_input,
                img_urls,
                thinking=thinking,
                max_iterations=max_iterations,
            )

    def _run_once(
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
        - thinking: 模型思考强度, 默认 off, 流式和非流式均支持
        - max_iterations: 最大工具调用迭代次数

        处理器语义:
        - before_user_send 可链式改写 (str/None/HandlerResult); respond/reject 短路跳过模型;
          abort 抛 HandlerAbortError (ctx.outcome 记录短路语义)
        - after_model_reply 在 finally 中执行; 异常路径 result=None 且 ctx.error 有值, 改写被忽略
        - handler 异常隔离 (error_policy="abort" 时抛 HandlerAbortError); 同一次 run 内
          handler 状态变更为一致性快照 (下次生效)
        - run 期间 remove/replace 的 handler 延迟 close, 本轮 run 结束后冲刷

        返回:
        - str | CommandAction: 命令结果或最终模型输出
        """
        self._wf.reset_context_stats()
        command_result, is_command = self.cmd_process(user_input)
        if is_command:
            if isinstance(command_result, CommandAction):
                return command_result
            return "" if command_result is None else str(command_result)

        with self._registry_lock:
            self._active_runs += 1
            handlers = self._enabled_handlers()
        try:
            call_id = uuid4().hex
            ctx = HandlerContext(
                config=HandlerConfig(
                    original_input=user_input,
                    img_urls=img_urls,
                    thinking=thinking,
                    max_iterations=max_iterations,
                    call_id=call_id,
                ),
                text=user_input,
            )
            text = user_input
            final_override: str | None = None
            for p in handlers:
                if p.before_user_send is None:
                    continue
                out = self._invoke_handler(
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
                    self._invoke_handler(
                        p, "after_user_send", p.after_user_send, text, ctx
                    )

                for p in handlers:
                    if p.before_model_reply is None:
                        continue
                    self._invoke_handler(
                        p, "before_model_reply", p.before_model_reply, ctx
                    )

            if self.recoverable:
                recovery.prepare_session_recovery(self)

            result: str | None = None
            ctx.error = None
            try:
                if final_override is not None:
                    result = final_override
                elif self.stream:
                    result = self._wf.stream_full_agent(
                        text,
                        img_urls=img_urls,
                        thinking=thinking,
                        max_iterations=max_iterations,
                    )
                else:
                    result = self._wf.full_agent(
                        text,
                        img_urls=img_urls,
                        thinking=thinking,
                        max_iterations=max_iterations,
                    )
            except BaseException as e:
                ctx.error = e
                raise
            finally:
                for p in handlers:
                    if p.after_model_reply is None:
                        continue
                    out = self._invoke_handler(
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
                self._close_handler(h.name, h)

    def _invoke_handler(
        self, handler: SessionHandler, stage: str, fn: Callable[..., Any], *args: Any
    ) -> Any:
        """
        调用单个处理器回调: 异常隔离 (error_policy="abort" 抛 HandlerAbortError) + 耗时观测

        参数:
        - handler: 处理器
        - stage: 处理阶段
        - fn: 待调用函数
        - args: 额外位置参数

        返回:
        - Any: 调用单个处理器回调: 异常隔离 (error_policy="abort" 抛 HandlerAbortError) + 耗时观测
        """
        t0 = time.perf_counter()
        try:
            return fn(*args)
        except HandlerAbortError:
            raise
        except Exception as e:
            if handler.error_policy == "abort":
                raise HandlerAbortError(
                    f"处理器 {handler.name}.{stage} 异常: {e}"
                ) from e
            logger.error(f"[edictum] 处理器 {handler.name}.{stage} 异常: {e}")
            return None
        finally:
            logger.debug(
                f"[edictum] 处理器 {handler.name}.{stage} 耗时 {time.perf_counter() - t0:.3f}s"
            )

    def __call__(self, user_input: str, **kwargs: Any) -> str | CommandAction:
        return self.run(user_input, **kwargs)

    def resume_run(self, run_id: str) -> str:
        """从保存步骤继续, 不重放 SessionHandler"""
        return recovery.resume_sync(self, run_id)

    def list_runs(self):
        """查询当前会话任务状态"""
        return recovery.summaries(self)

    def abort_run(self, run_id: str) -> None:
        """终止未完成的任务"""
        recovery.store_for_session(self).abort(run_id)

    def retry_uncertain_step(self, run_id: str, step_id: str) -> None:
        """明确授权未知结果工具步骤重试"""
        recovery.store_for_session(self).authorize_retry(run_id, step_id)

    # ---------- 命令管理 ----------

    def add_command(self, name: str, handler: Callable[..., Any], intro: str = "None"):
        """注册命令"""
        return sync_capabilities.add_command(self, name, handler, intro)

    def remove_command(self, name: str) -> bool:
        """注销命令"""
        return sync_capabilities.remove_command(self, name)

    def enable_command(self, name: str) -> bool:
        """启用命令"""
        return sync_capabilities.enable_command(self, name)

    def disable_command(self, name: str) -> bool:
        """停用命令 (停用后消息不再按命令处理)"""
        return sync_capabilities.disable_command(self, name)

    def is_command_enabled(self, name: str) -> bool:
        """检查命令是否启用"""
        return sync_capabilities.is_command_enabled(self, name)

    def list_commands(self) -> dict[str, str]:
        """列出已注册命令及其简介"""
        return sync_capabilities.list_commands(self)

    # ---------- 工具管理 ----------

    def add_tool(self, tool: Tool):
        """注册工具 (任意时刻可注入, 立即生效)"""
        return sync_capabilities.add_tool(self, tool)

    def add_tools(self, *tools: Tool):
        """批量注册工具"""
        return sync_capabilities.add_tools(self, *tools)

    def remove_tool(self, name: str) -> bool:
        """注销工具"""
        return sync_capabilities.remove_tool(self, name)

    def enable_tool(self, name: str) -> bool:
        """启用工具"""
        return sync_capabilities.enable_tool(self, name)

    def disable_tool(self, name: str) -> bool:
        """停用工具"""
        return sync_capabilities.disable_tool(self, name)

    def is_tool_enabled(self, name: str) -> bool:
        """检查工具是否启用"""
        return sync_capabilities.is_tool_enabled(self, name)

    def list_tools(self) -> list[str]:
        """列出已注册工具名"""
        return sync_capabilities.list_tools(self)

    # ---------- skill 管理 ----------

    def add_skill(
        self, skill_name: str, skills_manager: SkillsManager | None = None
    ) -> bool:
        """加载并激活技能到主工作流"""
        return sync_capabilities.add_skill(self, skill_name, skills_manager)

    def remove_skill(self, skill_name: str) -> bool:
        """从工作流卸载并从管理器移除技能定义"""
        return sync_capabilities.remove_skill(self, skill_name)

    def enable_skill(self, skill_name: str) -> bool:
        """重新激活技能"""
        return sync_capabilities.enable_skill(self, skill_name)

    def disable_skill(self, skill_name: str) -> bool:
        """停用技能 (从工作流卸载, 保留注册)"""
        return sync_capabilities.disable_skill(self, skill_name)

    # ---------- 处理器管理 (注册表实现在 _HandlerRegistryMixin) ----------

    def _tool_effective(self, tool_name: str) -> bool:
        """工具生效过滤 (执行路径合成): 所属插件缺失或启用时生效"""
        return sync_capabilities._tool_effective(self, tool_name)

    # ---------- 插件管理 (目录插件包) ----------

    def install_plugin(self, path: str, config: dict[str, Any] | None = None) -> Plugin:
        """安装目录插件 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py)"""
        return sync_plugins.install_plugin(self, path, config)

    def uninstall_plugin(self, name: str) -> bool:
        """卸载插件: 回收其全部能力 (含清理回调), 不留孤儿"""
        return sync_plugins.uninstall_plugin(self, name)

    def enable_plugin(self, name: str) -> bool:
        """启用插件 (按独立状态恢复名下能力, 独立停用的保持停用)"""
        return sync_plugins.enable_plugin(self, name)

    def disable_plugin(self, name: str) -> bool:
        """停用插件 (压制名下全部能力, 不改独立状态; 工具/处理器由执行路径合成压制)"""
        return sync_plugins.disable_plugin(self, name)

    def _activate_plugin_skill(self, name: str) -> bool:
        """插件技能独立启用钩子 (同步版)"""
        return sync_plugins._activate_plugin_skill(self, name)

    def _deactivate_plugin_skill(self, name: str) -> bool:
        """插件技能独立停用钩子 (同步版)"""
        return sync_plugins._deactivate_plugin_skill(self, name)

    # ---------- 模型与流式 ----------

    def set_llm(self, llm: LLM):
        """替换主模型"""
        return sync_capabilities.set_llm(self, llm)

    def set_model_parameters(self, **kwargs: Any):
        """调整主模型调用参数 (temperature / top_p / max_tokens 等)"""
        return sync_capabilities.set_model_parameters(self, **kwargs)

    def reload_llm(self, llm: LLM):
        """重载 LLM 实例 (Session 兼容接口)"""
        return sync_capabilities.reload_llm(self, llm)
