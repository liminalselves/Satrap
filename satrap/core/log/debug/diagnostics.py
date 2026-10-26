"""按实际错误边界生成轻量诊断摘要, 不读取正文或根据异常文字猜测责任"""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Literal, cast

from .contracts import DebugData


DiagnosticLevel = Literal["info", "warning", "error", "unknown"]
LEVEL_RANK = {"unknown": -1, "info": 0, "warning": 1, "error": 2}
RANK_LEVEL: dict[int, DiagnosticLevel] = {-1: "unknown", 0: "info", 1: "warning", 2: "error"}
_INPUT_ERRORS = frozenset({"invalid_arguments", "invalid_argument", "invalid_tool_call", "validation_error", "invalid_parameters"})
_SYSTEM_ERRORS = frozenset({"execution_error", "internal_error", "storage_unavailable", "network_timeout", "session_timeout",
                            "operation_timeout", "connection_error", "authentication_error", "provider_error", "rate_limit"})
_BUSINESS_ERRORS = frozenset({"permission_denied", "not_owner", "disabled", "entry_disabled", "plugin_disabled", "wrong_conversation",
                              "read_only", "write_disabled", "memory_disabled", "revision_conflict", "invalid_source", "not_found", "stale_call"})


def _code(value: object) -> str:
    """
    只接受有界的机器错误码, 不把任意返回正文复制成标签

    参数:
    - value: 框架或插件返回的错误类型

    返回:
    - 合法错误码, 无法确认时为空字符串
    """
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,79}", value) else ""


def _problem(level: DiagnosticLevel, kind: str, code: str = "", reason: object = "") -> dict[str, DebugData]:
    """
    构造不包含正文的受控分类摘要

    参数:
    - level: 诊断级别
    - kind: 错误所属类别
    - code: 可选稳定错误码
    - reason: 可选公开原因, 后续仍经过采集器脱敏

    返回:
    - 有界摘要字段
    """
    result: dict[str, DebugData] = {"diagnostic_level": level, "error_kind": kind}
    if code:
        result["error_code"] = code
    if isinstance(reason, str) and reason:
        result["reason"] = reason[:512]
    return result


def tool_diagnostic(result: object, name: str, *, missing_tool: bool = False) -> dict[str, DebugData]:
    """
    在真实工具返回边界区分参数问题, 业务拒绝和执行异常

    参数:
    - result: 原始工具返回, 只检查约定的顶层状态字段
    - name: 实际选择的工具名称
    - missing_tool: 框架已确认该工具未注册, 不依赖插件返回的 not_found 猜测

    返回:
    - 标题和分类摘要, 不改变业务返回或接受返回体中的自报日志级别
    """
    meta: dict[str, DebugData] = {"name": name, "diagnostic_level": "info"}
    status = result.get("status") if isinstance(result, Mapping) else None
    if isinstance(status, str) and status in ("pending", "ready", "waiting", "needs_attention", "saved", "deleted", "already_exists", "approved", "rejected", "expired"):
        meta["result_status"] = status
    if not isinstance(result, Mapping) or result.get("ok") is not False:
        return meta
    code = _code(result.get("error_type")) or _code(result.get("code"))
    kind = ("model_input" if code in _INPUT_ERRORS or missing_tool and code == "not_found" else
            "runtime_error" if code in _SYSTEM_ERRORS else "business_rejection" if code in _BUSINESS_ERRORS else "unclassified_failure")
    meta.update(_problem("error" if code in _SYSTEM_ERRORS else "warning", kind, code, result.get("error")))
    return meta


def event_diagnostic(category: str, status: str, metadata: Mapping[str, DebugData], *, legacy: bool = False) -> dict[str, DebugData]:
    """
    为已冻结事件补齐级别, 旧记录仅使用已有明确证据

    参数:
    - category: 宿主固定事件类别
    - status: 实际执行状态, 与诊断级别分别保存
    - metadata: 已脱敏的轻量摘要, 不包含工具或模型正文
    - legacy: 是否读取未分类的旧记录, 普通旧成功记录保持 unknown

    返回:
    - 可直接持久化或展示的分类字段
    """
    level = metadata.get("diagnostic_level")
    if level != "error" and (metadata.get("omission") or metadata.get("incomplete") is True or status == "observation_incomplete"):
        return _problem("warning", "observation_gap", reason=metadata.get("reason", ""))
    if level in ("info", "warning", "error", "unknown"):
        return {"diagnostic_level": cast(str, level)}
    code = _code(metadata.get("error_code")) or _code(metadata.get("code"))
    reason = metadata.get("reason", "")
    if code in _INPUT_ERRORS:
        return _problem("warning", "model_input", code, reason)
    if code in _SYSTEM_ERRORS:
        return _problem("error", "upstream_error" if category == "model" else "runtime_error", code, reason)
    if code in _BUSINESS_ERRORS:
        return _problem("warning", "business_rejection", code, reason)
    if status in {"cancelled", "skipped", "pending", "ready", "ask", "deny", "rejected", "expired", "waiting", "needs_attention"}:
        return {"diagnostic_level": "info"} if category in {"approval", "platform_approval", "request", "reply"} or status in {"cancelled", "skipped"} else _problem("warning", "business_rejection", code, reason)
    if status in {"unknown", "partial", "interrupted"}:
        return _problem("warning", "uncertain_result", code, reason)
    if status in {"failed", "timeout"}:
        if category == "model":
            return _problem("error", "upstream_error", code, reason)
        if metadata.get("error_type") or category == "delivery" or status == "timeout":
            return _problem("error", "runtime_error", code, reason)
        return _problem("warning", "unclassified_failure", code, reason)
    return {"diagnostic_level": "unknown" if legacy else "info"}
