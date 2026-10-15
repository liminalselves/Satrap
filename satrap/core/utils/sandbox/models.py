"""
沙箱协议类型与公开能力配置

定义门面和 worker 共用的请求, 结果和首次违规记录, 固定协议与输出上限
公开模块名单只约束模型导入, 不等同于解释器内部依赖名单
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, NoReturn, TypeAlias, TypedDict


Operation = Literal["run", "run_file", "save", "read", "delete", "delete_dir", "list"]
OPERATIONS = ("run", "run_file", "save", "read", "delete", "delete_dir", "list")
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_CHARS = 20_000
PROTOCOL_VERSION = 1

STANDARD_MODULES = (
    "math", "statistics", "decimal", "fractions", "json", "csv", "re", "datetime",
    "time", "collections", "itertools", "functools", "random",
)
BUSINESS_MODULES = (
    "numpy", "numpy.linalg", "numpy.random", "pandas", "matplotlib.pyplot",
    "docx", "docx.shared", "docx.enum.text", "docx.enum.table", "docx.enum.section",
    "pptx", "pptx.util", "pptx.enum.text", "pptx.enum.shapes", "pptx.dml.color",
    "openpyxl", "openpyxl.styles", "openpyxl.utils", "openpyxl.chart",
    "pypdf", "pdfplumber", "reportlab.pdfgen.canvas", "reportlab.lib.pagesizes",
    "reportlab.lib.units", "reportlab.lib.styles", "reportlab.platypus",
    "reportlab.pdfbase.pdfmetrics", "reportlab.pdfbase.ttfonts",
)
DEFAULT_MODULES = STANDARD_MODULES + BUSINESS_MODULES
DEFAULT_MODULE_TEXT = ",".join(DEFAULT_MODULES)


class SandboxResult(TypedDict, total=False):
    """门面和工具共同使用的 JSON 结果"""

    operation: str
    path: str
    stdout: str
    stderr: str
    returncode: int
    content: str
    files: list[str]
    status: str
    error: str
    error_code: str
    action: str
    module: str
    line: int
    worker_pid: int
    stdout_truncated: bool
    stderr_truncated: bool
    elapsed_seconds: float
    setup_seconds: float
    execution_seconds: float


JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class Request:
    """父进程冻结后传入 worker 的单次请求"""

    operation: Operation
    root: str
    modules: tuple[str, ...]
    code: str | None = None
    path: str | None = None
    encoding: str = "utf-8"
    version: int = PROTOCOL_VERSION
    recursive: bool = True

    @classmethod
    def parse(cls, value: object) -> Request:
        """
        验证协议字段并构造不可变请求

        参数:
        - value: JSON 解码后的对象, 必须包含当前版本, 合法操作, 根目录和模块列表

        返回:
        - 验证后的 Request; 字段缺失或类型错误抛出 ValueError
        """
        # Step.1 检查协议版本与操作分派所需的必填字段
        if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != PROTOCOL_VERSION:
            raise ValueError("沙箱协议版本无效")
        if value.get("operation") not in OPERATIONS or not isinstance(value.get("root"), str):
            raise ValueError("沙箱操作或根目录无效")
        # Step.2 校验集合, 可选文本与递归标志, 避免隐式类型转换
        for name in ("modules",):
            items = value.get(name)
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise ValueError(f"沙箱字段 {name} 必须是字符串列表")
        for name in ("code", "path"):
            if value.get(name) is not None and not isinstance(value[name], str):
                raise ValueError(f"沙箱字段 {name} 必须是字符串")
        encoding = value.get("encoding", "utf-8")
        if not isinstance(encoding, str):
            raise ValueError("沙箱编码无效")
        if type(value.get("recursive", True)) is not bool:
            raise ValueError("沙箱递归标志无效")
        # Step.3 应用协议默认值并冻结本次请求
        return cls(value["operation"], value["root"], tuple(value["modules"]),
                   value.get("code"), value.get("path"), encoding, recursive=value.get("recursive", True))


class PolicyViolation(RuntimeError):
    """拒绝动作时携带结构化信息, 不保存敏感调用参数"""

    def __init__(self, code: str, message: str, *, action: str = "", module: str = "", line: int = 0):
        """
        保存策略失败的结构化定位信息

        参数:
        - code: 固定错误类别, 不从异常文本推断
        - message: 可返回模型的错误说明, 不应包含密钥或完整敏感参数
        - action: 被拒绝的 API 或动作, 默认空字符串
        - module: 被拒绝的模块名, 默认空字符串
        - line: 源码行号, 默认 0 表示无可用位置
        """
        super().__init__(message)
        self.code = code
        self.action = action
        self.module = module
        self.line = line


@dataclass
class Violations:
    """仅保存首次违规, 防止捕获异常后误报成功"""

    first: PolicyViolation | None = field(default=None, init=False)

    def reject(self, code: str, message: str, *, action: str = "", module: str = "", line: int = 0) -> NoReturn:
        """
        记录首次策略违规并抛出普通可捕获异常

        参数:
        - code: 固定错误类别, 不从异常文本推断
        - message: 可返回模型的错误说明, 不应包含密钥或完整敏感参数
        - action: 被拒绝的 API 或动作, 默认空字符串
        - module: 被拒绝的模块名, 默认空字符串
        - line: 源码行号, 默认 0 表示无可用位置

        已有首次违规不会被覆盖, 最终由 worker 优先汇总为失败
        """
        failure = PolicyViolation(code, message, action=action, module=module, line=line)
        if self.first is None:
            self.first = failure
        raise failure


def failure_result(code: str, message: str, *, returncode: int = -20) -> SandboxResult:
    """
    构造门面和 worker 共用的结构化失败

    参数:
    - code: 确定的错误类别
    - message: 可返回调用方的错误说明
    - returncode: 兼容执行返回码, 默认 -20

    返回:
    - 包含 error_code, error 和标准输出字段的失败结果
    """
    return {"returncode": returncode, "stdout": "", "stderr": message, "error": message, "error_code": code}


def allowed_modules(value: str | None) -> tuple[str, ...]:
    """
    解析管理员配置并拒绝未登记的公开模块

    参数:
    - value: 逗号分隔模块名, None 使用默认集合, 空字符串得到空集合

    返回:
    - 去重并保留配置顺序的模块元组; 非字符串或未支持模块抛出 ValueError
    """
    if value is not None and not isinstance(value, str):
        raise ValueError("sandbox_allowed_modules 必须是字符串")
    modules = DEFAULT_MODULES if value is None else tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    invalid = set(modules) - set(DEFAULT_MODULES)
    if invalid:
        raise ValueError("不支持的沙箱模块: " + ", ".join(sorted(invalid)))
    return modules
