from __future__ import annotations

import json
import sys
import uuid

from satrap.cli.client import DaemonClient
from satrap.core.config_loader import ConfigLoader
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager


def _init_mgr(args) -> SessionClassConfigManager:
    """离线模式: 直接初始化 SessionClassConfigManager"""
    config = ConfigLoader.autodetect()
    if getattr(args, 'config', None):
        config = ConfigLoader.from_json(args.config) or ConfigLoader.from_yaml(args.config)
    return SessionClassConfigManager(storage_path=config.session_class_config_path)


def _client_or_fallback(args):
    """尝试 HTTP 连接, 失败时返回 (None, offline_mgr)"""
    client = DaemonClient()
    if client.is_alive():
        return client, None
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
        configs = mgr.list_configs()

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
            mgr.enable(args.name)
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
            mgr.disable(args.name)
        print(f"已停用: {args.name}")
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)


def cmd_session_register(args):
    mgr = _init_mgr(args)
    try:
        cls = SessionClassConfigManager._load_class(args.class_path)
        ck = getattr(args, 'context_key', None) or ""
        mk = getattr(args, 'model_key', None) or ""
        mgr.register(args.name, cls, description=args.description or "",
                      context_key=ck, model_key=mk)
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
    mgr = _init_mgr(args)
    if mgr.remove_config(args.name):
        print(f"已注销: {args.name}")
    else:
        print(f"未找到: {args.name}")
        sys.exit(1)


def cmd_session_config_set(args):
    mgr = _init_mgr(args)
    try:
        if args.from_json:
            params = json.loads(args.from_json)
            mgr.set_config(args.name, params)
        else:
            kv = {}
            for group in args.set:
                for item in (group if isinstance(group, list) else [group]):
                    if "=" not in item:
                        print(f"无效格式: {item}, 请使用 key=value")
                        sys.exit(1)
                    key, val = item.split("=", 1)
                    kv[key.strip()] = val.strip()
            mgr.update_config(args.name, **kv)
        print(f"已更新配置: {args.name}")
    except (ValueError, json.JSONDecodeError) as e:
        print(f"配置失败: {e}")
        sys.exit(1)


def cmd_session_config_show(args):
    mgr = _init_mgr(args)
    cfg = mgr.get_config(args.name)
    if cfg is None:
        print(f"未找到: {args.name}")
        sys.exit(1)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def cmd_session_create(args):
    config = ConfigLoader.autodetect()
    if args.config:
        config = ConfigLoader.from_json(args.config) or ConfigLoader.from_yaml(args.config)
    scm = SessionClassConfigManager(storage_path=config.session_class_config_path)
    sm = SessionManager(db_path=config.session_db_path)
    context_value = getattr(args, 'context_value', None) or ""
    sid = args.id or ""

    extra = {}
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
                args.name, scm, context_value=context_value, extra_params=extra,
            )
            print(f"已创建会话: {cfg.session_id} (上下文: {context_value})")
        else:
            sid = sid or uuid.uuid4().hex
            cfg = sm.register_session_from_class_config(
                args.name, scm, session_id=sid, extra_params=extra,
            )
            print(f"已创建会话: {cfg.session_id}")
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
