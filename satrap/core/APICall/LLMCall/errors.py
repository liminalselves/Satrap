"""
模型调用错误的公开分类与诊断

同步和异步入口共享固定原因说明, 不把供应商异常中的认证资料或请求正文回传
"""
from collections.abc import Mapping
import re
import traceback

from openai import APITimeoutError, APIConnectionError, APIError

from satrap.core.type import LLMErrorInfo

from satrap.core.log import logger


def model_error_info(error: Exception, *, timeout: float | None = None) -> LLMErrorInfo:
    """
    将调用异常转换为可公开的错误资料

    参数:
    - error: SDK 或工作流异常, 原始字符串不直接进入公开结果
    - timeout: 已配置的单次网络超时秒数, 默认不补充数值

    返回:
    - 包含稳定分类和固定原因的脱敏错误
    """
    existing = getattr(error, "info", None)
    if isinstance(existing, LLMErrorInfo):
        return existing
    status_value = getattr(error, "status_code", None)
    status = status_value if isinstance(status_value, int) and not isinstance(status_value, bool) else None
    if isinstance(error, APITimeoutError) or isinstance(error, TimeoutError) and timeout is not None:
        message = "等待模型响应超时"
        if timeout is not None:
            message += f", 网络超时配置为 {timeout:g} 秒"
        code = "network_timeout"
    elif isinstance(error, TimeoutError):
        code, message = "operation_timeout", "会话内部操作超时, 请检查工具或调用日志"
    elif isinstance(error, (APIConnectionError, ConnectionError)):
        code, message = "connection_error", "无法连接模型服务, 请检查服务地址与网络"
    elif status in {401, 403}:
        code, message = "authentication_error", "模型服务拒绝访问, 请检查凭据与调用权限"
    elif status == 429:
        code, message = "rate_limit", "服务商限流或额度不足, 请检查额度或稍后重试"
    elif status == 404:
        code, message = "provider_error", "模型或接口不存在, 请检查模型名称与服务地址"
    elif status is not None and status >= 500:
        code, message = "provider_error", "模型服务暂时异常, 请稍后重试"
    elif status is not None or isinstance(error, APIError):
        code, message = "provider_error", "模型服务拒绝请求, 请检查模型配置与输入参数"
    else:
        code, message = "internal_error", "请求执行发生异常, 请查看日志中的诊断记录"
    provider_code = None
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        nested = body.get("error", body)
        candidate = nested.get("code") if isinstance(nested, Mapping) else None
        if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate):
            provider_code = candidate
    request_value = getattr(error, "request_id", None)
    request_id = (request_value if isinstance(request_value, str)
                  and re.fullmatch(r"(?:req|request)[_-][A-Za-z0-9_-]{1,96}", request_value) else None)
    return LLMErrorInfo(code, message, status, provider_code, request_id)


def log_model_error(info: LLMErrorInfo, model: str, error: Exception | None = None) -> None:
    """
    记录一次最终请求失败, 预期请求失败使用警告级别

    参数:
    - info: 已分类的脱敏资料
    - model: 模型名称, 仅用于关联本次请求
    - error: 可选原始异常, 仅记录类型和调用帧位置, 不打印请求正文
    """
    message = f"[LLM] model={model!r} code={info.code} {info.text}"
    if info.request_id:
        message += f" request_id={info.request_id}"
    if info.code == "internal_error":
        if error is not None:
            frames = traceback.extract_tb(error.__traceback__)[-8:]
            message += f" type={type(error).__name__} frames=" + " -> ".join(f"{frame.name}:{frame.lineno}" for frame in frames)
        logger.error(message)
    else:
        logger.warning(message)
