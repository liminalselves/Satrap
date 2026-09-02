"""代码沙箱的同步与异步执行工具封装"""
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.utils.sandbox import CodeSandbox
from collections.abc import Awaitable, Callable
from typing import Dict, Any, Optional
import re, asyncio

from satrap.core.log import logger


def extract_code(response: str) -> str:
    """
    从模型响应中提取代码块

    参数:
    - response: 响应

    返回:
    - str: 从模型响应中提取代码块
    """
    code_pattern = r"```(?:python)?\s*([\s\S]*?)```"
    match = re.search(code_pattern, response)
    if match:
        return match.group(1).strip()
    else:   # 如果没有代码块标记, 直接使用返回内容
        return response.strip()

SyncExecutionAuthorizer = Callable[[str], bool]
AsyncExecutionAuthorizer = Callable[[str], bool | Awaitable[bool]]


class CodeSandboxTool(Tool):
    """代码沙箱工具, 封装对 CodeSandbox 的各种操作"""
    def __init__(self, sandbox: CodeSandbox, execution_authorizer: SyncExecutionAuthorizer | None = None):
        """
        初始化 CodeSandboxTool

        参数:
        - sandbox: 沙箱实例
        - execution_authorizer: 代码执行授权回调, 未配置时拒绝执行
        """
        super().__init__(
            tool_name="code_sandbox",
            description="在代码沙箱中执行代码或管理文件。支持的操作：run（执行代码字符串）、run_file（执行文件）、save（保存代码到文件）、read（读取文件内容）、delete（删除文件）、delete_dir（删除目录）、list（列出文件）。",
            params_dict={
                "operation": ("string", "要执行的操作，可选值：'run', 'run_file', 'save', 'read', 'delete', 'delete_dir', 'list'"),
                "code": ("string", "当操作为'run'或'save'时，需要提供的代码字符串"),
                "path": ("string", "当操作为'save','run_file','read','delete','delete_dir','list'时，需要的文件或目录路径（相对于沙箱根目录）")
            }
        )
        self.sandbox = sandbox
        self.execution_authorizer = execution_authorizer

    def _authorize_execution(self, description: str) -> bool:
        """
        执行高风险代码前请求明确授权, 未配置授权通道时默认拒绝

        参数:
        - description: 本次代码执行说明

        返回:
        - 授权回调明确批准时返回 True
        """
        if self.execution_authorizer is None:
            return False
        return bool(self.execution_authorizer(description))

    def execute(self, operation: str, code: Optional[str] = None, path: Optional[str] = None) -> Dict[str, Any]:
        """
        执行沙箱操作, 返回结果字典

        参数:
        - operation: 操作信息
        - code: 代码内容
        - path: 路径

        返回:
        - Dict[str, Any]: 结果字典
        """
        try:
            if code is not None:   # 如果提供了代码参数, 尝试从中提取代码块
                code = extract_code(code)

            if operation == "run":
                if code is None:
                    return {"error": "操作 'run' 需要提供 code 参数"}
                if not self._authorize_execution(f"执行 Python 代码: {code}"):
                    return {"error": "代码执行需要用户明确批准, 当前请求已拒绝"}

                result = self.sandbox.run(code)
                return {
                    "operation": "run",
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "returncode": result.get("returncode", -1)
                }
            
            elif operation == "run_file":
                if path is None:
                    return {"error": "操作 'run_file' 需要提供 path 参数"}
                if not self._authorize_execution(f"执行沙箱文件: {path}"):
                    return {"error": "代码执行需要用户明确批准, 当前请求已拒绝"}
                result = self.sandbox.run_file(path)
                return {
                    "operation": "run_file",
                    "path": path,
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "returncode": result.get("returncode", -1)
                }

            elif operation == "save":
                if code is None or path is None:
                    return {"error": "操作 'save' 需要提供 code 和 path 参数"}
                self.sandbox.save_to_file(code, path)
                return {"operation": "save", "path": path, "status": "success"}

            elif operation == "read":
                if path is None:
                    return {"error": "操作 'read' 需要提供 path 参数"}
                result = self.sandbox.read_file(path)
                if result.get("returncode") != 0:
                    return {"operation": "read", "path": path, "error": result.get("stderr", "读取失败"), "returncode": result.get("returncode")}
                return {
                    "operation": "read",
                    "path": path,
                    "content": result.get("content", ""),
                    "status": "success"
                }

            elif operation == "delete":
                if path is None:
                    return {"error": "操作 'delete' 需要提供 path 参数"}
                self.sandbox.delete_file(path)
                return {"operation": "delete", "path": path, "status": "success"}

            elif operation == "delete_dir":
                if path is None:
                    return {"error": "操作 'delete_dir' 需要提供 path 参数"}
                self.sandbox.delete_directory(path, recursive=True)
                return {"operation": "delete_dir", "path": path, "status": "success"}

            elif operation == "list":
                files = self.sandbox.list_files(path if path else '')
                return {"operation": "list", "path": path or "/", "files": files}

            else:
                return {"error": f"不支持的操作: {operation}，支持的操作：run, save, delete, delete_dir, list"}

        except Exception as e:
            logger.error(f"[CodeSandboxTool] 操作 {operation} 失败: {e}")
            return {"error": str(e)}

