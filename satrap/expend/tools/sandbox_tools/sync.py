"""
代码沙箱工具的同步适配入口

校验模型参数并调用 CodeSandbox, 工具描述复用生效白名单
业务结果与策略错误原样交给宿主, 不触发代码审批
"""

from typing import Optional
from satrap.core.utils.sandbox.models import SandboxResult
from satrap.core.utils.TCBuilder import Tool
from satrap.core.utils.sandbox import CodeSandbox
from satrap.core.log import logger
from .utils import prepare_operation, sandbox_description, SandboxEnvironmentMixin
from satrap.core.plugin_interop import PluginInteractionError


class CodeSandboxTool(SandboxEnvironmentMixin, Tool):
    """代码沙箱工具, 封装对 CodeSandbox 的各种操作"""

    def __init__(
        self,
        sandbox: CodeSandbox,
    ):
        """
        创建同步工具并按沙箱生效配置生成描述

        参数:
        - sandbox: 已冻结根目录, 解释器和库白名单的沙箱实例
        """
        super().__init__(
            tool_name="code_sandbox",
            description=sandbox_description(sandbox.modules),
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

    def execute(
        self, operation: str, code: Optional[str] = None, path: Optional[str] = None
    ) -> SandboxResult:
        """
        同步校验工具参数并执行一次免审批 worker 操作

        参数:
        - operation: run/run_file/save/read/delete/delete_dir/list 之一
        - code: 默认 None, run/save 必须提供, 允许模型代码块
        - path: 默认 None, 文件操作使用沙箱内路径, list 可省略

        返回:
        - 业务结果, 参数错误或结构化策略失败; 非预期工具异常记录日志并返回 error
        """
        try:
            code, error = prepare_operation(operation, code, path)
            if error is not None:
                return error
            sandbox, verify = self._operation_sandbox()
            return sandbox.execute(operation, code, path, verify=verify)
        except PluginInteractionError:
            raise
        except Exception as e:
            logger.error(f"[CodeSandboxTool] 操作 {operation} 失败: {e}")
            return {"error": str(e)}
