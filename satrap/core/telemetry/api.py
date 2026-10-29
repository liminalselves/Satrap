"""
三服务观测 API

在各服务鉴权后解析固定查询, 用独立有界线程池读取来源和 SQLite, 不允许任意路径或 URL
"""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlsplit
from collections import OrderedDict
from dataclasses import dataclass
import threading
import asyncio
from pathlib import Path
import sqlite3
from typing import cast
from copy import deepcopy
import time

from satrap.core.utils.async_worker import BoundedAsyncWorker, WorkerBusyError
from satrap.core.storage.layout import StorageLayout
from .contracts import JsonValue, Service, SourceState
from .query import ActivityQuery, MetricQuery, local_activity, local_metrics, merge_activity, merge_metrics
from .store import SettingsConflict, SettingsStore, TelemetryStore, json_text

from satrap.core.log import logger

QUERY_WORKERS = BoundedAsyncWorker("telemetry-read", workers=2, capacity=8)
SOURCE_WORKERS = BoundedAsyncWorker("telemetry-source", workers=3, capacity=12)
SourceFetcher = Callable[[Service, str], dict[str, JsonValue]]
_CACHE: OrderedDict[tuple[Path, str], tuple[float, int, dict[str, JsonValue]]] = OrderedDict()
_CACHE_BYTES = 0
_CACHE_LOCK = threading.RLock()
_SOURCE_PROBLEMS: dict[str, str] = {}   # 每个来源最近一次已报告的问题, 状态不变时不重复告警
_SOURCE_LOCK = threading.Lock()


def _note_source(service: str, reason: str | None) -> None:
    """
    仅在来源状态变化时写日志, 离线是预期状态不按警告记录

    参数:
    - service: 统计来源服务
    - reason: 不可用原因, None 表示本次读取成功
    """
    with _SOURCE_LOCK:
        previous = _SOURCE_PROBLEMS.get(service)
        if reason == previous:
            return
        if reason is None:
            _SOURCE_PROBLEMS.pop(service, None)
        else:
            _SOURCE_PROBLEMS[service] = reason
    if reason is None:
        logger.info(f"[统计] 来源 {service} 已恢复")
    elif reason == "service_unreachable":
        logger.info(f"[统计] 来源 {service} 未运行, 使用已保存的统计快照")
    else:
        logger.warning(f"[统计] 来源 {service} 不可用: {reason}")


@dataclass
class SourceUnavailable(Exception):
    """依赖服务的确定状态, 不包含 URL 凭据或响应正文"""
    state: SourceState
    reason_code: str


def _cached(root: Path, key: str) -> dict[str, JsonValue] | None:
    """
    复用五秒内的完整聚合快照, 不在缓存中重写时间窗口

    参数:
    - root: 已解析配置数据根
    - key: 已规范化视图和查询

    返回:
    - 独立副本或 None
    """
    with _CACHE_LOCK:
        item = _CACHE.get((root, key))
        if item is None or time.monotonic() - item[0] >= 5:
            return None
        _CACHE.move_to_end((root, key))
        return deepcopy(item[2])


def _remember(root: Path, key: str, payload: dict[str, JsonValue]) -> None:
    """
    缓存预算按实际 JSON 大小限制, 大响应不长期保存在进程内

    参数:
    - root: 当前数据根
    - key: 查询身份
    - payload: 完整查询结果
    """
    global _CACHE_BYTES
    size = len(json_text(payload).encode("utf-8"))
    if size > 4_000_000:
        return
    with _CACHE_LOCK:
        old = _CACHE.pop((root, key), None)
        if old:
            _CACHE_BYTES -= old[1]
        _CACHE[(root, key)] = (time.monotonic(), size, deepcopy(payload))
        _CACHE_BYTES += size
        while len(_CACHE) > 8 or _CACHE_BYTES > 16_000_000:
            _, item = _CACHE.popitem(last=False)
            _CACHE_BYTES -= item[1]


