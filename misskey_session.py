from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from satrap import AsyncLLM, AsyncToolsManager
from satrap.core.framework import AsyncSession
from satrap.core.framework.Base import AsyncModelWorkflowFramework
from satrap.core.utils.sandbox import CodeSandbox
from satrap.expend.sandbox_tools import AsyncCodeSandboxTool
from satrap.expend.search import AsyncSearchTool, AsyncFetchPageTool

if TYPE_CHECKING:
    from satrap.core.framework.UserManager import UserManager


SYSTEM_PROMPT = """
你是一个有用的AI助手。你可以使用代码沙箱执行Python代码，也可以搜索网络信息。

注意: 用户看不见代码沙箱中的内容，在代码沙箱中执行的代码应当直接输出给用户。
"""

class MainWF(AsyncModelWorkflowFramework):
    """主工作流: 多轮工具调用"""

    async def forward(self, user_input: str) -> str:
        await self.ctx.add_user_message(user_input)

        response = await self.llm.call(
            self.ctx.get_context(),
            tools=self.tools_manager.get_tools_definitions(),
        )
        if not response:
            return "模型调用失败"

        context, success = await self.agent_executor(
            response, callback=True, max_iterations=10,
        )
        if not success:
            return "执行失败"

        return self.get_bot_message(context)


class MisskeySession(AsyncSession):
    """Misskey 平台 AI 助手会话

    参数(通过 SessionClassConfigManager.params 配置):
      - system_prompt: 系统提示词
      - sandbox_dir: 沙箱根目录
      - llm 参数由框架自动传入, 不在 params 中配置
    """

    # AsyncSession 中定义为 None, SessionManager 注入后才有值
    _user_manager: UserManager | None

    def __init__(self, session_id: str,
                 llm: AsyncLLM | None = None,
                 system_prompt: str = SYSTEM_PROMPT,
                 sandbox_dir: str = ".satrap/sandbox",
                 content_callback=None, command_handler=None):
        super().__init__(session_id, content_callback=content_callback,
                         command_handler=command_handler)

        self._llm = llm
        self.system_prompt = system_prompt
        # SessionClassConfigManager 模板会给 str 参数生成 "", 兜底为默认值
        sandbox_root = sandbox_dir or ".satrap/sandbox"
        self.user_sandbox_path = str(
            Path(sandbox_root) / session_id.replace(":", "_")
        )
        self._wf = None

    def get_about_text(self) -> str:
        """输出程序信息"""
        return (
            "Satrap AI 助手 v0.1 alpha\n"
            "仍然在测试中, 有 Bug 请反馈\n"
            "基于 Satrap 框架 / Misskey 平台\n"
            "功能: 代码执行 / 网页搜索 / 多轮对话 / 多对话切换"
        )

    async def on_session_switched(self, old_session_id: str, new_session_id: str) -> None:
        """切换上下文后重建工作流, 使模型读写新上下文。"""
        if not self._wf:
            return
        self._wf = await MainWF.create(
            llm=self._llm,
            context_id=new_session_id,
            tools_manager=self._wf.tools_manager,
            system_prompt=self.system_prompt,
            content_callback=self._content_callback,
        )

    async def _async_init(self):
        """异步初始化: 创建工作流并注册工具"""
        if self._llm is None:
            raise RuntimeError("LLM 未配置, 请通过 'satrap session create --llm <name>' 指定")

        tools_mgr = AsyncToolsManager()

        sandbox = CodeSandbox(sandbox_path=self.user_sandbox_path, env=sys.executable)
        tools_mgr.register_tool(AsyncCodeSandboxTool(sandbox=sandbox))
        tools_mgr.register_tool(AsyncSearchTool(timeout=10))
        tools_mgr.register_tool(AsyncFetchPageTool(timeout=10))

        self._wf = await MainWF.create(
            llm=self._llm,
            context_id=self.session_id,
            tools_manager=tools_mgr,
            system_prompt=self.system_prompt,
            content_callback=self._content_callback,
        )

    async def run(self, message: str) -> str:
        """处理用户消息: 命令检查 → 转发给工作流"""
        result, is_cmd = await self.cmd_process(message)
        if is_cmd:
            return result or ""
        if not self._wf:
            return "初始化中，请稍后再试"
        return await self._wf.forward(message)
