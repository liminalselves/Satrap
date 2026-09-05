"""
后端内嵌 HTTP + WebSocket 服务器 (管理 API 与前端静态托管)

基于共享基类 satrap.core.utils.minihttp.MiniHTTPServer, 只保留本服务特有逻辑:
- /api/* 管理路由: health / config / session-classes / edictum / models / users / checkpoint / shutdown
- /ws/logs, /ws/status WebSocket 推送
- 静态文件 / SPA 入口 (satrap-ui/dist, 生产模式托管前端构建产物)
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit
import dataclasses
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
import json

from satrap.core.config.session_class_service import SessionClassConfigService
from satrap.core.framework.session_discovery import SessionClassDiscoveryService, create_default_session_dir
from satrap.core.framework.providers.base import SESSION_CLASS_PROVIDER
from satrap.core.config.edictum_service import EdictumConfigService
from satrap.core.config.model_service import ModelConfigService
from satrap.core.backend.static_ui import DEFAULT_STATIC_DIR, SPAStaticService
from satrap.core.backend.ui_config import build_ui_config
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.core.log.stream import standard_log_stream
from satrap.core.storage import CHAT_PLATFORM_ID, LOCAL_PLATFORM_ID, StorageMaintenanceService
from satrap.core.type import safe_getattr, safe_getattr_str
from satrap.api import checkpoint as checkpoint_api
from satrap.api import user as user_api

if TYPE_CHECKING:
    from satrap.core.backend.BackendManager import BackendManager

STATIC_DIR = DEFAULT_STATIC_DIR
# 静态文件目录 - 前端构建产物

RouteResponse = tuple[int, dict[str, Any]]
# 管理 API 响应: (状态码, JSON 体)


def _parse_json_object(body: bytes) -> dict[str, Any]:
    """
    将请求体解析为 JSON 对象

    参数:
    - body: 请求体

    返回:
    - dict[str, Any]: JSON 对象
    """
    payload: object = json.loads(body or b"{}")
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return dict(cast(dict[str, Any], payload))


def _platform_runtimes(backend: BackendManager) -> dict[str, tuple[Any, Any]]:
    """
    获取后端的平台运行时映射

    参数:
    - backend: 后端实例

    返回:
    - dict[str, tuple[Any, Any]]: 平台 ID 到会话和用户管理器的映射
    """
    getter = getattr(backend, "list_platform_runtimes", None)
    if callable(getter):
        runtimes = cast(dict[str, tuple[Any, Any]], getter())
        return {
            platform_id: runtime
            for platform_id, runtime in runtimes.items()
            if platform_id != CHAT_PLATFORM_ID
        }
    session_manager = getattr(backend, "session_manager", None)
    user_manager = getattr(backend, "user_manager", None)
    if session_manager is None:
        return {}
    return {LOCAL_PLATFORM_ID: (session_manager, user_manager)}


def _platform_db_path(backend: BackendManager, platform_id: str) -> str:
    """
    返回指定平台实例的数据库路径

    参数:
    - backend: 后端实例
    - platform_id: 平台实例 ID

    返回:
    - str: 平台数据库路径
    """
    getter = getattr(backend, "platform_db_path", None)
    if callable(getter):
        return str(getter(platform_id))
    return str(backend.checkpoint_db_path)


class BackendHTTPServer(MiniHTTPServer):
    """
    内嵌 HTTP 服务器, 提供管理 API

    基于共享基类 (asyncio.start_server, 零外部依赖)
    默认监听 127.0.0.1:19870, 仅接受本地连接
    """

    def __init__(
        self,
        backend: BackendManager,
        host: str = "127.0.0.1",
        port: int = 19870,
        *,
        static_dir: str | Path | None = None,
    ):
        """
        初始化 BackendHTTPServer

        参数:
        - backend: 后端实例
        - host: 监听地址
        - port: 监听端口
        - static_dir: 前端静态文件目录
        """
        super().__init__(
            host=host,
            port=port,
            log_errors=False,
            session_namespace="backend",
        )
        self.backend = backend
        self._static_ui = SPAStaticService(
            static_dir or STATIC_DIR,
            excluded_prefixes=("/api/", "/ui-config.json"),
        )

    # ---------- 静态文件服务 ----------

    async def _serve_static(
        self,
        writer: asyncio.StreamWriter,
        path: str,
        request_headers: dict[str, str] | None = None,
    ) -> bool:
        """
        服务静态文件或 SPA 入口 (仅非 API 路径)

        参数:
        - writer: 流写入器
        - path: 路径
        - request_headers: 小写键名的 HTTP 请求头

        返回:
        - bool: 服务静态文件或 SPA 入口 (仅非 API 路径)
        """
        return await self._static_ui.serve(writer, path, request_headers)

    # ---------- WebSocket 端点分发 ----------

    async def _ws_dispatch(
        self, path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        按路径分发到对应的 WebSocket 处理器

        参数:
        - path: 路径
        - reader: 流读取器
        - writer: 流写入器
        """
        parsed_path = urlsplit(path)
        if parsed_path.path == "/ws/logs":
            query = parse_qs(parsed_path.query)
            try:
                history_lines = int(query.get("lines", ["100"])[0])
            except ValueError:
                history_lines = 100
            await self._ws_log_handler(reader, writer, max(50, min(history_lines, 500)))
        elif parsed_path.path == "/ws/status":
            await self._ws_status_handler(reader, writer)
        else:
            await self._ws_close(writer, 1008, "unknown endpoint")

    async def _ws_log_handler(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        history_lines: int = 100,
    ):
        """
        WebSocket 日志推送处理器

        参数:
        - reader: 流读取器
        - writer: 流写入器
        - history_lines: 首次连接时推送的历史行数
        """
        subscription = standard_log_stream.subscribe(history_limit=history_lines)
        try:
            for entry in subscription.history:
                await self._ws_send(writer, {
                    "type": "log",
                    "data": {"content": entry.content, "level": entry.level},
                })
            # 发送指定条数的标准日志历史

            while not reader.at_eof():
                try:
                    entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
                except TimeoutError:
                    continue
                await self._ws_send(writer, {
                    "type": "log",
                    "data": {"content": entry.content, "level": entry.level},
                })
        except asyncio.CancelledError:
            pass
        except Exception as e:
            try:
                await self._ws_send(writer, {"type": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            standard_log_stream.unsubscribe(subscription.subscription_id)

    async def _ws_status_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        WebSocket 状态推送处理器

        参数:
        - reader: 流读取器
        - writer: 流写入器
        """
        last_status = None

        try:
            while True:
                if reader.at_eof():
                    break
                # 检查客户端是否关闭连接

                health = await self.backend.health()
                # 获取当前状态
                current_status = {
                    "running": health.get("running", False),
                    "adapters": health.get("adapters", {}),
                }

                if current_status != last_status:
                    await self._ws_send(writer, {
                        "type": "status",
                        "data": current_status
                    })
                    last_status = current_status
                # 状态变化时推送

                await asyncio.sleep(2)   # 2秒轮询间隔

        except asyncio.CancelledError:
            pass
        except Exception as e:
            try:
                await self._ws_send(writer, {"type": "error", "message": str(e)})
            except Exception:
                pass

    # ---------- API 路由 ----------

    async def _route(
        self,
        method: str,
        path: str,
        body: bytes,
    ) -> tuple[int, dict[str, Any]]:
        """
        路由分发到 BackendManager 对应方法

        按源码区段顺序依次尝试各资源处理器, 处理器返回 None 表示路径不属于该区段

        参数:
        - method: HTTP 方法
        - path: 路径
        - body: 请求体

        返回:
        - tuple[int, dict[str, Any]]: 路由分发到 BackendManager 对应方法
        """
        for handler in (
            self._route_ui_health_reload,
            self._route_edictum_runtime_plugins,
            self._route_storage,
            self._route_shutdown,
            self._route_session_class_collection,
            self._route_edictum_config,
            self._route_discovery,
            self._route_session_instances,
            self._route_session_class_items,
            self._route_models,
            self._route_user,
            self._route_checkpoint,
        ):
            response = await handler(method, path, body)
            if response is not None:
                return response
        return 404, {"error": f"unknown route: {method} {path}"}

    async def _route_ui_health_reload(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """UI 配置 / 健康检查 / 配置热加载 (精确路径)"""
        backend = self.backend

        if method == "GET" and path == "/ui-config.json":
            return 200, build_ui_config(
                backend_host=backend.config.api_host,
                backend_port=backend.config.api_port,
            )
        # 接口: GET /ui-config.json

        if method == "GET" and path == "/api/health":
            health = await backend.health()
            return (200 if health.get("healthy", False) else 503), health
        # 接口: GET /api/health

        if method == "POST" and path == "/api/config/reload":
            return 200, await backend.reload_config()
        # 接口: POST /api/config/reload

        return None

    async def _route_edictum_runtime_plugins(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """Edictum 插件与运行时配置的热更预览/协调 (精确路径)"""
        backend = self.backend

        if method == "POST" and path == "/api/edictum/plugins/preview":
            try:
                payload = _parse_json_object(body)
                raw_refs = payload.get("session_refs")
                if raw_refs is not None and not isinstance(raw_refs, list):
                    raise ValueError("session_refs 必须是数组")
                session_refs: list[dict[str, str]] | None = None
                if isinstance(raw_refs, list):
                    session_refs = []
                    for raw_ref in cast(list[object], raw_refs):
                        if isinstance(raw_ref, dict):
                            session_refs.append(dict(cast(dict[str, str], raw_ref)))
                desired_plugins = payload.get("plugins") if "plugins" in payload else None
                sessions = backend.preview_edictum_plugin_changes(
                    config_name=str(payload.get("config_name") or "").strip() or None,
                    session_refs=session_refs,
                    desired_plugins=desired_plugins,
                )
                return 200, {
                    "ok": all(item.get("ok", False) for item in sessions),
                    "edictum_sessions": sessions,
                }
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/edictum/plugins/preview

        if method == "POST" and path == "/api/edictum/plugins/reconcile":
            try:
                payload = _parse_json_object(body)
                raw_refs = payload.get("session_refs")
                if raw_refs is not None and not isinstance(raw_refs, list):
                    raise ValueError("session_refs 必须是数组")
                session_refs = None
                if isinstance(raw_refs, list):
                    session_refs = []
                    for raw_ref in cast(list[object], raw_refs):
                        if isinstance(raw_ref, dict):
                            session_refs.append(dict(cast(dict[str, str], raw_ref)))
                sessions = await backend.reconcile_edictum_plugins_async(
                    config_name=str(payload.get("config_name") or "").strip() or None,
                    session_refs=session_refs,
                    concurrency=max(1, min(int(payload.get("concurrency") or 4), 16)),
                )
                return 200, {
                    "ok": all(item.get("ok", False) for item in sessions),
                    "edictum_sessions": sessions,
                }
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/edictum/plugins/reconcile

        if method == "POST" and path == "/api/edictum/runtime/preview":
            try:
                payload = _parse_json_object(body)
                raw_refs = payload.get("session_refs")
                if raw_refs is not None and not isinstance(raw_refs, list):
                    raise ValueError("session_refs 必须是数组")
                session_refs = None
                if isinstance(raw_refs, list):
                    session_refs = [
                        dict(cast(dict[str, str], item))
                        for item in cast(list[object], raw_refs)
                        if isinstance(item, dict)
                    ]
                raw_config = payload.get("config")
                if raw_config is not None and not isinstance(raw_config, dict):
                    raise ValueError("config 必须是对象")
                sessions = backend.preview_edictum_runtime_changes(
                    config_name=str(payload.get("config_name") or "").strip() or None,
                    session_refs=session_refs,
                    desired_config=(
                        dict(cast(dict[str, Any], raw_config))
                        if isinstance(raw_config, dict)
                        else None
                    ),
                )
                return 200, {
                    "ok": all(item.get("ok", False) for item in sessions),
                    "edictum_sessions": sessions,
                }
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/edictum/runtime/preview

        if method == "POST" and path == "/api/edictum/runtime/apply":
            try:
                payload = _parse_json_object(body)
                raw_refs = payload.get("session_refs")
                if raw_refs is not None and not isinstance(raw_refs, list):
                    raise ValueError("session_refs 必须是数组")
                session_refs = None
                if isinstance(raw_refs, list):
                    session_refs = [
                        dict(cast(dict[str, str], item))
                        for item in cast(list[object], raw_refs)
                        if isinstance(item, dict)
                    ]
                sessions = await backend.reconcile_edictum_runtime_async(
                    config_name=str(payload.get("config_name") or "").strip() or None,
                    session_refs=session_refs,
                    concurrency=max(1, min(int(payload.get("concurrency") or 4), 16)),
                )
                return 200, {
                    "ok": all(item.get("ok", False) for item in sessions),
                    "edictum_sessions": sessions,
                }
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/edictum/runtime/apply

        return None

    async def _route_storage(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """存储审计 / 清理 / 回收站恢复与清除 (urlsplit 解析路径)"""
        backend = self.backend
        storage_route = urlsplit(path).path

        if method == "GET" and storage_route == "/api/storage/audit":
            service = StorageMaintenanceService(backend.storage_layout)
            configured_platforms = {
                LOCAL_PLATFORM_ID,
                CHAT_PLATFORM_ID,
                *(
                    str(item.get("id", "")).strip()
                    for item in backend.config.platforms
                    if str(item.get("id", "")).strip()
                ),
            }
            items = await asyncio.to_thread(service.scan, configured_platforms)
            return 200, {
                "items": [item.to_dict() for item in items],
                "summary": {
                    "count": len(items),
                    "size_bytes": sum(item.size_bytes for item in items),
                },
            }
        # 接口: GET /api/storage/audit

        if method == "POST" and storage_route == "/api/storage/cleanup":
            try:
                payload = _parse_json_object(body)
                raw_ids = payload.get("item_ids", [])
                if not isinstance(raw_ids, list):
                    raise ValueError("item_ids 必须是数组")
                results = await asyncio.to_thread(
                    StorageMaintenanceService(backend.storage_layout).cleanup,
                    [str(item) for item in cast(list[object], raw_ids)],
                )
                return 200, {
                    "ok": all(item.get("ok", False) for item in results),
                    "results": results,
                }
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/storage/cleanup

        if method == "POST" and storage_route == "/api/storage/trash/restore":
            try:
                payload = _parse_json_object(body)
                result = await asyncio.to_thread(
                    StorageMaintenanceService(backend.storage_layout).restore_archive,
                    str(payload.get("platform_id", "")).strip(),
                    str(payload.get("archive_id", "")).strip(),
                )
                return 200, result
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/storage/trash/restore

        if method == "POST" and storage_route == "/api/storage/trash/purge":
            try:
                payload = _parse_json_object(body)
                deleted = await asyncio.to_thread(
                    StorageMaintenanceService(backend.storage_layout).purge_archive,
                    str(payload.get("platform_id", "")).strip(),
                    str(payload.get("archive_id", "")).strip(),
                )
                return 200, {"ok": deleted}
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/storage/trash/purge

        if method == "POST" and storage_route == "/api/storage/trash/purge-batch":
            try:
                payload = _parse_json_object(body)
                raw_refs = payload.get("archive_refs")
                if raw_refs is not None and not isinstance(raw_refs, list):
                    raise ValueError("archive_refs 必须是数组")
                archive_refs = [
                    dict(cast(dict[str, str], item))
                    for item in cast(list[object], raw_refs or [])
                    if isinstance(item, dict)
                ]
                raw_days = payload.get("older_than_days")
                results = await asyncio.to_thread(
                    StorageMaintenanceService(backend.storage_layout).purge_archives,
                    archive_refs=archive_refs,
                    older_than_days=(float(raw_days) if raw_days is not None else None),
                    platform_id=str(payload.get("platform_id", "")).strip() or None,
                )
                return 200, {
                    "ok": all(item.get("ok", False) for item in results),
                    "results": results,
                }
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/storage/trash/purge-batch

        return None

    async def _route_shutdown(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """后端关闭请求 (调度关闭事件后立即应答)"""
        if method == "POST" and path == "/api/shutdown":
            asyncio.get_event_loop().call_soon(self.backend.request_shutdown)
            return 200, {"ok": True}
        # 接口: POST /api/shutdown

        return None

    async def _route_session_class_collection(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """会话类配置集合 (GET 无管理器时响应空表, POST 无管理器时 fallthrough)"""
        backend = self.backend

        if method == "GET" and path == "/api/config/session-classes":
            mgr = backend.session_class_mgr
            if mgr:
                return 200, SessionClassConfigService(mgr).list_configs()
            return 200, {}
        # 接口: GET /api/config/session-classes

        if method == "POST" and path == "/api/config/session-classes" and backend.session_class_mgr:
            try:
                payload = _parse_json_object(body)
                SessionClassConfigService(backend.session_class_mgr).create(payload)
                return 200, {"ok": True}
            except Exception as e:
                return 400, {"error": str(e)}
        # 接口: POST /api/config/session-classes

        return None

    async def _route_edictum_config(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """Edictum 冷配置 (类型表 / 集合 / 前缀项: 详情, 更新, 启停, 删除)"""
        backend = self.backend

        if (
            method == "GET"
            and path == "/api/config/edictum/types"
            and backend.edictum_config_manager
            and backend.edictum_type_registry
        ):
            service = EdictumConfigService(
                backend.edictum_config_manager,
                backend.edictum_type_registry,
            )
            return 200, {"types": service.list_types()}
        # 接口: GET /api/config/edictum/types

        if (
            method == "GET"
            and path == "/api/config/edictum/sessions"
            and backend.edictum_config_manager
            and backend.edictum_type_registry
        ):
            service = EdictumConfigService(
                backend.edictum_config_manager,
                backend.edictum_type_registry,
            )
            return 200, service.list_configs()
        # 接口: GET /api/config/edictum/sessions

        if (
            method == "POST"
            and path == "/api/config/edictum/sessions"
            and backend.edictum_config_manager
            and backend.edictum_type_registry
        ):
            try:
                service = EdictumConfigService(
                    backend.edictum_config_manager,
                    backend.edictum_type_registry,
                )
                created = service.create(_parse_json_object(body))
                return 200, {"ok": True, "config": created}
            except Exception as e:
                return 400, {"error": str(e)}
        # 接口: POST /api/config/edictum/sessions

        edictum_prefix = "/api/config/edictum/sessions/"
        if (
            path.startswith(edictum_prefix)
            and backend.edictum_config_manager
            and backend.edictum_type_registry
        ):
            suffix = path[len(edictum_prefix):]
            action = ""
            if suffix.endswith("/enable"):
                suffix = suffix[:-len("/enable")]
                action = "enable"
            elif suffix.endswith("/disable"):
                suffix = suffix[:-len("/disable")]
                action = "disable"
            name = unquote(suffix)
            service = EdictumConfigService(
                backend.edictum_config_manager,
                backend.edictum_type_registry,
            )
            try:
                if method == "GET" and not action:
                    config_entry = service.get(name)
                    if config_entry is None:
                        return 404, {"error": "not found"}
                    return 200, config_entry
                if method in {"PATCH", "PUT"} and not action:
                    payload = _parse_json_object(body)
                    previous = service.get(name)
                    final_name, updated = service.update(name, payload)
                    migrated_refs: list[dict[str, str]] = []
                    if final_name != name:
                        try:
                            migrated_refs = backend.rename_edictum_config_references(
                                name,
                                final_name,
                            )
                        except Exception:
                            if previous is not None:
                                service.update(final_name, {**previous, "name": name})
                            raise
                    return 200, {
                        "ok": True,
                        "name": final_name,
                        "config": updated,
                        "migrated_refs": migrated_refs,
                    }
                if method == "POST" and action:
                    updated = service.set_enabled(name, action == "enable")
                    return 200, {"ok": True, "config": updated}
                if method == "DELETE" and not action:
                    references = backend.list_edictum_config_references(name)
                    if references:
                        return 409, {
                            "error": f"Edictum 配置仍被 {len(references)} 个会话实例引用",
                            "references": references,
                        }
                    if service.delete(name):
                        return 200, {"ok": True}
                    return 404, {"error": "not found"}
            except Exception as e:
                return 400, {"error": str(e)}

        return None

    async def _route_discovery(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """会话类目录发现与创建 (partition("?") 解析路径)"""
        backend = self.backend
        route_path, _, query_string = path.partition("?")

        if method == "GET" and route_path == "/api/session/discovery":
            try:
                query = parse_qs(query_string)
                requested_paths = [item for item in query.get("path", []) if item.strip()]
                configured_paths = list(backend.config.session_scan_paths)
                if any(item not in configured_paths for item in requested_paths):
                    raise ValueError("只能扫描配置中的 Session 目录")
                scan_paths = requested_paths or configured_paths
                results = [
                    item.to_dict()
                    for item in SessionClassDiscoveryService(configured_paths).discover(scan_paths)
                ]
                return 200, {"paths": list(backend.config.session_scan_paths), "results": results}
            except Exception as e:
                return 400, {"error": str(e)}
        # 接口: GET /api/session/discovery

        if method == "POST" and route_path == "/api/session/discovery/directories":
            try:
                payload = _parse_json_object(body)
                requested_path = str(payload.get("path", "")).strip()
                configured_paths = list(backend.config.session_scan_paths)
                if requested_path and requested_path not in configured_paths:
                    raise ValueError("只能创建配置中的 Session 扫描目录")
                target = create_default_session_dir([requested_path] if requested_path else configured_paths)
                return 200, {"ok": True, "path": str(target)}
            except Exception as e:
                return 400, {"error": str(e)}
        # 接口: POST /api/session/discovery/directories

        return None

    async def _route_session_instances(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """会话实例 (列表 / 创建 / 批量删除 / 重启 / 删除, partition("?") 解析路径)"""
        backend = self.backend
        route_path, _, _ = path.partition("?")

        if method == "GET" and route_path == "/api/sessions":
            sessions: list[dict[str, Any]] = []
            for platform_id, (session_manager, _) in _platform_runtimes(backend).items():
                active_ids = {item.session_id for item in session_manager.list_sessions()}
                for item in session_manager.list_session_configs():
                    serialized = dataclasses.asdict(item)
                    serialized["platform_id"] = platform_id
                    serialized["active"] = item.session_id in active_ids
                    serialized["runtime"] = session_manager.get_session_runtime_metadata(
                        item.session_id or ""
                    )
                    sessions.append(serialized)
            sessions.sort(key=lambda item: float(item.get("last_used_at") or 0), reverse=True)
            return 200, {"sessions": sessions}
        # 接口: GET /api/sessions

        if method == "POST" and route_path == "/api/sessions/bulk-delete":
            try:
                payload = _parse_json_object(body)
                mode = str(payload.get("mode", "selected")).strip()
                runtimes = _platform_runtimes(backend)
                targets: dict[str, list[str]] = {}
                if mode == "empty":
                    for platform_id, (session_manager, _) in runtimes.items():
                        targets[platform_id] = session_manager.store.list_ids_by_message_count(0)
                elif mode == "single":
                    for platform_id, (session_manager, _) in runtimes.items():
                        targets[platform_id] = session_manager.store.list_ids_by_message_count(1)
                elif mode == "selected":
                    raw_refs = cast(object, payload.get("session_refs", []))
                    if not isinstance(raw_refs, list):
                        raise ValueError("session_refs 必须是数组")
                    for raw_ref in cast(list[object], raw_refs):
                        if not isinstance(raw_ref, dict):
                            raise ValueError("session_refs 的每一项必须是对象")
                        ref = cast(dict[str, Any], raw_ref)
                        platform_id = str(ref.get("platform_id", "")).strip()
                        session_id = str(ref.get("session_id", "")).strip()
                        if platform_id not in runtimes:
                            raise ValueError(f"未知平台实例: {platform_id}")
                        if session_id:
                            targets.setdefault(platform_id, []).append(session_id)
                    if not any(targets.values()):
                        raise ValueError("至少选择一个会话实例")
                else:
                    raise ValueError(f"未知批量删除模式: {mode}")
                deleted_refs: list[dict[str, str]] = []
                for platform_id, session_ids in targets.items():
                    if not session_ids:
                        continue
                    session_manager = runtimes[platform_id][0]
                    deleted_ids = await session_manager.delete_sessions_async(session_ids)
                    deleted_refs.extend(
                        {"platform_id": platform_id, "session_id": session_id}
                        for session_id in deleted_ids
                    )
                return 200, {
                    "ok": True,
                    "deleted_count": len(deleted_refs),
                    "deleted_ids": [item["session_id"] for item in deleted_refs],
                    "deleted_refs": deleted_refs,
                }
            except Exception as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/sessions/bulk-delete

        session_path_prefix = "/api/sessions/"
        if (
            method == "POST"
            and route_path.startswith(session_path_prefix)
            and route_path.endswith("/restart")
        ):
            session_id = unquote(
                route_path[len(session_path_prefix):-len("/restart")]
            ).strip()
            if not session_id:
                return 400, {"error": "session_id 不能为空"}
            platform_id = self._query_param(path, "platform_id")
            runtime = _platform_runtimes(backend).get(platform_id)
            if runtime is None:
                return 404, {"error": f"平台实例不存在: {platform_id}"}
            session_manager = runtime[0]
            if session_manager.get_session_config(session_id) is None:
                return 404, {"error": "会话实例不存在"}
            result = await session_manager.restart_session_async(session_id)
            return (200 if result.get("ok", False) else 400), result
        # 接口: POST /api/sessions/{session_id}/restart

        if (
            method == "DELETE"
            and route_path.startswith(session_path_prefix)
        ):
            session_id = unquote(route_path[len(session_path_prefix):]).strip()
            if not session_id:
                return 400, {"error": "session_id 不能为空"}
            platform_id = self._query_param(path, "platform_id")
            runtimes = _platform_runtimes(backend)
            if not platform_id:
                matches = [
                    runtime_id
                    for runtime_id, (session_manager, _) in runtimes.items()
                    if session_manager.get_session_config(session_id) is not None
                ]
                if len(matches) != 1:
                    return 400, {"error": "删除会话实例必须提供 platform_id"}
                platform_id = matches[0]
            runtime = runtimes.get(platform_id)
            if runtime is None:
                return 404, {"error": f"平台实例不存在: {platform_id}"}
            deleted_ids = await runtime[0].delete_sessions_async([session_id])
            if not deleted_ids:
                return 404, {"error": "会话实例不存在"}
            return 200, {
                "ok": True,
                "deleted_count": 1,
                "deleted_ids": deleted_ids,
                "deleted_refs": [{"platform_id": platform_id, "session_id": session_id}],
            }
        # 接口: DELETE /api/sessions/{session_id}

        if (
            method == "POST"
            and route_path == "/api/sessions"
        ):
            try:
                payload = _parse_json_object(body)
                adapter_id = str(payload.get("adapter_id", "")).strip()
                platform_id = str(payload.get("platform_id", "")).strip() or adapter_id or LOCAL_PLATFORM_ID
                runtime = _platform_runtimes(backend).get(platform_id)
                if runtime is None:
                    raise ValueError(f"未知平台实例: {platform_id}")
                session_manager = runtime[0]
                provider_name = str(
                    payload.get("session_provider")
                    or payload.get("provider_name")
                    or SESSION_CLASS_PROVIDER
                ).strip()
                definition_name = str(
                    payload.get("session_type")
                    or payload.get("class_name")
                    or ""
                ).strip()
                if not definition_name:
                    raise ValueError("session_type 不能为空")
                resolved = session_manager.provider_registry.resolve_definition(
                    definition_name,
                    provider_name,
                )
                if resolved is None:
                    raise ValueError(
                        f"未知会话定义: provider={provider_name}, name={definition_name}"
                    )
                _, definition = resolved
                raw_extra_params = cast(object, payload.get("params") or {})
                if not isinstance(raw_extra_params, dict):
                    raise ValueError("params 必须是对象")
                extra_params = dict(cast(dict[str, Any], raw_extra_params))
                if adapter_id:
                    extra_params["adapter_id"] = adapter_id
                llm_name = str(payload.get("llm_name", "")).strip()
                if llm_name:
                    model_key = definition.model_key or "model_name"
                    extra_params[model_key] = llm_name
                requested_session_id = str(payload.get("session_id", "")).strip()
                if (
                    requested_session_id
                    and session_manager.get_session_config(requested_session_id) is not None
                ):
                    raise ValueError(f"session_id 已存在: {requested_session_id}")
                config = session_manager.register_session_from_provider_config(
                    provider_name,
                    definition_name,
                    session_id=requested_session_id or None,
                    extra_params=extra_params,
                )
                if bool(payload.get("activate", False)):
                    session_id = config.session_id or ""
                    if not await session_manager.activate_session_async(session_id):
                        await session_manager.remove_session_async(
                            session_id,
                            remove_config=True,
                        )
                        raise ValueError(
                            f"会话实例激活失败: provider={provider_name}, name={definition_name}"
                        )
                serialized = dataclasses.asdict(config)
                serialized["platform_id"] = platform_id
                serialized["active"] = bool(payload.get("activate", False))
                serialized["runtime"] = session_manager.get_session_runtime_metadata(
                    config.session_id or ""
                )
                return 200, {"ok": True, "session": serialized}
            except Exception as e:
                return 400, {"error": str(e)}
        # 接口: POST /api/sessions

        return None

    async def _route_session_class_items(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """会话类配置项 (启停 / 详情 / 更新 / 删除, 原始 path 前缀匹配)"""
        backend = self.backend
        path_prefix = "/api/config/session-classes/"

        if method == "POST" and path.startswith(path_prefix) and path.endswith("/enable") and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):-len("/enable")])
            updated = SessionClassConfigService(backend.session_class_mgr).set_enabled(name, True)
            return 200, {"ok": True, "config": updated}
        # 接口: POST /api/config/session-classes/{name}/enable

        if method == "POST" and path.startswith(path_prefix) and path.endswith("/disable") and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):-len("/disable")])
            updated = SessionClassConfigService(backend.session_class_mgr).set_enabled(name, False)
            return 200, {"ok": True, "config": updated}
        # 接口: POST /api/config/session-classes/{name}/disable

        # 接口: GET /api/config/session-classes/{name}
        if method == "GET" and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            cfg = SessionClassConfigService(backend.session_class_mgr).get(name)
            if cfg is None:
                return 404, {"error": "not found"}
            return 200, cfg

        if method in {"PATCH", "PUT"} and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            try:
                payload = _parse_json_object(body)
                updated = SessionClassConfigService(backend.session_class_mgr).update(name, payload)
                return 200, {"ok": True, "config": updated}
            except Exception as e:
                return 400, {"error": str(e)}
        # 接口: PUT /api/config/session-classes/{name}

        if method == "DELETE" and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            if SessionClassConfigService(backend.session_class_mgr).delete(name):
                return 200, {"ok": True}
            return 404, {"error": "not found"}
        # 接口: DELETE /api/config/session-classes/{name}

        return None

    async def _route_models(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """模型配置 (集合查询 / 项创建, 更新, 删除, 原始 path 前缀匹配)"""
        backend = self.backend

        if method == "GET" and path.startswith("/api/config/models") and backend.model_config_manager:
            typ = "llm"
            qs = path.split("?", 1)[1] if "?" in path else ""
            for pair in qs.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k == "type":
                        typ = v
            try:
                return 200, ModelConfigService(backend.model_config_manager).list_configs(typ)
            except ValueError as e:
                return 400, {"error": str(e)}
        # 接口: GET /api/config/models?type=llm

        model_prefix = "/api/config/models/"
        # 接口: POST/PATCH/DELETE /api/config/models/{type}/{name}
        if path.startswith(model_prefix) and backend.model_config_manager:
            parts = path[len(model_prefix):].split("/", 1)
            if len(parts) != 2:
                return 404, {"error": f"unknown route: {method} {path}"}
            typ = unquote(parts[0])
            name = unquote(parts[1])
            service = ModelConfigService(backend.model_config_manager)
            try:
                if method == "POST":
                    service.create(typ, name, json.loads(body or b"{}"))
                    return 200, {"ok": True}
                if method == "PATCH":
                    service.update(typ, name, json.loads(body or b"{}"))
                    return 200, {"ok": True}
                if method == "DELETE":
                    if service.delete(typ, name):
                        return 200, {"ok": True}
                    return 404, {"error": "not found"}
            except Exception as e:
                return 400, {"error": str(e)}

        return None

    async def _route_user(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """用户管理 (列表 / 详情 / 会话列表 / 创建, 更新, 删除, 绑定, 解绑)"""
        backend = self.backend

        # 接口: GET /api/users (列表, 支持 ?limit=) / GET /api/users?user_id=xxx (详情)
        # 接口: GET /api/user/sessions?user_id=xxx
        # 接口: POST /api/user/{create|update|delete|bind|unbind}
        if path.startswith("/api/user"):
            try:
                platform_id = self._query_param(path, "platform_id") or LOCAL_PLATFORM_ID
                user_db = _platform_db_path(backend, platform_id)
                if method == "GET" and path.startswith("/api/users"):
                    user_id = self._query_param(path, "user_id")
                    if user_id:
                        return 200, user_api.get_user(user_db, user_id)
                    limit = int(self._query_param(path, "limit") or "200")
                    if not 1 <= limit <= 1000:
                        return 400, {"error": "limit 必须在 1 到 1000 之间"}
                    return 200, user_api.list_users(user_db, limit=limit)
                if method == "GET" and path.startswith("/api/user/sessions"):
                    user_id = self._query_param(path, "user_id")
                    if not user_id:
                        return 400, {"error": "缺少 user_id 参数"}
                    return 200, user_api.list_user_sessions(user_db, user_id)
                if method == "POST":
                    payload = json.loads(body or b"{}")
                    platform_id = str(payload.get("platform_id", "")).strip() or LOCAL_PLATFORM_ID
                    user_db = _platform_db_path(backend, platform_id)
                    user_id = str(payload.get("user_id", "")).strip()
                    if not user_id:
                        return 400, {"error": "缺少 user_id 参数"}
                    if path == "/api/user/create":
                        return 200, user_api.create_user(
                            user_db, user_id,
                            platform=str(payload.get("platform", "")) or platform_id,
                            nickname=str(payload.get("nickname", "")),
                        )
                    if path == "/api/user/update":
                        return 200, user_api.update_user(
                            user_db, user_id,
                            nickname=payload.get("nickname"),
                            platform=payload.get("platform"),
                        )
                    if path == "/api/user/delete":
                        return 200, user_api.delete_user(user_db, user_id)
                    if path == "/api/user/bind":
                        session_id = str(payload.get("session_id", "")).strip()
                        if not session_id:
                            return 400, {"error": "缺少 session_id 参数"}
                        return 200, user_api.bind_session(user_db, user_id, session_id)
                    if path == "/api/user/unbind":
                        session_id = str(payload.get("session_id", "")).strip()
                        if not session_id:
                            return 400, {"error": "缺少 session_id 参数"}
                        return 200, user_api.unbind_session(user_db, user_id, session_id)
            except (ValueError, KeyError, IndexError) as e:
                return 400, {"error": str(e)}

        return None

    async def _route_checkpoint(self, method: str, path: str, body: bytes) -> RouteResponse | None:
        """会话检查点 (列表 / 分支 / 血缘 / 审计 / 创建, 回滚, 重试, 分叉)"""
        backend = self.backend

        # 接口: GET /api/checkpoints?conversation=xxx
        # 接口: GET /api/checkpoint/branches?conversation=xxx
        # 接口: GET /api/checkpoint/lineage?checkpoint_id=xxx
        # 接口: GET /api/checkpoint/audit?conversation=xxx
        # 接口: POST /api/checkpoint/{create|rollback|retry|fork}
        if path.startswith("/api/checkpoint"):
            try:
                platform_id = self._query_param(path, "platform_id") or LOCAL_PLATFORM_ID
                db = _platform_db_path(backend, platform_id)
                if method == "GET" and path.startswith("/api/checkpoints"):
                    conv = self._query_param(path, "conversation")
                    return 200, checkpoint_api.list_checkpoints(db, conv)
                if method == "GET" and path.startswith("/api/checkpoint/branches"):
                    conv = self._query_param(path, "conversation")
                    return 200, checkpoint_api.list_branches(db, conv)
                if method == "GET" and path.startswith("/api/checkpoint/lineage"):
                    cid = self._query_param(path, "checkpoint_id")
                    return 200, checkpoint_api.trace_lineage(db, cid)
                if method == "GET" and path.startswith("/api/checkpoint/audit"):
                    conv = self._query_param(path, "conversation")
                    return 200, checkpoint_api.list_mutations(db, conv)
                if method == "POST":
                    payload = json.loads(body or b"{}")
                    platform_id = str(payload.get("platform_id", "")).strip() or LOCAL_PLATFORM_ID
                    db = _platform_db_path(backend, platform_id)
                    conv = str(payload.get("conversation", "")).strip()
                    if not conv:
                        return 400, {"error": "缺少 conversation 参数"}
                    if path == "/api/checkpoint/create":
                        return 200, checkpoint_api.create_checkpoint(
                            db, conv,
                            name=str(payload.get("name", "")),
                            description=str(payload.get("description", "")),
                        )
                    if path == "/api/checkpoint/rollback":
                        return 200, checkpoint_api.rollback_checkpoint(
                            db, conv, str(payload.get("checkpoint_id", ""))
                        )
                    if path == "/api/checkpoint/retry":
                        return 200, checkpoint_api.retry_checkpoint(
                            db, conv, str(payload.get("checkpoint_id", ""))
                        )
                    if path == "/api/checkpoint/fork":
                        cid = payload.get("checkpoint_id")
                        return 200, checkpoint_api.fork_checkpoint(
                            db, conv,
                            branch_name=str(payload.get("branch_name", "")),
                            checkpoint_id=str(cid) if cid else None,
                        )
            except (ValueError, KeyError, IndexError) as e:
                return 400, {"error": str(e)}

        return None
