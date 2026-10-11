"""跨进程插件应用只提交一次, 等待期间通过有界短请求读取实际状态"""
from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from satrap.core.log import logger
from satrap.core.utils.application_progress import progress_external, progress_instance, progress_phase, progress_result
from satrap.core.utils.async_worker import BoundedAsyncWorker, WorkerBusyError


RUNTIME_REQUEST_WORKERS = BoundedAsyncWorker("satrap-plugin-status", workers=4, capacity=32)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class RuntimeRelayError(RuntimeError):
    """保留 HTTP 拒绝, 连接拒绝与网络结果未确认的区别"""

    def __init__(self, message: str, *, status: int | None = None, refused: bool = False) -> None:
        """
        记录一次内部请求的失败分类

        参数:
        - message: 不包含凭据的错误说明
        - status: HTTP 错误状态, 网络失败时为 None
        - refused: 建立连接前被拒绝, POST 时可确认目标服务未接受
        """
        super().__init__(message)
        self.status = status
        self.refused = refused


def _request(url: str, method: str, body: Mapping[str, Any] | None,
             request_factory: Callable[..., urllib.request.Request]) -> tuple[int, dict[str, Any]]:
    """
    执行一次设置 3 秒网络超时的内部 HTTP 请求, 不在线程中轮询或等待生命周期结束

    参数:
    - url: 当前任务固定的运行服务地址
    - method: POST 接受应用或 GET 查询状态
    - body: POST 的插件作用域和任务 ID, GET 为 None
    - request_factory: 控制服务原内部鉴权构造器

    返回:
    - HTTP 状态与对象响应; 失败抛出已分类异常并记录堆栈
    """
    request = request_factory(url, method)
    if body is not None:
        request.data = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError("运行服务响应超过大小限制")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("运行服务返回了无效结果")
            return response.status, payload
    except urllib.error.HTTPError as error:
        error.close()
        logger.warning(f"[插件应用] 内部请求返回 HTTP {error.code}\n{traceback.format_exc()}")
        raise RuntimeRelayError(f"运行服务返回 HTTP {error.code}", status=error.code) from error
    except urllib.error.URLError as error:
        logger.warning(f"[插件应用] 内部网络请求失败: {error.reason}\n{traceback.format_exc()}")
        raise RuntimeRelayError(str(error.reason), refused=isinstance(error.reason, ConnectionRefusedError)) from error
    except (OSError, TypeError, ValueError) as error:
        logger.error(f"[插件应用] 内部响应读取失败: {error}\n{traceback.format_exc()}")
        raise RuntimeRelayError(str(error)) from error


def runtime_result(target: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """
    归一化旧同步响应和任务最终响应, 不把部分失败解释为已应用

    参数:
    - target: Chat 或 Edictum 服务标识
    - payload: 服务的实际最终结果

    返回:
    - 原控制接口的服务状态及完整逐实例结果
    """
    sessions = payload.get("sessions", payload.get("edictum_sessions", []))
    if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
        raise ValueError("运行服务返回了无效实例结果")
    success = bool(payload.get("ok")) and payload.get("applied", True) and all(item.get("ok", False) for item in sessions)
    return {"target": target, "status": ("applied" if sessions else "next_activation") if success else "error", "sessions": sessions,
            "error": "" if success else payload.get("error", "部分运行实例应用失败")}


async def relay_plugin_application(target: str, url: str, scope: Mapping[str, str], operation_id: str,
                                   request_factory: Callable[..., urllib.request.Request], *, poll_interval: float = 1,
                                   worker: BoundedAsyncWorker | None = None) -> dict[str, Any]:
    """
    向固定运行服务提交一次应用并异步查询, 断线只查询已知子任务而不重发

    参数:
    - target: 固定的服务标识
    - url: 固定的 POST 应用地址
    - scope: 插件/命名配置范围
    - operation_id: 控制服务任务 ID, 用于派生各服务独立的子任务 ID
    - request_factory: 原内部鉴权构造器
    - poll_interval: 正常读取间隔, 默认 1 秒, 暂时网络失败逐步退避至 15 秒
    - worker: 可选隔离工作池, 默认使用独立有界 HTTP 池

    返回:
    - 服务应用状态; 404/拒绝/无效响应明确未确认, 不自动改用另一服务或重发 POST
    """
    child_id = hashlib.sha256(f"{operation_id}\0{target}\0{url}".encode("utf-8")).hexdigest()
    address = urllib.parse.urlsplit(url)
    query_url = urllib.parse.urlunsplit((address.scheme, address.netloc, f"/api/runtime-operations/{child_id}", "", ""))
    pool = worker or RUNTIME_REQUEST_WORKERS
    known_rejection = False
    with progress_instance(target, ""):
        try:
            try:
                with progress_phase("dispatch_ms"):
                    status, payload = await pool.run(_request, url, "POST", {**scope, "operation_id": child_id}, request_factory)
                if status == 200 and "operation_id" not in payload:
                    result = runtime_result(target, payload)
                    progress_result({"ok": result["status"] != "error"})
                    return result
            except RuntimeRelayError as error:
                if error.refused:
                    progress_result({"ok": True})
                    return {"target": target, "status": "next_activation", "sessions": [], "error": ""}
                if error.status is not None:
                    known_rejection = True
                    raise
                payload = None
            wait = poll_interval
            while True:
                if payload is not None:
                    if payload.get("operation_id") != child_id or payload.get("status") not in {"running", "completed", "failed", "cancelled"}:
                        raise ValueError("子任务 ID 或应用状态不匹配")
                    progress_external(target, payload)
                    if payload["status"] != "running":
                        final = payload.get("result")
                        if not isinstance(final, dict):
                            raise ValueError(payload.get("error") or "子任务结束但未返回实际应用结果")
                        result = runtime_result(target, final)
                        progress_result({"ok": result["status"] != "error"})
                        return {**result, "operation_id": child_id}
                with progress_phase("await_runtime_ms") if payload is None else nullcontext():
                    await asyncio.sleep(wait)
                with progress_phase("await_runtime_ms"):
                    try:
                        _status, payload = await pool.run(_request, query_url, "GET", None, request_factory)
                        wait = poll_interval
                    except RuntimeRelayError as error:
                        if error.status is not None:
                            raise
                        payload = None
                        wait = min(15, max(poll_interval, wait * 2))
        except (RuntimeRelayError, WorkerBusyError, OSError, TypeError, ValueError) as error:
            logger.error(f"[插件应用] {target} 任务 {child_id} 状态未确认: {error}\n{traceback.format_exc()}")
            progress_result({"ok": False})
            return {"target": target, "status": "error", "sessions": [], "error": str(error),
                    "operation_id": child_id, "confirmation": "not_accepted" if known_rejection else "unconfirmed"}
