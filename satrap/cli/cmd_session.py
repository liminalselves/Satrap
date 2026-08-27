"""CLI 会话配置与生命周期管理命令"""
from __future__ import annotations
import argparse

import json
import sys
from pathlib import Path
from typing import Any, cast

from satrap.cli.common import daemon_client_from_args, ensure_offline_allowed, load_cli_config, offline_requested, print_json
from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.type import safe_getattr, safe_getattr_str, safe_getattr_list
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.session_discovery import SessionClassDiscoveryService
from satrap.core.storage import LOCAL_PLATFORM_ID, StorageLayout


def _configured_adapter_ids(config: BackendConfig) -> set[str]:
    """
    返回配置中声明的平台适配器实例 ID 集合

    参数:
    - config: 配置信息

    返回:
    - set[str]: 配置中声明的平台适配器实例 ID 集合
    """
    ids: set[str] = set()
    for item in safe_getattr_list(config, "platforms"):
        adapter_id = str(item.get("id", "")).strip()
        if adapter_id:
            ids.add(adapter_id)
    return ids


def _init_mgr(args: argparse.Namespace) -> SessionClassConfigManager:
    """
    离线模式: 直接初始化 SessionClassConfigManager

    参数:
    - args: 额外位置参数

    返回:
    - SessionClassConfigManager: 离线模式: 直接初始化 SessionClassConfigManager
    """
    config = load_cli_config(args)
    return SessionClassConfigManager(
        storage_path=config.session_class_config_path,
        session_scan_paths=config.session_scan_paths,
    )


def _client_or_fallback(args: argparse.Namespace):
    """
    尝试 HTTP 连接, 失败时返回 (None, offline_mgr)

    参数:
    - args: 额外位置参数

    返回:
    - 尝试 HTTP 连接, 失败时返回 (None, offline_mgr)
    """
    client = daemon_client_from_args(args)
    if client.is_alive() and not offline_requested(args):
        return client, None
    if offline_requested(args):
        ensure_offline_allowed(args, "修改会话类配置")
    return None, _init_mgr(args)


def _fmt_table(rows: list[list[str]], header: list[str] | None = None) -> str:
    if not rows:
        return "(空)"
    col_widths: list[int] = []
    all_rows = ([header] if header else []) + rows
    for col_idx in range(len(all_rows[0])):
        col_widths.append(max(len(str(r[col_idx])) for r in all_rows))
    lines: list[str] = []
    if header:
        hdr = " | ".join(str(h).ljust(w) for h, w in zip(header, col_widths))
        lines.append(hdr)
        lines.append("-+-".join("-" * w for w in col_widths))
    for row in rows:
        lines.append(" | ".join(str(c).ljust(w) for c, w in zip(row, col_widths)))
    return "\n".join(lines)


def cmd_session_list(args: argparse.Namespace):
    """
    处理 session_list 命令

    参数:
    - args: 命令参数
    """
    client, mgr = _client_or_fallback(args)
    if client:
        data = client.list_session_classes()
        if "error" in data:
            print(f"错误: {data['error']}")
            sys.exit(1)
        configs = data
    else:
        configs = mgr.list_configs()   # type: ignore

    if not configs:
        print("没有已注册的会话类")
        return
    header = ["名称", "状态", "上下文键", "模型键", "Class Path", "参数"]
    rows: list[list[str]] = []
    for name, entry in configs.items():
        status = "启用" if entry.get("enabled", True) else "停用"
        ck = entry.get("context_key", "") or "-"
        mk = entry.get("model_key", "") or "-"
        params = json.dumps(entry.get("params", {}), ensure_ascii=False)
        rows.append([name, status, ck, mk, entry.get("class_path", ""), params])
    print(_fmt_table(rows, header))


def cmd_session_enable(args: argparse.Namespace):
    """
    处理 session_enable 命令

    参数:
    - args: 命令参数
    """
    client, mgr = _client_or_fallback(args)
    try:
        if client:
            result = client.enable_session_class(args.name)
            if "error" in result:
                raise ValueError(result["error"])
        else:
            mgr.enable(args.name)   # type: ignore
        print(f"已启用: {args.name}")
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)


