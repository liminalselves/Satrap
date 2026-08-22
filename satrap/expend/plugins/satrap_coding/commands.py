"""
satrap_coding 插件命令: /goal /plan /approve (同步 + 异步)

约定:
- build_commands(session) 工厂返回 (同步命令映射, 异步命令映射)
- 命令共享插件状态 (state.py), 与工具/处理器同实例: /plan 直接影响工具审批引擎

注: /memory 命令已随长期记忆移交 base_take 插件
"""
from __future__ import annotations

from typing import Any, Callable

from satrap.edictum import AsyncSimpleSession, SimpleSession

from satrap.expend.plugins.satrap_coding.core.goal_state import GoalState
from satrap.expend.plugins.satrap_coding.core.permission import PermissionEngine
from satrap.expend.plugins.satrap_coding.state import get_plugin_state

_APPROVE_MODES = ("user", "auto-agent", "full")


def _parse_args(args: list[str], default: str = "") -> str:
    """
    命令参数列表 -> 单字符串 (保留空格)

    参数:
    - args: 额外位置参数
    - default: 默认值

    返回:
    - str: 命令参数列表 -> 单字符串 (保留空格)
    """
    if not args:
        return default
    return " ".join(str(a) for a in args).strip()


SessionType = SimpleSession | AsyncSimpleSession
"""插件支持的会话类型"""


def _cmd_goal_impl(state: dict[str, Any], session: SessionType, args: list[str]) -> str:
    """
    目标命令: 设置 / status / done / clear / todo / todo-done

    参数:
    - state: 状态
    - session: 会话
    - args: 额外位置参数

    返回:
    - str: 目标命令: 设置 / status / done / clear / todo / todo-done
    """
    goals = state["goals"]
    assert isinstance(goals, GoalState)
    sid = session.session_id
    sub = args[0] if args else ""
    if sub in ("status", "show", "查看"):
        return goals.format_status(sid)
    if sub in ("done", "完成"):
        return "目标已标记完成" if goals.complete_goal(sid) else "当前没有目标"
    if sub in ("clear", "清除"):
        return "目标已清除" if goals.clear_goal(sid) else "当前没有目标"
    if sub in ("todo", "子任务"):
        item = _parse_args(args[1:])
        if not item:
            return "用法: /goal todo <子任务内容>"
        return "子任务已添加" if goals.add_todo(sid, item) else "没有 active 目标, 无法添加子任务"
    if sub in ("todo-done", "完成子任务"):
        try:
            index = int(args[1])
        except (IndexError, ValueError):
            return "用法: /goal todo-done <序号>"
        return f"子任务 {index} 已标记完成" if goals.complete_todo(sid, index) else f"子任务序号无效: {index}"
    text = _parse_args(args)
    if not text:
        return "用法: /goal <目标描述> 设置持续目标; /goal status 查看; /goal done 完成; /goal clear 清除; /goal todo <子任务>"
    goal = goals.set_goal(sid, text)
    return (
        f"目标已设置 (active): {goal['text']}\n"
        "目标自动推进 (yolo) 已启动: 模型将连续执行直到报告完成, 每轮自动推进; "
        "/goal done 或 /goal clear 可随时停止"
    )


def _cmd_plan_impl(state: dict[str, Any], args: list[str]) -> str:
    """
    计划模式: 写类工具全部拒绝, 只输出计划

    参数:
    - state: 状态
    - args: 额外位置参数

    返回:
    - str: 计划模式: 写类工具全部拒绝, 只输出计划
    """
    engine = state["engine"]
    assert isinstance(engine, PermissionEngine)
    sub = args[0] if args else "on"
    if sub in ("on", "进入", "start"):
        engine.set_plan_mode(True)
        return "已进入计划模式: 文件编辑/写命令等写操作全部禁用, 只输出计划; 完成后 /plan off 恢复"
    if sub in ("off", "退出", "end"):
        engine.set_plan_mode(False)
        return "已退出计划模式: 写操作恢复可用"
    if sub == "approve":
        return "/plan approve 已移除 (与审批语义混淆), 请使用 /plan off 退出计划模式"
    return "用法: /plan on 进入计划模式; /plan off 退出"


def _cmd_approve_impl(state: dict[str, Any], args: list[str]) -> str:
    """
    审批命令: 切换策略 / 查看与添加持久规则

    参数:
    - state: 状态
    - args: 额外位置参数

    返回:
    - str: 审批命令: 切换策略 / 查看与添加持久规则
    """
    engine = state["engine"]
    assert isinstance(engine, PermissionEngine)
    sub = args[0] if args else "mode"
    if sub in ("mode", "策略"):
        if len(args) < 2 or args[1] not in _APPROVE_MODES:
            return f"用法: /approve mode <{'|'.join(_APPROVE_MODES)}>; 当前: {engine.mode}"
        engine.set_mode(args[1])
        return f"审批策略已切换: {args[1]}"
    if sub in ("rules", "规则"):
        rules = engine.list_persistent_rules()
        if not rules:
            return "暂无持久规则"
        return "\n".join(f"- {op}: 风险级 <= {level} 直接放行" for op, level in rules.items())
    if sub in ("rule", "添加规则"):
        if len(args) < 3:
            return "用法: /approve rule <操作> <风险级 0-1> (如: /approve rule file_write 1); 最高 1, 高危操作不开放持久放行"
        try:
            level = int(args[2])
            if level not in (0, 1):
                return "风险级仅允许 0-1 (高危操作不开放持久规则, 如需放行请用 /approve mode full)"
            engine.add_persistent_rule(args[1], level)
        except ValueError:
            return "风险级必须是 0-1 的整数"
        return f"持久规则已添加: {args[1]} -> 风险级 <= {args[2]}"
    return f"用法: /approve mode <{'|'.join(_APPROVE_MODES)}> | rules | rule <操作> <风险级>; 当前策略: {engine.mode}"


def build_commands(session: SessionType) -> tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:
    """
    构建插件命令: 返回 (同步命令, 异步命令) 映射

    参数:
    - session: 会话

    返回:
    - tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:  (同步命令, 异步命令) 映射
    """
    state = get_plugin_state(session)

    def cmd_goal(*args: str) -> str:
        """
        设置/查看/完成持续目标

        参数:
        - args: 额外位置参数

        返回:
        - str: 设置/查看/完成持续目标
        """
        return _cmd_goal_impl(state, session, list(args))

    def cmd_plan(*args: str) -> str:
        """
        进入/退出计划模式 (写操作全部禁用)

        参数:
        - args: 额外位置参数

        返回:
        - str: 进入/退出计划模式 (写操作全部禁用)
        """
        return _cmd_plan_impl(state, list(args))

    def cmd_approve(*args: str) -> str:
        """
        切换审批策略/管理持久规则

        参数:
        - args: 额外位置参数

        返回:
        - str: 切换审批策略/管理持久规则
        """
        return _cmd_approve_impl(state, list(args))

    async def cmd_goal_async(*args: str) -> str:
        return _cmd_goal_impl(state, session, list(args))

    async def cmd_plan_async(*args: str) -> str:
        return _cmd_plan_impl(state, list(args))

    async def cmd_approve_async(*args: str) -> str:
        return _cmd_approve_impl(state, list(args))

    sync_map: dict[str, Callable[..., Any]] = {
        "goal": cmd_goal,
        "plan": cmd_plan,
        "approve": cmd_approve,
    }
    async_map: dict[str, Callable[..., Any]] = {
        "goal": cmd_goal_async,
        "plan": cmd_plan_async,
        "approve": cmd_approve_async,
    }
    return sync_map, async_map

