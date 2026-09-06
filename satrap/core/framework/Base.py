"""
模型工作流与会话基础框架

定义同步和异步工作流的模型调用, 工具执行与上下文处理流程,
并为会话提供命令, 状态检查点和持久化能力
"""
import asyncio
import inspect, copy, json, uuid
from pathlib import Path
from typing import Optional, Callable, Any, Awaitable, TypeVar, cast, Literal
from typing import TYPE_CHECKING

from satrap.core.utils.context_policy import apply_context_policy
from satrap.core.framework.command import CommandHandler, AsyncCommandHandler
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.utils.TCBuilder import Tool, create_tool_defined, ToolsManager, AsyncToolsManager
from satrap.core.state.mutation import state_mutation_context
from satrap.core.utils.context import add_user_message, add_bot_message, add_tool_message, add_tools_call_flow, clear_reasoning_content
from satrap.core.utils.context import AsyncContextManager, ContextManager, PreparedModelContext, _messages_domain
from satrap.core.utils.paths import get_db_path
from satrap.core.state import StateStore
from satrap.core.type import (
    LLMCallResponse,
    LLMConfig,
    ModelContextRequestStats,
    ModelContextTurnStats,
    StateCheckpoint,
    TokenUsage,
)

from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.storage.layout import StorageLayout
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.UserManager import UserManager

_WorkflowT = TypeVar("_WorkflowT")
"""工作流类泛型, 用于 create 工厂与 await 工具"""


def _build_context_request_stats(
    prepared: PreparedModelContext,
    usage: TokenUsage | None,
) -> ModelContextRequestStats:
    """
    将上下文准备结果和 API usage 合并为一次请求统计

    参数:
    - prepared: 上下文准备结果
    - usage: API 返回的真实 token 用量

    返回:
    - ModelContextRequestStats: 请求统计
    """
    return ModelContextRequestStats(
        model=prepared.model,
        strategy=prepared.strategy,
        compressed=prepared.compressed,
        original_turns=prepared.original_turns,
        prepared_turns=prepared.prepared_turns,
        original_estimated_input_tokens=prepared.original_estimated_input_tokens,
        estimated_input_tokens=prepared.estimated_input_tokens,
        effective_input_tokens=prepared.effective_input_tokens,
        preflight_token_source=prepared.token_source,
        history_budget=prepared.history_budget,
        trigger_tokens=prepared.trigger_tokens,
        floor_tokens=prepared.floor_tokens,
        api_input_tokens=usage.input_tokens if usage is not None else None,
        api_output_tokens=usage.output_tokens if usage is not None else None,
        api_total_tokens=usage.total_tokens if usage is not None else None,
        api_cached_tokens=usage.cached_tokens if usage is not None else None,
    )

