"""
代码沙箱工具的共用参数与描述适配

同步和异步入口共享代码块提取, 操作参数校验和生效白名单文案
不在工具适配层执行业务代码或维护独立能力配置
"""

import re
from satrap.core.utils.sandbox.models import SandboxResult


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


def sandbox_description(modules: tuple[str, ...]) -> str:
    """
    根据生效白名单生成工具说明

    参数:
    - modules: 工具实例实际允许的完整模块名元组, 空元组表示禁止导入

    返回:
    - 包含操作, 网络和产物边界的说明, 空白名单明确显示为无可用模块
    """
    return (
        "在独立 Python 进程执行代码或管理沙箱文件, 无需代码审批. "
        "支持 run/run_file/save/read/delete/delete_dir/list. "
        "网络和外部程序调用被拦截, 不保留 Python 变量. "
        "文件只保存到沙箱, 不自动展示图片或发送附件. "
        "群聊需要发送已生成图片时, 可调用已启用的 group_chat_import_sandbox_image 登记相对图片路径, 再用返回的 asset_id 回复. "
        "允许导入: " + (", ".join(modules) if modules else "(无)") + ". "
        "仅已登记子模块可用, 不支持沙箱内自定义模块和交互输入"
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
