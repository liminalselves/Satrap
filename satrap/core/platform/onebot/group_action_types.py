"""群管理写动作的目标、参数和风险元数据"""
from __future__ import annotations

from typing import Any

from satrap.core.config.group_store import GROUP_APPROVAL_ACTIONS, HIGH_IMPACT_ACTIONS
from satrap.core.platform.onebot.admin import (
    _normalize_duration, _normalize_message_id, normalize_flag, normalize_user_id,
)
from satrap.core.platform.onebot.request_registry import flag_digest


ACTION_FIELDS: dict[str, dict[str, str]] = {
    "recall_message": {"message_id": "message_id"},
    "kick_group_member": {"user_id": "user_id", "reject_add_request": "bool"},
    "ban_group_member": {"user_id": "user_id", "duration": "duration"},
    "set_group_whole_ban": {"enable": "bool"},
    "ban_anonymous": {"flag": "flag", "duration": "duration"},
    "set_group_admin": {"user_id": "user_id", "enable": "bool"},
    "set_group_anonymous": {"enable": "bool"},
    "set_group_card": {"user_id": "user_id", "card": "card"},
    "set_group_name": {"name": "name"},
    "set_group_special_title": {"user_id": "user_id", "title": "title"},
    "leave_group": {"dismiss": "bool"},
    "handle_group_request": {"flag": "flag", "sub_type": "sub_type", "approve": "bool", "reason": "reason"},
}
"""服务端动作参数结构, 前端成员页和群管理页共同读取"""


def action_metadata(action: str) -> dict[str, object]:
    """返回动作参数类型、写属性和风险级别"""
    if action not in GROUP_APPROVAL_ACTIONS or action not in ACTION_FIELDS:
        raise ValueError("未知群管理动作")
    return {"action_type": action, "schema": ACTION_FIELDS[action], "write": True,
            "risk": "high" if action in HIGH_IMPACT_ACTIONS else "normal"}


def normalize_action_params(action: str, raw: dict[str, object], self_id: str) -> tuple[dict[str, object], str | None]:
    """规范化动作参数并把可重放 flag 留在内存, 仅返回摘要用于持久化"""
    if action not in ACTION_FIELDS:
        raise ValueError("未知群管理动作")
    schema = ACTION_FIELDS[action]
    if set(raw) - set(schema):
        raise ValueError("管理动作包含未知参数")
    required = {
        "recall_message": {"message_id"}, "kick_group_member": {"user_id"},
        "ban_group_member": {"user_id"}, "set_group_whole_ban": {"enable"},
        "ban_anonymous": {"flag"}, "set_group_admin": {"user_id", "enable"},
        "set_group_anonymous": {"enable"}, "set_group_card": {"user_id", "card"},
        "set_group_name": {"name"}, "set_group_special_title": {"user_id", "title"},
        "leave_group": set(), "handle_group_request": {"flag", "sub_type", "approve"},
    }
    if required[action] - set(raw):
        raise ValueError("管理动作缺少必填参数")
    defaults: dict[str, object] = {
        "reject_add_request": False, "duration": 1800, "dismiss": False,
        "card": "", "title": "", "reason": "",
    }
    normalized: dict[str, object] = {}
    secret_flag: str | None = None
    for key, kind in schema.items():
        if key not in raw and key not in defaults:
            continue
        value = raw.get(key, defaults.get(key))
        if kind == "user_id":
            normalized[key] = normalize_user_id(value)
        elif kind == "message_id":
            normalized[key] = _normalize_message_id(value)
        elif kind == "duration":
            normalized[key] = _normalize_duration(value, "禁言秒数无效")
        elif kind == "bool":
            if type(value) is not bool:
                raise ValueError(f"{key} 必须为布尔值")
            normalized[key] = value
        elif kind == "flag":
            secret_flag = normalize_flag(value)
            normalized["flag_digest"] = flag_digest(
                "group" if action == "handle_group_request" else "anonymous", self_id, secret_flag,
            )
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须为文本")
            limits = {"card": 60, "name": 60, "title": 18, "reason": 120, "sub_type": 8}
            if len(value) > limits.get(key, 200) or (key == "name" and not value):
                raise ValueError(f"{key} 长度无效")
            if key == "sub_type" and value not in {"add", "invite"}:
                raise ValueError("sub_type 必须为 add 或 invite")
            normalized[key] = value
    return normalized, secret_flag