def _local(store: TelemetryStore, query: MetricQuery | ActivityQuery) -> dict[str, JsonValue]:
    """
    在查询线程中分派固定的统计或活动读取

    参数:
    - store: 已由宿主解析的服务库
    - query: 已校验的查询结构

    返回:
    - 当前服务的单份贡献
    """
    return local_activity(store, query) if isinstance(query, ActivityQuery) else local_metrics(store, query)


async def _source(service: Service, layout: StorageLayout, query: MetricQuery | ActivityQuery, fetch: SourceFetcher | None) -> dict[str, JsonValue]:
    """
    读取单个固定服务贡献, 仅网络离线允许已确认归属的本地快照回退

    参数:
    - service: 框架固定来源
    - layout: 当前配置数据根
    - query: 聚合窗口
    - fetch: Control 注入的已鉴权网络读取器

    返回:
    - 含来源状态的统计贡献, 认证拒绝不改走文件读取
    """
    store = TelemetryStore(layout.root / "telemetry" / f"{service}.sqlite", service)
    params = {**query.filters, "view": query.view, "from": str(query.window.start), "to": str(query.window.end),
              "tz": query.window.timezone, "limit": str(query.limit)}
    if query.window.bucket is not None:
        params["bucket"] = query.window.bucket
    if isinstance(query, ActivityQuery):
        if query.after is not None:
            params["cursor"] = query.cursor(query.after)
    elif query.after:
        params["cursor"] = query.cursor(query.after)
    state: SourceState = "error"
    reason = "source_invalid_response"
    try:
        if service == "control" or fetch is None:
            return await QUERY_WORKERS.run(_local, store, query)
        endpoint = "/api/activity/local?" if isinstance(query, ActivityQuery) else "/api/stats/local?"
        payload = await SOURCE_WORKERS.run(fetch, service, endpoint + urlencode(params))
        sources = payload.get("sources")
        if (payload.get("window") != query.window.payload() or not isinstance(sources, list) or len(sources) != 1
                or not isinstance(sources[0], dict) or sources[0].get("service") != service):
            raise ValueError("统计来源归属或窗口不符")
        if isinstance(query, ActivityQuery):
            await QUERY_WORKERS.run(merge_activity, query, [payload])
        else:
            await QUERY_WORKERS.run(merge_metrics, query, [payload])
        _note_source(service, None)
        return payload
    except SourceUnavailable as error:
        state, reason = error.state, error.reason_code
    except (ValueError, OSError, sqlite3.Error, WorkerBusyError) as error:
        reason = "local_snapshot_unavailable" if service == "control" else "source_invalid_response"
    _note_source(service, reason)
    if state == "offline" and store.path.is_file():
        try:
            payload = await QUERY_WORKERS.run(_local, store, query)
            sources = cast(list[dict[str, JsonValue]], payload["sources"])
            sources[0].update(state="stale", reason_code=reason)
            payload["warnings"] = [f"{service} 离线, 显示当前数据根已保存的统计快照"]
            return payload
        except (OSError, ValueError, sqlite3.Error, WorkerBusyError):
            reason = "local_snapshot_unavailable"
    empty = await QUERY_WORKERS.run(_local, TelemetryStore(layout.root / "telemetry" / ".absent" / f"{service}.sqlite", service), query)
    sources = cast(list[dict[str, JsonValue]], empty["sources"])
    sources[0].update(state=state, reason_code=reason)
    empty["warnings"] = [f"{service} 统计不可用: {reason}"]
    return empty


