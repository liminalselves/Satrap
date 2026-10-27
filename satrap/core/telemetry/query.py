"""
有界观测查询与服务聚合

归并计数和直方图并说明覆盖范围, 只读取统计元数据, 不还原模型或消息正文
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
import sqlite3
import base64
from bisect import bisect_right
from typing import TypedDict, cast
import json
import math
import time

from .contracts import JsonValue, MessageCounts, MetricWindow, UsageTotals
from .store import COUNTERS, HISTOGRAM_KEYS, HISTOGRAM_MS, TelemetryStore, json_text


FILTERS = ("platform_id", "model_config", "agent", "session_id", "purpose")
MODEL_KEYS = ("model_config", "model", "provider")
SESSION_KEYS = ("platform_id", "agent", "session_id")


class ContextUsageSummary(TypedDict):
    """最新一次实际主模型请求的上下文占用"""

    used: int | None
    window: int | None
    as_of: float
    model_config: str
    source: str


class SessionMetricSummary(TypedDict):
    """实例目录按需请求的有界用量摘要, 不附带趋势"""

    usage: UsageTotals | None
    context: ContextUsageSummary | None
    window: dict[str, JsonValue]
    coverage: dict[str, JsonValue]


def batch_session_metrics(store: TelemetryStore, identities: list[tuple[str, str]], include: set[str]) -> dict[tuple[str, str], SessionMetricSummary]:
    """
    一遍读取元数据并批量补齐实例摘要, 不逐项访问历史或模型正文

    参数:
    - store: 当前服务独立统计库
    - identities: 当前已加载目录的完整平台和实例身份, 最多 1000 项
    - include: usage 与 context 的显式选择

    返回:
    - 身份对应的最近 24 小时用量和最新上下文占用, 未观测时保持为空
    """
    wanted = set(identities)
    if len(wanted) > 1000 or include - {"usage", "context"}:
        raise ValueError("摘要最多支持 1000 个实例和 usage/context, 更大范围请使用统计分页接口")
    now = time.time()
    query = MetricQuery.parse({"from": str(now - 86400), "to": str(now), "bucket": "hour"}, "overview")
    source = coverage(store, query)
    counters: dict[tuple[str, str], dict[str, int]] = {}
    contexts: dict[tuple[str, str], ContextUsageSummary] = {}
    if wanted and store.path.is_file():
        with store.connect() as connection:
            if "usage" in include:
                for _, dimensions, values in contributions(connection, query):
                    key = (dimensions.get("platform_id", ""), dimensions.get("session_id", ""))
                    if key in wanted:
                        _add(counters.setdefault(key, {}), values)
            if "context" in include:
                ordered = sorted(wanted)
                for start in range(0, len(ordered), 400):
                    batch = ordered[start:start + 400]
                    parameters = ",".join("(?,?)" for _ in batch)
                    for row in connection.execute("WITH wanted(platform_id,session_id) AS (VALUES " + parameters + ") "
                                                   "SELECT c.* FROM context_occupancy c JOIN wanted w USING(platform_id,session_id)",
                                                   [value for item in batch for value in item]):
                        key = (row["platform_id"], row["session_id"])
                        if key not in contexts or row["as_of"] >= contexts[key]["as_of"]:
                            contexts[key] = ContextUsageSummary(used=row["used"], window=row["window"], as_of=row["as_of"], model_config=row["model_config"],
                                                                source="reported" if row["used"] is not None else "unknown")
    return {key: SessionMetricSummary(usage=usage_totals(counters.get(key, {})) if source["as_of"] is not None and "usage" in include else None,
                                      context=contexts.get(key), window=query.window.payload(), coverage=source) for key in wanted}


@dataclass(frozen=True)
class MetricQuery:
    """绑定窗口和过滤条件的稳定分页查询"""
    view: str
    window: MetricWindow
    filters: dict[str, str]
    limit: int
    after: tuple[str, ...] = ()

    @classmethod
    def parse(cls, values: Mapping[str, str], view: str, *, now: float | None = None) -> MetricQuery:
        """
        校验查询并恢复游标原窗口, 翻页不改变默认截止时间

        参数:
        - values: 已拒绝重复参数的查询
        - view: overview/models/sessions
        - now: 可注入时间

        返回:
        - 绑定过滤和窗口的查询, 游标跨范围复用时拒绝
        """
        if view not in {"overview", "models", "sessions"}:
            raise ValueError("统计视图无效")
        if set(values) - {*FILTERS, "from", "to", "tz", "bucket", "limit", "cursor", "view"}:
            raise ValueError("统计查询包含未知参数")
        filters = {key: value for key, value in values.items() if key in FILTERS and value and not (key == "purpose" and value == "all")}
        if any(len(value) > 512 for value in filters.values()) or filters.get("purpose", "conversation") not in {"conversation", "summary", "approval", "other"}:
            raise ValueError("统计过滤条件无效")
        limit = int(values.get("limit", "50"))
        if not 1 <= limit <= 100:
            raise ValueError("统计分页 limit 必须为 1-100")
        parameters = dict(values)
        cursor: dict[str, object] | None = None
        if values.get("cursor"):
            try:
                raw = values["cursor"]
                if len(raw) > 4096:
                    raise ValueError("游标过长")
                decoded: object = json.loads(base64.urlsafe_b64decode(raw))
                if not isinstance(decoded, dict):
                    raise ValueError("游标必须为对象")
                cursor = cast(dict[str, object], decoded)
                if cursor.get("view") != view or cursor.get("filters") != filters:
                    raise ValueError("游标不属于当前筛选")
                stored = cursor["window"]
                if not isinstance(stored, dict):
                    raise ValueError("游标窗口无效")
                stored = cast(dict[str, object], stored)
                for name in ("from", "to", "tz", "bucket"):
                    if name in values and str(stored.get(name)) != values[name]:
                        if name not in {"from", "to"} or float(values[name]) != stored.get(name):
                            raise ValueError("游标窗口已变化")
                    parameters[name] = str(stored[name])
            except (KeyError, TypeError, ValueError, UnicodeError) as error:
                raise ValueError("统计游标无效或查询范围变化") from error
        window = MetricWindow.parse(parameters, view, now=now)
        raw_keys: object = cursor.get("after") if cursor else []
        if not isinstance(raw_keys, list):
            raise ValueError("统计游标排序键无效")
        keys = cast(list[object], raw_keys)
        if any(not isinstance(key, str) or len(key) > 512 for key in keys) or (keys and len(keys) != 3):
            raise ValueError("统计游标排序键无效")
        if len(window.buckets()) * limit > 10000 and view != "overview":
            raise ValueError("趋势输出超过 10000 桶, 请缩小时间窗口或分页条数")
        return cls(view, window, filters, limit, tuple(cast(list[str], keys)))

    def cursor(self, keys: tuple[str, ...]) -> str:
        """
        将排序键与完整查询绑定

        参数:
        - keys: 最后一项的固定维度键

        返回:
        - 不透明有界游标
        """
        value: dict[str, object] = {"view": self.view, "window": self.window.payload(), "filters": self.filters, "after": keys}
        return base64.urlsafe_b64encode(json_text(value).encode("utf-8")).decode("ascii")


@dataclass(frozen=True)
class ActivityQuery:
    """将活动翻页绑定首次确定的窗口与固定过滤"""
    window: MetricWindow
    filters: dict[str, str]
    limit: int
    after: tuple[float, str, str] | None = None
    view: str = "activity"

    @classmethod
    def parse(cls, values: Mapping[str, str], *, now: float | None = None) -> ActivityQuery:
        """
        校验活动范围, 游标不能改变筛选或推进截止时间

        参数:
        - values: 已拒绝重复参数的查询
        - now: 可注入的当前时间

        返回:
        - 默认最近 24 小时且最多 31 天的活动查询
        """
        if set(values) - {"from", "to", "kind", "tone", "platform_id", "limit", "cursor", "view", "tz"}:
            raise ValueError("活动查询包含未知参数")
        if values.get("view", "activity") != "activity":
            raise ValueError("活动查询视图无效")
        filters = {key: values[key] for key in ("kind", "tone", "platform_id") if values.get(key)}
        if filters.get("tone", "info") not in {"info", "warning", "error"} or any(len(value) > 512 for value in filters.values()):
            raise ValueError("活动筛选无效")
        limit = int(values.get("limit", "50"))
        if not 1 <= limit <= 100:
            raise ValueError("活动 limit 必须为 1-100")
        params = {key: values[key] for key in ("from", "to", "tz") if key in values}
        after: tuple[float, str, str] | None = None
        if values.get("cursor"):
            try:
                raw = values["cursor"]
                if len(raw) > 4096:
                    raise ValueError("活动游标过长")
                decoded: object = json.loads(base64.urlsafe_b64decode(raw))
                if not isinstance(decoded, dict):
                    raise ValueError("活动游标必须为对象")
                saved = cast(dict[str, object], decoded)
                if saved.get("filters") != filters or saved.get("view") != "activity":
                    raise ValueError("活动游标不属于当前筛选")
                window, key = saved["window"], saved["after"]
                if not isinstance(window, dict) or not isinstance(key, list):
                    raise ValueError("活动游标格式无效")
                window = cast(dict[str, str | float], window)
                key = cast(list[object], key)
                if (len(key) != 3 or not isinstance(key[0], (int, float)) or isinstance(key[0], bool) or not math.isfinite(key[0])
                        or any(not isinstance(part, str) or len(part) > 512 for part in key[1:])):
                    raise ValueError("活动游标格式无效")
                for name in ("from", "to", "tz"):
                    if name in params and (float(window[name]) != float(params[name]) if name in {"from", "to"} else str(window[name]) != params[name]):
                        raise ValueError("活动游标窗口已变化")
                    params[name] = str(window[name])
                after = (float(key[0]), cast(str, key[1]), cast(str, key[2]))
            except (ValueError, TypeError, KeyError, UnicodeError) as error:
                raise ValueError("活动游标无效或筛选已变化") from error
        if "from" not in params and "to" not in params:
            stamp = time.time() if now is None else now
            params.update({"from": str(stamp - 86400), "to": str(stamp)})
        window = replace(MetricWindow.parse({**params, "bucket": "hour"}, now=now), bucket=None)
        return cls(window, filters, limit, after)

    def cursor(self, keys: tuple[float, str, str]) -> str:
        """
        保存最后一项的稳定排序键和当前查询

        参数:
        - keys: 负时间, 事件 ID 和服务身份

        返回:
        - 不包含业务凭据的有界游标
        """
        return base64.urlsafe_b64encode(json_text({"view": "activity", "window": self.window.payload(),
                                                  "filters": self.filters, "after": keys}).encode()).decode("ascii")


def usage_totals(counters: Mapping[str, int]) -> UsageTotals:
    """
    从可合并计数和直方图还原公开用量摘要

    参数:
    - counters: 当前查询范围的计数

    返回:
    - 缺失用量保持 null, 分位数明确标记近似方法
    """
    count = counters.get("calls", 0)
    reported = counters.get("usage_reported_calls", 0)
    def percentile(ratio: float) -> float | None:
        """
        选择累计计数到达分位点的桶上界

        参数:
        - ratio: 0-1 范围内的目标分位

        返回:
        - 可确定上界, 无数据或落入开放尾桶时为 None
        """
        threshold, reached = math.ceil(count * ratio), 0
        for index, key in enumerate(HISTOGRAM_KEYS):
            reached += counters.get(key, 0)
            if count and reached >= threshold:
                return float(HISTOGRAM_MS[index]) if index < len(HISTOGRAM_MS) else None
        return None
    return {"calls": count, "succeeded": counters.get("succeeded", 0), "failed": counters.get("failed", 0),
            "cancelled": counters.get("cancelled", 0), "usage_reported_calls": reported,
            "usage_missing_calls": counters.get("usage_missing_calls", 0),
            "input_tokens": counters.get("input_tokens", 0) if not count or counters.get("input_tokens_reported") else None,
            "output_tokens": counters.get("output_tokens", 0) if not count or counters.get("output_tokens_reported") else None,
            "cached_tokens": counters.get("cached_tokens", 0) if not count or counters.get("cached_tokens_reported") else None,
            "total_tokens": counters.get("total_tokens", 0) if not count or counters.get("total_tokens_reported") else None,
            "token_reported_calls": {name: counters.get(name + "_reported", 0) for name in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")},
            "duration_p50_ms": percentile(.5), "duration_p95_ms": percentile(.95), "duration_method": "histogram"}


def message_counts(counters: Mapping[str, int]) -> MessageCounts:
    """
    区分用户消息, 发送消息和模型请求

    参数:
    - counters: 已知范围内的合并计数

    返回:
    - 不把 Token 或展示变体当消息条数的摘要
    """
    return {"inbound": counters.get("inbound", 0), "outbound": counters.get("outbound", 0),
            "model_requests": counters.get("calls", 0), "commands": counters.get("commands", 0)}


def _add(target: dict[str, int], source: Mapping[str, int]) -> None:
    """
    合并固定计数, 不平均多个分位数

    参数:
    - target: 当前累加器
    - source: 当前贡献
    """
    for key in (*COUNTERS, *HISTOGRAM_KEYS):
        target[key] = target.get(key, 0) + source.get(key, 0)


def _where(filters: Mapping[str, str], column: str) -> tuple[str, list[str]]:
    """
    只允许固定维度参与参数化 SQL

    参数:
    - filters: 已校验过滤
    - column: 框架提供的 JSON 列名

    返回:
    - SQL 条件和占位参数
    """
    return "".join(f" AND json_extract({column},'$.{key}')=?" for key in filters), list(filters.values())


def contributions(db: sqlite3.Connection, query: MetricQuery) -> Iterator[tuple[float, dict[str, str], dict[str, int]]]:
    """
    一次顺序读取当前窗口元数据, 边界分钟从明细精确过滤

    参数:
    - db: 当前只读快照连接
    - query: 已校验窗口及过滤

    返回:
    - 时间, 维度和固定计数; 不读取任何消息或模型正文
    """
    start, end = query.window.start, query.window.end
    interior_start, interior_end = math.ceil(start / 60) * 60, math.floor(end / 60) * 60
    condition, args = _where(query.filters, "dimensions_json")
    if query.view != "overview":
        condition += " AND json_extract(counters_json,'$.calls')>0"
    cursor = db.execute("SELECT bucket,dimensions_json,counters_json FROM metric_buckets WHERE granularity=60 AND bucket>=? AND bucket<?" + condition,
                        (interior_start, interior_end, *args))
    while rows := cursor.fetchmany(512):
        for row in rows:
            yield float(row[0]), json.loads(row[1]), json.loads(row[2])
    if interior_start >= interior_end:
        edges = [(start, end)]
    else:
        edges = [(start, min(interior_start, end)), (max(interior_end, start), end)]
    for low, high in edges:
        if low >= high:
            continue
        condition, args = _where(query.filters, "scope_json")
        for row in db.execute("SELECT * FROM model_calls WHERE started_at>=? AND started_at<?" + condition, (low, high, *args)):
            dimensions = {key: value for key, value in json.loads(row["scope_json"]).items() if key not in {"request_id", "context_id"}}
            dimensions.update(model=row["model"], provider=row["provider"])
            usage = json.loads(row["usage_json"])
            reported = any(value is not None for value in usage.values())
            counts: dict[str, int] = {"calls": 1, row["status"]: 1, "usage_reported_calls": int(reported), "usage_missing_calls": int(not reported),
                      **{key: value for key, value in usage.items() if isinstance(value, int)},
                      **{key + "_reported": int(isinstance(value, int)) for key, value in usage.items()},
                      HISTOGRAM_KEYS[next((index for index, bound in enumerate(HISTOGRAM_MS) if row["duration_ms"] <= bound), len(HISTOGRAM_MS))]: 1}
            yield row["started_at"], dimensions, counts
        if query.view != "overview":
            continue
        for row in db.execute("SELECT * FROM message_events WHERE occurred_at>=? AND occurred_at<?" + condition, (low, high, *args)):
            dimensions = {key: value for key, value in json.loads(row["scope_json"]).items() if key not in {"request_id", "context_id"}}
            dimensions.update(model="", provider="")
            yield row["occurred_at"], dimensions, {row["direction"]: 1, "commands": row["command"]}


def coverage(store: TelemetryStore, query: MetricQuery | ActivityQuery) -> dict[str, JsonValue]:
    """
    从采集租期和缺口判断实际覆盖, 缺少库不初始化空表

    参数:
    - store: 当前服务独立库
    - query: 当前窗口

    返回:
    - 当前来源覆盖, 不把未观测范围伪装成零
    """
    value: dict[str, JsonValue] = {"service": store.service, "state": "available", "as_of": None, "collection_started_at": None,
                                  "reason_code": "not_collected", "complete": False, "gaps": [], "gaps_truncated": False, "dropped_events": 0}
    if not store.path.is_file():
        return value
    with store.connect() as db:
        runs = db.execute("SELECT * FROM collection_runs WHERE started_at<? ORDER BY started_at", (query.window.end,)).fetchall()
        if not runs:
            return value
        value.update(as_of=max(row["as_of"] for row in runs), collection_started_at=runs[0]["started_at"], reason_code=None)
        gaps: list[JsonValue] = []
        bounds = {row["dataset"]: row for row in db.execute("SELECT * FROM retention_bounds")}
        required = ["activity_events"] if query.view == "activity" else ["metric_buckets"]
        if query.view != "activity" and (query.window.start % 60 or query.window.end % 60):
            required.extend(("model_calls", "message_events"))
        for dataset in required:
            bound = bounds.get(dataset)
            if bound is not None and bound["retained_from"] > query.window.start:
                gaps.append({"from": query.window.start, "to": min(query.window.end, bound["retained_from"]), "reason_code": bound["reason_code"]})
        covered_to = query.window.start
        for row in runs:
            if row["started_at"] > covered_to:
                gaps.append({"from": covered_to, "to": min(row["started_at"], query.window.end), "reason_code": "not_collected"})
            covered_to = max(covered_to, row["as_of"])
            if row["as_of"] < query.window.start:
                continue
            value["dropped_events"] = cast(int, value["dropped_events"]) + row["dropped_events"]
            for gap in json.loads(row["gaps_json"]):
                ignored = "usage_disabled" if query.view == "activity" else "activity_disabled"
                if gap["from"] <= query.window.end and gap["to"] >= query.window.start and gap["reason_code"] != ignored:
                    gaps.append(gap)
            if row["ended_at"] and not row["closed"]:
                gaps.append({"from": row["as_of"], "to": row["ended_at"], "reason_code": "unclean_shutdown"})
        if covered_to < query.window.end:
            gaps.append({"from": max(query.window.start, covered_to), "to": query.window.end, "reason_code": "not_collected"})
        value.update(gaps=gaps[:100], gaps_truncated=len(gaps) > 100, complete=not gaps and not value["dropped_events"])
        return value


def local_activity(store: TelemetryStore, query: ActivityQuery) -> dict[str, JsonValue]:
    """
    以 SQL 游标读取一个服务的活动, 只解析当前页摘要

    参数:
    - store: 当前服务的独立库
    - query: 固定窗口和过滤

    返回:
    - 当前来源的有界活动页与覆盖信息
    """
    source = coverage(store, query)
    items: list[JsonValue] = []
    if store.path.is_file():
        conditions = ["occurred_at>=?", "occurred_at<?"]
        args: list[object] = [query.window.start, query.window.end]
        for key, value in query.filters.items():
            conditions.append(("json_extract(scope_json,'$.platform_id')" if key == "platform_id" else key) + "=?")
            args.append(value)
        if query.after is not None:
            conditions.append("occurred_at<=?")
            args.append(-query.after[0])
            conditions.append("(-occurred_at,event_id,?)>(?,?,?)")
            args.extend((store.service, *query.after))
        with store.connect() as db:
            rows = db.execute("SELECT * FROM activity_events WHERE " + " AND ".join(conditions)
                              + " ORDER BY occurred_at DESC,event_id LIMIT ?", (*args, query.limit + 1)).fetchall()
        for row in rows:
            scope = json.loads(row["scope_json"])
            identity = {name: scope.get(name) for name in ("platform_id", "self_id", "conversation_kind", "target_id")}
            obj = identity if all(identity.values()) and identity["conversation_kind"] in {"group", "private", "chat"} else None
            items.append({"event_id": row["event_id"], "occurred_at": row["occurred_at"], "service": store.service,
                          "kind": row["kind"], "tone": row["tone"], "title": row["title"], "object": obj,
                          "count": row["event_count"] if "event_count" in row.keys() else 1,
                          "request_id": scope.get("request_id") or None, "ref": json.loads(row["ref_json"]) if row["ref_json"] else None})
    return {"generated_at": time.time(), "window": query.window.payload(), "complete": source["complete"], "sources": [source],
            "warnings": [], "items": items[:query.limit], "has_more": len(items) > query.limit,
            "next_cursor": query.cursor(activity_key(items[query.limit - 1])) if len(items) > query.limit else None}


def activity_key(item: JsonValue) -> tuple[float, str, str]:
    """
    验证服务传回的活动并取得全局稳定排序键

    参数:
    - item: 当前事件的公开 JSON

    返回:
    - 时间倒序, ID 和服务正序的键, 格式损坏时拒绝来源
    """
    if not isinstance(item, dict):
        raise ValueError("活动来源的记录无效")
    stamp, identity, service = item.get("occurred_at"), item.get("event_id"), item.get("service")
    if (type(stamp) not in {int, float} or not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
            or not isinstance(identity, str) or not 1 <= len(identity) <= 512
            or service not in {"control", "backend", "chat", "local"}
            or not isinstance(service, str) or item.get("tone") not in {"info", "warning", "error"}
            or not isinstance(item.get("title"), str) or not isinstance(item.get("kind"), str)):
        raise ValueError("活动来源的记录格式无效")
    return (-float(stamp), identity, service)


def merge_activity(query: ActivityQuery, responses: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """
    合并三个已鉴权服务的有限候选, 保留失败来源的覆盖信息

    参数:
    - query: 全局稳定窗口
    - responses: 不递归聚合的服务贡献

    返回:
    - 一页活动及下一页游标, 不接受外部跳转 URL
    """
    sources: list[JsonValue] = []
    warnings: list[JsonValue] = []
    candidates: dict[tuple[float, str, str], JsonValue] = {}
    upstream_more = False
    for response in responses:
        entries, source = response.get("items"), response.get("sources")
        if response.get("window") != query.window.payload() or not isinstance(entries, list) or len(entries) > query.limit or not isinstance(source, list):
            raise ValueError("活动来源窗口或分页无效")
        sources.extend(source)
        upstream_more = upstream_more or response.get("has_more") is True
        messages = response.get("warnings", [])
        if not isinstance(messages, list):
            raise ValueError("活动来源告警无效")
        warnings.extend(messages)
        for item in entries:
            key = activity_key(item)
            if query.after is not None and key <= query.after:
                raise ValueError("活动来源没有遵循当前游标")
            if not query.window.start <= -key[0] < query.window.end:
                raise ValueError("活动来源没有遵循当前窗口")
            candidates[key] = item
    keys = sorted(candidates)[:query.limit]
    has_more = upstream_more or len(candidates) > query.limit
    return {"generated_at": time.time(), "window": query.window.payload(),
            "complete": bool(sources) and all(isinstance(source, dict) and source.get("complete") is True for source in sources),
            "sources": sources, "warnings": warnings, "items": [candidates[key] for key in keys], "has_more": has_more,
            "next_cursor": query.cursor(keys[-1]) if has_more and keys else None}


def local_metrics(store: TelemetryStore, query: MetricQuery) -> dict[str, JsonValue]:
    """
    有界读取单服务统计, 趋势按当前窗口一遍归并

    参数:
    - store: 当前服务的固定存储
    - query: 明确的视图和查询

    返回:
    - 当前服务贡献及可供聚合的固定计数, 不在来源内递归查询其它服务
    """
    source = coverage(store, query)
    bounds = query.window.buckets()
    starts = [item[0] for item in bounds]
    totals: dict[str, int] = {}
    series: list[dict[str, int]] = [{} for _ in bounds]
    grouped: dict[tuple[str, ...], tuple[dict[str, int], list[dict[str, int]]]] = {}
    adapters: dict[tuple[str, str], tuple[dict[str, int], list[dict[str, int]]]] = {}
    keys = MODEL_KEYS if query.view == "models" else SESSION_KEYS
    if store.path.is_file():
        with store.connect() as db:
            for occurred_at, dimensions, counts in contributions(db, query):
                index = bisect_right(starts, occurred_at) - 1
                if not 0 <= index < len(bounds):
                    continue
                _add(totals, counts)
                _add(series[index], counts)
                if query.view == "overview":
                    key = (dimensions.get("platform_id", ""), dimensions.get("self_id", ""))
                    if key not in adapters:
                        if len(adapters) >= 1000 or (len(adapters) + 1) * len(bounds) > 10000:
                            raise ValueError("平台维度过多, 请缩小查询范围")
                        adapters[key] = ({}, [{} for _ in bounds])
                    _add(adapters[key][0], counts)
                    _add(adapters[key][1][index], counts)
                elif counts.get("calls", 0) and (query.view != "sessions" or dimensions.get("session_id")):
                    group = tuple(dimensions.get(key, "") for key in keys)
                    if query.after and group <= query.after:
                        continue
                    if group not in grouped:
                        grouped[group] = ({}, [{} for _ in bounds])
                        if len(grouped) > query.limit + 1:
                            grouped.pop(max(grouped))
                        if group not in grouped:
                            continue
                    _add(grouped[group][0], counts)
                    _add(grouped[group][1][index], counts)

    def bucket_payload(values: list[dict[str, int]]) -> list[JsonValue]:
        """
        生成当前已明确边界的趋势

        参数:
        - values: 每个桶的固定计数

        返回:
        - 有界公开趋势, 附带供上级合并的内部计数
        """
        return [{"from": bound[0], "to": bound[1], "messages": cast(dict[str, JsonValue], dict(message_counts(counts))),
                 "usage": cast(dict[str, JsonValue], dict(usage_totals(counts))), "counters": dict(counts)} for bound, counts in zip(bounds, values)]

    result: dict[str, JsonValue] = {"generated_at": time.time(), "window": query.window.payload(), "complete": source["complete"],
                                   "sources": [source], "warnings": [], "counters": dict(totals),
                                   "messages": cast(dict[str, JsonValue], dict(message_counts(totals))),
                                   "usage": cast(dict[str, JsonValue], dict(usage_totals(totals))), "series": bucket_payload(series)}
    if query.view == "overview":
        result["by_adapter"] = [{"platform_id": key[0], "self_id": key[1], "messages": cast(dict[str, JsonValue], dict(message_counts(value[0]))),
                                  "series": bucket_payload(value[1])} for key, value in sorted(adapters.items())]
        zone = ZoneInfo(query.window.timezone)
        previous_window = MetricWindow((datetime.fromtimestamp(query.window.start, zone) - timedelta(days=1)).timestamp(),
                                       (datetime.fromtimestamp(query.window.end, zone) - timedelta(days=1)).timestamp(),
                                       query.window.timezone, query.window.bucket)
        previous_query = MetricQuery("overview", previous_window, query.filters, query.limit)
        previous: dict[str, int] = {}
        if store.path.is_file():
            with store.connect() as db:
                for _, _, counts in contributions(db, previous_query):
                    _add(previous, counts)
        result["baseline"] = {"window": previous_window.payload(), "counters": dict(previous), "source": coverage(store, previous_query)}
    else:
        selected = sorted(grouped)[:query.limit]
        contexts: dict[tuple[str, ...], dict[str, JsonValue]] = {}
        if query.view == "sessions" and selected and store.path.is_file():
            with store.connect() as db:
                placeholders = ",".join("(?,?,?)" for _ in selected)
                for row in db.execute("WITH wanted(platform_id,agent,session_id) AS (VALUES " + placeholders + ") "
                                      "SELECT c.* FROM context_occupancy c JOIN wanted w USING(platform_id,agent,session_id)",
                                      [value for key in selected for value in key]):
                    contexts[(row["platform_id"], row["agent"], row["session_id"])] = {
                        "used": row["used"], "window": row["window"], "source": "reported" if row["used"] is not None else "unknown",
                        "as_of": row["as_of"], "model_config": row["model_config"]}
        rows: list[JsonValue] = []
        for key in selected:
            counts, trend = grouped[key]
            item: dict[str, JsonValue] = {**dict(zip(keys, key)), "usage": cast(dict[str, JsonValue], dict(usage_totals(counts))),
                                          "trend": bucket_payload(trend), "counters": dict(counts)}
            if query.view == "sessions":
                item["agent"] = key[1] or None
                item["context"] = contexts.get(key, {"used": None, "window": None, "source": "unknown", "as_of": None, "model_config": None})
            else:
                item["model_config"], item["provider"] = key[0] or None, key[2] or None
            rows.append(item)
        has_more = len(grouped) > query.limit
        result.update({"models" if query.view == "models" else "sessions": rows, "has_more": has_more,
                       "next_cursor": query.cursor(selected[-1]) if has_more and selected else None})
    return result


def _counts(value: JsonValue) -> dict[str, int]:
    """
    校验服务响应中的固定计数, 不接受任意结构参与计算

    参数:
    - value: 统计来源返回的计数对象

    返回:
    - 固定字段整数计数, 响应损坏时拒绝
    """
    if not isinstance(value, dict) or any(key not in {*COUNTERS, *HISTOGRAM_KEYS} or type(item) is not int or item < 0 for key, item in value.items()):
        raise ValueError("统计来源计数格式无效")
    return cast(dict[str, int], value)


def merge_metrics(query: MetricQuery, responses: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """
    合并同窗口来源, 使用原始计数与直方图而不是累加百分位

    参数:
    - query: Control 已确定的查询窗口
    - responses: 已确认归属和响应结构的来源

    返回:
    - 面向界面的公共契约, 不泄露内部合并计数
    """
    totals: dict[str, int] = {}
    bounds = query.window.buckets()
    indexes = {bound[0]: index for index, bound in enumerate(bounds)}
    series: list[dict[str, int]] = [{} for _ in bounds]
    sources: list[JsonValue] = []
    warnings: list[JsonValue] = []
    rows: dict[tuple[str, ...], dict[str, JsonValue]] = {}
    previous: dict[str, int] = {}
    baseline_window: JsonValue = None
    baseline_complete = bool(responses)
    upstream_more = False
    keys = MODEL_KEYS if query.view == "models" else SESSION_KEYS

    def merge_series(target: list[dict[str, int]], value: JsonValue) -> None:
        """
        将来源的明确时间桶加入已校验窗口

        参数:
        - target: 目标计数数组
        - value: 来源序列
        """
        if not isinstance(value, list):
            raise ValueError("统计来源缺少趋势")
        for entry in value:
            if not isinstance(entry, dict) or not isinstance(entry.get("from"), (float, int)) or entry["from"] not in indexes:
                raise ValueError("统计来源趋势窗口不一致")
            _add(target[indexes[cast(float, entry["from"])]], _counts(entry.get("counters")))

    for response in responses:
        if response.get("window") != query.window.payload() or not isinstance(response.get("sources"), list):
            raise ValueError("统计来源窗口不一致")
        sources.extend(cast(list[JsonValue], response["sources"]))
        warnings.extend(cast(list[JsonValue], response.get("warnings", [])))
        _add(totals, _counts(response.get("counters", {})))
        merge_series(series, response.get("series", []))
        if query.view == "overview":
            baseline = response.get("baseline")
            if isinstance(baseline, dict):
                if baseline_window is not None and baseline.get("window") != baseline_window:
                    raise ValueError("昨日比较窗口不一致")
                baseline_window = baseline.get("window")
                _add(previous, _counts(baseline.get("counters", {})))
                source = baseline.get("source")
                baseline_complete = baseline_complete and isinstance(source, dict) and source.get("complete") is True
            else:
                baseline_complete = False
            items = response.get("by_adapter", [])
        else:
            items = response.get("models" if query.view == "models" else "sessions", [])
            upstream_more = upstream_more or response.get("has_more") is True
        if not isinstance(items, list):
            raise ValueError("统计来源分组格式无效")
        for entry in items:
            if not isinstance(entry, dict):
                raise ValueError("统计来源分组格式无效")
            item = entry
            item_keys = ("platform_id", "self_id") if query.view == "overview" else keys
            group = tuple(str(item.get(key) or "") for key in item_keys)
            if group not in rows:
                if query.view != "overview" and len(rows) >= query.limit + 1:
                    largest = max(rows)
                    if group >= largest:
                        continue
                    rows.pop(largest)
                if len(rows) >= 1000 or (len(rows) + 1) * len(bounds) > (10000 if query.view == "overview" else 11000):
                    raise ValueError("聚合趋势输出超出预算, 请缩小查询范围")
                rows[group] = {**{key: item.get(key) for key in item_keys}, "counters": {}, "_trend": [{} for _ in bounds], "context": item.get("context")}
            target = rows[group]
            incoming = item.get("context")
            existing = target.get("context")
            incoming_time = incoming.get("as_of") if isinstance(incoming, dict) else None
            existing_time = existing.get("as_of") if isinstance(existing, dict) else None
            if (isinstance(incoming_time, (int, float))
                    and (not isinstance(existing_time, (int, float)) or incoming_time > existing_time)):
                target["context"] = incoming
            counts = cast(dict[str, int], target["counters"])
            trend = cast(list[dict[str, int]], target["_trend"])
            if query.view == "overview":
                value = item.get("series")
                if not isinstance(value, list):
                    raise ValueError("平台趋势格式无效")
                for bucket in value:
                    if isinstance(bucket, dict):
                        _add(counts, _counts(bucket.get("counters", {})))
            else:
                _add(counts, _counts(item.get("counters", {})))
            merge_series(trend, item.get("series" if query.view == "overview" else "trend", []))

    def public_series(values: list[dict[str, int]]) -> list[JsonValue]:
        """
        输出趋势, 内部直方图留在服务合并层

        参数:
        - values: 各桶计数

        返回:
        - 明确的公开消息/用量序列
        """
        return [{"from": bound[0], "to": bound[1], "messages": cast(dict[str, JsonValue], dict(message_counts(counts))),
                 "usage": cast(dict[str, JsonValue], dict(usage_totals(counts)))} for bound, counts in zip(bounds, values)]

    complete = bool(sources) and all(isinstance(source, dict) and source.get("complete") is True for source in sources)
    result: dict[str, JsonValue] = {"generated_at": time.time(), "window": query.window.payload(), "complete": complete,
                                   "sources": sources, "warnings": warnings, "messages": cast(dict[str, JsonValue], dict(message_counts(totals))),
                                   "usage": cast(dict[str, JsonValue], dict(usage_totals(totals))), "series": public_series(series)}
    if query.view == "overview":
        def comparison(current: int | None, old: int | None, comparable: bool) -> dict[str, JsonValue]:
            """
            只对覆盖一致的观测计算差值

            参数:
            - current: 当前已知计数
            - old: 昨日已知计数
            - comparable: 是否确认两段覆盖完整

            返回:
            - 缺少比较依据时不生成涨幅
            """
            delta = current - old if comparable and current is not None and old is not None else None
            return {"current": current, "previous": old, "absolute_delta": delta,
                    "percent_delta": delta * 100 / old if delta is not None and old else None}
        current_usage, previous_usage = usage_totals(totals), usage_totals(previous)
        result["comparison"] = {"mode": "yesterday_same_elapsed", "baseline_window": baseline_window,
                                 "messages": comparison(totals.get("inbound", 0), previous.get("inbound", 0), complete and baseline_complete),
                                 "tokens": comparison(current_usage["total_tokens"], previous_usage["total_tokens"], complete and baseline_complete
                                                      and totals.get("total_tokens_reported", 0) == current_usage["calls"]
                                                      and previous.get("total_tokens_reported", 0) == previous_usage["calls"])}
        result["by_adapter"] = [{"platform_id": key[0], "self_id": key[1],
                                  "messages": cast(dict[str, JsonValue], dict(message_counts(cast(dict[str, int], value["counters"])))),
                                  "series": public_series(cast(list[dict[str, int]], value["_trend"]))} for key, value in sorted(rows.items())]
    else:
        selected = sorted(rows)[:query.limit]
        public_rows: list[JsonValue] = []
        for key in selected:
            value = rows[key]
            item: dict[str, JsonValue] = {**{name: value[name] for name in keys},
                                          "usage": cast(dict[str, JsonValue], dict(usage_totals(cast(dict[str, int], value["counters"])))),
                                          "trend": public_series(cast(list[dict[str, int]], value["_trend"]))}
            if query.view == "sessions":
                item["context"] = value["context"]
            public_rows.append(item)
        has_more = upstream_more or len(rows) > query.limit
        result.update({"models" if query.view == "models" else "sessions": public_rows,
                       "has_more": has_more, "next_cursor": query.cursor(selected[-1]) if has_more and selected else None})
    return result
