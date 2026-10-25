"""
三服务共用的高级诊断只读接口

调用方先完成自身认证, 查询使用独立库和有界线程池,
通过事件关联读取正文而不是公开全局摘要查找
"""
from __future__ import annotations

import sqlite3
import asyncio
import json
import time
from collections.abc import Generator, Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from satrap.core.server_auth import ServerAuth

from satrap.core.utils.async_worker import RAG_WORKERS, WorkerBusyError
from .capture import debug_store, debug_health
from .policy import policy_store


async def debug_response(method: str, path: str, service: str) -> tuple[int, dict[str, object]] | None:
    """
    处理鉴权后的诊断摘要与详情查询

    参数:
    - method: HTTP 方法
    - path: 含分页参数的路径
    - service: 当前实际服务, 不由查询参数选择其它数据库

    返回:
    - 状态及响应, 无关路径返回 None
    """
    parsed = urlsplit(path)
    if not parsed.path.startswith("/api/debug/") or parsed.path == "/api/debug/export":
        return None
    if method != "GET":
        return 405, {"error": "高级诊断查询只支持 GET"}
    query = parse_qs(parsed.query, keep_blank_values=True)
    allowed = {"limit", "cursor", "object_key", "context_id", "status", "request_id", "after", "event_id", "slot", "from", "to",
               "q", "platform_id", "context_kind", "recording"}
    if set(query) - allowed or any(len(values) != 1 or len(values[0]) > 4096 for values in query.values()):
        return 400, {"error": "高级诊断查询参数无效"}
    def value(name: str, default: str = "") -> str:
        """
        读取已限制长度和数量的单值参数

        参数:
        - name: 参数名
        - default: 缺少时的默认文本

        返回:
        - 已校验的参数文本
        """
        return query.get(name, [default])[0]
    store = debug_store(service)
    try:
        limit = int(value("limit", "50"))
        if parsed.path == "/api/debug/status":
            return 200, await RAG_WORKERS.run(debug_health, service)
        if parsed.path == "/api/debug/contexts":
            recording = value("recording")
            if recording not in ("", "on", "off"):
                raise ValueError("对象记录过滤只能为 on 或 off")
            keys, exclude = None, False
            if recording:
                policy = await RAG_WORKERS.run(policy_store().read)
                enabled = recording == "on"
                override = "off" if policy.default_enabled else "on"
                keys = [obj.key for obj, mode in policy.objects if mode == override]
                exclude = enabled == policy.default_enabled
            return 200, await RAG_WORKERS.run(store.contexts, limit=limit, cursor=value("cursor"), object_key=value("object_key"),
                                              query=value("q"), platform_id=value("platform_id"), context_kind=value("context_kind"),
                                              recording_keys=keys, exclude_keys=exclude)
        if parsed.path == "/api/debug/requests":
            return 200, await RAG_WORKERS.run(store.requests, limit=limit, cursor=value("cursor"), object_key=value("object_key"),
                                              context_id=value("context_id") if "context_id" in query else None, status=value("status"),
                                              started_after=float(value("from")) if "from" in query else None,
                                              started_before=float(value("to")) if "to" in query else None)
        if parsed.path == "/api/debug/request":
            return 200, {"request": await RAG_WORKERS.run(store.request_info, value("request_id"))}
        if parsed.path == "/api/debug/events":
            return 200, dict(await RAG_WORKERS.run(store.events, value("request_id"), after=int(value("after", "-1")), limit=limit))
        if parsed.path == "/api/debug/body":
            body = await RAG_WORKERS.run(store.body, value("request_id"), value("event_id"), value("slot"))
            return 200, {"body": body}
        return 404, {"error": "高级诊断接口不存在"}
    except (ValueError, TypeError) as error:
        return 400, {"error": str(error)}
    except (OSError, sqlite3.Error, WorkerBusyError) as error:
        return 503, {"error": "高级诊断存储暂时不可用", "error_type": type(error).__name__}


def _export_lines(service: str, request_id: str) -> Generator[bytes, None, None]:
    """
    分页还原单请求, 不把整个链路一次装入内存

    参数:
    - service: 可信服务标识
    - request_id: 已确认存在的请求

    返回:
    - UTF-8 JSONL 分块, 超过导出预算时以不完整尾行收尾
    """
    store = debug_store(service)
    summary = store.request_info(request_id)
    yield (json.dumps({"request": summary, "service": service, "export_started_at": time.time()}, ensure_ascii=False) + "\n").encode("utf-8")
    after, remaining = -1, 64_000_000
    while True:
        page = store.events(request_id, after=after, limit=100)
        for entry in page["events"]:
            bodies = {}
            for slot in entry["body_slots"]:
                try:
                    bodies[slot] = store.body(request_id, entry["event_id"], slot)
                except ValueError as error:
                    bodies[slot] = {"omitted": True, "reason": str(error)}
            line = (json.dumps({"event": entry, "bodies": bodies}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(line) > remaining:
                yield b'{"export_complete":false,"reason":"export_budget"}\n'
                return
            remaining -= len(line)
            yield line
            after = entry["sequence"]
        if not page["has_more"]:
            yield (json.dumps({"export_complete": True, "chain_closed_at_export_start": summary["status"] != "running", "capture_incomplete": bool(summary["incomplete"])}) + "\n").encode("utf-8")
            return


async def stream_debug_export(writer: asyncio.StreamWriter, method: str, path: str,
                              headers: Mapping[str, str], auth: ServerAuth, service: str) -> bool:
    """
    鉴权后以背压和逐页查询导出链路, 无关路径交还原路由

    参数:
    - writer: 当前连接
    - method: HTTP 方法
    - path: 原始路径
    - headers: 已认证请求头
    - auth: 当前服务认证与 CORS 策略
    - service: 当前实际服务标识

    返回:
    - 是否已处理, 调用前必须完成当前服务认证
    """
    parsed = urlsplit(path)
    if parsed.path != "/api/debug/export":
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    ids = query.get("request_id", [])
    valid = method == "GET" and set(query) == {"request_id"} and len(ids) == 1 and 0 < len(ids[0]) <= 512
    status, reason = (200, "OK") if valid else (400, "Bad Request")
    if valid:
        try:
            page = await RAG_WORKERS.run(debug_store(service).events, ids[0], limit=1)
            if not page["events"]:
                status, reason = 404, "Not Found"
        except (OSError, sqlite3.Error):
            status, reason = 503, "Service Unavailable"
    cors = "".join(f"{key}: {value}\r\n" for key, value in auth.cors_headers(headers.get("origin")).items())
    writer.write((f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/x-ndjson; charset=utf-8\r\n"
                  f"{cors}Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode("ascii"))
    if status != 200:
        writer.write(json.dumps({"error": f"链路导出失败: {reason}"}, ensure_ascii=False).encode("utf-8"))
        await writer.drain()
        return True
    lines = _export_lines(service, ids[0])
    try:
        while True:
            chunk = await RAG_WORKERS.run(lambda: next(lines, b""))
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except ConnectionError:
        return True
    except (OSError, sqlite3.Error, ValueError):
        writer.write(b'{"export_complete":false,"reason":"storage_unavailable"}\n')
        await writer.drain()
    finally:
        try:
            lines.close()
        except ValueError:
            pass   # 取消时后台 next 可能仍在执行, 其局部只持有已关闭的只读查询结果
    return True
