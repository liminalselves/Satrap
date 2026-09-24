"""按请求关联的有界诊断: 唤醒决策, 限流, 补全, 模型与发送各阶段的就地采集

未唤醒或被限流的消息不会进入投影与模型调用, A5 持久化存储与 ProjectedInput.notes 均覆盖不到;
本模块在实际阶段位置就地采集, 每适配器按请求环形保留 (默认 256 个请求), 只读端点供 UI 查询。

记录只保存脱敏的原因码, 阶段状态与定位字段: 不保存音频, 正文, 密钥或供应商原始响应;
阶段失败与最终发送结果各自成条, 不压成单一失败; 诊断淘汰不影响持久账本中的幂等与发送证据
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
import threading

REQUEST_CAPACITY = 256
"""每适配器保留的最近请求数"""

RECORDS_PER_REQUEST = 16
"""单个请求保留的阶段记录条数上限"""

REJECTION_QUERY_MAX_LIMIT = 256
"""单次查询返回条数上限"""

DIAGNOSTIC_STAGES = frozenset({"wake_decision", "rate_limit", "projection", "model", "send"})
"""可采集的阶段; wake_decision/rate_limit 属于拒绝阶段, 其余属于执行阶段"""

REJECTION_STAGES = frozenset({"wake_decision", "rate_limit"})
"""拒绝阶段: 未唤醒与限流; 旧版拒绝记录查询只读这些阶段, 不混入已执行请求的阶段"""


@dataclass(frozen=True)
class RequestDiagnostic:
    """一次阶段事实: 拒绝 (未唤醒/限流) 或执行阶段结果 (补全/模型/发送)"""

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
    self_id: str = ""
    status: str = "dropped"
    reason_code: str = ""
    turn_id: str = ""
    attachments: str = ""
    notes: str = ""


def _iso_time(recorded_at: float) -> str:
    """采集时间转本机 ISO 字符串, 超出平台可表示范围时退回原始时间戳"""
    try:
        return datetime.fromtimestamp(recorded_at).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return f"{recorded_at:.6f}"


def _request_key(record: RequestDiagnostic) -> str:
    """请求分组键: 优先 request_id, 退化为消息 ID 或时间戳占位"""
    if record.request_id:
        return record.request_id
    if record.message_id:
        return f"message:{record.message_id}"
    return f"anon:{record.recorded_at:.6f}"


class RequestDiagnosticLog:
    """按适配器与请求分隔的线程安全环形诊断, 进程内保存不持久化"""

    def __init__(self, per_adapter: int = REQUEST_CAPACITY) -> None:
        if per_adapter <= 0:
            raise ValueError("每适配器记录容量必须为正数")
        self._per_adapter = per_adapter
        self._requests: dict[str, OrderedDict[str, list[RequestDiagnostic]]] = {}
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """每适配器保留的请求数上限"""
        return self._per_adapter

    def record(self, entry: RequestDiagnostic) -> None:
        """
        追加一条阶段记录, 超出容量时淘汰最旧请求

        参数:
        - entry: 阶段位置采集的事实; 同阶段同原因码的重复采集覆盖前一条
        """
        key = _request_key(entry)
        with self._lock:
            requests = self._requests.get(entry.adapter_id)
            if requests is None:
                requests = OrderedDict[str, list[RequestDiagnostic]]()
                self._requests[entry.adapter_id] = requests
            rows = requests.get(key)
            if rows is None:
                # 新请求: 超出容量先淘汰最旧请求, 持久账本中的发送证据不受影响
                if len(requests) >= self._per_adapter:
                    requests.popitem(last=False)
                rows = []
                requests[key] = rows
            rows[:] = [
                item for item in rows
                if not (item.stage == entry.stage and item.reason_code == entry.reason_code)
            ]
            rows.append(entry)
            del rows[:-RECORDS_PER_REQUEST]
            requests.move_to_end(key)

    @staticmethod
    def _row(item: RequestDiagnostic) -> dict[str, object]:
        """可 JSON 序列化的单条记录, 含本机 ISO 时间"""
        return {
            "recorded_at": _iso_time(item.recorded_at),
            "adapter_id": item.adapter_id, "session_id": item.session_id, "actor_id": item.actor_id,
            "stage": item.stage, "decision": item.decision, "reason": item.reason,
            "message_id": item.message_id, "request_id": item.request_id, "send_status": item.send_status,
            "self_id": item.self_id, "status": item.status, "reason_code": item.reason_code,
            "turn_id": item.turn_id, "attachments": item.attachments, "notes": item.notes,
        }

    def _selected(self, adapter_id: str | None, stages: frozenset[str] | None = None) -> list[RequestDiagnostic]:
        """按适配器取全部记录 (调用方持锁), 可选只保留给定阶段"""
        if adapter_id is not None:
            groups = [self._requests.get(adapter_id, OrderedDict())]
        else:
            groups = list(self._requests.values())
        return [
            item for requests in groups for rows in requests.values() for item in rows
            if stages is None or item.stage in stages
        ]

    def list(
        self, adapter_id: str | None = None, limit: int = 50, *, stages: frozenset[str] | None = None,
    ) -> list[dict[str, object]]:
        """
        读取阶段记录, 最新在前 (保留字段与旧版拒绝记录接口一致)

        参数:
        - adapter_id: 可选适配器过滤, 缺省跨适配器按时间合并
        - limit: 返回条数上限 (不超过 REJECTION_QUERY_MAX_LIMIT)
        - stages: 可选阶段过滤, 拒绝记录查询传 REJECTION_STAGES

        返回:
        - list[dict[str, object]]: 记录列表, 含阶段, 状态与脱敏原因码
        """
        limit = max(1, min(REJECTION_QUERY_MAX_LIMIT, limit))
        with self._lock:
            selected = self._selected(adapter_id, stages)
        selected.sort(key=lambda item: item.recorded_at, reverse=True)
        return [self._row(item) for item in selected[:limit]]

    def list_requests(
        self, adapter_id: str | None = None, *, stage: str = "", request_id: str = "", limit: int = 50,
    ) -> list[dict[str, object]]:
        """
        按请求汇总诊断, 最新在前

        参数:
        - adapter_id: 可选适配器过滤
        - stage: 只保留包含该阶段的请求
        - request_id: 只保留该请求
        - limit: 返回请求数上限 (不超过 REJECTION_QUERY_MAX_LIMIT)

        返回:
        - list[dict[str, object]]: 每项为请求摘要 (最新时间, 阶段列表, 各阶段状态与附件情况)
        """
        limit = max(1, min(REJECTION_QUERY_MAX_LIMIT, limit))
        with self._lock:
            if adapter_id is not None:
                groups = [(adapter_id, self._requests.get(adapter_id, OrderedDict()))]
            else:
                groups = list(self._requests.items())
        summaries: list[dict[str, object]] = []
        for group_id, requests in groups:
            for key, rows in requests.items():
                if request_id and key != request_id:
                    continue
                if stage and not any(item.stage == stage for item in rows):
                    continue
                if not rows:
                    continue
                latest = max(rows, key=lambda item: item.recorded_at)
                summaries.append({
                    "request_id": latest.request_id or key,
                    "adapter_id": group_id, "session_id": latest.session_id, "actor_id": latest.actor_id,
                    "self_id": latest.self_id, "message_id": latest.message_id,
                    "recorded_at": _iso_time(latest.recorded_at),
                    "stages": [item.stage for item in rows],
                    "statuses": {item.stage: item.status for item in rows},
                    "reason_codes": [item.reason_code for item in rows if item.reason_code],
                    "attachments": latest.attachments, "notes": latest.notes,
                    "send_status": latest.send_status, "turn_id": latest.turn_id,
                })
        summaries.sort(key=lambda item: str(item["recorded_at"]), reverse=True)
        return summaries[:limit]

    def get_request(self, request_id: str, adapter_id: str | None = None) -> dict[str, object] | None:
        """
        单个请求的完整诊断明细

        参数:
        - request_id: 逻辑请求标识
        - adapter_id: 可选适配器过滤

        返回:
        - dict[str, object] | None: 阶段记录与是否被容量淘汰过的标记; 未采集到时为 None
        """
        with self._lock:
            if adapter_id is not None:
                candidates = [(adapter_id, self._requests.get(adapter_id, OrderedDict()))]
            else:
                candidates = list(self._requests.items())
            for group_id, requests in candidates:
                rows = requests.get(request_id)
                if not rows:
                    continue
                ordered = sorted(rows, key=lambda item: item.recorded_at)
                return {
                    "request_id": request_id, "adapter_id": group_id,
                    "records": [self._row(item) for item in ordered],
                    "stages": [item.stage for item in ordered],
                    "available": True, "truncated": len(rows) >= RECORDS_PER_REQUEST,
                }
        return None

    def stats(self, adapter_id: str | None = None) -> dict[str, object]:
        """诊断容量与占用情况, 供健康检查与测试观察"""
        with self._lock:
            if adapter_id is not None:
                groups = [self._requests.get(adapter_id, OrderedDict())]
            else:
                groups = list(self._requests.values())
            requests_total = sum(len(requests) for requests in groups)
            records_total = sum(len(rows) for requests in groups for rows in requests.values())
        return {
            "capacity": self._per_adapter, "records_per_request": RECORDS_PER_REQUEST,
            "requests_total": requests_total, "records_total": records_total,
        }

    def clear_adapter(self, adapter_id: str) -> None:
        """
        清除停用或重载平台的诊断记录

        参数:
        - adapter_id: 平台实例 ID
        """
        with self._lock:
            self._requests.pop(adapter_id, None)

