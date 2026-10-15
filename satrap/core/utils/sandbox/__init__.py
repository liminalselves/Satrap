"""
代码沙箱的公开门面与兼容导出

冻结根目录, 库白名单和解释器配置, 将所有业务操作交给一次性 worker
与 runner 协作处理同步和异步调用, 保留旧方法的错误返回与异常约定
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import os
from pathlib import Path
from typing import cast

from .models import (
    OPERATIONS, Operation, Request, SandboxResult, allowed_modules as parse_modules, failure_result,
)
from .runner import run, run_async, validate_environment


class CodeSandbox:
    """保留文件/执行方法, 不在主进程执行模型代码或业务文件 I/O"""

    def __init__(self, sandbox_path: str, env: str, execution_timeout: float = 30.0,
                 allowed_env: Iterable[str] = (), allowed_modules: str | None = None) -> None:
        """
        冻结沙箱配置并记录配置错误, 不在宿主执行文件操作

        参数:
        - sandbox_path: 业务根目录, 解析为绝对路径, 不允许与项目或强制保护目录冲突
        - env: 既定 Python 解释器的绝对路径, 不通过 PATH 查找
        - execution_timeout: 整次调用的秒数预算, 默认 30.0, 最低为 0.1
        - allowed_env: 兼容环境变量配置, 默认空集合; 显式放行密钥或启动控制变量会记录配置错误
        - allowed_modules: 逗号分隔的公开模块名, 默认 None 使用内置集合, 空字符串禁止导入

        配置错误延迟到操作调用时返回, 不切回旧执行器
        """
        # Step.1 冻结调用配置, 不在宿主创建业务目录
        self.sandbox_path = str(Path(sandbox_path).resolve())
        self.python_executable = env
        self.execution_timeout = max(0.1, float(execution_timeout))
        self.allowed_env = frozenset(allowed_env)
        self._configuration_error = ""
        try:
            # Step.2 检查公开模块, 环境变量与解释器来源
            self.modules = parse_modules(allowed_modules)
            validate_environment(self.allowed_env)
            if not Path(env).is_absolute():
                raise ValueError("沙箱解释器必须使用既定的绝对路径")
            # Step.3 检查业务根与强制保护目录的重叠关系
            project = Path(__file__).resolve().parents[4]
            root = Path(self.sandbox_path)
            protected = [project / ".git", project / ".satrap" / "config", project / ".satrap" / "credentials"]
            protected.extend(Path(value).resolve() for name in ("SATRAP_CONFIG_ROOT", "SATRAP_CREDENTIALS_ROOT", "SATRAP_RUNTIME_ROOT")
                             if (value := os.environ.get(name)))
            if project.is_relative_to(root) or any(root.is_relative_to(path) or path.is_relative_to(root) for path in protected):
                raise ValueError("sandbox_root 与项目或强制保护目录冲突")
        except ValueError as error:
            self.modules = ()
            self._configuration_error = str(error)
            # 将配置失败固定为实例状态, 后续所有操作返回相同失败而不启动进程

    def _request(self, operation: str, code: str | None, path: str | None, encoding: str = "utf-8") -> Request:
        """
        构造冻结配置的单次 worker 请求

        参数:
        - operation: 七种操作之一, 未知操作抛出 ValueError
        - code: run/save 的源码或文本, 其他操作可为 None
        - path: 目标文件或目录, 相对路径基于沙箱根, 可为 None
        - encoding: read 使用的文本编码, 默认 utf-8

        返回:
        - 包含操作与配置的 Request, 不在此处访问业务文件
        """
        if operation not in OPERATIONS:
            raise ValueError("不支持的沙箱操作: " + operation)
        return Request(cast(Operation, operation), self.sandbox_path, self.modules, code, path, encoding)

    def execute(self, operation: str, code: str | None = None, path: str | None = None,
                *, encoding: str = "utf-8") -> SandboxResult:
        """
        同步分派一次沙箱操作并等待真实 worker 退出

        参数:
        - operation: 七种操作之一, 未知操作抛出 ValueError
        - code: run/save 的源码或文本, 其他操作可为 None
        - path: 目标文件或目录, 相对路径基于沙箱根, 可为 None
        - encoding: read 使用的文本编码, 默认 utf-8

        返回:
        - 操作结果或结构化失败; 已记录的配置错误直接返回, 不启动 worker
        """
        if self._configuration_error:
            return failure_result("configuration_error", self._configuration_error)
        return run(self._request(operation, code, path, encoding), self.python_executable, self.execution_timeout, self.allowed_env)

    async def execute_async(self, operation: str, code: str | None = None, path: str | None = None) -> SandboxResult:
        """
        异步分派一次沙箱操作并在取消时等待 worker 回收

        参数:
        - operation: 七种操作之一, 未知操作抛出 ValueError
        - code: run/save 的源码或文本, 其他操作可为 None
        - path: 目标文件或目录, 相对路径基于沙箱根, 可为 None

        返回:
        - 操作结果或结构化失败; 已记录的配置错误直接返回, 取消继续向调用方传播
        """
        if self._configuration_error:
            return failure_result("configuration_error", self._configuration_error)
        return await run_async(self._request(operation, code, path), self.python_executable, self.execution_timeout, self.allowed_env)

    def _checked(self, operation: str, code: str | None = None, path: str | None = None) -> SandboxResult:
        """
        同步执行文件操作并恢复旧门面的异常约定

        参数:
        - operation: 七种操作之一, 未知操作抛出 ValueError
        - code: run/save 的源码或文本, 其他操作可为 None
        - path: 目标文件或目录, 相对路径基于沙箱根, 可为 None

        返回:
        - 成功结果; 路径, 权限和文件错误映射为对应异常, 其他失败抛出 RuntimeError
        """
        result = self.execute(operation, code, path)
        if result.get("returncode", -1) != 0:
            message = result.get("error", "沙箱操作失败")
            code_name = result.get("error_code", "")
            exception = {"path_outside_sandbox": ValueError, "permission_denied": PermissionError,
                         "file_not_found": FileNotFoundError, "invalid_path": ValueError}.get(code_name, RuntimeError)
            raise exception(message)
        return result

    def run(self, code: str) -> SandboxResult:
        """
        在一次性 worker 中执行给定源码

        参数:
        - code: 待检查和受限执行的 Python 源码

        返回:
        - 包含输出和返回码的结果, 策略或运行失败以 error_code 表示
        """
        return self.execute("run", code=code)

    def run_file(self, path: str) -> SandboxResult:
        """
        在一次性 worker 中读取并受限执行脚本

        参数:
        - path: 沙箱内脚本路径, 不加入模块搜索路径

        返回:
        - 执行结果或结构化失败; 越界路径沿用旧约定抛出 ValueError
        """
        result = self.execute("run_file", path=path)
        if result.get("error_code") == "path_outside_sandbox":
            raise ValueError(result.get("error"))
        return result

    def save_to_file(self, code: str, path: str) -> None:
        """
        在一次性 worker 中保存文本, 不预先检查其可执行性

        参数:
        - code: 按 UTF-8 保存的文本
        - path: 沙箱内目标文件路径, 自动创建父目录

        操作失败沿用文件门面的异常约定, 后续 run_file 仍需独立检查源码
        """
        self._checked("save", code, path)

    def read_file(self, path: str, encoding: str = "utf-8") -> SandboxResult:
        """
        在一次性 worker 中读取有界文本

        参数:
        - path: 沙箱内文件路径
        - encoding: read 使用的文本编码, 默认 utf-8

        返回:
        - 包含 content 的结果或结构化失败; 越界路径使用兼容返回码 -12
        """
        result = self.execute("read", path=path, encoding=encoding)
        if result.get("error_code") == "path_outside_sandbox":
            result["returncode"] = -12
        return result

    def list_files(self, subdir: str = "") -> list[str]:
        """
        在一次性 worker 中列出业务文件

        参数:
        - subdir: 相对沙箱根的子目录, 默认空字符串表示根目录

        返回:
        - 排序后的相对文件路径列表, 失败抛出门面异常
        """
        return self._checked("list", path=subdir).get("files", [])

    def delete_file(self, path: str) -> None:
        """
        在一次性 worker 中删除指定文件

        参数:
        - path: 沙箱内文件路径, 目录必须改用 delete_directory

        路径或文件操作失败抛出门面异常
        """
        self._checked("delete", path=path)

    def delete_directory(self, path: str, recursive: bool = True) -> None:
        """
        在一次性 worker 中删除子目录并保护业务根

        参数:
        - path: 待删除的沙箱内子目录
        - recursive: 默认 True 递归删除, False 仅删除空目录

        递归分支沿用门面异常, 非递归失败抛出 OSError, 配置错误抛出 RuntimeError
        """
        if recursive:
            self._checked("delete_dir", path=path)
            return
        request = replace(self._request("delete_dir", None, path), recursive=False)
        if self._configuration_error:
            raise RuntimeError(self._configuration_error)
        result = run(request, self.python_executable, self.execution_timeout, self.allowed_env)
        if result.get("returncode", -1):
            raise OSError(result.get("error", "无法删除目录"))
