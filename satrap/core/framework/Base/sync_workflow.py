from __future__ import annotations
import copy
from typing import Optional, Callable, Any, Literal
from typing import TYPE_CHECKING
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import ToolsManager
from satrap.core.utils.context import (
    add_bot_message,
    add_tools_call_flow,
    clear_reasoning_content,
)
from satrap.core.utils.paths import get_db_path
from satrap.core.type import LLMCallResponse, ModelContextTurnStats
from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.storage.layout import StorageLayout
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.UserManager import UserManager

from .base import _WorkflowCore
from .utils import _new_context


class ModelWorkflowFramework(_WorkflowCore):
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
        *,
        db_path: str = get_db_path(),
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
        self.ctx = _new_context(context_id, db_path=db_path)
        self.tools_manager = tools_manager if tools_manager else ToolsManager()
        # 如果未提供工具管理器, 则创建一个空的工具管理器实例

        self.return_thinking = return_thinking
        self.thinking_callback = thinking_callback

        self.ctx.load_context()
        if system_prompt:
            self.ctx.reset_system_prompt(system_prompt)

        self.content_callback = content_callback
        self._context_turn_stats = ModelContextTurnStats()

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

    def agent_executor(
        self,
        model_response: LLMCallResponse,
        callback: bool = False,
        max_iterations: int = 10,
        img_urls: list[str] | None = None,
    ) -> tuple[list[dict[str, str | list[Any]]], bool]:
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
                        self._content_callback(
                            f"<think>\n{now_response.thinking}\n</think>"
                        )
                    if now_response.content and self.content_callback:
                        self._content_callback(now_response.content)

                for tool_call in now_response.tool_calls:
                    result = self.tools_manager.execute_tool_call(tool_call)
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

                new_response = self._call_model(
                    pending_messages=turn_messages,
                    tools=self.tools_manager.get_tools_definitions(),
                    img_urls=img_urls,
                )  # 调用模型

                if not new_response:  # 模型调用失败, 无响应返回
                    clear_reasoning_content(turn_messages)
                    self.ctx.add_turn_messages(turn_messages)
                    logger.error("模型调用失败, 无响应返回")
                    break

                if now_iteration >= max_iterations:  # 达到最大迭代次数

                    if callback:  # 回调回复
                        if now_response.thinking and self.content_callback:
                            self._content_callback(
                                f"<think>\n{now_response.thinking}\n</think>"
                            )
                        if now_response.content and self.content_callback:
                            self._content_callback(now_response.content)

                    clear_reasoning_content(turn_messages)
                    self.ctx.add_turn_messages(turn_messages)
                    logger.warning("已达到最大工具调用迭代次数, 停止执行")
                    self.ctx.add_user_message(
                        "已达到最大工具调用尝试次数，请基于已有信息给出最终答案。"
                    )
                    final_response = self._call_model(tools=[], img_urls=img_urls)

                    if not final_response:
                        logger.error("达到最大迭代次数后调用 LLM 生成最终答案失败")
                        return self.ctx.get_context(), False

                    self.ctx.add_bot_message(final_response.content)
                    if callback:
                        self._content_callback(final_response.content)

                    return self.ctx.get_context(), True

                now_response = new_response  # 更新当前响应

            else:  # 模型直接返回最终答案
                if callback:  # 回调回复
                    if now_response.thinking and self.content_callback:
                        self._content_callback(
                            f"<think>\n{now_response.thinking}\n</think>"
                        )
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

    def _restore_context_keep_system(self, system_messages: list[dict[str, Any]]):
        """
        恢复上下文为系统消息

        参数:
        - system_messages: system消息列表
        """
        self.ctx._messages = copy.deepcopy(system_messages)
        self.ctx._mark_dirty()
        self.ctx._sync()

    def full_agent(
        self,
        user_input: str,
        callback: bool = True,
        max_iterations: int = 10,
        img_urls: list[str] | None = None,
    ) -> str:
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
            response,
            callback=callback,
            max_iterations=max_iterations,
            img_urls=img_urls,
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
        for event in self.llm.stream_call(
            prepared.messages, tools=tools, thinking=thinking, img_urls=img_urls
        ):
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

        while (
            now_response.type == "tools_call"
            and now_response.tool_calls
            and now_iteration < max_iterations
        ):
            now_iteration += 1
            tool_messages: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []

            for tool_call in now_response.tool_calls:
                tool_message, tool_result = self.tools_manager.execute_tool_call(
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
                self.ctx.add_user_message(
                    "已达到最大工具调用尝试次数，请基于已有信息给出最终答案。"
                )
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

        add_bot_message(
            turn_messages, now_response.content, reasoning=now_response.thinking
        )
        clear_reasoning_content(turn_messages)
        self.ctx.add_turn_messages(turn_messages)
        return self.get_bot_message(self.ctx.get_context())

    def tools_agent(
        self,
        user_input: str,
        callback: bool = True,
        max_iterations: int = 10,
        img_urls: list[str] | None = None,
    ) -> str:
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
                response,
                callback=callback,
                max_iterations=max_iterations,
                img_urls=img_urls,
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
