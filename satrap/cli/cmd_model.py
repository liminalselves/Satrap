from __future__ import annotations

import json
import sys

from satrap.cli.client import DaemonClient
from satrap.core.config_loader import ConfigLoader
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.type import EmbeddingConfig, LLMConfig, ReRankConfig


TYPE_MAP = {
    "llm": ("list_llm_configs", "get_llm_config", "set_llm_config", "update_llm_config", "remove_llm_config"),
    "embedding": ("list_embedding_configs", "get_embedding_config", "set_embedding_config", "update_embedding_config", "remove_embedding_config"),
    "rerank": ("list_rerank_configs", "get_rerank_config", "set_rerank_config", "update_rerank_config", "remove_rerank_config"),
}
CLS_MAP = {"llm": LLMConfig, "embedding": EmbeddingConfig, "rerank": ReRankConfig}


def _init_mgr(args) -> ModelConfigManager:
    config = ConfigLoader.autodetect()
    if getattr(args, 'config', None):
        config = ConfigLoader.from_json(args.config) or ConfigLoader.from_yaml(args.config)
    return ModelConfigManager(storage_path=config.model_config_path)


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


def _fmt_model_config(config) -> dict:
    data = {f.name: getattr(config, f.name) for f in config.__dataclass_fields__.values()}
    return data


def cmd_model_list(args):
    client = DaemonClient()
    if client.is_alive():
        data = client.list_models(typ=args.type) if args.type != "all" else {}
        if args.type == "all":
            all_data = {}
            for t in TYPE_MAP:
                all_data[t] = client.list_models(typ=t)
            data = all_data
        if "error" in data:
            mgr = _init_mgr(args)
        else:
            _print_model_table(data, args)
            return

    mgr = _init_mgr(args)
    types = TYPE_MAP.keys() if args.type == "all" else [args.type]
    rows = []
    for t in types:
        info = TYPE_MAP.get(t)
        if not info:
            continue
        configs = getattr(mgr, info[0])(mask_api_key=True)
        for name, entry in configs.items():
            model = entry.get("model") or entry.get("base_url", "")
            rows.append([t, name, model])
    if not rows:
        print("没有已注册的模型配置")
        return
    header = ["类型", "名称", "Model / URL"]
    print(_fmt_table(rows, header))


def _print_model_table(data: dict, args):
    if args.type == "all":
        rows = []
        for t in TYPE_MAP:
            for name, entry in data.get(t, {}).items():
                model = entry.get("model") or entry.get("base_url", "")
                rows.append([t, name, model])
        if not rows:
            print("没有已注册的模型配置")
            return
        print(_fmt_table(rows, ["类型", "名称", "Model / URL"]))
    else:
        for name, entry in data.items():
            model = entry.get("model") or entry.get("base_url", "")
            print(f"[{args.type}] {name}: {model}")


def cmd_model_show(args):
    mgr = _init_mgr(args)
    info = TYPE_MAP.get(args.type)
    if not info:
        print(f"未知类型: {args.type}")
        sys.exit(1)
    config = getattr(mgr, info[1])(name=args.name)
    data = _fmt_model_config(config)
    if not args.show_key and "api_key" in data:
        ak = data.get("api_key", "") or ""
        data["api_key"] = "****" + ak[-4:] if len(ak) > 4 else "****"
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_model_set(args):
    mgr = _init_mgr(args)
    info = TYPE_MAP.get(args.type)
    if not info:
        print(f"未知类型: {args.type}")
        sys.exit(1)
    try:
        if args.from_json:
            params = json.loads(args.from_json)
            config = CLS_MAP[args.type](**params)
            getattr(mgr, info[2])(config, name=args.name)
        else:
            kv = {}
            for group in args.set:
                for item in (group if isinstance(group, list) else [group]):
                    if "=" not in item:
                        print(f"无效格式: {item}")
                        sys.exit(1)
                    key, val = item.split("=", 1)
                    kv[key.strip()] = val.strip()
            getattr(mgr, info[3])(name=args.name, **kv)
        print(f"已更新模型配置: {args.type}/{args.name}")
    except Exception as e:
        print(f"设置失败: {e}")
        sys.exit(1)


def cmd_model_remove(args):
    mgr = _init_mgr(args)
    info = TYPE_MAP.get(args.type)
    if not info:
        print(f"未知类型: {args.type}")
        sys.exit(1)
    try:
        success = getattr(mgr, info[4])(name=args.name)
        if success:
            print(f"已删除: {args.type}/{args.name}")
        else:
            print(f"未找到: {args.type}/{args.name}")
            sys.exit(1)
    except Exception as e:
        print(f"删除失败: {e}")
        sys.exit(1)


def dispatch(args):
    action_map = {
        "list": cmd_model_list,
        "show": cmd_model_show,
        "set": cmd_model_set,
        "remove": cmd_model_remove,
    }
    handler = action_map.get(args.action)
    if handler:
        handler(args)
    else:
        print(f"未知操作: {args.action}")
        sys.exit(1)
