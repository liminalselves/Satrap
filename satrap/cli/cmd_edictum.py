"""
Edictum 命名配置管理 CLI: types / list / show / create / update / enable / disable / delete / preview / apply

在线时走后端 HTTP API (与控制面板同源);
离线时复用 EdictumConfigService 直改冷配置文件, 引用保护与控制服务一致
"""
from __future__ import annotations

import argparse
from typing import Any, cast
import json

from satrap.core.config.edictum_service import EdictumConfigService
from satrap.core.config.edictum_references import list_edictum_config_references, rename_edictum_config_references
from satrap.core.storage import CHAT_PLATFORM_ID, LOCAL_PLATFORM_ID, StorageLayout
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.cli.common import daemon_client_from_args, ensure_offline_allowed, load_cli_config, offline_requested, parse_kv_pairs
from satrap.cli.output import CliError, dispatch_action, info, ok, print_json, print_table, render_data
from satrap.core.type import safe_getattr, safe_getattr_str, safe_getattr_list


def _edictum_service(args: argparse.Namespace) -> EdictumConfigService:
    """
    离线模式: 直接构造 Edictum 冷配置服务 (与控制服务同套接线)

    参数:
    - args: 命令参数

    返回:
    - EdictumConfigService: 冷配置服务
    """
    config = load_cli_config(args)
    registry = create_default_edictum_type_registry()
    manager = EdictumConfigManager(registry, storage_path=config.edictum_config_path)
    return EdictumConfigService(manager, registry)


def _online_client(args: argparse.Namespace):
    """
    后端在线且未强制离线时返回 client, 否则 None

    参数:
    - args: 命令参数

    返回:
    - 后端 client 或 None
    """
    client = daemon_client_from_args(args)
    if client.is_alive() and not offline_requested(args):
        return client
    return None


def _layout_and_platforms(args: argparse.Namespace) -> tuple[StorageLayout, list[str]]:
    """
    参数:
    - args: 命令参数

    返回:
    - tuple[StorageLayout, list[str]]: 数据布局与可管理平台 (含 chat)
    """
    config = load_cli_config(args)
    layout = StorageLayout(config.data_root)
    platform_ids = [LOCAL_PLATFORM_ID]
    for item in safe_getattr_list(config, "platforms"):
        platform_id = str(item.get("id", "")).strip()
        if platform_id and platform_id != CHAT_PLATFORM_ID and platform_id not in platform_ids:
            platform_ids.append(platform_id)
    return layout, platform_ids


def _after_mutation(args: argparse.Namespace) -> None:
    """
    冷配置变更后的运行态提示

    参数:
    - args: 命令参数
    """
    client = daemon_client_from_args(args)
    if client.is_alive():
        info("后端正在运行, 活动会话需执行 `satrap edictum apply` 或 `satrap reload` 后应用变更")


def cmd_edictum_types(args: argparse.Namespace):
    """
    列出 Edictum 类型

    参数:
    - args: 命令参数
    """
    client = _online_client(args)
    if client is not None:
        data = client.list_edictum_types()
        types = data.get("types", [])
    else:
        types = create_default_edictum_type_registry().list_types()

    def _human() -> None:
        if not types:
            print("没有可用的 Edictum 类型")
            return
        rows = [
            [str(t.get("name", "")), str(t.get("title", "") or t.get("description", ""))]
            for t in cast(list[dict[str, Any]], types)
        ]
        print_table(rows, ["类型", "说明"])

    render_data({"types": types}, _human)


def cmd_edictum_list(args: argparse.Namespace):
    """
    列出 Edictum 命名配置

    参数:
    - args: 命令参数
    """
    client = _online_client(args)
    if client is not None:
        configs = client.list_edictum_configs()
    else:
        configs = _edictum_service(args).list_configs()

    def _human() -> None:
        if not configs:
            print("没有 Edictum 命名配置")
            return
        rows: list[list[str]] = []
        for name, entry in configs.items():
            plugins = entry.get("plugins", [])
            rows.append([
                name,
                str(entry.get("edictum_type", "")),
                "启用" if entry.get("enabled", True) else "停用",
                str(entry.get("model_name", "") or "-"),
                str(len(plugins)),
                str(entry.get("description", "") or "-"),
            ])
        print_table(rows, ["名称", "类型", "状态", "模型", "插件数", "说明"])

    render_data(configs, _human)