class AsyncCodeSandboxTool(AsyncTool):
    """异步代码沙箱工具, 封装对 CodeSandbox 的各种异步操作"""
    def __init__(self, sandbox: CodeSandbox, execution_authorizer: AsyncExecutionAuthorizer | None = None):
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
                "operation": ("string", "要执行的操作，可选值：'run', 'run_file', 'save', 'read', 'delete', 'delete_dir', 'list'"),
                "code": ("string", "当操作为'run'或'save'时，需要提供的代码字符串"),
                "path": ("string", "当操作为'save','run_file','read','delete','delete_dir','list'时，需要的文件或目录路径（相对于沙箱根目录）")
            }
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

    async def execute(self, operation: str, code: Optional[str] = None, path: Optional[str] = None) -> Dict[str, Any]:
        """
        异步执行沙箱操作, 返回结果字典

        参数:
        - operation: 操作信息
        - code: 代码内容
        - path: 路径

        返回:
        - Dict[str, Any]: 结果字典
        """
        try:
            if code is not None:
                code = extract_code(code)   # 假设 extract_code 是同步函数
            # 如果提供了代码参数, 尝试从中提取代码块(同步函数, 直接调用)

            if operation == "run":
                if code is None:
                    return {"error": "操作 'run' 需要提供 code 参数"}
                if not await self._authorize_execution(f"执行 Python 代码: {code}"):
                    return {"error": "代码执行需要用户明确批准, 当前请求已拒绝"}
                # 同步 run 方法转为异步执行
                result = await asyncio.to_thread(self.sandbox.run, code)
                return {
                    "operation": "run",
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "returncode": result.get("returncode", -1)
                }

            elif operation == "run_file":
                if path is None:
                    return {"error": "操作 'run_file' 需要提供 path 参数"}
                if not await self._authorize_execution(f"执行沙箱文件: {path}"):
                    return {"error": "代码执行需要用户明确批准, 当前请求已拒绝"}
                result = await asyncio.to_thread(self.sandbox.run_file, path)
                return {
                    "operation": "run_file",
                    "path": path,
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "returncode": result.get("returncode", -1)
                }

            elif operation == "save":
                if code is None or path is None:
                    return {"error": "操作 'save' 需要提供 code 和 path 参数"}
                await asyncio.to_thread(self.sandbox.save_to_file, code, path)
                return {"operation": "save", "path": path, "status": "success"}

            elif operation == "read":
                if path is None:
                    return {"error": "操作 'read' 需要提供 path 参数"}
                result = await asyncio.to_thread(self.sandbox.read_file, path)
                if result.get("returncode") != 0:
                    return {
                        "operation": "read",
                        "path": path,
                        "error": result.get("stderr", "读取失败"),
                        "returncode": result.get("returncode")
                    }
                return {
                    "operation": "read",
                    "path": path,
                    "content": result.get("content", ""),
                    "status": "success"
                }

            elif operation == "delete":
                if path is None:
                    return {"error": "操作 'delete' 需要提供 path 参数"}
                await asyncio.to_thread(self.sandbox.delete_file, path)
                return {"operation": "delete", "path": path, "status": "success"}

            elif operation == "delete_dir":
                if path is None:
                    return {"error": "操作 'delete_dir' 需要提供 path 参数"}
                await asyncio.to_thread(self.sandbox.delete_directory, path, recursive=True)
                return {"operation": "delete_dir", "path": path, "status": "success"}

            elif operation == "list":
                files = await asyncio.to_thread(self.sandbox.list_files, path if path else '')
                return {"operation": "list", "path": path or "/", "files": files}

            else:
                return {"error": f"不支持的操作: {operation}，支持的操作：run, save, delete, delete_dir, list"}

        except Exception as e:
            logger.error(f"[AsyncCodeSandboxTool] 操作 {operation} 失败: {e}")
            return {"error": str(e)}
