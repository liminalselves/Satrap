"""
代码沙箱工具的共用参数与描述适配

同步和异步入口共享代码块提取, 操作参数校验和生效白名单文案
不在工具适配层执行业务代码或维护独立能力配置
"""

import re
from collections.abc import Callable
from pathlib import Path
from satrap.core.plugin_interop import PluginHost, PluginInteractionError
from satrap.core.plugin_interop.environment import environment_snapshot
from satrap.core.utils.sandbox import CodeSandbox
from satrap.core.utils.sandbox.models import SandboxResult


class SandboxEnvironmentMixin:
    """为每次 worker 操作固定当前宿主目录, 不复用过期安装环境"""

    sandbox: CodeSandbox
    _plugin_host: PluginHost | None = None
    _session: object | None = None

    def _bind(self, session: object) -> None:
        """
        保存会话以读取统一环境绑定

        参数:
        - session: 当前宿主会话
        """
        self._session = session

    def _operation_sandbox(self) -> tuple[CodeSandbox, Callable[[], None]]:
        """
        冻结本次目录和执行检查, 保留既定解释器与白名单

        返回:
        - 本次 worker 配置及调度前后可重复调用的检查函数
        """
        plugin_host = self._plugin_host
        snapshot = (plugin_host.environment_snapshot() if plugin_host is not None
                    else environment_snapshot(self._session))
        sandbox = self.sandbox
        if snapshot.sandbox_root is not None and snapshot.sandbox_root != Path(sandbox.sandbox_path):
            sandbox = CodeSandbox(str(snapshot.sandbox_root), sandbox.python_executable,
                                  sandbox.execution_timeout, sandbox.allowed_env, ",".join(sandbox.modules))

        def verify() -> None:
            """在进程创建和等待期间检查安装代次与环境版本"""
            current = (plugin_host.environment_snapshot() if plugin_host is not None
                       else environment_snapshot(self._session))
            if current != snapshot:
                raise PluginInteractionError("capability_stale", "沙箱操作期间宿主环境已改变")
            if plugin_host is not None:
                plugin_host._runtime().require_execution(plugin_host)

        verify()
        return sandbox, verify


def extract_code(response: str) -> str:
    """
    提取首个模型代码块, 无代码块时保留原文本内容

    参数:
    - response: 模型提供的代码或待保存文本

    返回:
    - 去掉外围空白后的首个代码块内容, 无匹配时返回去掉外围空白的原文本
    """
    code_pattern = r"```(?:python)?\s*([\s\S]*?)```"
    match = re.search(code_pattern, response)
    if match:
        return match.group(1).strip()
    else:   # 无代码块标记时保留原内容, 仅清理外围空白
        return response.strip()


SANDBOX_PARAMS = {
    "operation": {
        "type": "string",
        "enum": ["run", "run_file", "save", "read", "delete", "delete_dir", "list"],
        "description": "要执行的操作",
    },
    "code": {"type": "string", "description": "run 或 save 使用的代码/文本；其他操作省略"},
    "path": {"type": "string", "description": "沙箱内相对路径；文件操作必填，list 可省略"},
}


def sandbox_description(modules: tuple[str, ...]) -> str:
    """
    根据生效白名单生成工具说明

    参数:
    - modules: 工具实例实际允许的完整模块名元组, 空元组表示禁止导入

    返回:
    - 包含操作, 网络和产物边界的说明, 空白名单明确显示为无可用模块
    """
    return (
        "在隔离沙箱中运行 Python 或管理文件，适合计算、解析和生成文件。"
        "不能联网或启动外部程序，变量不跨调用保留，文件不会自动展示或发送。"
        "群聊需要发送图片时，若提供 group_chat_import_sandbox_image，先登记相对路径，再把返回的 asset_id 用于回复。"
        "可导入模块: " + (", ".join(modules) if modules else "(无)") + "。"
        "未列出的子模块和自定义模块不可用，不支持交互输入"
    )


def prepare_operation(
    operation: str, code: str | None, path: str | None
) -> tuple[str | None, SandboxResult | None]:
    """
    统一提取代码并检查七种操作所需参数

    参数:
    - operation: 待调用的操作名
    - code: 源码或文本, 可为 None, 有值时提取代码块
    - path: 目标路径, 可为 None, 本层只检查是否提供

    返回:
    - 规范化后的 code 与可选错误结果; 未知操作或缺少参数时返回错误, 合法参数返回 None 错误
    """
    # Step.1 统一代码块形态, 不在此处检查脚本可执行性
    if code is not None:
        code = extract_code(code)
    # Step.2 检查操作集合及必需参数, 路径与源码策略交给 worker
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
            "error": f"不支持的操作: {operation}，支持的操作：run, run_file, save, read, delete, delete_dir, list"
        }
    values = {"code": code, "path": path}
    if any(values[name] is None for name in required[operation]):
        return code, {
            "error": f"操作 '{operation}' 需要提供 {' 和 '.join(required[operation])} 参数"
        }
    return code, None
