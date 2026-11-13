"""群管理动作的公共静态参数契约。"""
from __future__ import annotations


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
    "handle_group_request": {"flag": "flag", "sub_type": "sub_type", "approve": "bool", "reason": "reason",
                             "request_id": "request_id", "expected_revision": "revision"},
}
"""服务端动作参数结构, 前端成员页和群管理页共同读取"""
