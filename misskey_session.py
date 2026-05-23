from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from satrap import AsyncLLM, AsyncToolsManager
from satrap.core.framework import AsyncSession
from satrap.core.framework.Base import AsyncModelWorkflowFramework
from satrap.core.utils.context import AsyncContextManager
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

        self.command_handler.register_command("new", self._cmd_new, "开始新对话")
        self.command_handler.register_command("history", self._cmd_history, "查看此用户的所有对话")
        self.command_handler.register_command("switch", self._cmd_switch, "切换至指定对话: /switch <session_id>")
        self.command_handler.register_command("about", self._cmd_about, "关于")

    @property
    def user_contexts(self) -> list[str]:
        """获取当前用户的所有上下文 session_id 列表

        相对于父类按 : 最多分割2次, 这里直接取第3段作为 user_id,
        兼容多对话后缀格式: type:platform:user_id 和 type:platform:user_id:ts
        """
        if not self._user_manager:
            return []
        parts = self.session_id.split(":")
        if len(parts) < 3:
            return []
        user_id = parts[2]
        return self._user_manager.get_user_session_ids(user_id)

    async def _cmd_new(self):
        """开始新的对话（保留当前对话历史）"""
        import time

        # 取原始 base session_id (前3段 type:platform:user_id)，附加时间戳
        # 避免在当前已带时间戳的 id 上继续堆叠
        parts = self.session_id.split(":")
        base_id = ":".join(parts[:3])
        new_id = f"{base_id}:{int(time.time())}"

        # 绑定到用户管理器，使其出现在 /history 列表中
        if self._user_manager:
            parts = self.session_id.split(":")
            if len(parts) >= 3:
                user_id = parts[2]
                self._user_manager.bind_session(user_id, new_id)

        # 切换到新上下文（沙箱路径不跟随切换，所有对话共享同一沙箱）
        self.session_id = new_id
        self.session_ctx = AsyncContextManager(self.session_id)
        await self.session_ctx.initialize()

        self._wf = await MainWF.create(
            llm=self._llm,
            context_id=self.session_id,
            tools_manager=self._wf.tools_manager,   # type: ignore
            system_prompt=self.system_prompt,
            content_callback=self._content_callback,
        )

        return "已开始新对话（旧对话可通过 /history 查看和切换）"

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

        # 更新 session_id（沙箱路径不跟随切换）
        old_sid = self.session_id
        self.session_id = target_id

        # 重建上下文管理器和工作流, 使其使用新上下文的 ContextManager
        self.session_ctx = AsyncContextManager(self.session_id)
        await self.session_ctx.initialize()

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