def cmd_edictum_show(args: argparse.Namespace):
    """
    查看 Edictum 命名配置详情

    参数:
    - args: 命令参数
    """
    client = _online_client(args)
    if client is not None:
        print_json(client.get_edictum_config(args.name))
        return
    entry = _edictum_service(args).get(args.name)
    if entry is None:
        raise CliError(f"未找到: {args.name}")
    print_json(entry)


def _collect_params(args: argparse.Namespace) -> dict[str, Any]:
    """
    从 --set / --params-json 收集 params 覆盖

    参数:
    - args: 命令参数

    返回:
    - dict[str, Any]: params 覆盖
    """
    params_json = safe_getattr_str(args, "params_json")
    if params_json:
        params = json.loads(params_json)
        if not isinstance(params, dict):
            raise CliError("--params-json 必须是 JSON 对象")
        return cast(dict[str, Any], params)
    return parse_kv_pairs(cast(list[list[str]] | None, safe_getattr(args, "set")))


def cmd_edictum_create(args: argparse.Namespace):
    """
    创建 Edictum 命名配置

    参数:
    - args: 命令参数
    """
    payload: dict[str, Any] = {
        "name": str(args.name),
        "edictum_type": str(args.type),
        "description": str(safe_getattr(args, "description") or ""),
        "params": _collect_params(args),
    }
    model_name = safe_getattr_str(args, "model").strip()
    if model_name:
        payload["model_name"] = model_name
    plugins = [str(p).strip() for p in safe_getattr_list(args, "plugin") if str(p).strip()]
    if plugins:
        payload["plugins"] = plugins

    client = _online_client(args)
    if client is not None:
        client.create_edictum_config(payload)
    else:
        if offline_requested(args):
            ensure_offline_allowed(args, "创建 Edictum 配置")
        _edictum_service(args).create(payload)
    ok(f"已创建 Edictum 配置: {args.name}")
    _after_mutation(args)


def cmd_edictum_update(args: argparse.Namespace):
    """
    更新 Edictum 命名配置 (支持改名并迁移会话引用)

    参数:
    - args: 命令参数
    """
    payload: dict[str, Any] = {}
    new_name = safe_getattr_str(args, "rename").strip()
    if new_name:
        payload["name"] = new_name
    description = safe_getattr(args, "description")
    if description is not None:
        payload["description"] = str(description)
    model_name = safe_getattr_str(args, "model").strip()
    if model_name:
        payload["model_name"] = model_name
    params = _collect_params(args)
    if params:
        payload["params"] = params
    if not payload:
        raise CliError("没有需要更新的字段", hint="支持 --rename / --description / --model / --set / --params-json")

    client = _online_client(args)
    if client is not None:
        result = client.update_edictum_config(args.name, payload)
        final_name = str(result.get("name", new_name or args.name))
        migrated = result.get("migrated_refs") or []
        ok(f"已更新 Edictum 配置: {final_name}")
        if migrated:
            info(f"已迁移 {len(migrated)} 个会话实例引用")
        return

    if offline_requested(args):
        ensure_offline_allowed(args, "更新 Edictum 配置")
    service = _edictum_service(args)
    previous = service.get(args.name)
    final_name, _updated = service.update(args.name, payload)
    if final_name != args.name:
        layout, platform_ids = _layout_and_platforms(args)
        try:
            migrated = rename_edictum_config_references(layout, platform_ids, args.name, final_name)
        except Exception:
            if previous is not None:
                service.update(final_name, {**previous, "name": args.name})
            raise
        if migrated:
            info(f"已迁移 {len(migrated)} 个会话实例引用")
    ok(f"已更新 Edictum 配置: {final_name}")
    _after_mutation(args)


