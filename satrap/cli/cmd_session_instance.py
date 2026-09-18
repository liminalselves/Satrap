"""
会话实例管理 CLI: list / delete / bulk-delete / restart

在线时走后端 HTTP API (热管理, 同步运行态);
离线时复用 SessionInstanceConfigService 直读 platform.db (与控制服务冷管理同套逻辑)
"""
from __future__ import annotations

import argparse
from typing import Any

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config.session_instance_service import SessionInstanceConfigService
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.storage import CHAT_PLATFORM_ID, LOCAL_PLATFORM_ID, StorageLayout
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.cli.common import daemon_client_from_args, ensure_offline_allowed, load_cli_config, offline_requested
from satrap.cli.output import CliError, dispatch_action, ok, print_table, render_data
from satrap.core.type import safe_getattr_list, safe_getattr_str


def _configured_platform_ids(config: BackendConfig) -> list[str]:
    """
    返回可管理的平台实例 ID (始终含 local, 排除 chat 内部平台)

    参数:
    - config: 配置信息

    返回:
    - list[str]: 去重后的平台实例 ID
    """
    result = [LOCAL_PLATFORM_ID]
    for item in safe_getattr_list(config, "platforms"):
        platform_id = str(item.get("id", "")).strip()
        if platform_id and platform_id != CHAT_PLATFORM_ID and platform_id not in result:
            result.append(platform_id)
    return result


def _offline_service(args: argparse.Namespace, config: BackendConfig, platform_id: str) -> SessionInstanceConfigService:
    """
    构造指定平台的会话实例冷管理服务 (与控制服务同一套接线)

    参数:
    - args: 命令参数
    - config: 配置信息
    - platform_id: 平台实例 ID

    返回:
    - SessionInstanceConfigService: 冷管理服务
    """
    layout = StorageLayout(config.data_root)
    database = layout.platform_db(platform_id)
    session_class_manager = SessionClassConfigManager(
        storage_path=config.session_class_config_path,
        session_scan_paths=config.session_scan_paths,
    )
    edictum_manager = EdictumConfigManager(
        create_default_edictum_type_registry(),
        storage_path=config.edictum_config_path,
    )
    return SessionInstanceConfigService(
        SessionConfigStore(database),
        UserInfoStore(database),
        session_class_manager,
        edictum_manager,
        platform_id,
        layout,
    )


def _use_online(args: argparse.Namespace) -> bool:
    """
    参数:
    - args: 命令参数

    返回:
    - bool: 后端在线且未强制离线
    """
    client = daemon_client_from_args(args)
    return client.is_alive() and not offline_requested(args)


def _guard_offline_write(args: argparse.Namespace, action: str) -> None:
    """
    离线写入守卫: 后端在线时仅允许显式 --force-offline

    参数:
    - args: 命令参数
    - action: 操作描述
    """
    ensure_offline_allowed(args, action)


def _resolve_platform(args: argparse.Namespace, config: BackendConfig) -> str:
    """
    解析 --platform-id, 默认 local

    参数:
    - args: 命令参数
    - config: 配置信息

    返回:
    - str: 平台实例 ID
    """
    platform_id = safe_getattr_str(args, "platform_id").strip() or LOCAL_PLATFORM_ID
    known = _configured_platform_ids(config)
    if platform_id not in known:
        raise CliError(f"未知平台实例: {platform_id}", hint=f"可选: {', '.join(known)}")
    return platform_id


def cmd_instance_list(args: argparse.Namespace):
    """
    列出持久化会话实例

    参数:
    - args: 命令参数
    """
    if _use_online(args):
        client = daemon_client_from_args(args)
        sessions = client.list_session_instances()
        platform_filter = safe_getattr_str(args, "platform_id").strip()
        if platform_filter:
            sessions = [s for s in sessions if str(s.get("platform_id", "")) == platform_filter]
    else:
        config = load_cli_config(args)
        platform_filter = safe_getattr_str(args, "platform_id").strip()
        platform_ids = [platform_filter] if platform_filter else _configured_platform_ids(config)
        sessions = []
        for platform_id in platform_ids:
            sessions.extend(_offline_service(args, config, platform_id).list_instances())
        sessions.sort(key=lambda item: float(item.get("last_used_at") or 0), reverse=True)

    def _human() -> None:
        if not sessions:
            print("没有持久化会话实例")
            return
        rows: list[list[str]] = []
        for item in sessions:
            rows.append([
                str(item.get("session_id", "")),
                str(item.get("platform_id", "")),
                str(item.get("provider_name", "")),
                str(item.get("session_type_name", "")),
                "是" if item.get("active") else "否",
                str(item.get("message_count", 0)),
            ])
        print_table(rows, ["会话 ID", "平台", "Provider", "类型", "激活", "消息数"])

    render_data({"sessions": sessions}, _human)


