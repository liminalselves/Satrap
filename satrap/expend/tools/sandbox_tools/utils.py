"""代码沙箱的同步与异步执行工具封装"""

from collections.abc import Awaitable, Callable
import re
from typing import Any
from satrap.core.utils.sandbox import CodeSandbox


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
    else:  # 如果没有代码块标记, 直接使用返回内容
        return response.strip()


SyncExecutionAuthorizer = Callable[[str], bool]

AsyncExecutionAuthorizer = Callable[[str], bool | Awaitable[bool]]


def prepare_operation(
    operation: str, code: str | None, path: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """统一提取代码并校验操作参数, 授权由各入口执行"""
    if code is not None:
        code = extract_code(code)
    required = {
        "run": ("code",),
        "run_file": ("path",),
        "save": ("code", "path"),
        "read": ("path",),
        "delete": ("path",),
        "delete_dir": ("path",),
        "list": (),
    }
    if operation not in required:
        return code, {
            "error": f"不支持的操作: {operation}，支持的操作：run, save, delete, delete_dir, list"
        }
    values = {"code": code, "path": path}
    if any(values[name] is None for name in required[operation]):
        return code, {
            "error": f"操作 '{operation}' 需要提供 {' 和 '.join(required[operation])} 参数"
        }
    return code, None


def execute_operation(
    sandbox: CodeSandbox, operation: str, code: str | None, path: str | None
) -> dict[str, Any]:
    """执行已校验及授权的沙箱操作, 同步与异步共用业务实现"""
    if operation in ("run", "run_file"):
        if operation == "run":
            assert code is not None
            result = sandbox.run(code)
        else:
            assert path is not None
            result = sandbox.run_file(path)
        response = {
            "operation": operation,
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "returncode": result.get("returncode", -1),
        }
        if operation == "run_file":
            response["path"] = path
        return response
    if operation == "list":
        return {
            "operation": "list",
            "path": path or "/",
            "files": sandbox.list_files(path if path else ""),
        }
    assert path is not None
    if operation == "read":
        result = sandbox.read_file(path)
        if result.get("returncode") != 0:
            return {
                "operation": "read",
                "path": path,
                "error": result.get("stderr", "读取失败"),
                "returncode": result.get("returncode"),
            }
        return {
            "operation": "read",
            "path": path,
            "content": result.get("content", ""),
            "status": "success",
        }
    if operation == "save":
        assert code is not None
        sandbox.save_to_file(code, path)
    elif operation == "delete":
        sandbox.delete_file(path)
    else:
        sandbox.delete_directory(path, recursive=True)
    return {"operation": operation, "path": path, "status": "success"}
