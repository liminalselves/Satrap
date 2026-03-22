from satrap.core.utils.TCBuilder import Tool, create_tool_defined, ToolsManager
from satrap.core.utils.context import ContextManager, AsyncContextManager
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.type import LLMCallResponse
from typing import Optional, Callable
import inspect

from satrap.core.log import logger

class ModelWorkflowFramework:
    """模型工作流框架"""
    def __init__(self, llm: LLM, context_id: str, tools_manager: ToolsManager, system_prompt: str | None = None, content_callback: Optional[Callable[[str], None]] | None = None):
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
                msg, has_tool = self.agent_executor(response)
                if has_tool:
                    res = self.llm.call(msg, tools=self.tools_manager.get_tools_definitions())
                    return self.final_response(res)
                return self.final_response(response)

        # 2. 初始化并调用工作流
        workflow = MyWorkflow(llm, "conversation_id", tools_manager, "You are a helper")
        result = workflow("北京今天天气怎么样？")
        print(result)

        # 或者集成进 `Session` 类中, 以实现复杂多模型 Agent 与会话管理
        ```
        """
        self.llm = llm
        self.ctx = ContextManager(context_id)
        self.tools_manager = tools_manager

        self.ctx.load_context()
        if system_prompt:
            self.ctx.reset_system_prompt(system_prompt)

        self.content_callback = content_callback

    def _content_content(self, content: str):
        """调用回调返回模型回复内容"""
        if self.content_callback and content:
            self.content_callback(content)

    def agent_executor(self, model_response: LLMCallResponse) -> tuple[list[dict[str, str | list]], bool]:
        """智能体执行器, 用于执行智能体调用流程

        参数:
        - model_response: 模型调用响应

        返回:
        - 元组(list, bool): 上下文消息列表和是否有工具调用
        """
        try:
            if model_response.type == "tools_call" and model_response.tool_calls is not None:
                tool_messages = []
                tool_results = []

                for tool_call in model_response.tool_calls:   # 处理所有工具调用
                    tool_message, tool_result = self.tools_manager.execute_tool_call(tool_call)
                    tool_messages.append(tool_message)
                    tool_results.append(tool_result)

                self.ctx.add_tool_call_flow(model_response.content, tool_messages, tool_results)
                messages = self.ctx.get_context()
                return messages, True

            else:
                self.ctx.add_bot_message(model_response.content)   # 添加模型回复到上下文
                messages = self.ctx.get_context()   # 获取最新上下文
                return messages, False

        except Exception as e:
            logger.error(f"智能体执行器错误: {e}")
            return [], False

    @staticmethod
    def final_response(messages: LLMCallResponse | bool) -> str:
        """获取最终回复"""
        if isinstance(messages, bool):
            return "模型调用失败, 请查看日志以获得更多信息"
        else:
            return messages.content

    def forward(self):
        """执行工作流; 调用模型并返回结果"""
        return None

    def __call__(self, *input, **kwargs):
        result = self.forward(*input, **kwargs)
        return result

class Session:
    """会话类, 用于管理多个模型工作流的会话"""
    def __init__(self, session_id: str, content_callback: Optional[Callable[[str], None]] | None = None):
        """会话框架, 用于管理多个模型工作流的会话
        任何依赖多模型的复杂 Agent 都应当继承自该类, 并实现 `forward` 方法

        并在初始化时进行 `super().__init__(session_id, content_callback)`

        参数:
        - session_id: 会话 ID
        - content_callback: 内容回调函数, 用于在复杂模型调用过程中抛出模型回复内容; 如果只取最终回复, 则可以设置为 None
        """
        self.session_ctx = ContextManager(session_id)
        """会话共享上下文"""

        self.session_ctx.load_context()
        self.session_id = session_id
        self.wf_list = []

        self.content_callback = content_callback

    def _content_content(self, content: str):
        """调用回调返回模型回复内容"""
        if self.content_callback and content:
            self.content_callback(content)

    def run(self):
        """执行会话; 调用模型并返回结果"""
        return None
    
    def workflow_id_assign(self, wf_id: str):
        """为会话分配工作流 ID

        返回:
        - 工作流 ID (str) (session_id + "_" + wf_id)
        """
        workflow_id = self.session_id + "_" + wf_id
        self.wf_list.append(workflow_id)
        return workflow_id
    
    def clear_memory(self):
        """清除会话内存"""
        try:
            self.session_ctx.del_context()
            for wf_id in self.wf_list:
                wf_ctx = ContextManager(wf_id)
                wf_ctx.del_context()

            logger.info(f"[会话管理器] 清除工作流上下文完成, 工作流ID: {wf_id}")

        except Exception as e:
            logger.error(f"[会话管理器] 清除会话上下文错误: {e}")                

    def __call__(self, *input, **kwargs):
        result = self.run(*input, **kwargs)
        return result


class AsyncModelWorkflowFramework:
    """异步版模型工作流框架"""
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        forward = cls.__dict__.get("forward")
        if forward is not None and inspect.iscoroutinefunction(forward):
            async def _wrapped_forward(self, *args, **kw):
                await self.initialize()
                return await forward(self, *args, **kw)
            cls.forward = _wrapped_forward

    def __init__(
        self,
        llm: AsyncLLM,
        context_id: str,
        tools_manager: ToolsManager,
        system_prompt: str | None = None,
        content_callback: Optional[Callable[[str], None]] | None = None
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
        """
        self.llm = llm
        self.ctx = AsyncContextManager(context_id)
        self.tools_manager = tools_manager
        self.system_prompt = system_prompt
        self.content_callback = content_callback
        self._initialized = False

    async def initialize(self):
        """初始化异步上下文与系统提示词"""
        if self._initialized:
            return
        await self.ctx.initialize()
        if self.system_prompt:
            await self.ctx.reset_system_prompt(self.system_prompt)
        self._initialized = True

    @classmethod
    async def create(cls, *args, **kwargs):
        """创建并初始化实例"""
        instance = cls(*args, **kwargs)
        await instance.initialize()
        return instance

    def _content_content(self, content: str):
        """调用回调返回模型回复内容"""
        if self.content_callback and content:
            self.content_callback(content)

    @staticmethod
    async def _await_if_needed(value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def agent_executor(self, model_response: LLMCallResponse) -> tuple[list[dict[str, str | list]], bool]:
        """异步智能体执行器, 用于执行智能体调用流程"""
        try:
            await self.initialize()
            if model_response.type == "tools_call" and model_response.tool_calls is not None:
                tool_messages = []
                tool_results = []

                for tool_call in model_response.tool_calls:
                    result = self.tools_manager.execute_tool_call(tool_call)
                    tool_message, tool_result = await self._await_if_needed(result)
                    tool_messages.append(tool_message)
                    tool_results.append(tool_result)

                await self.ctx.add_tool_call_flow(model_response.content, tool_messages, tool_results)
                messages = self.ctx.get_context()
                return messages, True

            await self.ctx.add_bot_message(model_response.content)
            messages = self.ctx.get_context()
            return messages, False

        except Exception as e:
            logger.error(f"异步智能体执行器错误: {e}")
            return [], False

    @staticmethod
    def final_response(messages: LLMCallResponse | bool) -> str:
        """获取最终回复"""
        if isinstance(messages, bool):
            return "模型调用失败, 请查看日志以获得更多信息"
        return messages.content

    async def forward(self, *input, **kwargs):
        """执行工作流"""
        return None

    async def __call__(self, *input, **kwargs):
        await self.initialize()
        result = await self.forward(*input, **kwargs)
        return result


class AsyncSession:
    """异步版会话类"""
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        run = cls.__dict__.get("run")
        if run is not None and inspect.iscoroutinefunction(run):
            async def _wrapped_run(self, *args, **kw):
                await self.initialize()
                return await run(self, *args, **kw)
            cls.run = _wrapped_run

    def __init__(self, session_id: str, content_callback: Optional[Callable[[str], None]] | None = None):
        """
        异步会话框架, 用于管理多个异步模型工作流协作的会话
        任何依赖多模型的复杂异步 Agent 都应继承该类, 并实现 `async run` 方法

        子类初始化时应调用:
        `super().__init__(session_id, content_callback)`

        由于使用异步上下文管理器, 实例创建后需要先初始化会话上下文:
        - 推荐使用 `await AsyncSession.create(...)`
        - 或在调用前显式执行 `await self.initialize()`

        参数:
        - session_id: 会话 ID
        - content_callback: 内容回调函数, 用于在复杂调用流程中回传模型内容
        """
        self.session_ctx = AsyncContextManager(session_id)
        self.session_id = session_id
        self.wf_list = []
        self.content_callback = content_callback
        self._initialized = False

    async def initialize(self):
        """初始化异步会话上下文"""
        if self._initialized:
            return
        await self.session_ctx.initialize()
        self._initialized = True

    @classmethod
    async def create(cls, *args, **kwargs):
        """创建并初始化实例"""
        instance = cls(*args, **kwargs)
        await instance.initialize()
        return instance

    def _content_content(self, content: str):
        """调用回调返回模型回复内容"""
        if self.content_callback and content:
            self.content_callback(content)

    async def run(self, *input, **kwargs):
        """执行会话"""
        return None

    def workflow_id_assign(self, wf_id: str):
        """为会话分配工作流 ID"""
        workflow_id = self.session_id + "_" + wf_id
        self.wf_list.append(workflow_id)
        return workflow_id

    async def clear_memory(self):
        """清除会话内存"""
        try:
            await self.initialize()
            await self.session_ctx.del_context()
            for wf_id in self.wf_list:
                wf_ctx = AsyncContextManager(wf_id)
                await wf_ctx.initialize()
                await wf_ctx.del_context()
                logger.info(f"[会话管理器] 清除工作流上下文完成, 工作流ID: {wf_id}")

        except Exception as e:
            logger.error(f"[会话管理器] 清除会话上下文错误: {e}")

    async def __call__(self, *input, **kwargs):
        await self.initialize()
        result = await self.run(*input, **kwargs)
        return result
