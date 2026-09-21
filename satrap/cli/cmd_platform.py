"""CLI 平台适配器配置与状态管理命令"""
from __future__ import annotations
import argparse
from typing import Any, cast
import json
import uuid

from satrap.core.config.document import (
    config_document_revision,
    delete_platform,
    find_config_path,
    load_config_document,
    save_config_document,
    upsert_platform,
    validate_platforms,
)
from satrap.cli.output import CliError, dispatch_action, info, ok, print_json, print_table
from satrap.cli.common import daemon_client_from_args, parse_kv_pairs
from satrap.cli.client import DaemonError
from satrap.core.type import safe_getattr


def _warn_if_backend_running(args: argparse.Namespace):
    """
    平台配置变更需要重启后端后生效

    参数:
    - args: 额外位置参数
    """
    client = daemon_client_from_args(args)
    if client.is_alive():
        info("后端正在运行, 平台配置变更需要重启后端后生效")


def cmd_platform_list(args: argparse.Namespace):
    """
    列出平台适配器和配置中的平台

    参数:
    - args: 额外位置参数
    """
    config_data = load_config_document(find_config_path())
    configured = cast(list[Any], config_data.get("platforms", []) or [])
    client = daemon_client_from_args(args)
    if client.is_alive():
        health = client.health()
        adapters = cast(dict[str, Any], health.get("adapters", {}))
        print(f"后端: {client.daemon.base_url}")
        if adapters:
            rows: list[list[str]] = []
            for aid, adapter_info in adapters.items():
                rows.append([
                    aid,
                    adapter_info.get("config_type", "?"),
                    adapter_info.get("session_type", "?"),
                    adapter_info.get("status", "?"),
                    str(adapter_info.get("started", False)),
                ])
            print_table(rows, ["ID", "类型", "会话类", "状态", "已启动"])
        else:
            print("当前无运行中的适配器实例")
    else:
        print(f"后端未运行: {client.daemon.base_url}")

    if configured:
        print("\n配置中的平台:")
        rows = [
            [
                str(p.get("id", "")),
                str(p.get("type", "")),
                str(p.get("session_type", "自动")),
                json.dumps(p.get("settings", {}), ensure_ascii=False),
            ]
            for p in configured
        ]
        print_table(rows, ["ID", "类型", "会话类", "settings"])
    else:
        print("\n配置中的平台: (空)")


def cmd_platform_show(args: argparse.Namespace):
    """
    查看平台配置

    参数:
    - args: 额外位置参数
    """
    platforms = cast(list[Any], load_config_document(find_config_path()).get("platforms", []) or [])
    for item in platforms:
        if str(item.get("id", "")) == args.id:
            print_json(item)
            return
    raise CliError(f"未找到平台: {args.id}")


def _settings_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """
    解析平台 settings 参数

    参数:
    - args: 额外位置参数

    返回:
    - dict[str, Any]: 解析平台 settings 参数
    """
    if safe_getattr(args, "from_json"):
        data = cast(dict[str, Any], json.loads(args.from_json))
        if not isinstance(data, dict):
            raise ValueError("--from-json 必须是 JSON 对象")
        return data
    return parse_kv_pairs(safe_getattr(args, "set"))


def cmd_platform_upsert(args: argparse.Namespace):
    """
    新增或更新平台配置

    参数:
    - args: 额外位置参数
    """
    _warn_if_backend_running(args)
    path = find_config_path()
    data = load_config_document(path)
    revision = config_document_revision(data)
    platforms = list(data.get("platforms", []) or [])
    try:
        platform: dict[str, Any] = {
            "id": str(args.id),
            "type": str(args.type),
            "settings": _settings_from_args(args),
        }
        session_type = str(safe_getattr(args, "session_type") or "").strip()
        if session_type:
            platform["session_type"] = session_type
        merged = upsert_platform(
            platforms,
            platform,
            original_id=args.id if args.action == "update" else None,
        )
        data["platforms"] = validate_platforms(merged)
        save_config_document(path, data, expected_revision=revision)
    except Exception as e:
        raise CliError(f"保存失败: {e}") from e
    ok(f"平台配置已保存: {args.id}")
    info("平台实例变更需要重启后端后生效")


def cmd_platform_remove(args: argparse.Namespace):
    """
    删除平台配置

    参数:
    - args: 额外位置参数
    """
    _warn_if_backend_running(args)
    path = find_config_path()
    data = load_config_document(path)
    revision = config_document_revision(data)
    data["platforms"] = delete_platform(list(data.get("platforms", []) or []), args.id)
    save_config_document(path, data, expected_revision=revision)
    ok(f"平台配置已删除: {args.id}")
    info("平台实例变更需要重启后端后生效")


def cmd_platform_wake(args: argparse.Namespace):
    """
    向运行中的后端提交 OneBot 群手动唤醒

    参数:
    - args: adapter id, 群号, 路由成员 ID, 可选 prompt/message-id/reason/request-id
    """
    if args.prompt and args.message_id:
        raise CliError("--prompt 与 --message-id 只能指定一个")
    payload: dict[str, Any] = {
        "adapter_id": args.id, "group_id": args.group, "user_id": args.user,
        "request_id": args.request_id or f"cli-{uuid.uuid4().hex}",
    }
    for key in ("prompt", "message_id", "reason"):
        value = getattr(args, key, None)
        if value:
            payload[key] = value
    client = daemon_client_from_args(args, timeout=10)
    client.require_alive()
    try:
        result = client.wake_platform(payload)
    except DaemonError as error:
        raise CliError(f"手动唤醒被拒绝: {error}") from error
    status = str(result.get("status", ""))
    if status == "accepted":
        ok(f"已接受手动唤醒, request_id={result.get('request_id', payload['request_id'])}")
    elif status == "already_pending":
        info(f"相同请求已在处理, 状态: {result.get('state', '')}")
    elif status == "no_pending":
        info("该群与成员范围内没有待处理正文, 未提交模型调用")
    else:
        raise CliError(f"手动唤醒未接受: {status or '未知状态'} {result.get('reason', '')}".rstrip())
    print_json(result)


def dispatch(args: argparse.Namespace):
    """
    分派命令

    参数:
    - args: 命令参数
    """
    dispatch_action({
        "list": cmd_platform_list,
        "show": cmd_platform_show,
        "add": cmd_platform_upsert,
        "update": cmd_platform_upsert,
        "remove": cmd_platform_remove,
        "wake": cmd_platform_wake,
    }, args)
