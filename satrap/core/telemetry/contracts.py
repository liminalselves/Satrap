"""
健康和轻量观测的公开协议

定义固定的服务, 计量与设置结构, 并校验 IANA 时区窗口和输出预算
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal, TypeAlias, TypedDict, cast
import math
import time


Service = Literal["control", "backend", "chat", "local"]
SourceState = Literal["available", "stale", "offline", "auth_required", "error"]
Purpose = Literal["conversation", "summary", "approval", "other"]
JsonValue: TypeAlias = None | str | bool | int | float | list["JsonValue"] | dict[str, "JsonValue"]


class ServiceHealth(TypedDict):
    """当前服务自己的运行身份, 不代替依赖服务的状态"""
    service: Service
    running: bool
    healthy: bool
    runtime_id: str
    pid: int
    started_at: float
    uptime_seconds: float
    generated_at: float
    python: str
    platform: str
    version: str | None
    revision: str | None


class ServiceProbe(TypedDict):
    """Control 对依赖服务的一次有界往返探测"""
    target_service: Service
    measured_at: float
    latency_ms: float | None
    state: SourceState


class MessageCounts(TypedDict):
    """已知范围内的消息和模型请求计数"""
    inbound: int | None
    outbound: int | None
    model_requests: int | None
    commands: int | None


class UsageTotals(TypedDict):
    """调用分母与已报告用量分开, 缺失 usage 不填零"""
    calls: int
    succeeded: int
    failed: int
    cancelled: int
    usage_reported_calls: int
    usage_missing_calls: int
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    token_reported_calls: dict[str, int]
    duration_p50_ms: float | None
    duration_p95_ms: float | None
    duration_method: Literal["exact", "histogram"]


@dataclass(frozen=True)
class TelemetrySettings:
    """不包含正文的观测设置, 与高级 Debug 开关独立"""
    usage_enabled: bool = True
    activity_enabled: bool = True
    usage_retention_days: int = 30
    activity_retention_days: int = 30
    aggregate_retention_days: int = 180
    max_service_mb: int = 128

    @classmethod
    def parse(cls, value: object) -> TelemetrySettings:
        """
        校验整体替换的设置, 不接受未知字段或布尔值冒充整数

        参数:
        - value: 完整设置对象

        返回:
        - 已校验设置, 无效结构抛出 ValueError
        """
        if not isinstance(value, dict):
            raise ValueError("统计设置必须为对象")
        value = cast(dict[str, object], value)
        if set(value) != {item.name for item in fields(cls)}:
            raise ValueError("统计设置必须包含全部已声明字段")
        for name in ("usage_enabled", "activity_enabled"):
            if type(value[name]) is not bool:
                raise ValueError(f"{name} 必须是布尔值")
        for name in ("usage_retention_days", "activity_retention_days", "aggregate_retention_days", "max_service_mb"):
            maximum, minimum = (4096, 16) if name == "max_service_mb" else (3660, 1)
            number = value[name]
            if not isinstance(number, int) or isinstance(number, bool) or not minimum <= number <= maximum:
                raise ValueError(f"{name} 必须在 {minimum}-{maximum} 范围内")
        return cls(usage_enabled=cast(bool, value["usage_enabled"]), activity_enabled=cast(bool, value["activity_enabled"]),
                   usage_retention_days=cast(int, value["usage_retention_days"]), activity_retention_days=cast(int, value["activity_retention_days"]),
                   aggregate_retention_days=cast(int, value["aggregate_retention_days"]), max_service_mb=cast(int, value["max_service_mb"]))

    def payload(self) -> dict[str, JsonValue]:
        """
        返回完整公开设置

        返回:
        - 只含布尔开关和整数预算的 JSON 对象
        """
        return cast(dict[str, JsonValue], asdict(self))


@dataclass(frozen=True)
class MetricScope:
    """由宿主绑定的调用归属, 不接收模型传来的统计身份"""
    platform_id: str = ""
    self_id: str = ""
    conversation_kind: str = ""
    target_id: str = ""
    agent: str = ""
    session_id: str = ""
    context_id: str = ""
    request_id: str = ""
    model_config: str = ""
    purpose: Purpose = "conversation"


@dataclass(frozen=True)
class ModelCall:
    """一次 SDK 调用的终态统计, 不含提示词或响应正文"""
    call_id: str
    scope: MetricScope
    model: str
    provider: str
    started_at: float
    finished_at: float
    duration_ms: float
    status: Literal["succeeded", "failed", "cancelled"]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    context_window: int | None = None


@dataclass(frozen=True)
class MessageMetric:
    """可去重的消息计数, 不保存消息内容"""
    message_id: str
    scope: MetricScope
    occurred_at: float
    direction: Literal["inbound", "outbound"]
    command: bool = False


@dataclass(frozen=True)
class ActivityEvent:
    """完成业务提交后的活动摘要, 引用固定业务身份"""
    event_id: str
    occurred_at: float
    kind: str
    tone: Literal["info", "warning", "error"]
    title: str
    scope: MetricScope
    reference: dict[str, JsonValue] | None = None
    count: int = 1


@dataclass(frozen=True)
class MetricWindow:
    """经时区和跨度校验的左闭右开统计窗口"""
    start: float
    end: float
    timezone: str
    bucket: Literal["minute", "hour", "day"] | None

    @classmethod
    def parse(cls, query: Mapping[str, str], view: str = "overview", *, now: float | None = None) -> MetricWindow:
        """
        将请求参数转换为明确时间窗口, 日界线使用 IANA 时区

        参数:
        - query: 已拒绝重复参数的查询
        - view: 总览或模型/实例视图, 决定默认跨度
        - now: 测试可注入当前时间, 默认使用墙钟

        返回:
        - 已校验窗口, 超限或无效时区抛出 ValueError
        """
        stamp = time.time() if now is None else now
        zone_name = query.get("tz", "Asia/Shanghai")
        if len(zone_name) > 128:
            raise ValueError("统计时区无效")
        try:
            zone = ZoneInfo(zone_name)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("统计时区无效") from error
        bucket = query.get("bucket", "hour" if view == "overview" else "day")
        if bucket not in {"minute", "hour", "day"}:
            raise ValueError("统计粒度必须为 minute/hour/day")
        if ("from" in query) != ("to" in query):
            raise ValueError("from/to 必须同时提供")
        if "from" in query:
            start, end = float(query["from"]), float(query["to"])
        else:
            days = 7 if view == "models" else 14 if view == "sessions" else 1
            start = (datetime.fromtimestamp(stamp, zone).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)).timestamp()
            end = stamp
        maximum = {"minute": 86400, "hour": 31 * 86400, "day": 366 * 86400}[bucket]
        if not all(math.isfinite(value) and value >= 0 for value in (start, end)) or not 0 < end - start <= maximum + 3600:
            raise ValueError("统计时间窗口无效或超过允许跨度")
        if bucket != "day" and end - start > maximum:
            raise ValueError("统计时间窗口超过允许跨度")
        return cls(start, end, zone_name, cast(Literal["minute", "hour", "day"], bucket))

    def payload(self) -> dict[str, JsonValue]:
        """
        导出明确的时间窗口

        返回:
        - API 公共 window 字段
        """
        return {"from": self.start, "to": self.end, "tz": self.timezone, "bucket": self.bucket}

    def buckets(self) -> list[tuple[float, float]]:
        """
        构造有界时间桶, 夏令时的重复小时保留不同 UTC 身份

        返回:
        - 与请求窗口相交的桶边界
        """
        if self.bucket is None:
            raise ValueError("活动窗口没有统计时间桶")
        zone = ZoneInfo(self.timezone)
        local = datetime.fromtimestamp(self.start, zone)
        rounded = local.replace(second=0, microsecond=0)
        if self.bucket != "minute":
            rounded = rounded.replace(minute=0)
        if self.bucket == "day":
            rounded = rounded.replace(hour=0)
        start = rounded.timestamp()
        values: list[tuple[float, float]] = []
        while start < self.end:
            end = ((datetime.fromtimestamp(start, zone).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
                   if self.bucket == "day" else start + (60 if self.bucket == "minute" else 3600))
            values.append((max(start, self.start), min(end, self.end)))
            start = end
        return values