class ModelWorkflowFramework:
    """模型工作流框架"""
    def __init__(
        self,
        llm: LLM,
        context_id: str,
        tools_manager: ToolsManager | None = None,
        system_prompt: str | None = None,
        content_callback: Optional[Callable[[str], None]] = None,
        return_thinking: bool = False,
        thinking_callback: Optional[Callable[[str], None]] = None,
        *, db_path: str = get_db_path(),
    ):
        """
        模型工作流框架, 负责管理模型的调用和工作流的执行
        这是 Satrap 的核心组件之一, 负责协调单个模型实例调用, 以实现复杂的任务处理和自动化流程;

        任何工作流都应该继承这个类, 并实现自己的工作流逻辑, 以便被调用和执行;
        工作流应当覆写 `forward` 方法, 并在其中实现模型的调用和工作流的逻辑

        并在初始化进行 `super().__init__(llm, context_id, tools_manager, system_prompt, content_callback)`

        如果设置了 `content_callback`, 则可以使用 `self._content_content(content)` 方法在模型调用过程中抛出模型回复内容, 以实现及时输出模型回复内容

        参数:
        - llm: 模型实例
        - context_id: 上下文 ID
        - tools_manager: 工具管理器实例
        - system_prompt: 系统提示词; 如果填写, 会重置上下文的系统提示词
        - content_callback: 内容回调函数, 用于在复杂模型调用过程中抛出模型回复内容; 如果只取最终回复, 则可以设置为 None
        - return_thinking: 是否返回模型思考内容; 如果为 True, 则会在模型回复内容前抛出思考内容
        - thinking_callback: 思考内容回调函数; 未设置时复用 content_callback
        - db_path: 上下文数据库路径

        使用示例
        ``` python
        class MyWorkflow(ModelWorkflowFramework):
            def __init__(self, llm, context_id, tools_manager, system_prompt=None, content_callback=None):
                super().__init__(llm, context_id, tools_manager, system_prompt, content_callback)

            def forward(self, user_input: str) -> str:
                self.ctx.add_user_message(user_input)
                response = self.llm.call(self.ctx.get_context(), tools=self.tools_manager.get_tools_definitions())

                if not response:
                    return "模型调用失败"

                # 处理可能的工具调用
                context, success = self.agent_executor(response)
                return context[-1]["content"] if success else "执行失败"

        # 2. 初始化并调用工作流
        workflow = MyWorkflow(llm, "conversation_id", tools_manager, "You are a helper")
        result = workflow("北京今天天气怎么样? ")
        print(result)

        # 或者集成进 `Session` 类中, 以实现复杂多模型 Agent 与会话管理
        ```
        """
        self.llm = llm
        self.ctx = ContextManager(context_id, db_path=db_path)
        self.tools_manager = tools_manager if tools_manager else ToolsManager()
        # 如果未提供工具管理器, 则创建一个空的工具管理器实例

        self.return_thinking = return_thinking
        self.thinking_callback = thinking_callback


        self.ctx.load_context()
        if system_prompt:
            self.ctx.reset_system_prompt(system_prompt)

        self.content_callback = content_callback
        self._context_turn_stats = ModelContextTurnStats()

    def reset_context_stats(self) -> None:
        """清空当前轮次的模型上下文统计"""
        self._context_turn_stats = ModelContextTurnStats()

    def get_context_stats(self) -> dict[str, Any] | None:
        """
        返回当前轮次的模型上下文统计

        返回:
        - dict[str, Any] | None: 没有模型请求时为 None
        """
        payload = self._context_turn_stats.to_dict()
        return payload or None

    def _record_context_request(
        self,
        prepared: PreparedModelContext,
        usage: TokenUsage | None,
    ) -> None:
        """
        记录一次正式模型请求

        参数:
        - prepared: 上下文准备结果
        - usage: API 返回的真实 token 用量
        """
        self._context_turn_stats.requests.append(_build_context_request_stats(prepared, usage))

    def _content_callback(self, content: str):
        """
        调用回调返回模型回复内容

        参数:
        - content: 内容
        """
        if self.content_callback and content:
            self.content_callback(content)

    def _call_model(
        self,
        *,
        pending_messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking: str = "off",
        img_urls: list[str] | None = None,
    ) -> LLMCallResponse | Literal[False]:
        """
        准备上下文, 调用模型并记录真实 usage

        参数:
        - pending_messages: 尚未写入完整历史的本轮临时消息
        - tools: 工具定义列表
        - thinking: 思考强度
        - img_urls: 图片 URL 列表

        返回:
        - LLMCallResponse | Literal[False]: 模型响应
        """
        prepared = self.ctx.prepare_model_context(
            llm=self.llm,
            pending_messages=pending_messages,
            tools=tools,
            img_urls=img_urls,
        )
        call_kwargs: dict[str, Any] = {"tools": tools, "img_urls": img_urls}
        if thinking != "off":
            call_kwargs["thinking"] = thinking
        response = self.llm.call(prepared.messages, **call_kwargs)
        if isinstance(response, LLMCallResponse):
            self._record_context_request(prepared, response.usage)
            self.ctx.record_model_usage(prepared, response.usage)
        return response

    def agent_executor(self, model_response: LLMCallResponse,
        callback: bool = False, max_iterations: int = 10,
        img_urls: list[str] | None = None) -> tuple[list[dict[str, str | list[Any]]], bool]:
        """
        智能体执行器, 用于执行智能体调用流程

        参数:
        - model_response: 模型调用响应
        - callback: 是否回调回复, 默认关闭
        - max_iterations: 最大迭代次数
        - img_urls: 随请求发送的图片 URL 列表

        返回:
        - list[dict[str, str | list[Any]]]: 上下文消息列表
        - bool: 是否成功执行
        """
        try:
            now_iteration = 0
            now_response = model_response
            turn_messages: list[dict[str, Any]] = []

            while now_response.type == "tools_call" and now_response.tool_calls and now_iteration < max_iterations:
                now_iteration += 1
                tool_messages: list[dict[str, Any]] = []
                tool_results: list[dict[str, Any]] = []

                if callback:   # 回调回复
                    if now_response.thinking and self.content_callback:
                        self._content_callback(f"<think>\n{now_response.thinking}\n</think>")
                    if now_response.content and self.content_callback:
                        self._content_callback(now_response.content)

                for tool_call in now_response.tool_calls:
                    result = self.tools_manager.execute_tool_call(tool_call)
                    tool_message, tool_result = result
                    tool_messages.append(tool_message)
                    tool_results.append(tool_result)
                    # 执行工具调用并获取结果

                add_tools_call_flow(turn_messages, now_response.content, tool_messages, tool_results, now_response.thinking)
                # 添加至本轮消息流

                new_response = self._call_model(
                    pending_messages=turn_messages,
                    tools=self.tools_manager.get_tools_definitions(),
                    img_urls=img_urls,
                )   # 调用模型

                if not new_response:   # 模型调用失败, 无响应返回
                    clear_reasoning_content(turn_messages)
                    self.ctx.add_turn_messages(turn_messages)
                    logger.error("模型调用失败, 无响应返回")
                    break

                if now_iteration >= max_iterations:   # 达到最大迭代次数

                    if callback:   # 回调回复
                        if now_response.thinking and self.content_callback:
                            self._content_callback(f"<think>\n{now_response.thinking}\n</think>")
                        if now_response.content and self.content_callback:
                            self._content_callback(now_response.content)

                    clear_reasoning_content(turn_messages)
                    self.ctx.add_turn_messages(turn_messages)
                    logger.warning("已达到最大工具调用迭代次数, 停止执行")
                    self.ctx.add_user_message("已达到最大工具调用尝试次数，请基于已有信息给出最终答案。")
                    final_response = self._call_model(tools=[], img_urls=img_urls)

                    if not final_response:
                        logger.error("达到最大迭代次数后调用 LLM 生成最终答案失败")
                        return self.ctx.get_context(), False

                    self.ctx.add_bot_message(final_response.content)
                    if callback:
                        self._content_callback(final_response.content)

                    return self.ctx.get_context(), True

                now_response = new_response   # 更新当前响应

            else:   # 模型直接返回最终答案
                if callback:   # 回调回复
                    if now_response.thinking and self.content_callback:
                        self._content_callback(f"<think>\n{now_response.thinking}\n</think>")
                    if now_response.content and self.content_callback:
                        self._content_callback(now_response.content)

                add_bot_message(turn_messages, now_response.content)
                clear_reasoning_content(turn_messages)
                self.ctx.add_turn_messages(turn_messages)

            return self.ctx.get_context(), True
    
        except Exception as e:
            logger.error(f"智能体执行器错误: {e}")
            return self.ctx.get_context(), False

    @staticmethod
    def final_response(messages: LLMCallResponse | bool) -> str:
        """
        获取最终回复

        参数:
        - messages: 消息列表

        返回:
        - str: 最终回复
        """
        if isinstance(messages, bool):
            return "模型调用失败, 请查看日志以获得更多信息"
        else:
            return messages.content

    @staticmethod
    def get_bot_message(messages: list[dict[str, str | list[Any]]]) -> str:
        """
        获取最后一条 assistant 回复

        参数:
        - messages: 消息列表

        返回:
        - str: 最后一条 assistant 回复
        """
        for message in reversed(messages):
            if message["role"] == "assistant":
                return str(message["content"])
        return ""

    @staticmethod
    def _get_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        提取系统消息副本

        参数:
        - messages: 消息列表

        返回:
        - list[dict[str, Any]]: 提取系统消息副本
        """
        return [copy.deepcopy(message) for message in messages if message.get("role") == "system"]

    def _restore_context_keep_system(self, system_messages: list[dict[str, Any]]):
        """
        恢复上下文为系统消息

        参数:
        - system_messages: system消息列表
        """
        self.ctx._messages = copy.deepcopy(system_messages)
        self.ctx._mark_dirty()
        self.ctx._sync()

    def full_agent(self, user_input: str, callback: bool = True, max_iterations: int = 10,
        img_urls: list[str] | None = None) -> str:
        """
        完整执行一轮 Agent 流程, 返回最终模型输出

        参数:
        - user_input: 用户输入
        - callback: 回调函数
        - max_iterations: 最大迭代次数
        - img_urls: 图片 URL 列表

        返回:
        - str: 完整执行一轮 Agent 流程, 返回最终模型输出
        """
        self.reset_context_stats()
        self.ctx.add_user_message(user_input)

        response = self._call_model(
            tools=self.tools_manager.get_tools_definitions(),
            img_urls=img_urls,
        )
        if not response:
            return "模型调用失败"

        context, success = self.agent_executor(
            response, callback=callback, max_iterations=max_iterations, img_urls=img_urls,
        )
        if not success:
            return "执行失败"

        return self.get_bot_message(context)

    def _stream_call_response(
        self,
        pending_messages: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        callback: bool,
        thinking: str,
        img_urls: list[str] | None = None,
    ) -> LLMCallResponse | bool:
        """
        消费一次流式请求并返回完整响应

        参数:
        - pending_messages: 尚未写入完整历史的本轮临时消息
        - tools: 可选参数, 工具定义列表, 用于 Function Calling
        - callback: 是否回调回复, 默认关闭
        - thinking: 是否要求模型进行思考, 默认为 False
        - img_urls: 随请求发送的图片 URL 列表

        返回:
        - LLMCallResponse | bool: 消费一次流式请求并返回完整响应
        """
        prepared = self.ctx.prepare_model_context(
            llm=self.llm,
            pending_messages=pending_messages,
            tools=tools,
            img_urls=img_urls,
        )
        response: Any = None
        for event in self.llm.stream_call(prepared.messages, tools=tools, thinking=thinking, img_urls=img_urls):
            if callback and event.kind == "content_delta":
                self._content_callback(event.delta)

            elif callback and event.kind == "thinking_delta" and self.return_thinking:
                if self.thinking_callback:
                    self.thinking_callback(event.delta)
                else:
                    self._content_callback(event.delta)

            elif event.kind == "error" and event.error:
                logger.error(f"流式 LLM 调用失败: {event.error}")

            elif event.kind == "done":
                response = event.response

        if isinstance(response, (LLMCallResponse, bool)):
            if isinstance(response, LLMCallResponse):
                self._record_context_request(prepared, response.usage)
                self.ctx.record_model_usage(prepared, response.usage)
            return response
        return False

    def stream_full_agent(
        self,
        user_input: str,
        callback: bool = True,
        max_iterations: int = 10,
        thinking: str = "off",
        img_urls: list[str] | None = None,
    ) -> str:
        """
        流式执行一轮 Agent 流程并返回最终模型输出

        参数:
        - user_input: 用户输入
        - callback: 是否回调回复, 默认关闭
        - max_iterations: 最大迭代次数, 默认 10
        - thinking: 是否要求模型进行思考, 默认为 False
        - img_urls: 随请求发送的图片 URL 列表

        返回:
        - 最终模型输出
        """
        self.reset_context_stats()
        self.ctx.add_user_message(user_input)
        max_iterations = max(1, max_iterations)

        response = self._stream_call_response(
            None,
            self.tools_manager.get_tools_definitions(),
            callback,
            thinking,
            img_urls=img_urls,
        )
        if not isinstance(response, LLMCallResponse):
            return "模型调用失败"

        now_response = response
        now_iteration = 0
        turn_messages: list[dict[str, Any]] = []

        while now_response.type == "tools_call" and now_response.tool_calls and now_iteration < max_iterations:
            now_iteration += 1
            tool_messages: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []

            for tool_call in now_response.tool_calls:
                tool_message, tool_result = self.tools_manager.execute_tool_call(tool_call)
                tool_messages.append(tool_message)
                tool_results.append(tool_result)

            add_tools_call_flow(
                turn_messages,
                now_response.content,
                tool_messages,
                tool_results,
                now_response.thinking,
            )

            new_response = self._stream_call_response(
                turn_messages,
                self.tools_manager.get_tools_definitions(),
                callback,
                thinking,
                img_urls=img_urls,
            )
            if not isinstance(new_response, LLMCallResponse):
                clear_reasoning_content(turn_messages)
                self.ctx.add_turn_messages(turn_messages)
                return "执行失败"

            if now_iteration >= max_iterations:
                clear_reasoning_content(turn_messages)
                self.ctx.add_turn_messages(turn_messages)
                self.ctx.add_user_message("已达到最大工具调用尝试次数，请基于已有信息给出最终答案。")
                final_response = self._stream_call_response(
                    None,
                    [],
                    callback,
                    thinking,
                    img_urls=img_urls,
                )
                if not isinstance(final_response, LLMCallResponse):
                    return "执行失败"
                self.ctx.add_bot_message(final_response.content)
                return final_response.content

            now_response = new_response

        add_bot_message(turn_messages, now_response.content, reasoning=now_response.thinking)
        clear_reasoning_content(turn_messages)
        self.ctx.add_turn_messages(turn_messages)
        return self.get_bot_message(self.ctx.get_context())

    def tools_agent(self, user_input: str, callback: bool = True, max_iterations: int = 10,
        img_urls: list[str] | None = None) -> str:
        """
        使用临时上下文完整执行一轮 Agent 流程, 返回最终模型输出

        参数:
        - user_input: 用户输入
        - callback: 回调函数
        - max_iterations: 最大迭代次数
        - img_urls: 图片 URL 列表

        返回:
        - str: 使用临时上下文完整执行一轮 Agent 流程, 返回最终模型输出
        """
        system_messages = self._get_system_messages(self.ctx.get_context())
        self.reset_context_stats()
        self._restore_context_keep_system(system_messages)

        try:
            self.ctx.add_user_message(user_input)

            response = self._call_model(
                tools=self.tools_manager.get_tools_definitions(),
                img_urls=img_urls,
            )
            if not response:
                return "模型调用失败"

            context, success = self.agent_executor(
                response, callback=callback, max_iterations=max_iterations, img_urls=img_urls,
            )
            if not success:
                return "执行失败"

            return self.get_bot_message(context)
        finally:
            self._restore_context_keep_system(system_messages)

    def stream_tools_agent(
        self,
        user_input: str,
        callback: bool = True,
        max_iterations: int = 10,
        thinking: str = "off",
        img_urls: list[str] | None = None,
    ) -> str:
        """
        使用临时上下文流式执行一轮 Agent 流程, 返回最终模型输出

        参数:
        - user_input: 用户输入
        - callback: 是否回调回复, 默认开启
        - max_iterations: 最大迭代次数, 默认 10
        - thinking: 是否要求模型进行思考, 默认为 False
        - img_urls: 随请求发送的图片 URL 列表

        返回:
        - 最终模型输出
        """
        system_messages = self._get_system_messages(self.ctx.get_context())
        self._restore_context_keep_system(system_messages)

        try:
            return self.stream_full_agent(
                user_input,
                callback=callback,
                max_iterations=max_iterations,
                thinking=thinking,
                img_urls=img_urls,
            )
        finally:
            self._restore_context_keep_system(system_messages)

    def reset_llm(self, llm: LLM):
        """
        重置会话模型

        参数:
        - llm: 模型实例
        """
        self.llm = llm

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        """
        执行工作流; 调用模型并返回结果

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行工作流; 调用模型并返回结果
        """
        return None

    def __call__(self, *input: Any, **kwargs: Any):
        result = self.forward(*input, **kwargs)
        return result

class Session:
    """会话类, 用于管理多个模型工作流的会话"""
    plugin_override_store: "SessionOverrideStore | None" = None
    plugin_model_manager: "ModelConfigManager | None" = None
    storage_layout: "StorageLayout | None" = None
    storage_platform_id: str | None = None

    def __init__(self, session_id: str, content_callback: Optional[Callable[[str], None]] | None = None,
        command_handler: Optional[CommandHandler] | None = None, *, db_path: str = get_db_path(),
        state_store: Optional[StateStore] = None, enable_checkpoint: bool = False):
        """
        会话框架, 用于管理多个模型工作流的会话
        任何依赖多模型的复杂 Agent 都应当继承自该类, 并实现 `forward` 方法

        并在初始化时进行 `super().__init__(session_id, content_callback, command_handler)`

        参数:
        - session_id: 会话 ID
        - content_callback: 内容回调函数, 用于在复杂模型调用过程中抛出模型回复内容; 如果只取最终回复, 则可以设置为 None
        - command_handler: 命令处理程序实例
        - db_path: 会话上下文数据库路径 (与检查点存储同库)
        - state_store: 状态检查点存储实例, 传入后启用会话级检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向 db_path 的 StateStore, 与显式传入 state_store 二选一
        """
        self._state_store = state_store
        if self._state_store is None and enable_checkpoint:
            self._state_store = StateStore(db_path=db_path)
        self._workflow_contexts: dict[str, ContextManager] = {}
        """工作流 ID -> 工作流上下文, 供会话级检查点聚合"""
        self._context_config: LLMConfig | None = None
        """当前会话使用的模型上下文配置"""

        self.session_ctx = ContextManager(session_id, db_path=db_path)
        """会话共享上下文"""

        if self._state_store is not None:
            self._state_store.register_domain(_messages_domain())
            self.session_ctx.state_store = self._state_store

        if command_handler is None:   # 创建默认命令处理器, 输出回调指向 _content_callback
            self.cmd_handler = CommandHandler(output_callback=self._content_callback)

        else:   # 确保输出回调被设置, 默认指向 _content_callback
            self.cmd_handler = command_handler
            if self.cmd_handler.output_callback is None:
                self.cmd_handler.output_callback = self._content_callback

        self.command_handler = self.cmd_handler
        self.session_ctx.load_context()
        self.session_id = session_id
        self.wf_list: list[str] = []

        self.content_callback = content_callback
        self._user_manager: UserManager | None = None

    def _content_callback(self, content: str):
        """
        调用回调返回模型回复内容

        参数:
        - content: 内容
        """
        if self.content_callback and content:
            self.content_callback(content)

    @staticmethod
    def _parse_user_id(session_id: str) -> str:
        """
        从 session_id 中提取 user_id, 兼容新旧格式

        参数:
        - session_id: 会话 ID

        返回:
        - str: 从 session_id 中提取 user_id, 兼容新旧格式
        """
        parts = session_id.split(":")
        if len(parts) >= 4:
            return parts[2]
        if len(parts) == 3:
            return parts[1]
        return ""

    @property
    def user_contexts(self) -> list[str]:
        """
        获取当前用户的所有上下文 session_id 列表

        返回:
        - list[str]: 当前用户的所有上下文 session_id 列表
        """
        if not self._user_manager:
            return []
        user_id = self._parse_user_id(self.session_id)
        if not user_id:
            return []
        return self._user_manager.get_user_session_ids(user_id)

    def on_session_switched(self, old_session_id: str, new_session_id: str) -> None:
        """
        上下文切换后调用, 子类可重写以刷新工作流

        参数:
        - old_session_id: old会话ID
        - new_session_id: new会话ID

        返回:
        - None: 上下文切换后调用, 子类可重写以刷新工作流
        """
        return None

    def reload_llm(self, llm: LLM):
        """
        重载 LLM 实例, 子类可重写以更新工作流内的 LLM 引用

        参数:
        - llm: 模型实例
        """

    def run(self, *input: Any, **kwargs: Any) -> Any:
        """
        执行会话; 调用模型并返回结果

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行会话; 调用模型并返回结果
        """
        return None
    
    def workflow_id_assign(self, wf_id: str) -> str:
        """
        为会话分配工作流 ID

        重复的 wf_id 自动追加序号 (如 main -> main_2) 并记录警告, 避免工作流上下文互相污染

        参数:
        - wf_id: 工作流 ID

        返回:
        - 工作流 ID (str) (session_id + "_" + wf_id)
        """
        workflow_id = self.session_id + "_" + wf_id
        if workflow_id in self.wf_list:
            logger.warning(f"[会话] 工作流 ID 重复, 自动追加序号: {wf_id} -> {wf_id}_2")
            suffix = 2
            while f"{self.session_id}_{wf_id}_{suffix}" in self.wf_list:
                suffix += 1
            workflow_id = f"{self.session_id}_{wf_id}_{suffix}"
        self.wf_list.append(workflow_id)
        return workflow_id

    def _require_session_store(self) -> StateStore:
        """
        获取会话级状态存储, 未启用时抛出 ValueError

        返回:
        - StateStore: 会话级状态存储, 未启用时抛出 ValueError
        """
        if self._state_store is None:
            raise ValueError("未启用会话级状态检查点, 请传入 state_store 或设置 enable_checkpoint=True")
        return self._state_store

    def _all_contexts(self) -> dict[str, ContextManager]:
        """
        返回 {上下文名: ContextManager}, 含会话共享上下文与全部工作流上下文

        返回:
        - dict[str, ContextManager]:  {上下文名: ContextManager}, 含会话共享上下文与全部工作流上下文
        """
        contexts: dict[str, ContextManager] = {"session": self.session_ctx}
        contexts.update(self._workflow_contexts)
        return contexts

    def apply_context_config(self, config: LLMConfig) -> None:
        """
        将模型上下文配置应用到会话及全部工作流上下文

        参数:
        - config: LLM 配置
        """
        self._context_config = config
        for context in self._all_contexts().values():
            apply_context_policy(context, config)

    def _track_workflow_context(self, wf_id: str, ctx: ContextManager) -> None:
        """
        注册工作流上下文, 使其纳入会话级检查点聚合

        参数:
        - wf_id: 工作流 ID (workflow_id_assign 的返回值)
        - ctx: 工作流的 ContextManager 实例
        """
        if wf_id in self._workflow_contexts:
            logger.warning(f"[会话] 工作流 {wf_id} 已注册, 将被覆盖")
        self._workflow_contexts[wf_id] = ctx
        if self._context_config is not None:
            apply_context_policy(ctx, self._context_config)
        if self._state_store is not None:
            if Path(str(ctx.db_path)).resolve() != Path(str(self._state_store.db_path)).resolve():
                raise ValueError(
                    f"工作流 {wf_id} 的上下文库 {ctx.db_path} 与会话状态库 {self._state_store.db_path} 不一致"
                )
            if ctx.state_store is None:
                ctx.state_store = self._state_store
                self._state_store.register_domain(_messages_domain())

    def create_checkpoint(self, name: str = "", description: str = "") -> str:
        """
        为会话创建聚合检查点 (会话共享上下文 + 全部工作流上下文), 返回批次 ID

        参数:
        - name: 检查点显示名称
        - description: 检查点说明

        返回:
        - str: 批次 ID, 用于 list_checkpoints / rollback
        """
        self._require_session_store()
        batch_id = f"batch-{uuid.uuid4().hex[:16]}"
        try:
            with state_mutation_context(
                source="session_checkpoint", reason=f"创建会话检查点 {name or batch_id}"
            ):
                for ctx_name, ctx in self._all_contexts().items():
                    ctx.create_checkpoint(
                        name=f"{name}[{ctx_name}]" if name else ctx_name,
                        description=description,
                        batch_id=batch_id,
                    )
        except Exception:
            store = self._require_session_store()
            # 补偿: 删除已创建的残批检查点, 避免部分作用域回滚的不一致状态
            for ctx in self._all_contexts().values():
                for cp in store.list_checkpoints(ctx._scope()):
                    if cp.batch_id == batch_id:
                        store.delete_checkpoint(cp.checkpoint_id)
            raise
        return batch_id

    def list_checkpoints(self) -> list[StateCheckpoint]:
        """
        列出会话的全部聚合检查点 (按批次去重, 时间升序)

        返回:
        - list[StateCheckpoint]: 检查点列表, 每个批次一个代表检查点
        """
        store = self._require_session_store()
        seen: dict[str, StateCheckpoint] = {}
        for ctx in self._all_contexts().values():
            for cp in store.list_checkpoints(ctx._scope()):
                if cp.batch_id:
                    seen.setdefault(cp.batch_id, cp)
                else:
                    seen.setdefault(cp.checkpoint_id, cp)
        return list(seen.values())

    def rollback(self, checkpoint_id: str) -> None:
        """
        回滚会话到指定检查点批次并重载全部上下文

        参数:
        - checkpoint_id: 批次 ID 或批次内任一检查点的 ID
        """
        store = self._require_session_store()
        is_batch, target = self._resolve_batch_id(store, checkpoint_id)
        with state_mutation_context(
            source="session_checkpoint_rollback", reason=f"回滚到检查点 {checkpoint_id}"
        ):
            if is_batch:
                store.rollback_batch(target)
            else:
                store.rollback(target)   # 单检查点 (非聚合)
        for ctx in self._all_contexts().values():
            ctx.load_context()

    def retry(self, checkpoint_id: str) -> None:
        """
        从指定检查点批次重试并重载全部上下文 (保留未来检查点)

        参数:
        - checkpoint_id: 批次 ID 或批次内任一检查点的 ID
        """
        store = self._require_session_store()
        is_batch, target = self._resolve_batch_id(store, checkpoint_id)
        with state_mutation_context(
            source="session_checkpoint_retry", reason=f"重试到检查点 {checkpoint_id}"
        ):
            if is_batch:
                store.retry_batch(target)
            else:
                store.retry(target)   # 单检查点 (非聚合)
        for ctx in self._all_contexts().values():
            ctx.load_context()

    def list_branches(self) -> list[StateCheckpoint]:
        """
        列出从本会话 fork 出的全部分支起点检查点 (会话共享 + 各工作流)

        返回:
        - list[StateCheckpoint]: 分支检查点列表 (按时间升序)
        """
        store = self._require_session_store()
        branches: list[StateCheckpoint] = []
        for ctx in self._all_contexts().values():
            branches.extend(store.list_branches(f"{ctx.conversation_id}:fork:"))
        return branches

    def list_mutations(self) -> list[StateCheckpoint]:
        """
        列出会话全部上下文的检查点变更记录 (最新在前), 含审计字段 source / reason

        返回:
        - list[StateCheckpoint]: 变更记录列表 (按创建时间倒序)
        """
        store = self._require_session_store()
        mutations: list[StateCheckpoint] = []
        for ctx in self._all_contexts().values():
            mutations.extend(store.list_mutations(ctx._scope()))
        mutations.sort(key=lambda cp: cp.created_at, reverse=True)
        return mutations

    def _resolve_batch_id(self, store: StateStore, checkpoint_id: str) -> tuple[bool, str]:
        """
        把检查点 ID 或批次 ID 解析为 (是否批次, 目标 ID) (用于 rollback / fork)

        参数:
        - store: 存储实例
        - checkpoint_id: 检查点 ID

        单检查点 (无批次) 时返回 (False, 检查点 ID)

        返回:
        - tuple[bool, str]: 把检查点 ID 或批次 ID 解析为 (是否批次, 目标 ID) (用于 rollback / fork)
        """
        cp = store.get_checkpoint(checkpoint_id)
        if cp is not None:
            return (bool(cp.batch_id), cp.batch_id or cp.checkpoint_id)
        if store.list_checkpoints_by_batch(checkpoint_id):
            return (True, checkpoint_id)
        raise ValueError(f"检查点不存在: {checkpoint_id}")

    def fork(
        self,
        branch_name: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, ContextManager]:
        """
        从指定检查点 (默认最近一个) fork 会话下全部上下文

        参数:
        - branch_name: 分支名称, 新上下文 ID 形如 "{原ID}:fork:{分支名}"
        - checkpoint_id: 源检查点 ID, 默认最近一个批次

        返回:
        - dict[str, ContextManager]: {上下文名: 新上下文管理器} ("session" 与会话内各工作流)

        异常:
        - ValueError: 会话没有检查点, 或指定检查点不存在
        """
        store = self._require_session_store()
        checkpoints = self.list_checkpoints()
        if not checkpoints:
            raise ValueError("当前会话没有检查点, 请先创建检查点")
        if checkpoint_id is not None:
            source = next((cp for cp in checkpoints if cp.checkpoint_id == checkpoint_id), None)
            if source is None:
                _, batch_id = self._resolve_batch_id(store, checkpoint_id)
                source = next((cp for cp in checkpoints if cp.batch_id == batch_id), None)

            if source is None:
                raise ValueError(f"检查点不存在: {checkpoint_id}")
        else:
            source = checkpoints[-1]

        batch_id = source.batch_id or source.checkpoint_id
        batch = store.list_checkpoints_by_batch(batch_id)
        by_scope = {cp.scope_id: cp for cp in (batch if batch else [source])}
        new_ctxs: dict[str, ContextManager] = {}

        for ctx_name, ctx in self._all_contexts().items():
            cp = by_scope.get(ctx.conversation_id)
            if cp is None:
                logger.warning(f"[会话] 上下文 {ctx_name} 无对应检查点, fork 跳过")
                continue

            new_ctxs[ctx_name] = ctx.fork(branch_name, checkpoint_id=cp.checkpoint_id)

        return new_ctxs
    
    def clear_memory(self):
        """清除会话内存"""
        try:
            contexts = self._all_contexts()
            for context in contexts.values():
                context.del_context()

            tracked_ids = {context.conversation_id for context in contexts.values()}
            for workflow_id in self.wf_list:
                if workflow_id in tracked_ids:
                    continue
                workflow_context = ContextManager(
                    workflow_id,
                    db_path=self.session_ctx.db_path,
                )
                try:
                    workflow_context.del_context()
                finally:
                    workflow_context.close()

            logger.info("[会话管理器] 清除工作流上下文完成")

        except Exception as e:
            logger.error(f"[会话管理器] 清除会话上下文错误: {e}")  

    def cmd_process(self, msg: str) -> tuple[Any, bool]:
        """
        处理命令字符串

        参数:
        - msg: 输入消息

        返回:
        - (Any, bool): 命令执行结果和是否为命令消息的元组
        """
        return self.command_handler.process_message(msg)              

    def register_command(self, name: str, handler: Callable[..., Any], intro: str = "None"):
        """
        注册命令处理函数

        参数:
        - name: 名称
        - handler: 处理器
        - intro: 简介文本
        """
        self.command_handler.register_command(name, handler, intro)

    def __call__(self, *input: Any, **kwargs: Any):
        result = self.run(*input, **kwargs)
        return result


class AsyncModelWorkflowFramework:
    """异步版模型工作流框架"""
    def __init_subclass__(cls: type, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        forward = cls.__dict__.get("forward")
        if forward is not None and inspect.iscoroutinefunction(forward):
            async def _wrapped_forward(self: Any, *args: Any, **kw: Any):
                await self.initialize()
                return await forward(self, *args, **kw)
            cls.forward = _wrapped_forward

    def __init__(
        self,
        llm: AsyncLLM,
        context_id: str,
        tools_manager: AsyncToolsManager | None = None,
        system_prompt: str | None = None,
        content_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        return_thinking: bool = False,
        thinking_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        *, db_path: str = get_db_path(),
    ):
        """
        异步模型工作流框架, 负责管理异步模型调用和工作流执行
        这是 Satrap 的核心组件之一, 用于协调单个模型实例调用, 以实现复杂任务处理和自动化流程

        任何异步工作流都应该继承该类, 并实现自己的异步工作流逻辑, 以便被调度和执行;
        工作流应重写 `async forward` 方法, 并在其中完成模型调用与流程控制

        子类初始化时应调用:
        `super().__init__(llm, context_id, tools_manager, system_prompt, content_callback)`

        由于使用异步上下文管理器, 实例创建后需要先初始化上下文:
        - 推荐使用 `await AsyncModelWorkflowFramework.create(...)`
        - 或在调用前显式执行 `await self.initialize()`

        如果设置了 `content_callback`, 可通过 `self._content_content(content)` 在模型调用过程中回传增量内容

        参数:
        - llm: 异步模型实例 (`AsyncLLM`)
        - context_id: 上下文 ID
        - tools_manager: 工具管理器实例
        - system_prompt: 系统提示词; 若提供, 会在初始化时重置上下文中的系统提示
        - content_callback: 内容回调函数; 用于在复杂调用流程中回传模型内容
        - return_thinking: 是否回传模型思考内容
        - thinking_callback: 思考内容回调函数; 未设置时复用 content_callback
        - db_path: 上下文数据库路径
        """
        self.llm = llm
        self.ctx = AsyncContextManager(context_id, db_path=db_path)
        self.tools_manager = tools_manager if tools_manager else AsyncToolsManager()
        # 如果未提供工具管理器, 则创建一个空的工具管理器实例

        self.content_callback = content_callback
        self.return_thinking = return_thinking
        self.thinking_callback = thinking_callback
        self.system_prompt = system_prompt
        self._initialized = False
        self._context_turn_stats = ModelContextTurnStats()

    def reset_context_stats(self) -> None:
        """清空当前轮次的模型上下文统计"""
        self._context_turn_stats = ModelContextTurnStats()

    def get_context_stats(self) -> dict[str, Any] | None:
        """
        返回当前轮次的模型上下文统计

        返回:
        - dict[str, Any] | None: 没有模型请求时为 None
        """
        payload = self._context_turn_stats.to_dict()
        return payload or None

    def _record_context_request(
        self,
        prepared: PreparedModelContext,
        usage: TokenUsage | None,
    ) -> None:
        """
        记录一次正式模型请求

        参数:
        - prepared: 上下文准备结果
        - usage: API 返回的真实 token 用量
        """
        self._context_turn_stats.requests.append(_build_context_request_stats(prepared, usage))

    async def initialize(self):
        """初始化异步上下文与系统提示词"""
        if self._initialized:
            return
        await self.ctx.initialize()
        if self.system_prompt:
            await self.ctx.reset_system_prompt(self.system_prompt)
        self._initialized = True

    @classmethod
    async def create(cls: type[_WorkflowT], *args: Any, **kwargs: Any) -> _WorkflowT:
        """
        创建并初始化实例

        参数:
        - args: 额外位置参数
        - kwargs: 额外关键字参数

        返回:
        - _WorkflowT: 创建并初始化实例
        """
        instance = cls(*args, **kwargs)
        await cast(AsyncModelWorkflowFramework, instance).initialize()
        return instance

    async def _content_callback(self, content: str):
        """
        调用回调返回模型回复内容

        参数:
        - content: 内容
        """
        if self.content_callback and content:
            await self.content_callback(content)

    async def _call_model(
        self,
        *,
        pending_messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking: str = "off",
        img_urls: list[str] | None = None,
    ) -> LLMCallResponse | Literal[False]:
        """准备上下文, 异步调用模型并记录真实 usage"""
        prepared = await self.ctx.prepare_model_context(
            llm=self.llm,
            pending_messages=pending_messages,
            tools=tools,
            img_urls=img_urls,
        )
        call_kwargs: dict[str, Any] = {"tools": tools, "img_urls": img_urls}
        if thinking != "off":
            call_kwargs["thinking"] = thinking
        response = await self.llm.call(prepared.messages, **call_kwargs)
        if isinstance(response, LLMCallResponse):
            self._record_context_request(prepared, response.usage)
            await self.ctx.record_model_usage(prepared, response.usage)
        return response

    @staticmethod
    async def _await_if_needed(value: Awaitable[_WorkflowT] | _WorkflowT) -> _WorkflowT:
        if inspect.isawaitable(value):
            return await cast(Awaitable[_WorkflowT], value)
        return value

    async def agent_executor(self, model_response: LLMCallResponse,
        callback: bool = False, max_iterations: int = 10,
        img_urls: list[str] | None = None) -> tuple[list[dict[str, str | list[Any]]], bool]:
        """
        异步智能体执行器, 用于执行智能体调用流程

        参数:
        - model_response: 模型调用响应
        - callback: 是否回调回复, 默认关闭
        - max_iterations: 最大迭代次数
        - img_urls: 随请求发送的图片 URL 列表

        返回:
        - list[dict[str, str | list[Any]]]: 上下文消息列表
        - bool: 是否成功执行
        """
        try:
            now_iteration = 0
            now_response = model_response
            turn_messages: list[dict[str, Any]] = []

            while now_response.type == "tools_call" and now_response.tool_calls and now_iteration < max_iterations:
                now_iteration += 1
                tool_messages: list[dict[str, Any]] = []
                tool_results: list[dict[str, Any]] = []

                if callback:   # 回调回复
                    if now_response.thinking and self.content_callback:
                        await self._content_callback(f"<think>\n{now_response.thinking}\n</think>")
                    if now_response.content and self.content_callback:
                        await self._content_callback(now_response.content)

                for tool_call in now_response.tool_calls:
                    result = await self.tools_manager.execute_tool_call(tool_call)
                    tool_message, tool_result = result
                    tool_messages.append(tool_message)
                    tool_results.append(tool_result)
                    # 执行工具调用并获取结果

                add_tools_call_flow(turn_messages, now_response.content, tool_messages, tool_results, now_response.thinking)
                # 添加至本轮消息流

                new_response = await self._call_model(
                    pending_messages=turn_messages,
                    tools=self.tools_manager.get_tools_definitions(),
                    img_urls=img_urls,
                )   # 调用模型

                if not new_response:   # 模型调用失败, 无响应返回
                    clear_reasoning_content(turn_messages)
                    await self.ctx.add_turn_messages(turn_messages)
                    logger.error("模型调用失败, 无响应返回")
                    break

                if now_iteration >= max_iterations:   # 达到最大迭代次数
                    if callback:   # 回调回复
                        if now_response.thinking and self.content_callback:
                            await self._content_callback(f"<think>\n{now_response.thinking}\n</think>")
                        if now_response.content and self.content_callback:
                            await self._content_callback(now_response.content)

                    clear_reasoning_content(turn_messages)
                    await self.ctx.add_turn_messages(turn_messages)
                    logger.warning("已达到最大工具调用迭代次数, 停止执行")
                    await self.ctx.add_user_message("已达到最大工具调用尝试次数，请基于已有信息给出最终答案。")
                    final_response = await self._call_model(tools=[], img_urls=img_urls)

                    if not final_response:
                        logger.error("达到最大迭代次数后调用 LLM 生成最终答案失败")
                        return self.ctx.get_context(), False

                    await self.ctx.add_bot_message(final_response.content)
                    if callback:
                        await self._content_callback(final_response.content)

                    return self.ctx.get_context(), True

                now_response = new_response   # 更新当前响应

            else:   # 模型直接返回最终答案
                if callback:   # 回调回复
                    if now_response.thinking and self.content_callback:
                        await self._content_callback(f"<think>\n{now_response.thinking}\n</think>")
                    if now_response.content and self.content_callback:
                        await self._content_callback(now_response.content)

                add_bot_message(turn_messages, now_response.content)
                clear_reasoning_content(turn_messages)
                await self.ctx.add_turn_messages(turn_messages)

            return self.ctx.get_context(), True
    
        except Exception as e:
            logger.error(f"智能体执行器错误: {e}")
            return self.ctx.get_context(), False

    @staticmethod
    def final_response(messages: LLMCallResponse | bool) -> str:
        """
        获取最终回复

        参数:
        - messages: 消息列表

        返回:
        - str: 最终回复
        """
        if isinstance(messages, bool):
            return "模型调用失败, 请查看日志以获得更多信息"
        return messages.content
    
    @staticmethod
    def get_bot_message(messages: list[dict[str, str | list[Any]]]) -> str:
        """
        获取最后一条 assistant 回复

        参数:
        - messages: 消息列表

        返回:
        - str: 最后一条 assistant 回复
        """
        for message in reversed(messages):
            if message["role"] == "assistant":
                return str(message["content"])
        return ""

    @staticmethod
    def _get_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        提取系统消息副本

        参数:
        - messages: 消息列表

        返回:
        - list[dict[str, Any]]: 提取系统消息副本
        """
        return [copy.deepcopy(message) for message in messages if message.get("role") == "system"]

    async def _restore_context_keep_system(self, system_messages: list[dict[str, Any]]):
        """
        恢复上下文为系统消息

        参数:
        - system_messages: system消息列表
        """
        self.ctx._messages = copy.deepcopy(system_messages)
        self.ctx._mark_dirty()
        await self.ctx._sync()

    async def full_agent(self, user_input: str, callback: bool = True, max_iterations: int = 10,
        img_urls: list[str] | None = None) -> str:
        """
        完整执行一轮异步 Agent 流程, 返回最终模型输出

        参数:
        - user_input: 用户输入
        - callback: 回调函数
        - max_iterations: 最大迭代次数
        - img_urls: 图片 URL 列表

        返回:
        - str: 完整执行一轮异步 Agent 流程, 返回最终模型输出
        """
        self.reset_context_stats()
        await self.ctx.add_user_message(user_input)

        response = await self._call_model(
            tools=self.tools_manager.get_tools_definitions(),
            img_urls=img_urls,
        )
        if not response:
            return "模型调用失败"

        context, success = await self.agent_executor(
            response, callback=callback, max_iterations=max_iterations, img_urls=img_urls,
        )
        if not success:
            return "执行失败"

        return self.get_bot_message(context)

    async def _stream_call_response(
        self,
        pending_messages: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        callback: bool,
        thinking: str,
        img_urls: list[str] | None = None,
    ) -> LLMCallResponse | bool:
        """
        消费一次异步流式请求并返回完整响应

        参数:
        - pending_messages: 尚未写入完整历史的本轮临时消息
        - tools: 可选参数, 工具定义列表, 用于 Function Calling
        - callback: 是否回调回复, 默认关闭
        - thinking: 是否要求模型进行思考, 默认为 False
        - img_urls: 随请求发送的图片 URL 列表

        返回:
        - LLMCallResponse | bool: 模型调用响应, 或 False 表示失败
        """
        prepared = await self.ctx.prepare_model_context(
            llm=self.llm,
            pending_messages=pending_messages,
            tools=tools,
            img_urls=img_urls,
        )
        response: Any = None
        async for event in self.llm.stream_call(prepared.messages, tools=tools, thinking=thinking, img_urls=img_urls):
            if callback and event.kind == "content_delta":
                await self._content_callback(event.delta)

            elif callback and event.kind == "thinking_delta" and self.return_thinking:
                if self.thinking_callback:
                    await self.thinking_callback(event.delta)
                else:
                    await self._content_callback(event.delta)

            elif event.kind == "error" and event.error:
                logger.error(f"异步流式 LLM 调用失败: {event.error}")

            elif event.kind == "done":
                response = event.response

        if isinstance(response, (LLMCallResponse, bool)):
            if isinstance(response, LLMCallResponse):
                self._record_context_request(prepared, response.usage)
                await self.ctx.record_model_usage(prepared, response.usage)
            return response
        return False

    async def stream_full_agent(
        self,
        user_input: str,
        callback: bool = True,
        max_iterations: int = 10,
        thinking: str = "off",
        img_urls: list[str] | None = None,
    ) -> str:
        """
        异步流式执行一轮 Agent 流程并返回最终模型输出

        参数:
        - user_input: 用户输入
        - callback: 是否回调回复, 默认关闭
        - max_iterations: 最大迭代次数, 默认 10
        - thinking: 是否要求模型进行思考, 默认为 False
        - img_urls: 随请求发送的图片 URL 列表

        返回:
        - 最终模型输出
        """
        self.reset_context_stats()
        await self.ctx.add_user_message(user_input)
        max_iterations = max(1, max_iterations)

        response = await self._stream_call_response(
            None,
            self.tools_manager.get_tools_definitions(),
            callback,
            thinking,
            img_urls=img_urls,
        )
        if not isinstance(response, LLMCallResponse):
            return "模型调用失败"

        now_response = response
        now_iteration = 0
        turn_messages: list[dict[str, Any]] = []

        while now_response.type == "tools_call" and now_response.tool_calls and now_iteration < max_iterations:
            now_iteration += 1
            tool_messages: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []

            for tool_call in now_response.tool_calls:
                tool_message, tool_result = await self.tools_manager.execute_tool_call(tool_call)
                tool_messages.append(tool_message)
                tool_results.append(tool_result)

            add_tools_call_flow(
                turn_messages,
                now_response.content,
                tool_messages,
                tool_results,
                now_response.thinking,
            )

            new_response = await self._stream_call_response(
                turn_messages,
                self.tools_manager.get_tools_definitions(),
                callback,
                thinking,
                img_urls=img_urls,
            )

            if not isinstance(new_response, LLMCallResponse):
                clear_reasoning_content(turn_messages)
                await self.ctx.add_turn_messages(turn_messages)
                return "执行失败"

            if now_iteration >= max_iterations:
                clear_reasoning_content(turn_messages)
                await self.ctx.add_turn_messages(turn_messages)
                await self.ctx.add_user_message("已达到最大工具调用尝试次数，请基于已有信息给出最终答案。")
                final_response = await self._stream_call_response(
                    None,
                    [],
                    callback,
                    thinking,
                    img_urls=img_urls,
                )
                if not isinstance(final_response, LLMCallResponse):
                    return "执行失败"
                await self.ctx.add_bot_message(final_response.content)
                return final_response.content

            now_response = new_response

        add_bot_message(turn_messages, now_response.content, reasoning=now_response.thinking)
        clear_reasoning_content(turn_messages)
        await self.ctx.add_turn_messages(turn_messages)
        return self.get_bot_message(self.ctx.get_context())

    async def tools_agent(self, user_input: str, callback: bool = True, max_iterations: int = 10,
        img_urls: list[str] | None = None) -> str:
        """
        使用临时上下文完整执行一轮异步 Agent 流程, 返回最终模型输出

        参数:
        - user_input: 用户输入
        - callback: 回调函数
        - max_iterations: 最大迭代次数
        - img_urls: 图片 URL 列表

        返回:
        - str: 使用临时上下文完整执行一轮异步 Agent 流程, 返回最终模型输出
        """
        system_messages = self._get_system_messages(self.ctx.get_context())
        self.reset_context_stats()
        await self._restore_context_keep_system(system_messages)

        try:
            await self.ctx.add_user_message(user_input)

            response = await self._call_model(
                tools=self.tools_manager.get_tools_definitions(),
                img_urls=img_urls,
            )
            if not response:
                return "模型调用失败"

            context, success = await self.agent_executor(
                response, callback=callback, max_iterations=max_iterations, img_urls=img_urls,
            )
            if not success:
                return "执行失败"

            return self.get_bot_message(context)
        finally:
            await self._restore_context_keep_system(system_messages)

    async def stream_tools_agent(
        self,
        user_input: str,
        callback: bool = True,
        max_iterations: int = 10,
        thinking: str = "off",
        img_urls: list[str] | None = None,
    ) -> str:
        """
        使用临时上下文异步流式执行一轮 Agent 流程, 返回最终模型输出

        参数:
        - user_input: 用户输入
        - callback: 是否回调回复, 默认开启
        - max_iterations: 最大迭代次数, 默认 10
        - thinking: 是否要求模型进行思考, 默认为 False
        - img_urls: 随请求发送的图片 URL 列表

        返回:
        - 最终模型输出
        """
        system_messages = self._get_system_messages(self.ctx.get_context())
        await self._restore_context_keep_system(system_messages)

        try:
            return await self.stream_full_agent(
                user_input,
                callback=callback,
                max_iterations=max_iterations,
                thinking=thinking,
                img_urls=img_urls,
            )
        finally:
            await self._restore_context_keep_system(system_messages)

    def reset_llm(self, llm: AsyncLLM):
        """
        重置会话模型

        参数:
        - llm: 模型实例
        """
        self.llm = llm

    async def forward(self, *input: Any, **kwargs: Any) -> Any:
        """
        执行工作流

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行工作流
        """
        return None

    async def __call__(self, *input: Any, **kwargs: Any):
        await self.initialize()
        result = await self.forward(*input, **kwargs)
        return result