async def telemetry_response(method: str, path: str, service: Service, layout: StorageLayout, *,
                             payload: object = None, fetch: SourceFetcher | None = None) -> tuple[int, dict[str, JsonValue]] | None:
    """
    在宿主完成认证后分派统计与设置, 不接收客户端指定的服务地址

    参数:
    - method: HTTP 方法
    - path: 含查询参数的原始路径
    - service: 当前实际服务
    - layout: 当前配置绑定的数据目录
    - payload: 已有 HTTP 层解析的设置正文
    - fetch: 仅 Control 使用的固定服务读取器

    返回:
    - 已处理响应或 None, 表示不属于本接口
    """
    parsed = urlsplit(path)
    settings_route = parsed.path == "/config/telemetry" and service == "control"
    activity = parsed.path in {"/api/activity", "/api/activity/local"}
    local = parsed.path in {"/api/stats/local", "/api/activity/local"}
    aggregate = service == "control" and parsed.path in {"/api/stats/overview", "/api/stats/models", "/api/stats/sessions", "/api/activity"}
    if not settings_route and not local and not aggregate:
        return None
    try:
        if settings_route:
            store = SettingsStore()
            if method == "GET":
                settings, revision = await QUERY_WORKERS.run(store.read)
                return 200, {"settings": settings.payload(), "revision": revision}
            if method == "PUT" and isinstance(payload, dict) and set(cast(dict[str, object], payload)) == {"settings", "expected_revision"}:
                document = cast(dict[str, object], payload)
                result = await QUERY_WORKERS.run(store.save, document["settings"], document["expected_revision"])
                global _CACHE_BYTES
                with _CACHE_LOCK:
                    _CACHE.clear()
                    _CACHE_BYTES = 0
                return 200, result
            return 400 if method == "PUT" else 405, {"error": "统计设置请求格式或方法无效", "code": "invalid_settings"}
        if method != "GET":
            return 405, {"error": "统计接口只支持 GET", "code": "method_not_allowed"}
        values = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(items) != 1 or len(items[0]) > (4096 if key == "cursor" else 512) for key, items in values.items()):
            raise ValueError("统计查询包含重复或超长参数")
        params = {key: values[0] for key, values in values.items()}
        view = "activity" if activity else params.get("view", "overview") if local else parsed.path.rsplit("/", 1)[-1]
        cache_key = json_text([view, params])
        if aggregate:
            cached = await QUERY_WORKERS.run(_cached, layout.root, cache_key)
            if cached is not None:
                return 200, cached
        query = ActivityQuery.parse(params) if activity else MetricQuery.parse(params, view)
        if local:
            result = await QUERY_WORKERS.run(_local, TelemetryStore(layout.root / "telemetry" / f"{service}.sqlite", service), query)
            return 200, result
        responses = await asyncio.gather(*(_source(name, layout, query, fetch) for name in ("control", "backend", "chat")))
        usable = any(isinstance(source, dict) and source.get("state") in {"available", "stale"} and isinstance(source.get("as_of"), (int, float))
                     for item in responses for source in cast(list[JsonValue], item.get("sources", [])))
        if not usable:
            return 503, {"error": "统计来源均不可用", "code": "sources_unavailable"}
        result = (await QUERY_WORKERS.run(merge_activity, query, responses) if isinstance(query, ActivityQuery)
                  else await QUERY_WORKERS.run(merge_metrics, query, responses))
        await QUERY_WORKERS.run(_remember, layout.root, cache_key, result)
        return 200, result
    except SettingsConflict as error:
        _, revision = await QUERY_WORKERS.run(SettingsStore().read)
        logger.warning("[统计设置] 保存失败: 修订冲突")
        return 409, {"error": str(error), "code": "revision_conflict", "current_revision": revision}
    except (ValueError, TypeError) as error:
        logger.warning(f"[统计] 查询参数无效: {type(error).__name__}")
        return 400, {"error": str(error), "code": "invalid_query"}
    except (OSError, sqlite3.Error, WorkerBusyError, TimeoutError) as error:
        logger.warning(f"[统计] 存储或队列不可用: {type(error).__name__}")
        return 503, {"error": "统计存储或工作队列暂时不可用", "code": "telemetry_unavailable", "details": {"error_type": type(error).__name__}}