def cmd_instance_delete(args: argparse.Namespace):
    """
    删除单个会话实例

    参数:
    - args: 命令参数
    """
    session_id = str(args.session_id).strip()
    if not session_id:
        raise CliError("session_id 不能为空")
    if _use_online(args):
        client = daemon_client_from_args(args)
        result = client.delete_session_instance(session_id, safe_getattr_str(args, "platform_id").strip())
        deleted = result.get("deleted_ids") or []
        if not deleted:
            raise CliError(f"会话实例不存在: {session_id}")
        ok(f"已删除会话实例: {session_id}")
        return

    _guard_offline_write(args, "删除会话实例")
    config = load_cli_config(args)
    platform_id = _resolve_platform(args, config)
    deleted = _offline_service(args, config, platform_id).delete_instances([session_id])
    if not deleted:
        raise CliError(f"会话实例不存在: {session_id} (平台: {platform_id})")
    ok(f"已删除会话实例: {session_id} (平台: {platform_id})")


def cmd_instance_bulk_delete(args: argparse.Namespace):
    """
    批量删除会话实例 (empty: 无消息 / single: 仅一条消息 / selected: 显式指定)

    参数:
    - args: 命令参数
    """
    mode = str(args.mode).strip()
    session_ids = [str(s).strip() for s in safe_getattr_list(args, "session_ids") if str(s).strip()]
    if mode == "selected" and not session_ids:
        raise CliError("selected 模式至少选择一个会话实例", hint="用法: satrap session instance bulk-delete --mode selected <session_id...>")

    if _use_online(args):
        client = daemon_client_from_args(args)
        refs = None
        if mode == "selected":
            refs = [
                {"platform_id": safe_getattr_str(args, "platform_id").strip() or LOCAL_PLATFORM_ID, "session_id": sid}
                for sid in session_ids
            ]
        result = client.bulk_delete_session_instances(mode, refs)
        deleted = result.get("deleted_ids") or []
        ok(f"已删除 {len(deleted)} 个会话实例")
        for sid in deleted:
            print(f"  - {sid}")
        return

    _guard_offline_write(args, "批量删除会话实例")
    config = load_cli_config(args)
    if mode == "selected":
        platform_id = _resolve_platform(args, config)
        deleted = _offline_service(args, config, platform_id).delete_by_mode(mode, session_ids)
        ok(f"已删除 {len(deleted)} 个会话实例 (平台: {platform_id})")
        for sid in deleted:
            print(f"  - {sid}")
        return
    deleted_total = 0
    for platform_id in _configured_platform_ids(config):
        deleted = _offline_service(args, config, platform_id).delete_by_mode(mode)
        deleted_total += len(deleted)
        for sid in deleted:
            print(f"  - [{platform_id}] {sid}")
    ok(f"已删除 {deleted_total} 个会话实例")


def cmd_instance_restart(args: argparse.Namespace):
    """
    按冷配置重启会话实例 (仅在线模式, 需要运行时)

    参数:
    - args: 命令参数
    """
    session_id = str(args.session_id).strip()
    if not session_id:
        raise CliError("session_id 不能为空")
    if not _use_online(args):
        raise CliError(
            "实例重启需要后端运行 (卸载并重新激活运行时会话)",
            hint="先执行 `satrap start` 启动后端; 离线场景可删除后重新创建",
        )
    config = load_cli_config(args)
    platform_id = _resolve_platform(args, config)
    client = daemon_client_from_args(args)
    result = client.restart_session_instance(session_id, platform_id)
    if not result.get("ok", False):
        raise CliError(str(result.get("error") or f"重启失败: {session_id}"))
    ok(f"已重启会话实例: {session_id} (平台: {platform_id})")


def dispatch(args: argparse.Namespace):
    """
    session instance 子命令分发

    参数:
    - args: 命令参数
    """
    dispatch_action({
        "list": cmd_instance_list,
        "delete": cmd_instance_delete,
        "bulk-delete": cmd_instance_bulk_delete,
        "restart": cmd_instance_restart,
    }, args, key="instance_action")
