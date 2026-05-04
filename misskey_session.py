import sys
from pathlib import Path

from satrap import AsyncLLM, AsyncToolsManager
from satrap.core.framework import AsyncSession
from satrap.core.framework.Base import AsyncModelWorkflowFramework
from satrap.core.utils.sandbox import CodeSandbox
from satrap.expend.sandbox_tools import AsyncCodeSandboxTool
from satrap.expend.search import AsyncSearchTool, AsyncFetchPageTool


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

    def __init__(self, session_id: str,
                 llm: AsyncLLM | None = None,
                 system_prompt: str = "你是一个有用的AI助手。你可以使用代码沙箱执行Python代码，也可以搜索网络信息。",
                 sandbox_dir: str = "./sandbox",
                 content_callback=None, command_handler=None):
        super().__init__(session_id, content_callback=content_callback,
                         command_handler=command_handler)

        self._llm = llm
        self.system_prompt = system_prompt
        self.user_sandbox_path = str(
            Path(sandbox_dir) / session_id.replace(":", "_")
        )
        self._wf = None

        self.command_handler.register_command("new", self._cmd_new, "开始新对话")
        self.command_handler.register_command("history", self._cmd_history, "查看此用户的所有对话")
        self.command_handler.register_command("switch", self._cmd_switch, "切换至指定对话: /switch <session_id>")
        self.command_handler.register_command("about", self._cmd_about, "关于")

    async def _cmd_new(self):
        """清除当前上下文，开始新对话"""
        await self.session_ctx.del_context()
        await self.session_ctx.initialize()
        return "已开始新对话"

    async def _cmd_history(self):
        """列出此用户的所有对话上下文"""
        contexts = self.user_contexts
        if not contexts:
            return "暂无其他对话"

        current = self.session_id
        lines = ["当前用户的所有对话:"]
        for sid in contexts:
            tag = " ← 当前" if sid == current else ""
            lines.append(f"  {sid}{tag}")
        return "\n".join(lines)

    async def _cmd_switch(self, target_id: str = ""):
        """切换至指定对话: /switch <session_id>"""
        if not target_id:
            return "用法: /switch <session_id>\n请通过 /history 查看可用对话的 session_id"

        contexts = self.user_contexts
        if target_id not in contexts:
            return f"对话不存在: {target_id}\n请通过 /history 查看可用对话"

        if target_id == self.session_id:
            return f"已在当前对话: {target_id}"

        # 更新 session_id
        old_sid = self.session_id
        self.session_id = target_id
        self.user_sandbox_path = str(
            Path(self.user_sandbox_path).parent / target_id.replace(":", "_")
        )

        # 重建工作流, 使其使用新上下文的 ContextManager
        self._wf = await MainWF.create(
            llm=self._llm,
            context_id=self.session_id,
            tools_manager=self._wf.tools_manager,   # type: ignore
            system_prompt=self.system_prompt,
            content_callback=self._content_callback,
        )

        return f"已切换至: {target_id}"

    async def _cmd_about(self):
        """输出程序信息"""
        return (
            "Satrap AI 助手 v0.1 alpha\n"
            "仍然在测试中, 有 Bug 请反馈\n"
            "基于 Satrap 框架 / Misskey 平台\n"
            "功能: 代码执行 / 网页搜索 / 多轮对话 / 多对话切换"
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
