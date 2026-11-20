"""
健康和轻量观测的公开协议

定义固定的服务, 计量与设置结构, 并校验 IANA 时区窗口和输出预算
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal, TypedDict, cast
import math
import time

from satrap.core.type.json_value import JsonValue


Service = Literal["control", "backend", "chat", "local"]
SourceState = Literal["available", "stale", "offline", "auth_required", "error"]
Purpose = Literal["conversation", "summary", "approval", "other"]


class ServiceHealth(TypedDict):
    """当前服务自己的运行身份, 不代替依赖服务的状态"""
    service: Service
    """生成快照的真实服务身份, 为 control, backend, chat 或 local"""
    running: bool
    """当前服务自身是否运行, 由宿主传入, 不代表依赖服务在线"""
    healthy: bool
    """当前服务自身是否健康, 由宿主传入, 不代替依赖探测"""
    runtime_id: str
    """服务启动时生成的 UUID 标识, 同一运行生命周期内不变"""
    pid: int
    """服务构造时记录的操作系统进程号"""
    started_at: float
    """服务启动的 Unix 墙钟时间戳, 单位秒"""
    uptime_seconds: float
    """基于单调时钟计算的服务运行时长, 单位秒"""
    generated_at: float
    """生成本次快照的 Unix 墙钟时间戳, 单位秒"""
    python: str
    """服务进程启动时记录的 Python 版本"""
    platform: str
    """服务进程启动时记录的运行平台描述"""
    version: str | None
    """已安装的 Satrap 发布版本, 无法确认时为 None"""
    revision: str | None
    """构建环境或 Git 中确认的提交修订, 无法确认时为 None"""


class ServiceProbe(TypedDict):
    """Control 对依赖服务的一次有界往返探测"""
    target_service: Service
    """被 Control 探测的依赖服务身份"""
    measured_at: float
    """探测开始时的 Unix 墙钟时间戳, 单位秒"""
    latency_ms: float | None
    """可达时按单调时钟测得的往返耗时, 单位毫秒; 不可达时为 None"""
    state: SourceState
    """来源状态: available, stale, offline, auth_required 或 error"""


class MessageCounts(TypedDict):
    """已知范围内的消息和模型请求计数"""
    inbound: int | None
    """已准入且按平台消息身份去重的入站消息数, 未观测范围为 None"""
    outbound: int | None
    """确认发送且按平台消息身份去重的出站消息数, 未观测范围为 None"""
    model_requests: int | None
    """实际 SDK 模型调用次数, 不把 Token 数或展示变体计作消息; 未观测时为 None"""
    commands: int | None
    """识别为命令的入站消息数, 同一消息重复识别不重复计数; 未观测时为 None"""


class UsageTotals(TypedDict):
    """调用分母与已报告用量分开, 缺失 usage 不填零"""
    calls: int
    """窗口内去重后的实际模型 SDK 调用总数, 包含成功, 失败和取消"""
    succeeded: int
    """终态为 succeeded 的实际调用次数"""
    failed: int
    """终态为 failed 的实际调用次数, 包含远端失败或不完整结果"""
    cancelled: int
    """终态为 cancelled 的实际调用次数, 包含提前关闭的响应流"""
    usage_reported_calls: int
    """至少报告一种 Token 用量的调用数, 不是所有字段均已报告的调用数"""
    usage_missing_calls: int
    """四种 Token 用量均未报告的调用数, 不作为零用量推断"""
    input_tokens: int | None
    """已报告的输入 Token 之和; 有调用但无人报告时为 None, 无调用时为 0"""
    output_tokens: int | None
    """已报告的输出 Token 之和; 有调用但无人报告时为 None, 无调用时为 0"""
    cached_tokens: int | None
    """已报告的缓存命中 Token 之和; 有调用但无人报告时为 None, 无调用时为 0"""
    total_tokens: int | None
    """已报告的总 Token 之和, 不从其他字段推算; 有调用但无人报告时为 None"""
    token_reported_calls: dict[str, int]
    """按四种 Token 字段分别统计有报告值的调用数, 供判断各总量覆盖"""
    duration_p50_ms: float | None
    """调用耗时的第 50 百分位, 单位毫秒; 无数据或无法确定上界时为 None"""
    duration_p95_ms: float | None
    """调用耗时的第 95 百分位, 单位毫秒; 无数据或无法确定上界时为 None"""
    duration_method: Literal["exact", "histogram"]
    """耗时分位数算法, exact 为精确值, histogram 为桶上界近似; 当前查询使用 histogram"""


@dataclass(frozen=True)
class TelemetrySettings:
    """不包含正文的观测设置, 与高级 Debug 开关独立"""
    usage_enabled: bool = True
    """是否接收模型调用用量采集, 默认 True; 关闭期间记录覆盖缺口"""
    activity_enabled: bool = True
    """是否接收活动摘要采集, 默认 True; 关闭期间记录覆盖缺口"""
    usage_retention_days: int = 30
    """模型调用及消息明细的保留天数, 默认 30, 允许 1 到 3660"""
    activity_retention_days: int = 30
    """活动摘要明细的保留天数, 默认 30, 允许 1 到 3660"""
    aggregate_retention_days: int = 180
    """统计汇总及上下文占用等记录的保留天数, 默认 180, 允许 1 到 3660"""
    max_service_mb: int = 128
    """单服务统计库空间预算, 十进制 MB, 默认 128, 允许 16 到 4096; 超限可提前清理"""

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
    """宿主可信来源的适配器实例 ID, 无平台的本地调用为 local, 默认空串"""
    self_id: str = ""
    """可信平台来源中的机器人账号, 未绑定时默认为空串"""
    conversation_kind: str = ""
    """宿主确认的会话类别, 可由来源推断 group 或 private, 默认空串"""
    target_id: str = ""
    """可信来源中的聊天目标 ID, 如群号或私聊对象, 默认空串"""
    agent: str = ""
    """宿主实际执行的命名 Agent 配置身份, 未绑定时默认为空串"""
    session_id: str = ""
    """宿主实际执行的持久会话实例 ID, 未绑定时默认为空串"""
    context_id: str = ""
    """可信来源的会话上下文 ID, 未绑定时为空串, 不参与汇总维度"""
    request_id: str = ""
    """可信来源的逻辑请求 ID, 未绑定时为空串, 不参与汇总维度"""
    model_config: str = ""
    """宿主传入的逻辑模型配置名, 调用边界可覆盖, 默认空串"""
    purpose: Purpose = "conversation"
    """调用用途, 为 conversation, summary, approval 或 other, 默认 conversation"""


@dataclass(frozen=True)
class ModelCall:
    """一次 SDK 调用的终态统计, 不含提示词或响应正文"""
    call_id: str
    """每次真实 SDK 调用由观察器生成的 UUID, 用于持久化去重"""
    scope: MetricScope
    """调用开始时冻结的宿主可信归属及用途"""
    model: str
    """最终 SDK 请求中的实际模型名, 缺失时为空串"""
    provider: str
    """配置 API 基址提取的主机名, 不保存凭据或路径; 未知时为空串"""
    started_at: float
    """SDK 调用开始的 Unix 墙钟时间戳, 单位秒, 统计窗口以此归桶"""
    finished_at: float
    """调用或响应流结算的 Unix 墙钟时间戳, 单位秒"""
    duration_ms: float
    """从调用开始到终态的单调时钟耗时, 单位毫秒"""
    status: Literal["succeeded", "failed", "cancelled"]
    """真实调用终态, 为 succeeded, failed 或 cancelled"""
    input_tokens: int | None = None
    """响应报告的输入 Token 数, 未报告时默认 None, 不推断为零"""
    output_tokens: int | None = None
    """响应报告的输出 Token 数, 未报告时默认 None, 不推断为零"""
    cached_tokens: int | None = None
    """响应报告的缓存命中 Token 数, 未报告时默认 None"""
    total_tokens: int | None = None
    """响应报告的总 Token 数, 未报告时默认 None, 不从分项推算"""
    context_window: int | None = None
    """调用配置声明的上下文窗口 Token 上限, 未配置时默认 None"""


@dataclass(frozen=True)
class MessageMetric:
    """可去重的消息计数, 不保存消息内容"""
    message_id: str
    """真实平台消息或 Chat 轮次 ID, 与平台, 账号和会话目标组合去重"""
    scope: MetricScope
    """宿主核验的消息归属, 不包含消息正文"""
    occurred_at: float
    """实际接收时间或采集时墙钟, Unix 时间戳, 单位秒"""
    direction: Literal["inbound", "outbound"]
    """inbound 为准入消息, outbound 为确认发送; 同一身份出站优先"""
    command: bool = False
    """是否识别为命令消息, 默认 False; 入站消息可在重复采集时补记"""


@dataclass(frozen=True)
class ActivityEvent:
    """完成业务提交后的活动摘要, 引用固定业务身份"""
    event_id: str
    """由服务与稳定业务键计算的摘要或新生成的 UUID, 用于活动去重和合并"""
    occurred_at: float
    """业务提交后采集活动的 Unix 墙钟时间戳, 单位秒"""
    kind: str
    """宿主定义的固定业务事件类别, 如 approval.pending 或 session.completed"""
    tone: Literal["info", "warning", "error"]
    """展示级别, 为 info, warning 或 error"""
    title: str
    """宿主生成的简短活动标题, 持久化时最多保留 240 字符"""
    scope: MetricScope
    """业务提交时固定的平台, 账号和会话等可信归属"""
    reference: dict[str, JsonValue] | None = None
    """可选固定业务对象引用, 默认 None, 不接受任意跳转 URL"""
    count: int = 1
    """本条摘要代表的事件数, 默认 1; 同一会话完成桶可累计"""


@dataclass(frozen=True)
class MetricWindow:
    """经时区和跨度校验的左闭右开统计窗口"""
    start: float
    """窗口左闭起点, Unix 时间戳, 单位秒"""
    end: float
    """窗口右开终点, Unix 时间戳, 单位秒; 必须晚于起点"""
    timezone: str
    """用于默认日界线和分桶的 IANA 时区名, 查询默认 Asia/Shanghai"""
    bucket: Literal["minute", "hour", "day"] | None
    """统计分桶粒度, 活动查询不分桶时为 None"""

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
