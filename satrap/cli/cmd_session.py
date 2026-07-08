from __future__ import annotations

import json
import sys
from pathlib import Path

from satrap.cli.common import daemon_client_from_args, ensure_offline_allowed, load_cli_config, offline_requested, print_json
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.session_discovery import discover_session_classes


def _configured_adapter_ids(config) -> set[str]:
    """返回配置中声明的平台适配器实例 ID 集合"""
    ids: set[str] = set()
    for item in getattr(config, "platforms", []) or []:
        adapter_id = str(item.get("id", "")).strip()
        if adapter_id:
            ids.add(adapter_id)
    return ids


def _init_mgr(args) -> SessionClassConfigManager:
    """离线模式: 直接初始化 SessionClassConfigManager"""
    config = load_cli_config(args)
    return SessionClassConfigManager(
        storage_path=config.session_class_config_path,
        session_scan_paths=config.session_scan_paths,
    )


def _client_or_fallback(args):
    """尝试 HTTP 连接, 失败时返回 (None, offline_mgr)"""
    client = daemon_client_from_args(args)
    if client.is_alive() and not offline_requested(args):
        return client, None
    if offline_requested(args):
        ensure_offline_allowed(args, "修改会话类配置")
    return None, _init_mgr(args)


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


def cmd_session_list(args):
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
    rows = []
    for name, entry in configs.items():
        status = "启用" if entry.get("enabled", True) else "停用"
        ck = entry.get("context_key", "") or "-"
        mk = entry.get("model_key", "") or "-"
        params = json.dumps(entry.get("params", {}), ensure_ascii=False)
        rows.append([name, status, ck, mk, entry.get("class_path", ""), params])
    print(_fmt_table(rows, header))


def cmd_session_enable(args):
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


def cmd_session_disable(args):
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


def cmd_session_register(args):
    class_path = (getattr(args, "from_scan", None) or getattr(args, "class_path", None) or "").strip()
    if not class_path:
        print("注册失败: 请提供 --class-path 或 --from-scan")
        sys.exit(1)
    client = daemon_client_from_args(args)
    try:
        ck = getattr(args, 'context_key', None) or ""
        mk = getattr(args, 'model_key', None) or ""
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
        tags = []
        if ck:
            tags.append(f"上下文键: {ck}")
        if mk:
            tags.append(f"模型键: {mk}")
        suffix = f" ({', '.join(tags)})" if tags else ""
        print(f"已注册会话类: {args.name}{suffix}")
    except Exception as e:
        print(f"注册失败: {e}")
        sys.exit(1)


def cmd_session_unregister(args):
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


def cmd_session_config_set(args):
    client, mgr = _client_or_fallback(args)
    try:
        if args.from_json:
            params = json.loads(args.from_json)
            if not isinstance(params, dict):
                raise ValueError("--from-json 必须是 JSON 对象")
            if client:
                result = client.set_session_class_params(args.name, params)
                if "error" in result:
                    raise ValueError(result["error"])
            else:
                mgr.set_config(args.name, params)  # type: ignore
        else:
            kv = {}
            for group in args.set:
                for item in (group if isinstance(group, list) else [group]):
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
                mgr.update_config(args.name, **kv)  # type: ignore
        print(f"已更新配置: {args.name}")
    except ValueError as e:
        print(f"配置失败: {e}")
        sys.exit(1)


def cmd_session_config_show(args):
    client, mgr = _client_or_fallback(args)
    cfg = client.get_session_class(args.name) if client else mgr.get_config(args.name)  # type: ignore
    if isinstance(cfg, dict) and "error" in cfg:
        print(f"查询失败: {cfg['error']}")
        sys.exit(1)
    if cfg is None:
        print(f"未找到: {args.name}")
        sys.exit(1)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def cmd_session_scan(args):
    """扫描 Session 类"""
    config = load_cli_config(args)
    paths = getattr(args, "path", None) or config.session_scan_paths
    results = discover_session_classes(paths)
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


def cmd_session_create(args):
    config = load_cli_config(args)
    client = daemon_client_from_args(args)
    if client.is_alive():
        print("提示: 后端正在运行, 此命令只创建持久化 session 实例, 不会直接修改已加载的运行时缓存.")
    scm = SessionClassConfigManager(
        storage_path=config.session_class_config_path,
        session_scan_paths=config.session_scan_paths,
    )
    sm = SessionManager(db_path=config.session_db_path)
    context_value = getattr(args, 'context_value', None) or ""
    sid = args.id or ""

    extra = {}
    adapter_id = (getattr(args, 'adapter_id', None) or "").strip()
    if adapter_id:
        configured_ids = _configured_adapter_ids(config)
        if configured_ids and adapter_id not in configured_ids:
            print(f"错误: 未找到适配器实例: {adapter_id}")
            sys.exit(1)
        extra["adapter_id"] = adapter_id

    llm_val = getattr(args, 'llm', None) or ""
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


def dispatch(args):
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