def cmd_session_disable(args: argparse.Namespace):
    """
    处理 session_disable 命令

    参数:
    - args: 命令参数
    """
    client, mgr = _client_or_fallback(args)
    try:
        if client:
            result = client.disable_session_class(args.name)
            if "error" in result:
                raise ValueError(result["error"])
        else:
            mgr.disable(args.name)   # type: ignore
        print(f"已停用: {args.name}")
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)


def cmd_session_register(args: argparse.Namespace):
    """
    处理 session_register 命令

    参数:
    - args: 命令参数
    """
    class_path = (safe_getattr_str(args, "from_scan") or safe_getattr_str(args, "class_path")).strip()
    if not class_path:
        print("注册失败: 请提供 --class-path 或 --from-scan")
        sys.exit(1)
    client = daemon_client_from_args(args)
    try:
        ck = safe_getattr_str(args, 'context_key')
        mk = safe_getattr_str(args, 'model_key')
        if client.is_alive() and not offline_requested(args):
            result = client.register_session_class(
                args.name,
                class_path,
                description=args.description or "",
                context_key=ck,
                model_key=mk,
            )
            if "error" in result:
                raise ValueError(result["error"])
        else:
            if offline_requested(args):
                ensure_offline_allowed(args, "注册会话类")
            mgr = _init_mgr(args)
            mgr.register_by_class_path(
                args.name,
                class_path,
                description=args.description or "",
                context_key=ck,
                model_key=mk,
            )
        tags: list[str] = []
        if ck:
            tags.append(f"上下文键: {ck}")
        if mk:
            tags.append(f"模型键: {mk}")
        suffix = f" ({', '.join(tags)})" if tags else ""
        print(f"已注册会话类: {args.name}{suffix}")
    except Exception as e:
        print(f"注册失败: {e}")
        sys.exit(1)


def cmd_session_unregister(args: argparse.Namespace):
    """
    处理 session_unregister 命令

    参数:
    - args: 命令参数
    """
    client, mgr = _client_or_fallback(args)
    if client:
        result = client.unregister_session_class(args.name)
        if "error" in result:
            print(f"注销失败: {result['error']}")
            sys.exit(1)
        print(f"已注销: {args.name}")
        return
    if mgr and mgr.remove_config(args.name):
        print(f"已注销: {args.name}")
        return
    print(f"未找到: {args.name}")
    sys.exit(1)


def cmd_session_config_set(args: argparse.Namespace):
    """
    处理 session_config_set 命令

    参数:
    - args: 命令参数
    """
    client, mgr = _client_or_fallback(args)
    try:
        if args.from_json:
            params = json.loads(args.from_json)
            if not isinstance(params, dict):
                raise ValueError("--from-json 必须是 JSON 对象")
            params = cast(dict[str, Any], params)
            if client:
                result = client.set_session_class_params(args.name, params)
                if "error" in result:
                    raise ValueError(result["error"])
            else:
                mgr.set_config(args.name, params)   # type: ignore
        else:
            kv: dict[str, str] = {}
            for group in args.set:
                items = cast(list[str], group) if isinstance(group, list) else [str(group)]
                for item in items:
                    if "=" not in item:
                        print(f"无效格式: {item}, 请使用 key=value")
                        sys.exit(1)
                    key, val = item.split("=", 1)
                    kv[key.strip()] = val.strip()
            if client:
                current = client.get_session_class(args.name)
                if "error" in current:
                    raise ValueError(current["error"])
                params = dict(current.get("params", {}))
                params.update(kv)
                result = client.set_session_class_params(args.name, params)
                if "error" in result:
                    raise ValueError(result["error"])
            else:
                mgr.update_config(args.name, **kv)   # type: ignore
        print(f"已更新配置: {args.name}")
    except ValueError as e:
        print(f"配置失败: {e}")
        sys.exit(1)


