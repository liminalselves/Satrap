"""
编程工具的参数解析与普通提问适配

保留普通提问的同步与异步适配, 执行审批统一使用宿主协调器
"""
from __future__ import annotations
import inspect
from typing import Awaitable, Callable, cast

from satrap.core.approval.coordinator import approve_sync as _approve_sync, approve_async as _approve_async, _turn_identity
from satrap.core.type import safe_getattr_callable
from satrap.edictum import AsyncSimpleSession, SimpleSession

from satrap.core.log import logger


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
