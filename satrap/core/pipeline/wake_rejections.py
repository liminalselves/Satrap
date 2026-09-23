"""自动参与决策与限流拒绝的有界环形记录, 供界面解释消息未唤醒/未处理的原因

未唤醒或被限流的消息不会进入投影与模型调用, A5 持久化存储与 ProjectedInput.notes 均覆盖不到;
本模块在实际决策/限流位置就地采集, 每适配器环形保留, 只读端点供 UI 查询
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import threading

REJECTION_CAPACITY = 256
"""每适配器保留的最近拒绝记录条数"""

REJECTION_QUERY_MAX_LIMIT = 256
"""单次查询返回条数上限"""


@dataclass(frozen=True)
class WakeRejection:
    """一次决策点或限流点的拒绝记录"""

    adapter_id: str
    session_id: str
    actor_id: str
    stage: str
    decision: str
    reason: str
    recorded_at: float
    message_id: str = ""
    request_id: str = ""
    send_status: str = ""


class WakeRejectionLog:
    """按适配器分隔的线程安全环形记录, 进程内保存不持久化"""

    def __init__(self, per_adapter: int = REJECTION_CAPACITY) -> None:
        if per_adapter <= 0:
            raise ValueError("每适配器记录容量必须为正数")
        self._per_adapter = per_adapter
        self._records: dict[str, deque[WakeRejection]] = {}
        self._lock = threading.Lock()

    def record(self, rejection: WakeRejection) -> None:
        """
        追加一条拒绝记录, 超出容量时淘汰最旧记录

        参数:
        - rejection: 决策点/限流点采集的拒绝事实
        """
        with self._lock:
            rows = self._records.get(rejection.adapter_id)
            if rows is None:
                rows = deque[WakeRejection](maxlen=self._per_adapter)
                self._records[rejection.adapter_id] = rows
            rows.append(rejection)

    def list(self, adapter_id: str | None = None, limit: int = 50) -> list[dict[str, object]]:
        """
        读取拒绝记录, 最新在前

        参数:
        - adapter_id: 可选适配器过滤, 缺省跨适配器按时间合并
        - limit: 返回条数上限 (不超过 REJECTION_QUERY_MAX_LIMIT)

        返回:
        - list[dict[str, object]]: 可 JSON 序列化的记录, 含本机 ISO 时间
        """
        limit = max(1, min(REJECTION_QUERY_MAX_LIMIT, limit))
        with self._lock:
            if adapter_id is not None:
                selected = list(self._records.get(adapter_id, ()))
            else:
                selected = [item for rows in self._records.values() for item in rows]
        selected.sort(key=lambda item: item.recorded_at, reverse=True)
        return [
            {
                "recorded_at": datetime.fromtimestamp(item.recorded_at).astimezone().isoformat(timespec="seconds"),
                "adapter_id": item.adapter_id, "session_id": item.session_id, "actor_id": item.actor_id,
                "stage": item.stage, "decision": item.decision, "reason": item.reason,
                "message_id": item.message_id, "request_id": item.request_id, "send_status": item.send_status,
            }
            for item in selected[:limit]
        ]

    def clear_adapter(self, adapter_id: str) -> None:
        """
        清除停用或重载平台的拒绝记录

        参数:
        - adapter_id: 平台实例 ID
        """
        with self._lock:
            self._records.pop(adapter_id, None)