class AsyncSession:
    """异步版会话类"""
    def __init_subclass__(cls: type, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        run = cls.__dict__.get("run")
        if run is not None and inspect.iscoroutinefunction(run):
            async def _wrapped_run(self: Any, *args: Any, **kw: Any):
                await self._ensure_initialized()
                return await run(self, *args, **kw)
            cls.run = _wrapped_run

    plugin_override_store: "SessionOverrideStore | None" = None
    plugin_model_manager: "ModelConfigManager | None" = None
    storage_layout: "StorageLayout | None" = None
    storage_platform_id: str | None = None

    def __init__(self, session_id: str,
        content_callback: Optional[Callable[[str], Awaitable[None]]] | None = None,
        command_handler: Optional[AsyncCommandHandler] | None = None, *,
        db_path: str = get_db_path(),
        state_store: Optional[StateStore] = None, enable_checkpoint: bool = False
    ):
        """
        异步会话框架, 用于管理多个异步模型工作流协作的会话
        任何依赖多模型的复杂异步 Agent 都应继承该类, 并实现 `async run` 方法

        子类初始化时应调用:
        `super().__init__(session_id, content_callback)`

        ``` python
        class MySession(AsyncSession):
            def __init__(self, session_id: str,
                content_callback: Optional[Callable[[str], Awaitable[None]]] | None = None,
                command_handler: Optional[AsyncCommandHandler] | None = None
            ):
                super().__init__(session_id, content_callback, command_handler)

            async def _async_init(self):
                self.workflow = await MyWorkflow.create(...)
                # 异步初始化钩子, 用于创建工作流等

            async def run(self, user_input: str) -> str:
                return await self.workflow.forward(user_input)

        # 直接使用, 无需 create 或 initialize
        session = MySession("user_123", content_callback=print)
        reply = await session.run("你好")
        ```

        参数:
        - session_id: 会话 ID
        - content_callback: 内容回调函数, 用于在复杂调用流程中回传模型内容
        - command_handler: 命令处理器实例, 用于处理用户输入的命令
        - db_path: 会话上下文数据库路径 (与检查点存储同库)
        - state_store: 状态检查点存储实例, 传入后启用会话级检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向 db_path 的 StateStore, 与显式传入 state_store 二选一
        """
        self._state_store = state_store
        if self._state_store is None and enable_checkpoint:
            self._state_store = StateStore(db_path=db_path)
        self._workflow_contexts: dict[str, AsyncContextManager] = {}
        """工作流 ID -> 工作流上下文, 供会话级检查点聚合"""
        self._context_config: LLMConfig | None = None
        """当前会话使用的模型上下文配置"""

        self.session_ctx = AsyncContextManager(session_id, db_path=db_path)
        self.session_id = session_id
        self.wf_list: list[str] = []
        self.content_callback = content_callback
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._user_manager: UserManager | None = None

        if self._state_store is not None:
            self._state_store.register_domain(_messages_domain())
            self.session_ctx.state_store = self._state_store

        self.command_handler = command_handler if command_handler else AsyncCommandHandler()
        # 如果未提供命令处理器, 则创建一个空的命令处理器实例

    async def _ensure_initialized(self) -> None:
        """确保异步初始化完成 (幂等)"""
        await self.initialize()

    async def initialize(self) -> None:
        """执行实际初始化, 可被子类重写, 但需调用 super().initialize()"""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self.session_ctx.initialize()
            await self._async_init()   # 钩子: 子类可在此创建工作流等
            self._initialized = True

    async def _async_init(self):
        """子类可重写的异步初始化钩子"""
        pass

    async def _content_callback(self, content: str):
        """
        调用回调返回模型回复内容

        参数:
        - content: 内容
        """
        if self.content_callback and content:
            await self.content_callback(content)

    @property
    def user_contexts(self) -> list[str]:
        """
        获取当前用户的所有上下文 session_id 列表

        返回:
        - list[str]: 当前用户的所有上下文 session_id 列表
        """
        if not self._user_manager:
            return []
        user_id = Session._parse_user_id(self.session_id)
        if not user_id:
            return []
        return self._user_manager.get_user_session_ids(user_id)

    async def on_session_switched(self, old_session_id: str, new_session_id: str) -> None:
        """
        上下文切换后调用, 子类可重写以刷新工作流

        参数:
        - old_session_id: old会话ID
        - new_session_id: new会话ID

        返回:
        - None: 上下文切换后调用, 子类可重写以刷新工作流
        """
        return None

    def reload_llm(self, llm: AsyncLLM):
        """
        重载 LLM 实例, 子类可重写以更新工作流内的 LLM 引用

        参数:
        - llm: 模型实例
        """

    async def run(self, *input: Any, **kwargs: Any) -> Any:
        """
        执行会话

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行会话
        """
        return None

    def workflow_id_assign(self, wf_id: str) -> str:
        """
        为会话分配工作流 ID

        重复的 wf_id 自动追加序号 (如 main -> main_2) 并记录警告, 避免工作流上下文互相污染

        参数:
        - wf_id: 工作流 ID

        返回:
        - 工作流 ID (str) (session_id + "_" + wf_id)
        """
        workflow_id = self.session_id + "_" + wf_id
        if workflow_id in self.wf_list:
            logger.warning(f"[会话] 工作流 ID 重复, 自动追加序号: {wf_id} -> {wf_id}_2")
            suffix = 2
            while f"{self.session_id}_{wf_id}_{suffix}" in self.wf_list:
                suffix += 1
            workflow_id = f"{self.session_id}_{wf_id}_{suffix}"
        self.wf_list.append(workflow_id)
        return workflow_id

    def _require_session_store(self) -> StateStore:
        """
        获取会话级状态存储, 未启用时抛出 ValueError

        返回:
        - StateStore: 会话级状态存储, 未启用时抛出 ValueError
        """
        if self._state_store is None:
            raise ValueError("未启用会话级状态检查点, 请传入 state_store 或设置 enable_checkpoint=True")
        return self._state_store

    def _all_contexts(self) -> dict[str, AsyncContextManager]:
        """
        返回 {上下文名: AsyncContextManager}, 含会话共享上下文与全部工作流上下文

        返回:
        - dict[str, AsyncContextManager]:  {上下文名: AsyncContextManager}, 含会话共享上下文与全部工作流上下文
        """
        contexts: dict[str, AsyncContextManager] = {"session": self.session_ctx}
        contexts.update(self._workflow_contexts)
        return contexts

    def apply_context_config(self, config: LLMConfig) -> None:
        """
        将模型上下文配置应用到会话及全部工作流上下文

        参数:
        - config: LLM 配置
        """
        self._context_config = config
        for context in self._all_contexts().values():
            apply_context_policy(context, config)

    def _track_workflow_context(self, wf_id: str, ctx: AsyncContextManager) -> None:
        """
        注册工作流上下文, 使其纳入会话级检查点聚合

        参数:
        - wf_id: 工作流 ID (workflow_id_assign 的返回值)
        - ctx: 工作流的 AsyncContextManager 实例
        """
        if wf_id in self._workflow_contexts:
            logger.warning(f"[会话] 工作流 {wf_id} 已注册, 将被覆盖")
        self._workflow_contexts[wf_id] = ctx
        if self._context_config is not None:
            apply_context_policy(ctx, self._context_config)
        if self._state_store is not None:
            if Path(str(ctx.db_path)).resolve() != Path(str(self._state_store.db_path)).resolve():
                raise ValueError(
                    f"工作流 {wf_id} 的上下文库 {ctx.db_path} 与会话状态库 {self._state_store.db_path} 不一致"
                )
            if ctx.state_store is None:
                ctx.state_store = self._state_store
                self._state_store.register_domain(_messages_domain())

    async def create_checkpoint(self, name: str = "", description: str = "") -> str:
        """
        为会话创建聚合检查点 (会话共享上下文 + 全部工作流上下文), 返回批次 ID

        参数:
        - name: 检查点显示名称
        - description: 检查点说明

        返回:
        - str: 批次 ID, 用于 list_checkpoints / rollback
        """
        self._require_session_store()
        batch_id = f"batch-{uuid.uuid4().hex[:16]}"
        try:
            with state_mutation_context(
                source="session_checkpoint", reason=f"创建会话检查点 {name or batch_id}"
            ):
                for ctx_name, ctx in self._all_contexts().items():
                    await ctx.create_checkpoint(
                        name=f"{name}[{ctx_name}]" if name else ctx_name,
                        description=description,
                        batch_id=batch_id,
                    )
        except Exception:
            store = self._require_session_store()
            # 补偿: 删除已创建的残批检查点, 避免部分作用域回滚的不一致状态
            for ctx in self._all_contexts().values():
                for cp in await asyncio.to_thread(store.list_checkpoints, ctx._scope()):
                    if cp.batch_id == batch_id:
                        await asyncio.to_thread(store.delete_checkpoint, cp.checkpoint_id)
            raise
        return batch_id

    async def list_checkpoints(self) -> list[StateCheckpoint]:
        """
        列出会话的全部聚合检查点 (按批次去重, 时间升序)

        返回:
        - list[StateCheckpoint]: 检查点列表, 每个批次一个代表检查点
        """
        store = self._require_session_store()
        seen: dict[str, StateCheckpoint] = {}
        for ctx in self._all_contexts().values():
            for cp in await asyncio.to_thread(store.list_checkpoints, ctx._scope()):
                if cp.batch_id:
                    seen.setdefault(cp.batch_id, cp)
                else:
                    seen.setdefault(cp.checkpoint_id, cp)
        return list(seen.values())

    async def rollback(self, checkpoint_id: str) -> None:
        """
        回滚会话到指定检查点批次并重载全部上下文

        参数:
        - checkpoint_id: 批次 ID 或批次内任一检查点的 ID
        """
        store = self._require_session_store()
        is_batch, target = self._resolve_batch_id(store, checkpoint_id)
        with state_mutation_context(
            source="session_checkpoint_rollback", reason=f"回滚到检查点 {checkpoint_id}"
        ):
            if is_batch:
                await asyncio.to_thread(store.rollback_batch, target)
            else:
                await asyncio.to_thread(store.rollback, target)   # 单检查点 (非聚合)
        for ctx in self._all_contexts().values():
            await ctx.load_context()

    async def retry(self, checkpoint_id: str) -> None:
        """
        从指定检查点批次重试并重载全部上下文 (保留未来检查点)

        参数:
        - checkpoint_id: 批次 ID 或批次内任一检查点的 ID
        """
        store = self._require_session_store()
        is_batch, target = self._resolve_batch_id(store, checkpoint_id)
        with state_mutation_context(
            source="session_checkpoint_retry", reason=f"重试到检查点 {checkpoint_id}"
        ):
            if is_batch:
                await asyncio.to_thread(store.retry_batch, target)
            else:
                await asyncio.to_thread(store.retry, target)   # 单检查点 (非聚合)
        for ctx in self._all_contexts().values():
            await ctx.load_context()

    async def list_branches(self) -> list[StateCheckpoint]:
        """
        列出从本会话 fork 出的全部分支起点检查点 (会话共享 + 各工作流)

        返回:
        - list[StateCheckpoint]: 分支检查点列表 (按时间升序)
        """
        store = self._require_session_store()
        branches: list[StateCheckpoint] = []
        for ctx in self._all_contexts().values():
            branches.extend(await asyncio.to_thread(store.list_branches, f"{ctx.conversation_id}:fork:"))
        return branches

    async def list_mutations(self) -> list[StateCheckpoint]:
        """
        列出会话全部上下文的检查点变更记录 (最新在前), 含审计字段 source / reason

        返回:
        - list[StateCheckpoint]: 变更记录列表 (按创建时间倒序)
        """
        store = self._require_session_store()
        mutations: list[StateCheckpoint] = []
        for ctx in self._all_contexts().values():
            mutations.extend(await asyncio.to_thread(store.list_mutations, ctx._scope()))
        mutations.sort(key=lambda cp: cp.created_at, reverse=True)
        return mutations

    def _resolve_batch_id(self, store: StateStore, checkpoint_id: str) -> tuple[bool, str]:
        """
        把检查点 ID 或批次 ID 解析为 (是否批次, 目标 ID) (用于 rollback / fork)

        参数:
        - store: 存储实例
        - checkpoint_id: 检查点 ID

        单检查点 (无批次) 时返回 (False, 检查点 ID)

        返回:
        - tuple[bool, str]: 把检查点 ID 或批次 ID 解析为 (是否批次, 目标 ID) (用于 rollback / fork)
        """
        cp = store.get_checkpoint(checkpoint_id)
        if cp is not None:
            return (bool(cp.batch_id), cp.batch_id or cp.checkpoint_id)
        if store.list_checkpoints_by_batch(checkpoint_id):
            return (True, checkpoint_id)
        raise ValueError(f"检查点不存在: {checkpoint_id}")

    async def fork(
        self,
        branch_name: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, AsyncContextManager]:
        """
        从指定检查点 (默认最近一个) fork 会话下全部上下文

        参数:
        - branch_name: 分支名称, 新上下文 ID 形如 "{原ID}:fork:{分支名}"
        - checkpoint_id: 源检查点 ID, 默认最近一个批次

        返回:
        - dict[str, AsyncContextManager]: {上下文名: 新上下文管理器} (已初始化)

        异常:
        - ValueError: 会话没有检查点, 或指定检查点不存在
        """
        store = self._require_session_store()
        checkpoints = await self.list_checkpoints()
        if not checkpoints:
            raise ValueError("当前会话没有检查点, 请先创建检查点")
        if checkpoint_id is not None:
            source = next((cp for cp in checkpoints if cp.checkpoint_id == checkpoint_id), None)
            if source is None:
                _, batch_id = self._resolve_batch_id(store, checkpoint_id)
                source = next((cp for cp in checkpoints if cp.batch_id == batch_id), None)
            if source is None:
                raise ValueError(f"检查点不存在: {checkpoint_id}")
        else:
            source = checkpoints[-1]
        batch_id = source.batch_id or source.checkpoint_id
        batch = await asyncio.to_thread(store.list_checkpoints_by_batch, batch_id)
        by_scope = {cp.scope_id: cp for cp in (batch if batch else [source])}
        new_ctxs: dict[str, AsyncContextManager] = {}
        for ctx_name, ctx in self._all_contexts().items():
            cp = by_scope.get(ctx.conversation_id)
            if cp is None:
                logger.warning(f"[会话] 上下文 {ctx_name} 无对应检查点, fork 跳过")
                continue
            new_ctxs[ctx_name] = await ctx.fork(branch_name, checkpoint_id=cp.checkpoint_id)
        return new_ctxs

    async def clear_memory(self):
        """清除会话内存"""
        try:
            await self.initialize()
            contexts = self._all_contexts()
            for context in contexts.values():
                await context.initialize()
                await context.del_context()
            tracked_ids = {context.conversation_id for context in contexts.values()}
            for workflow_id in self.wf_list:
                if workflow_id in tracked_ids:
                    continue
                workflow_context = AsyncContextManager(
                    workflow_id,
                    db_path=self.session_ctx.db_path,
                )
                await workflow_context.initialize()
                await workflow_context.del_context()
            logger.info("[会话管理器] 清除工作流上下文完成")

        except Exception as e:
            logger.error(f"[会话管理器] 清除会话上下文错误: {e}")

    async def cmd_process(self, msg: str) -> tuple[Any, bool]:
        """
        处理命令字符串

        参数:
        - msg: 输入消息

        返回:
        - (Any, bool): 命令执行结果和是否为命令消息的元组
        """
        return await self.command_handler.process_message(msg)

    def register_command(self, name: str, handler: Callable[..., Any], intro: str = "None"):
        """
        注册命令处理函数

        参数:
        - name: 名称
        - handler: 处理器
        - intro: 简介文本
        """
        self.command_handler.register_command(name, handler, intro)

    async def __call__(self, *input: Any, **kwargs: Any):
        await self._ensure_initialized()
        result = await self.run(*input, **kwargs)
        return result


