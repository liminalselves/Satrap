"""
CLI 统一输出与错误约定

约定:
- 前缀: 错误: / 警告: / 提示: / 成功信息不带前缀
- 退出码: 0 成功, 1 业务或运行错误, 2 用法错误 (未知操作, 与 argparse 一致)
- 命令函数不直接 sys.exit, 业务失败抛 CliError/ValueError,
  由 dispatch_action / run_cli_action 集中渲染并退出
- --json 全局开关开启时, 结构化数据经 render_data 输出 JSON
"""
from __future__ import annotations

from typing import Any, Callable
import argparse
import json
import sys

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

_json_mode = False


class CliError(Exception):
    """CLI 业务错误, 由集中分发层渲染为统一话术并退出"""

    def __init__(self, message: str, *, hint: str = "", exit_code: int = EXIT_ERROR):
        """
        参数:
        - message: 错误消息
        - hint: 给用户的下一步建议
        - exit_code: 退出码
        """
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


def set_json_mode(enabled: bool) -> None:
    """
    设置全局 JSON 输出模式 (由 main 在解析参数后调用)

    参数:
    - enabled: 是否启用
    """
    global _json_mode
    _json_mode = enabled


def json_mode() -> bool:
    """
    返回:
    - bool: 当前是否处于 JSON 输出模式
    """
    return _json_mode


def ok(message: str) -> None:
    """
    成功信息

    参数:
    - message: 消息文本
    """
    if not _json_mode:
        print(message)


def info(message: str) -> None:
    """
    提示信息

    参数:
    - message: 消息文本
    """
    if not _json_mode:
        print(f"提示: {message}")


def warn(message: str) -> None:
    """
    警告信息 (始终输出, JSON 模式下走 stderr 以免污染结构化输出)

    参数:
    - message: 消息文本
    """
    print(f"警告: {message}", file=sys.stderr if _json_mode else sys.stdout)


def error(message: str, *, hint: str = "") -> None:
    """
    错误信息

    参数:
    - message: 消息文本
    - hint: 给用户的下一步建议
    """
    if _json_mode:
        payload: dict[str, Any] = {"ok": False, "error": message}
        if hint:
            payload["hint"] = hint
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(f"错误: {message}")
    if hint:
        print(f"提示: {hint}")


def print_json(data: Any) -> None:
    """
    输出 JSON

    参数:
    - data: 输入数据
    """
    print(json.dumps(data, ensure_ascii=False, indent=2))


def print_table(rows: list[list[str]], header: list[str] | None = None) -> None:
    """
    输出对齐表格 (唯一的表格实现, 各命令不再自带副本)

    参数:
    - rows: 数据行
    - header: 可选表头
    """
    print(format_table(rows, header))


def format_table(rows: list[list[str]], header: list[str] | None = None) -> str:
    """
    生成对齐表格文本

    参数:
    - rows: 数据行
    - header: 可选表头

    返回:
    - str: 对齐后的表格文本
    """
    if not rows:
        return "(空)"
    col_widths: list[int] = []
    all_rows = ([header] if header else []) + rows
    for col_idx in range(len(all_rows[0])):
        col_widths.append(max(len(str(r[col_idx])) for r in all_rows))
    lines: list[str] = []
    if header:
        lines.append(" | ".join(str(h).ljust(w) for h, w in zip(header, col_widths)))
        lines.append("-+-".join("-" * w for w in col_widths))
    for row in rows:
        lines.append(" | ".join(str(c).ljust(w) for c, w in zip(row, col_widths)))
    return "\n".join(lines)


def render_data(data: Any, human: Callable[[], None]) -> None:
    """
    结构化输出入口: JSON 模式输出 data, 否则调用 human 渲染人类可读文本

    参数:
    - data: 结构化数据
    - human: 人类可读渲染函数
    """
    if _json_mode:
        print_json(data)
    else:
        human()


def dispatch_action(
    action_map: dict[str, Callable[[argparse.Namespace], None]],
    args: argparse.Namespace,
    *,
    key: str = "action",
) -> None:
    """
    子命令集中分发: 未知操作退出码 2, CliError/ValueError 统一渲染

    参数:
    - action_map: 操作到处理函数的映射
    - args: 命令参数
    - key: 携带操作名的参数属性 (子命令组为 action, 顶层命令为 command)
    """
    handler = action_map.get(str(getattr(args, key, "")))
    if handler is None:
        run_cli_action(lambda: _raise_unknown_action(args, key))
        return
    run_cli_action(lambda: handler(args))


def run_cli_action(fn: Callable[[], Any]) -> None:
    """
    执行命令函数并集中处理异常: CliError 按自带退出码, ValueError 按业务错误

    参数:
    - fn: 命令函数
    """
    from satrap.cli.client import DaemonError, DaemonUnavailable   # 延迟导入避免循环依赖

    try:
        fn()
    except CliError as e:
        error(e.message, hint=e.hint)
        sys.exit(e.exit_code)
    except DaemonUnavailable as e:
        error(str(e), hint="可先用 `satrap status` 确认服务状态; 离线操作请加 --offline")
        sys.exit(EXIT_ERROR)
    except DaemonError as e:
        error(str(e))
        sys.exit(EXIT_ERROR)
    except ValueError as e:
        error(str(e))
        sys.exit(EXIT_ERROR)


def _raise_unknown_action(args: argparse.Namespace, key: str = "action") -> None:
    """
    参数:
    - args: 命令参数
    - key: 携带操作名的参数属性
    """
    raise CliError(f"未知操作: {getattr(args, key, '')}", exit_code=EXIT_USAGE)
