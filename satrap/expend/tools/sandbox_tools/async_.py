from collections.abc import Awaitable
import asyncio
from typing import Dict, Any, Optional
from satrap.core.utils.TCBuilder import AsyncTool
from satrap.core.utils.sandbox import CodeSandbox
from satrap.core.log import logger
from .utils import AsyncExecutionAuthorizer
from .utils import prepare_operation, execute_operation


class AsyncCodeSandboxTool(AsyncTool):
    """异步代码沙箱工具, 封装对 CodeSandbox 的各种异步操作"""

    def __init__(
        self,
        sandbox: CodeSandbox,
        execution_authorizer: AsyncExecutionAuthorizer | None = None,
    ):
        """
        初始化 AsyncCodeSandboxTool

        参数:
        - sandbox: 沙箱实例
        - execution_authorizer: 代码执行授权回调, 未配置时拒绝执行
        """
        super().__init__(
            tool_name="code_sandbox",
            description="在代码沙箱中执行代码或管理文件。支持的操作：run（执行代码字符串）、run_file（执行文件）、save（保存代码到文件）、read（读取文件内容）、delete（删除文件）、delete_dir（删除目录）、list（列出文件）。",
            params_dict={
                "operation": (
                    "string",
                    "要执行的操作，可选值：'run', 'run_file', 'save', 'read', 'delete', 'delete_dir', 'list'",
                ),
                "code": ("string", "当操作为'run'或'save'时，需要提供的代码字符串"),
                "path": (
                    "string",
                    "当操作为'save','run_file','read','delete','delete_dir','list'时，需要的文件或目录路径（相对于沙箱根目录）",
                ),
            },
        )
        self.sandbox = sandbox
        self.execution_authorizer = execution_authorizer

    async def _authorize_execution(self, description: str) -> bool:
        """
        异步执行高风险代码前请求明确授权, 未配置授权通道时默认拒绝

        参数:
        - description: 本次代码执行说明

        返回:
        - 授权回调明确批准时返回 True
        """
        if self.execution_authorizer is None:
            return False
        decision = self.execution_authorizer(description)
        if isinstance(decision, Awaitable):
            decision = await decision
        return bool(decision)

    async def execute(
        self, operation: str, code: Optional[str] = None, path: Optional[str] = None
    ) -> Dict[str, Any]:
        """校验参数与授权后执行共用沙箱操作"""
        try:
            code, error = prepare_operation(operation, code, path)
            if error is not None:
                return error
            if operation in ("run", "run_file"):
                description = (
                    f"执行 Python 代码: {code}"
                    if operation == "run"
                    else f"执行沙箱文件: {path}"
                )
                if not await self._authorize_execution(description):
                    return {"error": "代码执行需要用户明确批准, 当前请求已拒绝"}
            return await asyncio.to_thread(
                execute_operation, self.sandbox, operation, code, path
            )
        except Exception as e:
            logger.error(f"[AsyncCodeSandboxTool] 操作 {operation} 失败: {e}")
            return {"error": str(e)}
