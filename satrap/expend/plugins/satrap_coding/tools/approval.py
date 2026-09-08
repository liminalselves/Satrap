from __future__ import annotations
import inspect
from typing import Awaitable, Callable, cast
from satrap.expend.plugins.satrap_coding.core.permission import (
    PermissionDecision,
    PermissionEngine,
    RiskLevel,
)
from satrap.core.type import safe_getattr_callable
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap.core.log import logger
from .constants import _APPROVAL_PROMPT


def _parse_integer_argument(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> tuple[int | None, str | None]:
    """
    严格解析有界整数工具参数

    参数:
    - value: 待解析的动态工具参数
    - name: 参数显示名称
    - minimum: 允许的最小值
    - maximum: 允许的最大值

    返回:
    - 解析值与错误信息; 解析成功时错误信息为 None
    """
    if isinstance(value, bool):
        return None, f"错误: {name} 必须是整数"
    if not isinstance(value, (str, int, float)):
        return None, f"{name} 必须是整数"
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None, f"错误: {name} 必须是整数"
    if isinstance(value, float) and not value.is_integer():
        return None, f"错误: {name} 必须是整数"
    if not minimum <= parsed <= maximum:
        return None, f"错误: {name} 必须在 {minimum} 到 {maximum} 之间"
    return parsed, None


def _make_sync_judge(session: SimpleSession) -> Callable[[str, RiskLevel, str], str]:
    """
    构造同步 auto-agent 审批判断 (调用会话主模型, 只读判断不执行)

    参数:
    - session: 会话

    返回:
    - Callable[[str, RiskLevel, str], str]: 构造同步 auto-agent 审批判断 (调用会话主模型, 只读判断不执行)
    """

    from . import _APPROVAL_PROMPT

    def judge(operation: str, risk: RiskLevel, description: str) -> str:
        try:
            prompt = _APPROVAL_PROMPT.format(
                operation=operation,
                risk=int(risk),
                description=description,
            )
            resp = session.llm.chat(messages=[{"role": "user", "content": prompt}])
            return str(resp or "ask")[:20]
        except Exception as e:
            logger.warning(f"[satrap_coding] 审批判断失败, 降级询问: {e}")
            return "ask"

    return judge


def _make_async_judge(
    session: AsyncSimpleSession,
) -> Callable[[str, RiskLevel, str], Awaitable[str]]:
    """
    构造异步 auto-agent 审批判断

    参数:
    - session: 会话

    返回:
    - Callable[[str, RiskLevel, str], Awaitable[str]]: 构造异步 auto-agent 审批判断
    """

    from . import _APPROVAL_PROMPT

    async def judge(operation: str, risk: RiskLevel, description: str) -> str:
        try:
            prompt = _APPROVAL_PROMPT.format(
                operation=operation,
                risk=int(risk),
                description=description,
            )
            resp = await session.llm.chat(
                messages=[{"role": "user", "content": prompt}]
            )
            return str(resp or "ask")[:20]
        except Exception as e:
            logger.warning(f"[satrap_coding] 审批判断失败, 降级询问: {e}")
            return "ask"

    return judge


def _ask_user_sync(
    session: SimpleSession,
    question: str,
    options: list[str] | None = None,
) -> str | None:
    """
    同步询问用户, 未配置输入通道返回 None

    参数:
    - session: 会话
    - question: 问题内容
    - options: 选项集合

    返回:
    - str | None:  None
    """
    provider = safe_getattr_callable(session, "user_input_provider")
    if provider is None:
        return None
    try:
        return str(_call_user_input_provider(provider, question, options))
    except Exception as e:
        logger.warning(f"[satrap_coding] 用户输入通道异常: {e}")
        return None


async def _ask_user_async(
    session: AsyncSimpleSession,
    question: str,
    options: list[str] | None = None,
) -> str | None:
    """
    异步询问用户 (provider 可为同步或异步), 未配置输入通道返回 None

    参数:
    - session: 会话
    - question: 问题内容
    - options: 选项集合

    返回:
    - str | None:  None
    """
    provider = safe_getattr_callable(session, "user_input_provider")
    if provider is None:
        return None
    try:
        answer = _call_user_input_provider(provider, question, options)
        if inspect.isawaitable(answer):
            answer = await cast(Awaitable[object], answer)
        return str(answer)
    except Exception as e:
        logger.warning(f"[satrap_coding] 用户输入通道异常: {e}")
        return None


def _call_user_input_provider(
    provider: Callable[..., object],
    question: str,
    options: list[str] | None,
) -> object:
    """
    调用用户输入通道, 新通道结构化传递选项, 旧通道保留拼接文本

    参数:
    - provider: 用户输入回调
    - question: 问题内容
    - options: 选项集合

    返回:
    - object: Provider 返回值
    """
    normalized_options = [
        str(option).strip() for option in options or [] if str(option).strip()
    ]
    if normalized_options:
        try:
            inspect.signature(provider).bind(question, normalized_options)
        except (TypeError, ValueError):
            numbered = "  ".join(
                f"{index}. {option}"
                for index, option in enumerate(normalized_options, 1)
            )
            return provider(f"{question} 可选: {numbered}")
        return provider(question, normalized_options)
    return provider(question)


def _approve_sync(
    session: SimpleSession,
    engine: PermissionEngine,
    operation: str,
    risk: RiskLevel,
    description: str,
) -> tuple[bool, str]:
    """
    同步审批入口: 返回 (是否放行, 结果消息)

    参数:
    - session: 会话
    - engine: 执行引擎
    - operation: 操作信息
    - risk: 风险级别
    - description: 说明文本

    返回:
    - tuple[bool, str]:  (是否放行, 结果消息)
    """
    decision = engine.evaluate(
        operation, risk, description, judge=_make_sync_judge(session)
    )
    if decision == PermissionDecision.ALLOW:
        return True, ""
    if decision == PermissionDecision.DENY:
        if engine.is_plan_mode_block(operation, risk):
            return False, f"计划模式已拒绝写操作: {description}"
        return False, f"操作被拒绝: {description} (风险级 {int(risk)})"
    choices = (
        "y 仅本次批准 / n 拒绝"
        if operation == "shell"
        else "y 仅本次批准 / n 拒绝 / all 本会话全部放行"
    )
    answer = _ask_user_sync(session, f"是否允许执行: {description}? ({choices})")
    if answer is None:
        return (
            False,
            f"需要用户批准: {description} (未配置 user_input_provider, 已拒绝)",
        )
    answer = answer.strip().lower()
    if answer in ("y", "yes", "允许", "批准"):
        engine.approve(operation, risk, remember=False)
        return True, ""
    if operation != "shell" and answer in ("all", "全部", "全放行"):
        engine.set_mode("full", persist=False)
        return True, "已授予本会话全部权限"
    return False, f"用户拒绝了操作: {description}"


async def _approve_async(
    session: AsyncSimpleSession,
    engine: PermissionEngine,
    operation: str,
    risk: RiskLevel,
    description: str,
) -> tuple[bool, str]:
    """
    异步审批入口: 返回 (是否放行, 结果消息)

    参数:
    - session: 会话
    - engine: 执行引擎
    - operation: 操作信息
    - risk: 风险级别
    - description: 说明文本

    返回:
    - tuple[bool, str]:  (是否放行, 结果消息)
    """
    decision = await engine.evaluate_async(
        operation, risk, description, judge=_make_async_judge(session)
    )
    if decision == PermissionDecision.ALLOW:
        return True, ""
    if decision == PermissionDecision.DENY:
        if engine.is_plan_mode_block(operation, risk):
            return False, f"计划模式已拒绝写操作: {description}"
        return False, f"操作被拒绝: {description} (风险级 {int(risk)})"
    choices = (
        "y 仅本次批准 / n 拒绝"
        if operation == "shell"
        else "y 仅本次批准 / n 拒绝 / all 本会话全部放行"
    )
    answer = await _ask_user_async(session, f"是否允许执行: {description}? ({choices})")
    if answer is None:
        return (
            False,
            f"需要用户批准: {description} (未配置 user_input_provider, 已拒绝)",
        )
    answer = answer.strip().lower()
    if answer in ("y", "yes", "允许", "批准"):
        engine.approve(operation, risk, remember=False)
        return True, ""
    if operation != "shell" and answer in ("all", "全部", "全放行"):
        engine.set_mode("full", persist=False)
        return True, "已授予本会话全部权限"
    return False, f"用户拒绝了操作: {description}"
