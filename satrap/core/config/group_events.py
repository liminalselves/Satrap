"""逐群业务事件可见设置与有界脱敏近期缓存"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

from satrap.core.platform import PlatformEvent
from satrap.core.platform.notices import NoticePayload


EVENT_KINDS = frozenset({
    "group_increase", "group_decrease", "group_recall", "group_ban",
    "group_upload", "group_admin", "group_card", "group_request",
})
EVENT_CAPACITY = 4096


def event_values(explicit: Mapping[str, object]) -> dict[str, bool]:
    """校验每类群事件的继承或显式接收开关"""
    result: dict[str, bool] = {}
    for kind, raw in explicit.items():
        if kind not in EVENT_KINDS or not isinstance(raw, dict) or raw.get("mode") not in {"inherit", "value"}:
            raise ValueError("群事件设置包含未知类别或模式")
        if raw["mode"] == "inherit":
            if set(raw) != {"mode"}:
                raise ValueError("继承事件设置不能携带值")
            continue
        if set(raw) != {"mode", "value"} or type(raw["value"]) is not bool:
            raise ValueError("显式事件设置必须为布尔值")
        result[kind] = raw["value"]
    return result


class GroupEventBuffer:
    """只缓存允许展示的固定字段, 不保存原始载荷、请求 flag 或正文"""

    def __init__(self) -> None:
        self._rows: deque[dict[str, Any]] = deque(maxlen=EVENT_CAPACITY)
        self._sequence = 0

    def append(self, event: PlatformEvent) -> None:
        """接受事件中心去重后的群通知并抽取安全字段"""
        payload = event.extras.get("payload")
        if not isinstance(payload, NoticePayload) or not payload.group_id or not payload.self_id:
            return
        kind = "group_request" if payload.category == "request" and payload.kind == "group" else payload.kind
        if kind not in EVENT_KINDS:
            return
        self._sequence += 1
        self._rows.append({
            "id": self._sequence, "adapter_id": event.platform_id, "self_id": payload.self_id,
            "group_id": payload.group_id, "kind": kind, "sub_type": payload.sub_type,
            "user_id": payload.user_id, "operator_id": payload.operator_id,
            "target_id": payload.target_id, "message_id": payload.message_id,
            "duration": payload.duration, "time": payload.time or event.timestamp,
        })

    def list(self, adapter_id: str, self_id: str, group_id: str, *,
             visibility: Mapping[str, bool], limit: int = 50) -> dict[str, Any]:
        """按平台、账号和群在服务端过滤近期事件"""
        if limit not in {50, 100}:
            raise ValueError("事件数量只能为 50 或 100")
        rows = [item for item in reversed(self._rows)
                if item["adapter_id"] == adapter_id and item["self_id"] == self_id
                and item["group_id"] == group_id and visibility.get(item["kind"], True)]
        return {"items": rows[:limit], "capacity": EVENT_CAPACITY, "volatile": True,
                "truncated": len(rows) > limit}
