"""
同步模型工作流入口

协调模型, 上下文与工具管理器, 提供普通和流式 Agent 调用,
共用固定执行循环并按构造参数选择步骤持久化与任务恢复
"""

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
        recoverable: bool = False,
    ):
        """
        模型工作流框架, 负责管理模型的调用和工作流的执行
        这是 Satrap 的核心组件之一, 负责协调单个模型实例调用, 以实现复杂的任务处理和自动化流程;

        任何工作流都应该继承这个类, 并实现自己的工作流逻辑, 以便被调用和执行;
        工作流应当覆写 `forward` 方法, 并在其中实现模型的调用和工作流的逻辑

        并在初始化进行 `super().__init__(llm, context_id, tools_manager, system_prompt, content_callback)`

        如果设置了 `content_callback`, 则可以使用 `self._content_callback(content)` 方法在模型调用过程中抛出模型回复内容, 以实现及时输出模型回复内容

        参数:
        - llm: 模型实例
        - context_id: 上下文 ID
        - tools_manager: 工具管理器实例
        - system_prompt: 系统提示词; 如果填写, 会重置上下文的系统提示词
        - content_callback: 内容回调函数, 用于在复杂模型调用过程中抛出模型回复内容; 如果只取最终回复, 则可以设置为 None
        - return_thinking: 是否返回模型思考内容; 如果为 True, 则会在模型回复内容前抛出思考内容
        - thinking_callback: 思考内容回调函数; 未设置时复用 content_callback
        - db_path: 上下文与执行记录数据库路径, 默认使用 get_db_path() 返回的路径
        - recoverable: 是否启用可恢复 Agent 执行, 默认 False; 为 True 时将模型与工具步骤持久化到 db_path

        恢复边界:
        - full_agent 和 stream_full_agent 共用执行循环, recoverable 仅控制步骤记录与恢复
        - full_agent 不再调用可覆写的 agent_executor, 自定义编排应在 forward 中显式调用兼容接口
        - 不自动接管自定义 forward 或 tools_agent 的任意逻辑
        - 中断后可按任务 ID 恢复并复用已完成步骤, 工具结果未知时按恢复策略重试或等待人工确认
        - 恢复时会检查会话历史及模型和工具配置, 不匹配时拒绝继续原任务

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
        self.recoverable = recoverable
        self.last_run_id: str | None = None
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
        self, model_response: LLMCallResponse, callback: bool = False,
        max_iterations: int = 10, img_urls: list[str] | None = None,
        *, thinking: str = "off",
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        使用共用循环处理已有模型响应, 保留元组返回接口

        参数:
        - model_response: 已获取的首次模型响应, 用户消息由调用方预先写入上下文
        - callback: 是否回传模型内容, 默认 False
        - max_iterations: 最大工具轮数, 默认 10, 非正数按 1 处理
        - img_urls: 附加图片地址, 默认 None
        - thinking: 后续模型请求的思考强度, 默认 off

        返回:
        - 当前上下文和成功标志, 执行异常返回 False, 取消信号继续向外传播
        """
        from .execution.engine import run_sync

        try:
            run_sync(
                self, user_input=None, initial_response=model_response,
                recoverable=False, callback=callback, max_iterations=max_iterations,
                img_urls=img_urls, thinking=thinking,
            )
            return self.ctx.get_context(), True
        except Exception as error:
            logger.error(f"智能体执行器错误: {error}")
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
        self, user_input: str, callback: bool = True, max_iterations: int = 10,
        img_urls: list[str] | None = None, *, thinking: str = "off",
    ) -> str:
        """
        同步执行完整 Agent 循环, 成功后提交本轮消息

        参数:
        - user_input: 本轮用户输入
        - callback: 是否回传模型内容, 默认 True
        - max_iterations: 最大工具轮数, 默认 10, 非正数按 1 处理
        - img_urls: 附加图片地址, 默认 None, 保留既有位置参数调用
        - thinking: 模型思考强度, 默认 off, 仅可通过关键字传入

        返回:
        - 最终模型回答, 模型或工具执行失败时抛出异常, 不提交半轮消息
        """
        from .execution.engine import run_sync

        return run_sync(
            self, user_input=user_input, callback=callback,
            max_iterations=max_iterations, img_urls=img_urls,
            stream=False, thinking=thinking, recoverable=self.recoverable,
        )

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
        - thinking: 模型思考强度, 默认 off
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
        self, user_input: str, callback: bool = True, max_iterations: int = 10,
        thinking: str = "off", img_urls: list[str] | None = None,
    ) -> str:
        """
        同步流式执行完整 Agent 循环, 成功后提交本轮消息

        参数:
        - user_input: 本轮用户输入
        - callback: 是否回传模型内容, 默认 True
        - max_iterations: 最大工具轮数, 默认 10, 非正数按 1 处理
        - thinking: 模型思考强度, 默认 off
        - img_urls: 附加图片地址, 默认 None

        返回:
        - 最终模型回答, 模型或工具执行失败时抛出异常, 不提交半轮消息
        """
        from .execution.engine import run_sync

        return run_sync(
            self, user_input=user_input, callback=callback,
            max_iterations=max_iterations, img_urls=img_urls,
            stream=True, thinking=thinking, recoverable=self.recoverable,
        )

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
        - thinking: 模型思考强度, 默认 off
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
