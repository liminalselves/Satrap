from __future__ import annotations

import json
import sys

from satrap.admin_utils.config_editor import (
    delete_platform,
    find_config_path,
    load_config_document,
    save_config_document,
    upsert_platform,
)
from satrap.cli.common import daemon_client_from_args, parse_kv_pairs, print_json


def _warn_if_backend_running(args):
    """平台配置变更需要重启后端后生效"""
    client = daemon_client_from_args(args)
    if client.is_alive():
        print("提示: 后端正在运行, 平台配置变更需要重启后端后生效.")


def cmd_platform_list(args):
    """列出平台适配器和配置中的平台"""
    config_data = load_config_document(find_config_path())
    configured = config_data.get("platforms", []) or []
    client = daemon_client_from_args(args)
    if client.is_alive():
        health = client.health()
        adapters = health.get("adapters", {})
        print(f"后端: {client.daemon.base_url}")
        if adapters:
            rows = []
            for aid, info in adapters.items():
                rows.append([aid, info.get("config_type", "?"), info.get("status", "?"), str(info.get("started", False))])
            print(_fmt_table(rows, ["ID", "类型", "状态", "已启动"]))
        else:
            print("当前无运行中的适配器实例")
    else:
        print(f"后端未运行: {client.daemon.base_url}")

    if configured:
        print("\n配置中的平台:")
        rows = [
            [str(p.get("id", "")), str(p.get("type", "")), json.dumps(p.get("settings", {}), ensure_ascii=False)]
            for p in configured
        ]
        print(_fmt_table(rows, ["ID", "类型", "settings"]))
    else:
        print("\n配置中的平台: (空)")


def cmd_platform_show(args):
    """查看平台配置"""
    platforms = load_config_document(find_config_path()).get("platforms", []) or []
    for item in platforms:
        if str(item.get("id", "")) == args.id:
            print_json(item)
            return
    print(f"未找到平台: {args.id}")
    sys.exit(1)


def _settings_from_args(args) -> dict:
    """解析平台 settings 参数"""
    if getattr(args, "from_json", None):
        data = json.loads(args.from_json)
        if not isinstance(data, dict):
            raise ValueError("--from-json 必须是 JSON 对象")
        return data
    return parse_kv_pairs(getattr(args, "set", None))


def cmd_platform_upsert(args):
    """新增或更新平台配置"""
    _warn_if_backend_running(args)
    path = find_config_path()
    data = load_config_document(path)
    platforms = list(data.get("platforms", []) or [])
    try:
        data["platforms"] = upsert_platform(
            platforms,
            original_id=args.id if args.action == "update" else None,
            platform_id=args.id,
            platform_type=args.type,
            settings=_settings_from_args(args),
        )
        save_config_document(path, data)
        print(f"平台配置已保存: {args.id}")
        print("提示: 平台实例变更需要重启后端后生效.")
    except Exception as e:
        print(f"保存失败: {e}")
        sys.exit(1)


def cmd_platform_remove(args):
    """删除平台配置"""
    _warn_if_backend_running(args)
    path = find_config_path()
    data = load_config_document(path)
    data["platforms"] = delete_platform(list(data.get("platforms", []) or []), args.id)
    save_config_document(path, data)
    print(f"平台配置已删除: {args.id}")
    print("提示: 平台实例变更需要重启后端后生效.")


def _fmt_table(rows: list[list[str]], header: list[str] | None = None) -> str:
    if not rows:
        return "(空)"
    col_widths = []
    all_rows = ([header] if header else []) + rows
    for col_idx in range(len(all_rows[0])):
        col_widths.append(max(len(str(r[col_idx])) for r in all_rows))
    lines = []
    if header:
        hdr = " | ".join(str(h).ljust(w) for h, w in zip(header, col_widths))
        lines.append(hdr)
        lines.append("-+-".join("-" * w for w in col_widths))
    for row in rows:
        lines.append(" | ".join(str(c).ljust(w) for c, w in zip(row, col_widths)))
    return "\n".join(lines)


def dispatch(args):
    if args.action == "list":
        cmd_platform_list(args)
    elif args.action == "show":
        cmd_platform_show(args)
    elif args.action in ("add", "update"):
        cmd_platform_upsert(args)
    elif args.action == "remove":
        cmd_platform_remove(args)
    else:
        print(f"未知操作: {args.action}")
        sys.exit(1)
