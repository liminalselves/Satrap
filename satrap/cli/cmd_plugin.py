"""
聊天插件管理 CLI: list / show / enable / disable / capability / config

Chat 服务在线时写操作走 HTTP (对活动会话即时 install/uninstall),
离线时直接操作 .satrap/chat_plugins.json 与全局插件配置 (下次启动生效)
"""
from __future__ import annotations

import argparse
from typing import Any, cast
import json

from satrap.display.plugins import CAPABILITY_LABELS, ChatPluginRegistry
from satrap.edictum.plugin import CAPABILITY_KINDS
from satrap.edictum.plugin_config import PluginConfigManager, schema_to_payload, validate_config_values
from satrap.cli.common import chat_client_from_args, offline_requested, parse_kv_pairs
from satrap.cli.output import CliError, dispatch_action, info, ok, print_json, print_table, render_data
from satrap.core.type import safe_getattr, safe_getattr_str


def _registry() -> ChatPluginRegistry:
    """
    返回:
    - ChatPluginRegistry: 插件注册表 (文件态, 总是可读)
    """
    return ChatPluginRegistry()


def _chat_online(args: argparse.Namespace) -> bool:
    """
    参数:
    - args: 命令参数

    返回:
    - bool: Chat 服务在线且未强制离线
    """
    return chat_client_from_args(args, timeout=2).is_alive() and not offline_requested(args)


def _require_plugin(registry: ChatPluginRegistry, name: str):
    """
    取插件目录条目, 不存在时抛业务错误

    参数:
    - registry: 插件注册表
    - name: 插件名称

    返回:
    - 插件目录条目
    """
    entry = registry.catalog.get(name)
    if entry is None:
        raise CliError(f"插件不存在: {name}", hint="可用 `satrap plugin list` 查看可用插件")
    return entry


def cmd_plugin_list(args: argparse.Namespace):
    """
    列出聊天插件清单

    参数:
    - args: 命令参数
    """
    plugins = _registry().scan()

    def _human() -> None:
        if not plugins:
            print("未发现插件 (官方目录 satrap/expend/plugins, 用户目录 .satrap/plugins)")
            return
        rows: list[list[str]] = []
        for item in plugins:
            caps = item.get("capabilities", {})
            cap_summary = ", ".join(
                f"{CAPABILITY_LABELS.get(kind, kind)}x{len(items)}"
                for kind, items in caps.items()
                if items
            )
            available = item.get("availability", {})
            state = "启用" if item.get("enabled") else "停用"
            if not available.get("allowed", True):
                state += "/不可用"
            rows.append([
                str(item.get("name", "")),
                str(item.get("version", "")),
                state,
                cap_summary or "-",
                str(item.get("description", "")),
            ])
        print_table(rows, ["名称", "版本", "状态", "能力", "说明"])

    render_data({"plugins": plugins}, _human)


def cmd_plugin_show(args: argparse.Namespace):
    """
    查看插件详情 (含兼容性/适用环境/能力声明)

    参数:
    - args: 命令参数
    """
    name = str(args.name)
    for item in _registry().scan():
        if item.get("name") == name:
            print_json(item)
            return
    raise CliError(f"插件不存在: {name}", hint="可用 `satrap plugin list` 查看可用插件")


def _set_enabled(args: argparse.Namespace, enabled: bool):
    """
    启停插件: 在线走 Chat 服务即时同步, 离线写状态文件

    参数:
    - args: 命令参数
    - enabled: 是否启用
    """
    name = str(args.name)
    action = "启用" if enabled else "停用"
    if _chat_online(args):
        result = chat_client_from_args(args).set_chat_plugin_enabled(name, enabled)
        if not result.get("ok", False):
            raise CliError(str(result.get("error") or f"{action}失败: {name}"))
        failed = int(result.get("failed", 0))
        if failed:
            raise CliError(f"{action}已保存, 但 {failed} 个活动会话同步失败", hint="详情见控制面板 Chat 设置")
        ok(f"已{action}插件: {name} (活动会话已同步)")
        return

    registry = _registry()
    _require_plugin(registry, name)
    try:
        registry.set_enabled(name, enabled)
    except Exception as e:
        raise CliError(f"{action}失败: {e}") from e
    ok(f"已{action}插件: {name}")
    info("Chat 服务未运行, 活动会话将在下次启动后应用该变更")


