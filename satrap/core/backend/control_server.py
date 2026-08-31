"""
后端控制服务 - 独立运行, 用于启动/停止/监控后端

这是一个简单的 HTTP 服务, 独立于主后端运行,
提供启动, 停止, 重启后端的功能, 以及配置文件的读写
默认监听 127.0.0.1:19871

特性:
- 单实例锁防止重复启动
- PID 文件管理
- 前端关闭时自动停止后端
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import ctypes
import dataclasses
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
import urllib.error
import urllib.parse
import urllib.request

from satrap.core.backend.static_ui import DEFAULT_STATIC_DIR, SPAStaticService
from satrap.core.backend.ui_config import build_ui_config
from satrap.core.config.document import (
    create_default_config,
    delete_platform,
    find_config_path,
    load_config_document,
    save_config_document,
    upsert_platform,
    validate_config_document,
    validate_platforms,
)
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.session_discovery import (
    SessionClassDiscoveryService,
    create_default_session_dir,
)
from satrap.core.config.model_service import ModelConfigService
from satrap.core.config.edictum_service import EdictumConfigService
from satrap.core.config.session_instance_service import SessionInstanceConfigService
from satrap.core.config.session_class_service import SessionClassConfigService
from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.framework.providers.base import SESSION_CLASS_PROVIDER
from satrap.core.storage import (
    CHAT_PLATFORM_ID,
    LOCAL_PLATFORM_ID,
    StorageLayout,
    StorageMaintenanceService,
)
from satrap.core.utils.paths import get_project_root
from satrap.display.recorder import query_conversations
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import EDICTUM_PROVIDER, create_default_edictum_type_registry

PROJECT_ROOT = get_project_root()
# 项目根目录

DATA_DIR = PROJECT_ROOT / ".satrap"
# 数据目录
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONTROL_PID_FILE = DATA_DIR / "control_server.pid"
# PID 文件路径
BACKEND_PID_FILE = DATA_DIR / "backend.pid"

_backend_process: subprocess.Popen | None = None
# 后端进程

CONFIG_PATH = find_config_path(PROJECT_ROOT)
# 配置文件路径

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
# CORS 头

CONTROL_STATIC_UI = SPAStaticService(
    DEFAULT_STATIC_DIR,
    excluded_prefixes=(
        "/status",
        "/start",
        "/stop",
        "/restart",
        "/shutdown",
        "/config",
        "/ui-config.json",
    ),
)
"""控制服务使用的 React SPA 静态文件服务"""


def _write_pid_file(pid_file: Path, pid: int) -> None:
    """
    写入 PID 文件

    参数:
    - pid_file: pid文件
    - pid: 进程 ID
    """
    try:
        pid_file.write_text(str(pid), encoding="utf-8")
    except Exception:
        pass


def _read_pid_file(pid_file: Path) -> int | None:
    """
    读取 PID 文件

    参数:
    - pid_file: pid文件

    返回:
    - int | None: 读取 PID 文件
    """
    try:
        if pid_file.exists():
            return int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        pass
    return None


def _remove_pid_file(pid_file: Path) -> None:
    """
    删除 PID 文件

    参数:
    - pid_file: pid文件
    """
    try:
        if pid_file.exists():
            pid_file.unlink()
    except Exception:
        pass


def _is_process_running(pid: int) -> bool:
    """
    检查进程是否在运行

    参数:
    - pid: 进程 ID

    返回:
    - bool: 检查结果
    """
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # Windows 进程查询权限标志
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _check_single_instance() -> bool:
    """
    检查是否已有控制服务实例在运行

    返回 True 表示可以继续启动, False 表示已有实例

    返回:
    - bool: 检查结果
    """
    if not CONTROL_PID_FILE.exists():
        return True
    
    old_pid = _read_pid_file(CONTROL_PID_FILE)
    if old_pid is None:
        return True
    
    if _is_process_running(old_pid):
        try:
            url = "http://127.0.0.1:19871/status"
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return False   # 已有实例在运行
        except Exception:
            pass
        # 检查是否是我们的控制服务
    
    _remove_pid_file(CONTROL_PID_FILE)
    # 旧进程已不存在, 清理 PID 文件
    return True


def _cleanup_backend() -> None:
    """清理后端进程"""
    global _backend_process
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:19870/api/shutdown",
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass
    # 尝试通过 API 停止
    
    if _backend_process is not None:
        try:
            _backend_process.terminate()
            _backend_process.wait(timeout=5)
        except Exception:
            try:
                _backend_process.kill()
            except Exception:
                pass
        _backend_process = None
    # 终止我们启动的进程
    
    backend_pid = _read_pid_file(BACKEND_PID_FILE)
    # 通过 PID 文件终止
    if backend_pid and _is_process_running(backend_pid):
        try:
            if sys.platform == "win32":
                os.kill(backend_pid, signal.SIGTERM)
            else:
                os.kill(backend_pid, signal.SIGTERM)
        except Exception:
            pass
    
    _remove_pid_file(BACKEND_PID_FILE)


def _cleanup_control() -> None:
    """清理控制服务"""
    _cleanup_backend()
    _remove_pid_file(CONTROL_PID_FILE)


def _get_backend_cmd(host: str = "127.0.0.1", port: int = 19870) -> list[str]:
    """
    获取后端启动命令

    参数:
    - host: 主机
    - port: 端口

    返回:
    - list[str]: 后端启动命令
    """
    return [
        sys.executable,
        "-m",
        "satrap.main",
        "--api-host", host,
        "--api-port", str(port),
        "run",
    ]


def _check_backend_health(host: str = "127.0.0.1", port: int = 19870) -> dict[str, Any]:
    """
    检查后端健康状态

    参数:
    - host: 主机
    - port: 端口

    返回:
    - dict[str, Any]: 检查结果
    """
    try:
        url = f"http://{host}:{port}/api/health"
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode())
    except urllib.error.URLError:
        return {"running": False}
    except Exception as e:
        return {"running": False, "error": str(e)}


async def _read_json_body(
    reader: asyncio.StreamReader,
    raw_request: bytes,
) -> dict[str, Any]:
    """
    读取并解析 JSON 请求体

    参数:
    - reader: 流读取器
    - raw_request: HTTP 请求头

    返回:
    - dict[str, Any]: JSON 对象
    """
    content_length: int | None = None
    for raw_line in raw_request.split(b"\r\n")[1:]:
        if raw_line.lower().startswith(b"content-length:"):
            content_length = int(raw_line.split(b":", 1)[1].strip())
            break
    if content_length is None or content_length <= 0:
        raise ValueError("缺少请求体")
    payload: object = json.loads((await reader.readexactly(content_length)).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return dict(cast(dict[str, Any], payload))


def _request_origin(raw_request: bytes) -> str:
    """
    从 HTTP 请求头解析当前控制服务来源地址

    参数:
    - raw_request: HTTP 请求头

    返回:
    - str: 控制服务来源地址
    """
    for raw_line in raw_request.split(b"\r\n")[1:]:
        if raw_line.lower().startswith(b"host:"):
            host = raw_line.split(b":", 1)[1].strip().decode("utf-8")
            if host:
                return f"http://{host}"
    return "http://127.0.0.1:19871"


def _request_headers(raw_request: bytes) -> dict[str, str]:
    """
    解析 HTTP 请求头

    参数:
    - raw_request: HTTP 请求头

    返回:
    - dict[str, str]: 小写键名的请求头
    """
    headers: dict[str, str] = {}
    for raw_line in raw_request.split(b"\r\n")[1:]:
        if b":" not in raw_line:
            continue
        key, value = raw_line.split(b":", 1)
        headers[key.decode("utf-8").strip().lower()] = value.decode("utf-8").strip()
    return headers


def _model_config_service() -> ModelConfigService:
    """
    根据当前后端配置创建共享模型配置服务

    返回:
    - ModelConfigService: 使用当前模型配置文件的领域服务
    """
    config_data = load_config_document(CONFIG_PATH)
    raw_path = config_data.get("model_config_path")
    storage_path: Path | None = None
    if raw_path:
        storage_path = Path(str(raw_path))
        if not storage_path.is_absolute():
            storage_path = PROJECT_ROOT / storage_path
    return ModelConfigService(ModelConfigManager(storage_path=storage_path))


def _session_class_config_service() -> SessionClassConfigService:
    """
    根据当前后端配置创建共享会话类配置服务

    返回:
    - SessionClassConfigService: 使用当前会话类配置文件的领域服务
    """
    config_data = load_config_document(CONFIG_PATH)
    raw_path = config_data.get("session_class_config_path")
    storage_path: Path | None = None
    if raw_path:
        storage_path = Path(str(raw_path))
        if not storage_path.is_absolute():
            storage_path = PROJECT_ROOT / storage_path
    manager = SessionClassConfigManager(
        storage_path=storage_path,
        session_scan_paths=_configured_session_scan_paths(config_data),
    )
    return SessionClassConfigService(manager)


def _edictum_config_service() -> EdictumConfigService:
    """
    根据当前后端配置创建共享 Edictum 冷配置服务

    返回:
    - EdictumConfigService: 使用当前 Edictum 配置文件的领域服务
    """
    config_data = load_config_document(CONFIG_PATH)
    raw_path = config_data.get("edictum_config_path")
    storage_path: Path | None = None
    if raw_path:
        storage_path = Path(str(raw_path))
        if not storage_path.is_absolute():
            storage_path = PROJECT_ROOT / storage_path
    registry = create_default_edictum_type_registry()
    manager = EdictumConfigManager(registry, storage_path=storage_path)
    return EdictumConfigService(manager, registry)


def _configured_storage_path(config_data: dict[str, Any], key: str) -> Path | None:
    """
    将配置中的可选存储路径解析为项目内绝对路径

    参数:
    - config_data: 当前后端配置文档
    - key: 路径配置键

    返回:
    - Path | None: 未配置时返回 None
    """
    raw_path = config_data.get(key)
    if not raw_path:
        return None
    storage_path = Path(str(raw_path))
    if not storage_path.is_absolute():
        storage_path = PROJECT_ROOT / storage_path
    return storage_path


def _session_instance_config_service(platform_id: str) -> SessionInstanceConfigService:
    """
    根据当前后端配置创建指定平台的会话实例冷管理服务

    参数:
    - platform_id: 平台实例 ID

    返回:
    - SessionInstanceConfigService: 共享运行时数据库的冷管理服务
    """
    config_data = load_config_document(CONFIG_PATH)
    raw_data_root = str(config_data.get("data_root", "")).strip()
    data_root = Path(raw_data_root) if raw_data_root else PROJECT_ROOT / ".satrap" / "data"
    if not data_root.is_absolute():
        data_root = PROJECT_ROOT / data_root
    storage_layout = StorageLayout(data_root)
    database = storage_layout.platform_db(platform_id)
    session_class_manager = SessionClassConfigManager(
        storage_path=_configured_storage_path(config_data, "session_class_config_path"),
        session_scan_paths=_configured_session_scan_paths(config_data),
    )
    edictum_registry = create_default_edictum_type_registry()
    edictum_manager = EdictumConfigManager(
        edictum_registry,
        storage_path=_configured_storage_path(config_data, "edictum_config_path"),
    )
    return SessionInstanceConfigService(
        SessionConfigStore(database),
        UserInfoStore(database),
        session_class_manager,
        edictum_manager,
        platform_id,
        storage_layout,
    )


def _configured_storage_layout(
    config_data: dict[str, Any] | None = None,
) -> StorageLayout:
    """
    根据当前配置返回 v2 数据布局

    参数:
    - config_data: 可选后端配置文档

    返回:
    - StorageLayout: 当前数据布局
    """
    document = config_data if config_data is not None else load_config_document(CONFIG_PATH)
    raw_data_root = str(document.get("data_root", "")).strip()
    data_root = Path(raw_data_root) if raw_data_root else PROJECT_ROOT / ".satrap" / "data"
    if not data_root.is_absolute():
        data_root = PROJECT_ROOT / data_root
    return StorageLayout(data_root)


def _configured_platform_ids(config_data: dict[str, Any] | None = None) -> list[str]:
    """
    返回可冷管理的平台实例 ID, 始终包含 local

    参数:
    - config_data: 可选后端配置文档

    返回:
    - list[str]: 去重后的平台实例 ID
    """
    document = config_data if config_data is not None else load_config_document(CONFIG_PATH)
    result = [LOCAL_PLATFORM_ID]
    raw_platforms: object = document.get("platforms", [])
    if isinstance(raw_platforms, list):
        for raw_platform in cast(list[object], raw_platforms):
            if not isinstance(raw_platform, dict):
                continue
            platform_id = str(cast(dict[str, Any], raw_platform).get("id", "")).strip()
            if (
                platform_id
                and platform_id != CHAT_PLATFORM_ID
                and platform_id not in result
            ):
                result.append(platform_id)
    return result


def _check_chat_health() -> bool:
    """检查独立 Chat 服务是否正在运行"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:19872/api/chat/health", timeout=0.4) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _require_chat_stopped() -> None:
    """阻止冷写操作与运行中的 Chat 服务争用状态"""
    if _check_chat_health():
        raise RuntimeError("Chat 服务正在运行, 请使用热管理接口")


