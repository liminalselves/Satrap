"""CLI 模型配置管理命令"""
from __future__ import annotations
import argparse
from typing import Any, cast
import json

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.cli.common import daemon_client_from_args, ensure_offline_allowed, load_cli_config, offline_requested, parse_kv_pairs
from satrap.cli.output import CliError, dispatch_action, ok, print_json, print_table, render_data
from satrap.core.type import EmbeddingConfig, LLMConfig, ReRankConfig, safe_getattr_callable


TYPE_MAP = {
    "llm": ("list_llm_configs", "get_llm_config", "set_llm_config", "update_llm_config", "remove_llm_config"),
    "embedding": ("list_embedding_configs", "get_embedding_config", "set_embedding_config", "update_embedding_config", "remove_embedding_config"),
    "rerank": ("list_rerank_configs", "get_rerank_config", "set_rerank_config", "update_rerank_config", "remove_rerank_config"),
}
CLS_MAP: dict[str, type[LLMConfig] | type[EmbeddingConfig] | type[ReRankConfig]] = {"llm": LLMConfig, "embedding": EmbeddingConfig, "rerank": ReRankConfig}


def _init_mgr(args: argparse.Namespace) -> ModelConfigManager:
    """
    参数:
    - args: 命令参数

    返回:
    - ModelConfigManager: 离线模型配置管理器
    """
    config = load_cli_config(args)
    return ModelConfigManager(storage_path=config.model_config_path)


def _fmt_model_config(config: LLMConfig | EmbeddingConfig | ReRankConfig) -> dict[str, Any]:
    """
    参数:
    - config: 配置信息

    返回:
    - dict[str, Any]: 模型配置字典
    """
    data = {f.name: getattr(config, f.name) for f in config.__dataclass_fields__.values()}
    return data


def _require_type(args: argparse.Namespace) -> tuple[str, str, str, str, str]:
    """
    校验模型类型并返回管理器方法名组

    参数:
    - args: 命令参数

    返回:
    - tuple[str, str, str, str, str]: list/get/set/update/remove 方法名
    """
    info = TYPE_MAP.get(args.type)
    if not info:
        raise CliError(f"未知类型: {args.type}", hint="可选: llm / embedding / rerank")
    return info


def cmd_model_list(args: argparse.Namespace):
    """
    处理 model_list 命令

    参数:
    - args: 命令参数
    """
    client = daemon_client_from_args(args)
    if client.is_alive() and not offline_requested(args):
        if args.type == "all":
            data: dict[str, Any] = {t: client.list_models(typ=t) for t in TYPE_MAP}
        else:
            data = {args.type: client.list_models(typ=args.type)}

        def _human_online() -> None:
            rows: list[list[str]] = []
            for t, configs in data.items():
                for name, entry in cast(dict[str, Any], configs).items():
                    model = entry.get("model") or entry.get("base_url", "")
                    rows.append([t, name, model])
            if not rows:
                print("没有已注册的模型配置")
                return
            print_table(rows, ["类型", "名称", "Model / URL"])

        render_data(data, _human_online)
        return

    mgr = _init_mgr(args)
    types = TYPE_MAP.keys() if args.type == "all" else [args.type]
    offline_data: dict[str, Any] = {}
    for t in types:
        info = TYPE_MAP.get(t)
        if not info:
            continue
        list_fn = safe_getattr_callable(mgr, info[0])
        if list_fn is None:
            continue
        offline_data[t] = list_fn(mask_api_key=True)

    def _human_offline() -> None:
        rows = []
        for t, configs in offline_data.items():
            for name, entry in configs.items():
                model = entry.get("model") or entry.get("base_url", "")
                rows.append([t, name, model])
        if not rows:
            print("没有已注册的模型配置")
            return
        print_table(rows, ["类型", "名称", "Model / URL"])

    render_data(offline_data, _human_offline)


def cmd_model_show(args: argparse.Namespace):
    """
    处理 model_show 命令

    参数:
    - args: 命令参数
    """
    info = _require_type(args)
    client = daemon_client_from_args(args)
    if client.is_alive() and not offline_requested(args):
        configs = client.list_models(typ=args.type)
        data = dict(configs.get(args.name, {}))
        if not data:
            raise CliError(f"未找到: {args.type}/{args.name}")
    else:
        if offline_requested(args):
            ensure_offline_allowed(args, "读取模型配置")
        mgr = _init_mgr(args)
        get_fn = safe_getattr_callable(mgr, info[1])
        if get_fn is None:
            raise CliError(f"接口不存在: {info[1]}")
        config = get_fn(name=args.name)
        data = _fmt_model_config(config)
    if not args.show_key and "api_key" in data:
        ak = data.get("api_key", "") or ""
        data["api_key"] = "****" + ak[-4:] if len(ak) > 4 else "****"
    print_json(data)


def cmd_model_set(args: argparse.Namespace):
    """
    处理 model_set 命令

    参数:
    - args: 命令参数
    """
    info = _require_type(args)
    if args.from_json:
        params = json.loads(args.from_json)
    else:
        params = parse_kv_pairs(args.set)
    if not isinstance(params, dict):
        raise ValueError("参数必须是对象")
    params = cast(dict[str, Any], params)

    client = daemon_client_from_args(args)
    if client.is_alive() and not offline_requested(args):
        if args.from_json:
            client.set_model(args.type, args.name, params)
        else:
            client.update_model(args.type, args.name, params)
    else:
        if offline_requested(args):
            ensure_offline_allowed(args, "修改模型配置")
        mgr = _init_mgr(args)
        if args.from_json:
            config = CLS_MAP[args.type](**params)
            set_fn = safe_getattr_callable(mgr, info[2])
            if set_fn is None:
                raise CliError(f"接口不存在: {info[2]}")
            set_fn(config, name=args.name)
        else:
            update_fn = safe_getattr_callable(mgr, info[3])
            if update_fn is None:
                raise CliError(f"接口不存在: {info[3]}")
            update_fn(name=args.name, **params)
    ok(f"已更新模型配置: {args.type}/{args.name}")


def cmd_model_remove(args: argparse.Namespace):
    """
    处理 model_remove 命令

    参数:
    - args: 命令参数
    """
    info = _require_type(args)
    client = daemon_client_from_args(args)
    if client.is_alive() and not offline_requested(args):
        client.remove_model(args.type, args.name)
        success = True
    else:
        if offline_requested(args):
            ensure_offline_allowed(args, "删除模型配置")
        mgr = _init_mgr(args)
        remove_fn = safe_getattr_callable(mgr, info[4])
        if remove_fn is None:
            raise CliError(f"接口不存在: {info[4]}")
        success = bool(remove_fn(name=args.name))
    if success:
        ok(f"已删除: {args.type}/{args.name}")
    else:
        raise CliError(f"未找到: {args.type}/{args.name}")


def dispatch(args: argparse.Namespace):
    """
    分派命令

    参数:
    - args: 命令参数
    """
    dispatch_action({
        "list": cmd_model_list,
        "show": cmd_model_show,
        "set": cmd_model_set,
        "remove": cmd_model_remove,
    }, args)
