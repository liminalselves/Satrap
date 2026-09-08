from __future__ import annotations
import inspect, copy
from typing import Optional, Callable, Any, Awaitable, cast, Literal
from typing import TYPE_CHECKING
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.utils.TCBuilder import AsyncToolsManager
from satrap.core.utils.context import (
    add_bot_message,
    add_tools_call_flow,
    clear_reasoning_content,
)
from satrap.core.utils.paths import get_db_path
from satrap.core.type import LLMCallResponse, ModelContextTurnStats
from satrap.core.log import logger
from .utils import _WorkflowT

if TYPE_CHECKING:
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.storage.layout import StorageLayout
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.UserManager import UserManager

from .base import _WorkflowCore
from .utils import _new_async_context


class AsyncModelWorkflowFramework(_WorkflowCore):
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
        *,
        db_path: str = get_db_path(),
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
        self.ctx = _new_async_context(context_id, db_path=db_path)
        self.tools_manager = tools_manager if tools_manager else AsyncToolsManager()
        # 如果未提供工具管理器, 则创建一个空的工具管理器实例

        self.content_callback = content_callback
        self.return_thinking = return_thinking
        self.thinking_callback = thinking_callback
        self.system_prompt = system_prompt
        self._initialized = False
        self._context_turn_stats = ModelContextTurnStats()

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

    async def agent_executor(
        self,
        model_response: LLMCallResponse,
        callback: bool = False,
        max_iterations: int = 10,
        img_urls: list[str] | None = None,
    ) -> tuple[list[dict[str, str | list[Any]]], bool]:
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

            while (
                now_response.type == "tools_call"
                and now_response.tool_calls
                and now_iteration < max_iterations
            ):
                now_iteration += 1
                tool_messages: list[dict[str, Any]] = []
                tool_results: list[dict[str, Any]] = []

                if callback:  # 回调回复
                    if now_response.thinking and self.content_callback:
                        await self._content_callback(
                            f"<think>\n{now_response.thinking}\n</think>"
                        )
                    if now_response.content and self.content_callback:
                        await self._content_callback(now_response.content)

                for tool_call in now_response.tool_calls:
                    result = await self.tools_manager.execute_tool_call(tool_call)
                    tool_message, tool_result = result
                    tool_messages.append(tool_message)
                    tool_results.append(tool_result)
                    # 执行工具调用并获取结果

                add_tools_call_flow(
                    turn_messages,
                    now_response.content,
                    tool_messages,
                    tool_results,
                    now_response.thinking,
                )
                # 添加至本轮消息流

                new_response = await self._call_model(
                    pending_messages=turn_messages,
                    tools=self.tools_manager.get_tools_definitions(),
                    img_urls=img_urls,
                )  # 调用模型

                if not new_response:  # 模型调用失败, 无响应返回
                    clear_reasoning_content(turn_messages)
                    await self.ctx.add_turn_messages(turn_messages)
                    logger.error("模型调用失败, 无响应返回")
                    break

                if now_iteration >= max_iterations:  # 达到最大迭代次数
                    if callback:  # 回调回复
                        if now_response.thinking and self.content_callback:
                            await self._content_callback(
                                f"<think>\n{now_response.thinking}\n</think>"
                            )
                        if now_response.content and self.content_callback:
                            await self._content_callback(now_response.content)

                    clear_reasoning_content(turn_messages)
                    await self.ctx.add_turn_messages(turn_messages)
                    logger.warning("已达到最大工具调用迭代次数, 停止执行")
                    await self.ctx.add_user_message(
                        "已达到最大工具调用尝试次数，请基于已有信息给出最终答案。"
                    )
                    final_response = await self._call_model(tools=[], img_urls=img_urls)

                    if not final_response:
                        logger.error("达到最大迭代次数后调用 LLM 生成最终答案失败")
                        return self.ctx.get_context(), False

                    await self.ctx.add_bot_message(final_response.content)
                    if callback:
                        await self._content_callback(final_response.content)

                    return self.ctx.get_context(), True

                now_response = new_response  # 更新当前响应

            else:  # 模型直接返回最终答案
                if callback:  # 回调回复
                    if now_response.thinking and self.content_callback:
                        await self._content_callback(
                            f"<think>\n{now_response.thinking}\n</think>"
                        )
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

    async def _restore_context_keep_system(self, system_messages: list[dict[str, Any]]):
        """
        恢复上下文为系统消息

        参数:
        - system_messages: system消息列表
        """
        self.ctx._messages = copy.deepcopy(system_messages)
        self.ctx._mark_dirty()
        await self.ctx._sync()

    async def full_agent(
        self,
        user_input: str,
        callback: bool = True,
        max_iterations: int = 10,
        img_urls: list[str] | None = None,
    ) -> str:
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
            response,
            callback=callback,
            max_iterations=max_iterations,
            img_urls=img_urls,
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
        async for event in self.llm.stream_call(
            prepared.messages, tools=tools, thinking=thinking, img_urls=img_urls
        ):
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

        while (
            now_response.type == "tools_call"
            and now_response.tool_calls
            and now_iteration < max_iterations
        ):
            now_iteration += 1
            tool_messages: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []

            for tool_call in now_response.tool_calls:
                tool_message, tool_result = await self.tools_manager.execute_tool_call(
                    tool_call
                )
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
                await self.ctx.add_user_message(
                    "已达到最大工具调用尝试次数，请基于已有信息给出最终答案。"
                )
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

        add_bot_message(
            turn_messages, now_response.content, reasoning=now_response.thinking
        )
        clear_reasoning_content(turn_messages)
        await self.ctx.add_turn_messages(turn_messages)
        return self.get_bot_message(self.ctx.get_context())

    async def tools_agent(
        self,
        user_input: str,
        callback: bool = True,
        max_iterations: int = 10,
        img_urls: list[str] | None = None,
    ) -> str:
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
                response,
                callback=callback,
                max_iterations=max_iterations,
                img_urls=img_urls,
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