def _cold_chat_history_query(
    *,
    search: str = "",
    project_id: str | None = None,
    model: str = "",
    turn_count: str = "all",
    older_than_days: float | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """
    从 Chat 平台数据库冷查询历史

    参数:
    - search: 标题或会话 ID 搜索文本
    - project_id: 项目 ID 或 `__none__`
    - model: 模型配置名称
    - turn_count: 轮数过滤
    - older_than_days: 最后使用时间过滤
    - page: 页码
    - page_size: 每页数量

    返回:
    - dict[str, Any]: 冷管理分页结果
    """
    layout = _configured_storage_layout()
    result = query_conversations(
        str(layout.platform_db(CHAT_PLATFORM_ID)),
        search=search,
        project_id=project_id,
        model=model,
        turn_count=turn_count,
        older_than_days=older_than_days,
        page=page,
        page_size=page_size,
    )
    for item in cast(list[dict[str, Any]], result["items"]):
        item.update({"active": False, "generating": False, "waiting_user": False})
    sessions_root = layout.platform_root(CHAT_PLATFORM_ID) / "sessions"
    result["storage_size_bytes"] = StorageMaintenanceService._directory_size(sessions_root)
    result["mode"] = "cold"
    return result


def _cold_chat_history_targets(
    mode: str,
    conversation_ids: list[str],
    filters: dict[str, Any],
) -> list[str]:
    """
    解析冷管理批量回收目标

    参数:
    - mode: selected、empty、single 或 filtered
    - conversation_ids: 显式选择的会话 ID
    - filters: 自定义过滤条件

    返回:
    - list[str]: 去重后的目标会话 ID
    """
    if mode == "selected":
        targets = list(dict.fromkeys(item.strip() for item in conversation_ids if item.strip()))
        if not targets:
            raise ValueError("至少选择一个会话")
        return targets
    if mode in {"empty", "single"}:
        query_filters: dict[str, Any] = {"turn_count": mode}
    elif mode != "filtered":
        raise ValueError(f"未知批量删除模式: {mode}")
    else:
        query_filters = dict(filters)
    raw_days = query_filters.get("older_than_days")
    page = 1
    results: list[str] = []
    while True:
        batch = _cold_chat_history_query(
            search=str(query_filters.get("search") or ""),
            project_id=str(query_filters.get("project_id") or "") or None,
            model=str(query_filters.get("model") or ""),
            turn_count=str(query_filters.get("turn_count") or "all"),
            older_than_days=float(raw_days) if raw_days is not None else None,
            page=page,
            page_size=200,
        )
        results.extend(
            str(item["conversation_id"])
            for item in cast(list[dict[str, Any]], batch["items"])
        )
        if len(results) >= int(batch["total"]):
            return results
        page += 1


def _edictum_config_references(config_name: str) -> list[dict[str, str]]:
    """
    列出全部可管理平台中的 Edictum 配置引用

    参数:
    - config_name: Edictum 配置名称

    返回:
    - list[dict[str, str]]: 平台和会话引用
    """
    layout = _configured_storage_layout()
    platform_ids = _configured_platform_ids()
    if CHAT_PLATFORM_ID not in platform_ids:
        platform_ids.append(CHAT_PLATFORM_ID)
    references: list[dict[str, str]] = []
    for platform_id in platform_ids:
        store = SessionConfigStore(layout.platform_db(platform_id))
        references.extend(
            {"platform_id": platform_id, "session_id": session_id}
            for session_id in store.list_definition_references(
                EDICTUM_PROVIDER,
                config_name,
            )
        )
    return references


def _rename_edictum_config_references(
    old_name: str,
    new_name: str,
) -> list[dict[str, str]]:
    """
    迁移全部可管理平台中的 Edictum 配置引用

    参数:
    - old_name: 原配置名称
    - new_name: 新配置名称

    返回:
    - list[dict[str, str]]: 已迁移的平台和会话引用
    """
    layout = _configured_storage_layout()
    platform_ids = _configured_platform_ids()
    if CHAT_PLATFORM_ID not in platform_ids:
        platform_ids.append(CHAT_PLATFORM_ID)
    completed: list[SessionConfigStore] = []
    migrated: list[dict[str, str]] = []
    try:
        for platform_id in platform_ids:
            store = SessionConfigStore(layout.platform_db(platform_id))
            session_ids = store.rename_definition_references(
                EDICTUM_PROVIDER,
                old_name,
                new_name,
            )
            completed.append(store)
            migrated.extend(
                {"platform_id": platform_id, "session_id": session_id}
                for session_id in session_ids
            )
    except Exception:
        for store in reversed(completed):
            store.rename_definition_references(
                EDICTUM_PROVIDER,
                new_name,
                old_name,
            )
        raise
    return migrated


def _require_backend_stopped() -> None:
    """阻止冷写操作与运行中的后端争用活跃会话状态"""
    if bool(_check_backend_health().get("running", False)):
        raise RuntimeError("平台后端运行中, 请使用热管理接口")


def _configured_session_scan_paths(
    config_data: dict[str, Any] | None = None,
) -> list[str]:
    """
    读取配置中的会话扫描目录

    参数:
    - config_data: 已加载的配置, 未提供时读取当前配置文件

    返回:
    - list[str]: 非空且保持配置顺序的扫描目录
    """
    document = config_data if config_data is not None else load_config_document(CONFIG_PATH)
    raw_paths: object = document.get("session_scan_paths", [".satrap/session"])
    if not isinstance(raw_paths, list):
        return [".satrap/session"]
    paths: list[str] = []
    for item in cast(list[object], raw_paths):
        text = str(item).strip()
        if text:
            paths.append(text)
    return paths or [".satrap/session"]


def _resolve_session_scan_paths(paths: list[str]) -> list[str]:
    """
    将会话扫描目录按项目根目录解析为绝对路径

    参数:
    - paths: 配置中的扫描目录

    返回:
    - list[str]: 可供发现服务使用的绝对路径
    """
    resolved: list[str] = []
    for item in paths:
        path = Path(item)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        resolved.append(str(path.resolve()))
    return resolved


async def _handle_request(   # pyright: ignore[reportGeneralTypeIssues] 控制路由集中维护, 运行时分支明确
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """
    处理 HTTP 请求

    参数:
    - reader: 流读取器
    - writer: 流写入器
    """
    global _backend_process
    
    try:
        raw_request = await reader.readuntil(b"\r\n\r\n")
        first_line = raw_request.split(b"\r\n")[0].decode()
        parts = first_line.split(" ")
        method = parts[0]
        raw_path = parts[1] if len(parts) > 1 else "/"
        path = urllib.parse.urlsplit(raw_path).path
        
        if method == "OPTIONS":
            response = "HTTP/1.1 204 No Content\r\n"
            for k, v in CORS_HEADERS.items():
                response += f"{k}: {v}\r\n"
            response += "\r\n"
            writer.write(response.encode())
            await writer.drain()
            return
        # CORS 预检
        
        status = 200
        body: dict[str, Any] = {}
        
        if method == "GET" and path == "/ui-config.json":
            try:
                config_data = load_config_document(CONFIG_PATH)
                raw_api_config: object = config_data.get("api", {})
                api_config = (
                    cast(dict[str, object], raw_api_config)
                    if isinstance(raw_api_config, dict)
                    else {}
                )
                raw_host = api_config.get("host", "127.0.0.1")
                raw_port = api_config.get("port", 19870)
                backend_host = raw_host if isinstance(raw_host, str) else "127.0.0.1"
                backend_port = int(raw_port) if isinstance(raw_port, (int, str)) else 19870
                body = build_ui_config(
                    backend_host=backend_host,
                    backend_port=backend_port,
                    control_api=_request_origin(raw_request),
                )
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "GET" and path == "/status":
            health = _check_backend_health()
            body = {
                "running": health.get("running", False),
                "managed": _backend_process is not None and _backend_process.poll() is None,
                "health": health,
            }

        elif method == "GET" and path == "/chat/history":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query)
                raw_days = str(query.get("older_than_days", [""])[0]).strip()
                body = await asyncio.to_thread(
                    _cold_chat_history_query,
                    search=str(query.get("search", [""])[0]),
                    project_id=str(query.get("project_id", [""])[0]) or None,
                    model=str(query.get("model", [""])[0]),
                    turn_count=str(query.get("turn_count", ["all"])[0]),
                    older_than_days=float(raw_days) if raw_days else None,
                    page=int(query.get("page", ["1"])[0]),
                    page_size=int(query.get("page_size", ["50"])[0]),
                )
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/chat/history/delete":
            try:
                _require_chat_stopped()
                payload = await _read_json_body(reader, raw_request)
                raw_ids = payload.get("conversation_ids", [])
                raw_filters = payload.get("filters", {})
                if not isinstance(raw_ids, list) or not isinstance(raw_filters, dict):
                    raise ValueError("conversation_ids 必须是数组且 filters 必须是对象")
                targets = _cold_chat_history_targets(
                    str(payload.get("mode") or "selected"),
                    [str(item) for item in raw_ids],
                    dict(cast(dict[str, Any], raw_filters)),
                )
                service = StorageMaintenanceService(_configured_storage_layout())
                results: list[dict[str, Any]] = []
                for conversation_id in targets:
                    try:
                        archived = await asyncio.to_thread(
                            service.archive_session,
                            CHAT_PLATFORM_ID,
                            conversation_id,
                        )
                        results.append({
                            "ok": True,
                            "conversation_id": conversation_id,
                            "archive_id": archived["archive_id"],
                        })
                    except Exception as error:
                        results.append({
                            "ok": False,
                            "conversation_id": conversation_id,
                            "error": str(error),
                        })
                body = {
                    "ok": all(item.get("ok", False) for item in results),
                    "deleted_count": sum(1 for item in results if item.get("ok", False)),
                    "results": results,
                }
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "GET" and path == "/chat/history/trash":
            try:
                items = await asyncio.to_thread(
                    StorageMaintenanceService(_configured_storage_layout()).list_archives,
                    CHAT_PLATFORM_ID,
                )
                body = {
                    "items": items,
                    "total": len(items),
                    "storage_size_bytes": sum(int(item["size_bytes"]) for item in items),
                    "mode": "cold",
                }
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/chat/history/trash/restore":
            try:
                _require_chat_stopped()
                payload = await _read_json_body(reader, raw_request)
                archive_id = str(payload.get("archive_id") or "").strip()
                if not archive_id:
                    raise ValueError("archive_id 不能为空")
                body = await asyncio.to_thread(
                    StorageMaintenanceService(_configured_storage_layout()).restore_archive,
                    CHAT_PLATFORM_ID,
                    archive_id,
                )
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/chat/history/trash/purge":
            try:
                _require_chat_stopped()
                payload = await _read_json_body(reader, raw_request)
                archive_id = str(payload.get("archive_id") or "").strip()
                if not archive_id:
                    raise ValueError("archive_id 不能为空")
                body = {
                    "ok": await asyncio.to_thread(
                        StorageMaintenanceService(_configured_storage_layout()).purge_archive,
                        CHAT_PLATFORM_ID,
                        archive_id,
                    ),
                    "archive_id": archive_id,
                }
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400
        
        elif method == "POST" and path == "/start":
            health = _check_backend_health()
            # 检查是否已在运行
            if health.get("running"):
                body = {"ok": True, "message": "后端已在运行中"}
            elif _backend_process is not None and _backend_process.poll() is None:
                body = {"ok": True, "message": "后端正在启动中"}
            else:
                old_backend_pid = _read_pid_file(BACKEND_PID_FILE)
                # 检查是否有其他后端进程
                if old_backend_pid and _is_process_running(old_backend_pid):
                    try:
                        os.kill(old_backend_pid, signal.SIGTERM)
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                    # 尝试终止旧进程
                
                cmd = _get_backend_cmd()
                # 启动后端
                
                startupinfo = None
                creationflags = 0
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    creationflags = subprocess.CREATE_NO_WINDOW
                
                try:
                    _backend_process = subprocess.Popen(
                        cmd,
                        cwd=str(PROJECT_ROOT),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        startupinfo=startupinfo,
                        creationflags=creationflags,
                    )
                    
                    _write_pid_file(BACKEND_PID_FILE, _backend_process.pid)
                    # 记录后端 PID
                    
                    for _ in range(30):   # 最多等待 15 秒
                        await asyncio.sleep(0.5)
                        health = _check_backend_health()
                        if health.get("running"):
                            body = {"ok": True, "message": "后端已启动"}
                            break
                    else:
                        body = {"ok": True, "message": "后端启动中，请稍候..."}
                    # 等待后端就绪
                        
                except Exception as e:
                    body = {"ok": False, "error": str(e)}
                    status = 500
        
        elif method == "POST" and path == "/stop":
            _cleanup_backend()
            body = {"ok": True, "message": "后端已停止"}
        
        elif method == "POST" and path == "/restart":
            _cleanup_backend()
            await asyncio.sleep(1)
            
            cmd = _get_backend_cmd()
            # 启动
            startupinfo = None
            creationflags = 0
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                creationflags = subprocess.CREATE_NO_WINDOW
            
            try:
                _backend_process = subprocess.Popen(
                    cmd,
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
                _write_pid_file(BACKEND_PID_FILE, _backend_process.pid)
                body = {"ok": True, "message": "后端重启中"}
            except Exception as e:
                body = {"ok": False, "error": str(e)}
                status = 500
        
        elif method == "POST" and path == "/shutdown":
            body = {"ok": True, "message": "控制服务即将停止"}
            
            response_body = json.dumps(body).encode()
            # 发送响应后再停止
            response = f"HTTP/1.1 {status} OK\r\n"
            response += "Content-Type: application/json\r\n"
            response += f"Content-Length: {len(response_body)}\r\n"
            for k, v in CORS_HEADERS.items():
                response += f"{k}: {v}\r\n"
            response += "\r\n"
            
            writer.write(response.encode() + response_body)
            await writer.drain()
            writer.close()
            
            asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
            # 延迟停止
            return
        
        elif method == "GET" and path == "/config":
            try:
                config_data = load_config_document(CONFIG_PATH)
                body = {
                    "ok": True,
                    "config": config_data,
                    "path": str(CONFIG_PATH),
                    "exists": CONFIG_PATH.exists(),
                }
            except (OSError, ValueError) as e:
                body = {"ok": False, "error": str(e)}
                status = 400
        
        elif method == "PUT" and path == "/config":
            try:
                config_data = save_config_document(CONFIG_PATH, await _read_json_body(reader, raw_request))
                body = {
                    "ok": True,
                    "message": "配置已保存",
                    "config": config_data,
                    "path": str(CONFIG_PATH),
                    "exists": True,
                }
            except (json.JSONDecodeError, OSError, ValueError) as e:
                body = {"ok": False, "error": str(e)}
                status = 400

        elif method == "POST" and path == "/config/default":
            try:
                config_data = create_default_config(CONFIG_PATH)
                body = {
                    "ok": True,
                    "message": "默认配置已创建",
                    "config": config_data,
                    "path": str(CONFIG_PATH),
                    "exists": True,
                }
            except (OSError, ValueError) as e:
                body = {"ok": False, "error": str(e)}
                status = 400

        elif method == "POST" and path == "/config/validate":
            try:
                config_data = validate_config_document(await _read_json_body(reader, raw_request))
                body = {"ok": True, "config": config_data}
            except (json.JSONDecodeError, ValueError) as e:
                body = {"ok": False, "error": str(e)}
                status = 400

        elif method == "GET" and path == "/config/platforms":
            try:
                config_data = load_config_document(CONFIG_PATH)
                platforms = validate_platforms(config_data.get("platforms", []))
                body = {"ok": True, "platforms": platforms, "exists": CONFIG_PATH.exists()}
            except (OSError, ValueError) as e:
                body = {"ok": False, "error": str(e)}
                status = 400

        elif method == "POST" and path == "/config/platforms":
            try:
                payload = await _read_json_body(reader, raw_request)
                config_data = load_config_document(CONFIG_PATH)
                config_data["platforms"] = upsert_platform(config_data.get("platforms", []), payload)
                saved_config = save_config_document(CONFIG_PATH, config_data)
                body = {"ok": True, "platforms": saved_config["platforms"], "message": "平台已创建"}
            except (json.JSONDecodeError, OSError, ValueError) as e:
                body = {"ok": False, "error": str(e)}
                status = 400

        elif method == "PUT" and path.startswith("/config/platforms/"):
            try:
                original_id = urllib.parse.unquote(path.removeprefix("/config/platforms/"))
                payload = await _read_json_body(reader, raw_request)
                config_data = load_config_document(CONFIG_PATH)
                config_data["platforms"] = upsert_platform(
                    config_data.get("platforms", []),
                    payload,
                    original_id=original_id,
                )
                saved_config = save_config_document(CONFIG_PATH, config_data)
                body = {"ok": True, "platforms": saved_config["platforms"], "message": "平台已更新"}
            except (json.JSONDecodeError, OSError, ValueError) as e:
                body = {"ok": False, "error": str(e)}
                status = 400

        elif method == "DELETE" and path.startswith("/config/platforms/"):
            try:
                platform_id = urllib.parse.unquote(path.removeprefix("/config/platforms/"))
                config_data = load_config_document(CONFIG_PATH)
                config_data["platforms"] = delete_platform(config_data.get("platforms", []), platform_id)
                saved_config = save_config_document(CONFIG_PATH, config_data)
                body = {"ok": True, "platforms": saved_config["platforms"], "message": "平台已删除"}
            except (OSError, ValueError) as e:
                body = {"ok": False, "error": str(e)}
                status = 400

        elif method == "GET" and path == "/config/models":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query)
                model_type = str(query.get("type", ["llm"])[0])
                body = _model_config_service().list_configs(model_type)
            except (OSError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif path.startswith("/config/models/"):
            parts = path.removeprefix("/config/models/").split("/", 1)
            if len(parts) != 2:
                body = {"error": f"not found: {method} {path}"}
                status = 404
            else:
                model_type = urllib.parse.unquote(parts[0])
                name = urllib.parse.unquote(parts[1])
                try:
                    service = _model_config_service()
                    if method == "POST":
                        service.create(model_type, name, await _read_json_body(reader, raw_request))
                        body = {"ok": True}
                    elif method == "PATCH":
                        service.update(model_type, name, await _read_json_body(reader, raw_request))
                        body = {"ok": True}
                    elif method == "DELETE":
                        if service.delete(model_type, name):
                            body = {"ok": True}
                        else:
                            body = {"error": "not found"}
                            status = 404
                    else:
                        body = {"error": f"not found: {method} {path}"}
                        status = 404
                except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                    body = {"error": str(e)}
                    status = 400

        elif method == "GET" and path == "/config/session-classes":
            try:
                body = _session_class_config_service().list_configs()
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "GET" and path == "/storage/audit":
            try:
                config_data = load_config_document(CONFIG_PATH)
                configured_platforms = {
                    CHAT_PLATFORM_ID,
                    *_configured_platform_ids(config_data),
                }
                items = await asyncio.to_thread(
                    StorageMaintenanceService(
                        _configured_storage_layout(config_data)
                    ).scan,
                    configured_platforms,
                )
                body = {
                    "items": [item.to_dict() for item in items],
                    "summary": {
                        "count": len(items),
                        "size_bytes": sum(item.size_bytes for item in items),
                    },
                }
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/storage/cleanup":
            try:
                _require_backend_stopped()
                payload = await _read_json_body(reader, raw_request)
                raw_ids = payload.get("item_ids", [])
                if not isinstance(raw_ids, list):
                    raise ValueError("item_ids 必须是数组")
                results = await asyncio.to_thread(
                    StorageMaintenanceService(_configured_storage_layout()).cleanup,
                    [str(item) for item in raw_ids],
                )
                body = {
                    "ok": all(item.get("ok", False) for item in results),
                    "results": results,
                }
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/storage/trash/restore":
            try:
                _require_backend_stopped()
                payload = await _read_json_body(reader, raw_request)
                body = await asyncio.to_thread(
                    StorageMaintenanceService(_configured_storage_layout()).restore_archive,
                    str(payload.get("platform_id", "")).strip(),
                    str(payload.get("archive_id", "")).strip(),
                )
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/storage/trash/purge":
            try:
                _require_backend_stopped()
                payload = await _read_json_body(reader, raw_request)
                deleted = await asyncio.to_thread(
                    StorageMaintenanceService(_configured_storage_layout()).purge_archive,
                    str(payload.get("platform_id", "")).strip(),
                    str(payload.get("archive_id", "")).strip(),
                )
                body = {"ok": deleted}
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/storage/trash/purge-batch":
            try:
                _require_backend_stopped()
                payload = await _read_json_body(reader, raw_request)
                raw_refs = payload.get("archive_refs")
                if raw_refs is not None and not isinstance(raw_refs, list):
                    raise ValueError("archive_refs 必须是数组")
                archive_refs = [
                    dict(cast(dict[str, str], item))
                    for item in raw_refs or []
                    if isinstance(item, dict)
                ]
                raw_days = payload.get("older_than_days")
                results = await asyncio.to_thread(
                    StorageMaintenanceService(_configured_storage_layout()).purge_archives,
                    archive_refs=archive_refs,
                    older_than_days=(float(raw_days) if raw_days is not None else None),
                    platform_id=str(payload.get("platform_id", "")).strip() or None,
                )
                body = {
                    "ok": all(item.get("ok", False) for item in results),
                    "results": results,
                }
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "GET" and path == "/config/edictum/types":
            try:
                body = {"types": _edictum_config_service().list_types()}
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "GET" and path == "/config/edictum/plugins":
            try:
                body = {"plugins": _edictum_config_service().list_plugins()}
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "GET" and path == "/config/edictum/sessions":
            try:
                body = _edictum_config_service().list_configs()
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "GET" and path == "/config/session-instances":
            try:
                sessions: list[dict[str, Any]] = []
                for platform_id in _configured_platform_ids():
                    sessions.extend(_session_instance_config_service(platform_id).list_instances())
                sessions.sort(key=lambda item: float(item.get("last_used_at") or 0), reverse=True)
                body = {"sessions": sessions}
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/config/session-instances":
            try:
                _require_backend_stopped()
                payload = await _read_json_body(reader, raw_request)
                raw_params: object = payload.get("params", {})
                if not isinstance(raw_params, dict):
                    raise ValueError("params 必须是对象")
                adapter_id = str(payload.get("adapter_id", "")).strip()
                platform_id = str(payload.get("platform_id", "")).strip() or adapter_id or LOCAL_PLATFORM_ID
                if platform_id not in _configured_platform_ids():
                    raise ValueError(f"未知平台实例: {platform_id}")
                extra_params = dict(cast(dict[str, Any], raw_params))
                if adapter_id:
                    extra_params["adapter_id"] = adapter_id
                created = _session_instance_config_service(platform_id).create_instance(
                    str(payload.get("session_provider") or payload.get("provider_name") or SESSION_CLASS_PROVIDER),
                    str(payload.get("session_type") or payload.get("class_name") or ""),
                    session_id=str(payload.get("session_id", "")).strip() or None,
                    llm_name=str(payload.get("llm_name", "")).strip() or None,
                    extra_params=extra_params,
                )
                serialized = dataclasses.asdict(created)
                serialized["platform_id"] = platform_id
                serialized["active"] = False
                serialized["runtime"] = {}
                body = {"ok": True, "session": serialized}
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/config/session-instances/bulk-delete":
            try:
                _require_backend_stopped()
                payload = await _read_json_body(reader, raw_request)
                mode = str(payload.get("mode", "selected")).strip()
                platform_ids = _configured_platform_ids()
                targets: dict[str, list[str]] = {}
                if mode == "selected":
                    raw_refs: object = payload.get("session_refs", [])
                    if not isinstance(raw_refs, list):
                        raise ValueError("session_refs 必须是数组")
                    for raw_ref in cast(list[object], raw_refs):
                        if not isinstance(raw_ref, dict):
                            raise ValueError("session_refs 的每一项必须是对象")
                        ref = cast(dict[str, Any], raw_ref)
                        platform_id = str(ref.get("platform_id", "")).strip()
                        session_id = str(ref.get("session_id", "")).strip()
                        if platform_id not in platform_ids:
                            raise ValueError(f"未知平台实例: {platform_id}")
                        if session_id:
                            targets.setdefault(platform_id, []).append(session_id)
                    if not any(targets.values()):
                        raise ValueError("至少选择一个会话实例")
                elif mode in {"empty", "single"}:
                    targets = {platform_id: [] for platform_id in platform_ids}
                else:
                    raise ValueError(f"未知批量删除模式: {mode}")
                deleted_refs: list[dict[str, str]] = []
                for platform_id, session_ids in targets.items():
                    deleted = _session_instance_config_service(platform_id).delete_by_mode(
                        mode,
                        session_ids,
                    )
                    deleted_refs.extend(
                        {"platform_id": platform_id, "session_id": session_id}
                        for session_id in deleted
                    )
                body = {
                    "ok": True,
                    "deleted_count": len(deleted_refs),
                    "deleted_ids": [item["session_id"] for item in deleted_refs],
                    "deleted_refs": deleted_refs,
                }
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif path.startswith("/config/session-instances/"):
            session_id = urllib.parse.unquote(path.removeprefix("/config/session-instances/")).strip()
            try:
                if method != "DELETE":
                    body = {"error": f"not found: {method} {path}"}
                    status = 404
                elif not session_id:
                    body = {"error": "session_id 不能为空"}
                    status = 400
                else:
                    _require_backend_stopped()
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query)
                    platform_id = str(query.get("platform_id", [""])[0]).strip()
                    if platform_id not in _configured_platform_ids():
                        raise ValueError(f"未知平台实例: {platform_id}")
                    deleted = _session_instance_config_service(platform_id).delete_instances([session_id])
                    if deleted:
                        body = {
                            "ok": True,
                            "deleted_count": 1,
                            "deleted_ids": deleted,
                            "deleted_refs": [{"platform_id": platform_id, "session_id": session_id}],
                        }
                    else:
                        body = {"error": "会话实例不存在"}
                        status = 404
            except RuntimeError as e:
                body = {"error": str(e)}
                status = 409
            except (OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/config/edictum/sessions":
            try:
                created = _edictum_config_service().create(
                    await _read_json_body(reader, raw_request)
                )
                body = {"ok": True, "config": created}
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif path.startswith("/config/edictum/sessions/"):
            suffix = path.removeprefix("/config/edictum/sessions/")
            action = ""
            if suffix.endswith("/enable"):
                suffix = suffix.removesuffix("/enable")
                action = "enable"
            elif suffix.endswith("/disable"):
                suffix = suffix.removesuffix("/disable")
                action = "disable"
            name = urllib.parse.unquote(suffix)
            try:
                service = _edictum_config_service()
                if method == "GET" and not action:
                    config_entry = service.get(name)
                    if config_entry is None:
                        body = {"error": "not found"}
                        status = 404
                    else:
                        body = config_entry
                elif method == "PATCH" and not action:
                    payload = await _read_json_body(reader, raw_request)
                    previous = service.get(name)
                    final_name, updated = service.update(name, payload)
                    migrated_refs: list[dict[str, str]] = []
                    if final_name != name:
                        try:
                            migrated_refs = _rename_edictum_config_references(
                                name,
                                final_name,
                            )
                        except Exception:
                            if previous is not None:
                                service.update(final_name, {**previous, "name": name})
                            raise
                    body = {
                        "ok": True,
                        "name": final_name,
                        "config": updated,
                        "migrated_refs": migrated_refs,
                    }
                elif method == "POST" and action:
                    updated = service.set_enabled(name, action == "enable")
                    body = {"ok": True, "config": updated}
                elif method == "DELETE" and not action:
                    references = _edictum_config_references(name)
                    if references:
                        body = {
                            "error": f"Edictum 配置仍被 {len(references)} 个会话实例引用",
                            "references": references,
                        }
                        status = 409
                    elif service.delete(name):
                        body = {"ok": True}
                    else:
                        body = {"error": "not found"}
                        status = 404
                else:
                    body = {"error": f"not found: {method} {path}"}
                    status = 404
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/config/session-classes":
            try:
                created = _session_class_config_service().create(
                    await _read_json_body(reader, raw_request)
                )
                body = {"ok": True, "config": created}
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "GET" and path == "/config/session/discovery":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query)
                requested_paths = [item for item in query.get("path", []) if item.strip()]
                configured_paths = _configured_session_scan_paths()
                if any(item not in configured_paths for item in requested_paths):
                    raise ValueError("只能扫描配置中的 Session 目录")
                scan_paths = requested_paths or configured_paths
                resolved_configured_paths = _resolve_session_scan_paths(configured_paths)
                resolved_scan_paths = _resolve_session_scan_paths(scan_paths)
                results = [
                    item.to_dict()
                    for item in SessionClassDiscoveryService(
                        resolved_configured_paths
                    ).discover(resolved_scan_paths)
                ]
                body = {"paths": configured_paths, "results": results}
            except (ImportError, OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif method == "POST" and path == "/config/session/discovery/directories":
            try:
                payload = await _read_json_body(reader, raw_request)
                requested_path = str(payload.get("path", "")).strip()
                configured_paths = _configured_session_scan_paths()
                if requested_path and requested_path not in configured_paths:
                    raise ValueError("只能创建配置中的 Session 扫描目录")
                target_paths = [requested_path] if requested_path else configured_paths
                target = create_default_session_dir(_resolve_session_scan_paths(target_paths))
                body = {"ok": True, "path": str(target)}
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400

        elif path.startswith("/config/session-classes/"):
            suffix = path.removeprefix("/config/session-classes/")
            action = ""
            if suffix.endswith("/enable"):
                suffix = suffix.removesuffix("/enable")
                action = "enable"
            elif suffix.endswith("/disable"):
                suffix = suffix.removesuffix("/disable")
                action = "disable"
            name = urllib.parse.unquote(suffix)
            try:
                service = _session_class_config_service()
                if method == "GET" and not action:
                    config_entry = service.get(name)
                    if config_entry is None:
                        body = {"error": "not found"}
                        status = 404
                    else:
                        body = config_entry
                elif method == "PATCH" and not action:
                    updated = service.update(name, await _read_json_body(reader, raw_request))
                    body = {"ok": True, "config": updated}
                elif method == "POST" and action:
                    updated = service.set_enabled(name, action == "enable")
                    body = {"ok": True, "config": updated}
                elif method == "DELETE" and not action:
                    if service.delete(name):
                        body = {"ok": True}
                    else:
                        body = {"error": "not found"}
                        status = 404
                else:
                    body = {"error": f"not found: {method} {path}"}
                    status = 404
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                body = {"error": str(e)}
                status = 400
        
        elif method == "GET" and await CONTROL_STATIC_UI.serve(
            writer,
            raw_path,
            _request_headers(raw_request),
        ):
            await writer.drain()
            return

        else:
            status = 404
            body = {"error": "not found"}
        # 接口: GET /status - 获取后端状态
        # 接口: POST /start - 启动后端
        # 接口: POST /stop - 停止后端
        # 接口: POST /restart - 重启后端
        # 接口: POST /shutdown - 停止控制服务和后端
        # 接口: GET /config - 读取配置文件
        # 接口: PUT /config - 保存配置文件
        # 接口: POST /config/default - 创建默认配置文件
        # 接口: POST /config/validate - 校验配置文件
        # 接口: GET/POST/PUT/DELETE /config/platforms - 管理平台配置
        # 接口: GET/POST/PATCH/DELETE /config/models - 管理模型配置
        # 接口: GET/POST/PATCH/DELETE /config/session-classes - 冷管理会话类配置
        # 接口: GET/POST/PATCH/DELETE /config/edictum - 冷管理 Edictum 命名配置
        # 接口: GET/POST/DELETE /config/session-instances - 冷管理持久化会话实例
        # 接口: GET /config/session/discovery - 冷扫描会话类
        # 接口: POST /config/session/discovery/directories - 冷创建会话扫描目录
        
        response_body = json.dumps(body).encode()
        # 发送响应
        response = f"HTTP/1.1 {status} OK\r\n"
        response += "Content-Type: application/json\r\n"
        response += f"Content-Length: {len(response_body)}\r\n"
        for k, v in CORS_HEADERS.items():
            response += f"{k}: {v}\r\n"
        response += "\r\n"
        
        writer.write(response.encode() + response_body)
        await writer.drain()
        
    except Exception as e:
        error_body = json.dumps({"error": str(e)}).encode()
        # 发送错误响应
        response = f"HTTP/1.1 500 Internal Server Error\r\n"
        response += "Content-Type: application/json\r\n"
        response += f"Content-Length: {len(error_body)}\r\n"
        for k, v in CORS_HEADERS.items():
            response += f"{k}: {v}\r\n"
        response += "\r\n"
        writer.write(response.encode() + error_body)
        await writer.drain()
    finally:
        writer.close()


async def run_server(host: str = "127.0.0.1", port: int = 19871):
    """
    运行控制服务

    参数:
    - host: 主机
    - port: 端口
    """
    # 检查单实例
    if not _check_single_instance():
        print("Control server is already running")
        sys.exit(1)
    
    _write_pid_file(CONTROL_PID_FILE, os.getpid())
    # 写入 PID 文件
    
    atexit.register(_cleanup_control)
    # 注册退出清理
    
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    # 注册信号处理
    
    server = await asyncio.start_server(_handle_request, host, port)
    print(f"Backend control server running at http://{host}:{port}")
    print("Endpoints:")
    print("  GET  /status   - Get backend status")
    print("  POST /start    - Start backend")
    print("  POST /stop     - Stop backend")
    print("  POST /restart  - Restart backend")
    print("  POST /shutdown - Stop control server and backend")
    print("  GET  /config   - Read config file")
    print("  PUT  /config   - Save config file")
    print("  POST /config/default - Create default config file")
    print("  POST /config/validate - Validate config file")
    print("  GET/POST/PUT/DELETE /config/platforms - Manage platform config")
    print("  GET/POST/PATCH/DELETE /config/models - Manage model config")
    print("  GET/POST/PATCH/DELETE /config/session-classes - Manage session class config")
    print("  GET  /config/session/discovery - Discover session classes")
    print("  POST /config/session/discovery/directories - Create session scan directory")
    
    async with server:
        await server.serve_forever()


def main():
    """入口函数"""
    parser = argparse.ArgumentParser(description="Backend Control Server")
    parser.add_argument("--host", default="127.0.0.1", help="Listen host")
    parser.add_argument("--port", type=int, default=19871, help="Listen port")
    args = parser.parse_args()
    
    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        _cleanup_control()


if __name__ == "__main__":
    main()