def _set_enabled(args: argparse.Namespace, enabled: bool):
    """
    启停 Edictum 命名配置

    参数:
    - args: 命令参数
    - enabled: 是否启用
    """
    action = "启用" if enabled else "停用"
    client = _online_client(args)
    if client is not None:
        client.set_edictum_enabled(args.name, enabled)
    else:
        if offline_requested(args):
            ensure_offline_allowed(args, f"{action} Edictum 配置")
        service = _edictum_service(args)
        if service.get(args.name) is None:
            raise CliError(f"未找到: {args.name}")
        service.set_enabled(args.name, enabled)
    ok(f"已{action}: {args.name}")
    _after_mutation(args)


def cmd_edictum_enable(args: argparse.Namespace):
    """
    启用 Edictum 命名配置

    参数:
    - args: 命令参数
    """
    _set_enabled(args, True)


def cmd_edictum_disable(args: argparse.Namespace):
    """
    停用 Edictum 命名配置

    参数:
    - args: 命令参数
    """
    _set_enabled(args, False)


def cmd_edictum_delete(args: argparse.Namespace):
    """
    删除 Edictum 命名配置 (仍被会话实例引用时拒绝)

    参数:
    - args: 命令参数
    """
    client = _online_client(args)
    if client is not None:
        client.delete_edictum_config(args.name)
        ok(f"已删除: {args.name}")
        return

    if offline_requested(args):
        ensure_offline_allowed(args, "删除 Edictum 配置")
    layout, platform_ids = _layout_and_platforms(args)
    references = list_edictum_config_references(layout, platform_ids, args.name)
    if references:
        raise CliError(
            f"Edictum 配置仍被 {len(references)} 个会话实例引用, 已拒绝删除",
            hint="请先删除相关会话实例: satrap session instance list",
        )
    service = _edictum_service(args)
    if not service.delete(args.name):
        raise CliError(f"未找到: {args.name}")
    ok(f"已删除: {args.name}")


def _runtime_op(args: argparse.Namespace, *, apply: bool):
    """
    Edictum 运行时预览/应用 (仅在线)

    参数:
    - args: 命令参数
    - apply: True 为应用, False 为预览
    """
    client = daemon_client_from_args(args)
    if not client.is_alive():
        raise CliError(
            f"{'应用' if apply else '预览'}运行时变更需要后端运行",
            hint="先执行 `satrap start` 启动后端",
        )
    config_name = safe_getattr_str(args, "name").strip()
    if apply:
        result = client.apply_edictum_runtime(config_name)
    else:
        result = client.preview_edictum_runtime(config_name)
    sessions = result.get("edictum_sessions", [])

    def _human() -> None:
        action = "已应用" if apply else "预览"
        print(f"{action}结果 ({len(sessions)} 个会话):")
        for item in cast(list[dict[str, Any]], sessions):
            status = "成功" if item.get("ok") else "失败"
            print(f"  [{status}] {item.get('session_id', '-')}: {json.dumps(item, ensure_ascii=False)[:200]}")

    render_data(result, _human)
    if not result.get("ok", False):
        raise CliError("部分会话处理失败", hint="可使用 --json 查看逐会话详情")


def cmd_edictum_preview(args: argparse.Namespace):
    """
    预览运行时配置变更影响

    参数:
    - args: 命令参数
    """
    _runtime_op(args, apply=False)


def cmd_edictum_apply(args: argparse.Namespace):
    """
    应用运行时配置变更

    参数:
    - args: 命令参数
    """
    _runtime_op(args, apply=True)


def dispatch(args: argparse.Namespace):
    """
    edictum 子命令分发

    参数:
    - args: 命令参数
    """
    dispatch_action({
        "types": cmd_edictum_types,
        "list": cmd_edictum_list,
        "show": cmd_edictum_show,
        "create": cmd_edictum_create,
        "update": cmd_edictum_update,
        "enable": cmd_edictum_enable,
        "disable": cmd_edictum_disable,
        "delete": cmd_edictum_delete,
        "preview": cmd_edictum_preview,
        "apply": cmd_edictum_apply,
    }, args)