def cmd_plugin_enable(args: argparse.Namespace):
    """
    启用插件

    参数:
    - args: 命令参数
    """
    _set_enabled(args, True)


def cmd_plugin_disable(args: argparse.Namespace):
    """
    停用插件

    参数:
    - args: 命令参数
    """
    _set_enabled(args, False)


def cmd_plugin_capability(args: argparse.Namespace):
    """
    设置插件单项能力启停

    参数:
    - args: 命令参数
    """
    name = str(args.name)
    kind = safe_getattr_str(args, "kind").strip()
    cap = safe_getattr_str(args, "cap").strip()
    state = safe_getattr_str(args, "state").strip().lower()
    if kind not in CAPABILITY_KINDS:
        raise CliError(f"非法能力类别: {kind}", hint=f"可选: {', '.join(CAPABILITY_KINDS)}")
    if state not in ("on", "off"):
        raise CliError(f"非法状态: {state}", hint="可选: on / off")
    enabled = state == "on"

    registry = _registry()
    entry = _require_plugin(registry, name)
    declared = entry.capabilities.get(kind, {})
    if cap not in declared:
        raise CliError(f"插件未声明能力: {name}.{kind}.{cap}")

    if _chat_online(args):
        result = chat_client_from_args(args).set_chat_plugin_capability(name, kind, cap, enabled)
        if not result.get("ok", False):
            raise CliError(str(result.get("error") or "设置失败"))
        ok(f"已{'启用' if enabled else '停用'}能力: {name}.{kind}.{cap} (活动会话已同步)")
        return

    if not registry.set_capability(name, kind, cap, enabled):
        raise CliError(f"非法能力类别: {kind}")
    ok(f"已{'启用' if enabled else '停用'}能力: {name}.{kind}.{cap}")
    info("Chat 服务未运行, 活动会话将在下次启动后应用该变更")


def cmd_plugin_config(args: argparse.Namespace):
    """
    查看或修改插件全局配置

    参数:
    - args: 命令参数
    """
    name = str(args.name)
    registry = _registry()
    entry = _require_plugin(registry, name)
    schema = entry.config_schema

    set_groups = cast(list[list[str]] | None, safe_getattr(args, "set"))
    from_json = safe_getattr_str(args, "from_json")
    if not set_groups and not from_json:
        mgr = PluginConfigManager()
        payload = {"ok": True, "schema": schema_to_payload(schema), "config": mgr.load_global(name, schema)}
        print_json(payload)
        return

    if from_json:
        values = json.loads(from_json)
        if not isinstance(values, dict):
            raise CliError("--from-json 必须是 JSON 对象")
        values = cast(dict[str, Any], values)
    else:
        values = parse_kv_pairs(set_groups)
    try:
        cleaned = validate_config_values(schema, values)
    except ValueError as e:
        raise CliError(f"配置校验失败: {e}") from e

    if _chat_online(args):
        result = chat_client_from_args(args).save_chat_plugin_config(name, cleaned)
        if not result.get("ok", False):
            raise CliError(str(result.get("error") or "保存失败"))
        ok(f"已保存插件配置: {name} (活动会话已同步)")
        return

    mgr = PluginConfigManager()
    mgr.save_global(name, schema, cleaned)
    ok(f"已保存插件配置: {name}")
    info("Chat 服务未运行, 活动会话将在下次启动后应用该配置")


def dispatch(args: argparse.Namespace):
    """
    plugin 子命令分发

    参数:
    - args: 命令参数
    """
    dispatch_action({
        "list": cmd_plugin_list,
        "show": cmd_plugin_show,
        "enable": cmd_plugin_enable,
        "disable": cmd_plugin_disable,
        "capability": cmd_plugin_capability,
        "config": cmd_plugin_config,
    }, args)