def cmd_session_config_show(args: argparse.Namespace):
    """
    处理 session_config_show 命令

    参数:
    - args: 命令参数
    """
    client, mgr = _client_or_fallback(args)
    cfg = client.get_session_class(args.name) if client else mgr.get_config(args.name)   # type: ignore
    if isinstance(cfg, dict) and "error" in cfg:
        print(f"查询失败: {cfg['error']}")
        sys.exit(1)
    if cfg is None:
        print(f"未找到: {args.name}")
        sys.exit(1)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def cmd_session_scan(args: argparse.Namespace):
    """
    扫描 Session 类

    参数:
    - args: 额外位置参数
    """
    config = load_cli_config(args)
    paths = safe_getattr(args, "path") or config.session_scan_paths
    results = SessionClassDiscoveryService(paths).discover()
    if not results:
        print("未发现 Session/AsyncSession 子类")
        return
    current_file = ""
    for item in results:
        file_name = str(Path(item.file_path))
        if file_name != current_file:
            current_file = file_name
            print(file_name)
        if item.error:
            print(f"  ! {item.error}")
        else:
            print(f"  - {item.class_name} ({'async' if item.is_async else 'sync'})")
            print(f"    class_path: {item.class_path}")
            print(f"    params: {json.dumps(item.init_params, ensure_ascii=False)}")


def cmd_session_create(args: argparse.Namespace):
    """
    处理 session_create 命令

    参数:
    - args: 命令参数
    """
    config = load_cli_config(args)
    client = daemon_client_from_args(args)
    if client.is_alive():
        print("提示: 后端正在运行, 此命令只创建持久化 session 实例, 不会直接修改已加载的运行时缓存.")
    scm = SessionClassConfigManager(
        storage_path=config.session_class_config_path,
        session_scan_paths=config.session_scan_paths,
    )
    context_value = safe_getattr_str(args, 'context_value')
    sid = args.id or ""

    extra: dict[str, Any] = {}
    adapter_id = safe_getattr_str(args, 'adapter_id').strip()
    platform_id = adapter_id or LOCAL_PLATFORM_ID
    storage_layout = StorageLayout(config.data_root)
    sm = SessionManager(
        db_path=storage_layout.platform_db(platform_id),
        platform_id=platform_id,
        storage_layout=storage_layout,
    )
    if adapter_id:
        configured_ids = _configured_adapter_ids(config)
        if configured_ids and adapter_id not in configured_ids:
            print(f"错误: 未找到适配器实例: {adapter_id}")
            sys.exit(1)
        extra["adapter_id"] = adapter_id

    llm_val = safe_getattr_str(args, 'llm')
    if llm_val:
        entry = scm.get_config(args.name)
        if entry:
            model_key = (entry.get("model_key") or "").strip()
            if model_key:
                extra[model_key] = llm_val
            else:
                extra["model_name"] = llm_val

    try:
        if context_value:
            cfg = sm.register_session_from_context(
                args.name,
                scm,
                context_value=context_value,
                platform=adapter_id,
                extra_params=extra,
            )
            print(f"已创建会话: {str(cfg.session_id)} (上下文: {context_value})")
        else:
            cfg = sm.register_session_from_class_config(
                args.name, scm, session_id=sid, extra_params=extra,
            )
            print(f"已创建会话: {str(cfg.session_id)}")
    except Exception as e:
        print(f"创建失败: {e}")
        sys.exit(1)


def dispatch(args: argparse.Namespace):
    """
    分派命令

    参数:
    - args: 命令参数
    """
    action_map = {
        "list": cmd_session_list,
        "enable": cmd_session_enable,
        "disable": cmd_session_disable,
        "register": cmd_session_register,
        "unregister": cmd_session_unregister,
        "create": cmd_session_create,
        "scan": cmd_session_scan,
    }
    if args.action in action_map:
        action_map[args.action](args)
    elif args.action == "config":
        if args.show:
            cmd_session_config_show(args)
        elif args.set or args.from_json:
            cmd_session_config_set(args)
        else:
            print("请使用 --show 查看或 --set/--from-json 设置参数")
            sys.exit(1)
    else:
        print(f"未知操作: {args.action}")
        sys.exit(1)
