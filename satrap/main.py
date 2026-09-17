#!/usr/bin/env python3
"""
Satrap CLI 入口

解析后端运行, 配置, 会话, 模型, 平台, 插件, Edictum, 检查点和用户等子命令,
并将请求分发到对应的 CLI 命令模块
"""
import argparse
import asyncio
from pathlib import Path
import sys

_proj_root = str(Path(__file__).resolve().parent.parent)
# 确保项目根目录在 sys.path 中
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

from satrap.cli import output
from satrap.cli.cmd_checkpoint import dispatch as dispatch_checkpoint
from satrap.cli.cmd_config import dispatch as dispatch_config
from satrap.cli.cmd_control import dispatch as dispatch_control
from satrap.cli.cmd_edictum import dispatch as dispatch_edictum
from satrap.cli.cmd_model import dispatch as dispatch_model
from satrap.cli.cmd_platform import dispatch as dispatch_platform
from satrap.cli.cmd_plugin import dispatch as dispatch_plugin
from satrap.cli.cmd_reload import cmd_reload
from satrap.cli.cmd_run import cmd_run
from satrap.cli.cmd_session import dispatch as dispatch_session
from satrap.cli.cmd_user import dispatch as dispatch_user


def _build_parser() -> argparse.ArgumentParser:
    def add_mode_flags(p: argparse.ArgumentParser):
        p.add_argument("--offline", action="store_true", default=argparse.SUPPRESS, help="强制离线模式")
        p.add_argument("--force-offline", action="store_true", default=argparse.SUPPRESS, help="后端在线时仍允许离线写入")

    def add_api_flags(p: argparse.ArgumentParser):
        p.add_argument("--api-host", default=argparse.SUPPRESS)
        p.add_argument("--api-port", type=int, default=argparse.SUPPRESS)

    def add_config_flag(p: argparse.ArgumentParser):
        p.add_argument("--config", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        description="Satrap 后端管理工具",
        epilog="全局选项: --json 可放在任意位置, 输出结构化 JSON",
    )
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--api-host")
    parser.add_argument("--api-port", type=int)
    parser.add_argument("--offline", action="store_true", help="强制离线模式")
    parser.add_argument("--force-offline", action="store_true", help="后端在线时仍允许离线写入")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    p = subparsers.add_parser("run", help="前台启动后端服务")
    # satrap run 命令
    add_config_flag(p)
    add_api_flags(p)
    p.add_argument("--log-level", default="INFO", help="控制台日志级别 (DEBUG/INFO/WARNING/ERROR)")

    for name, help_text in (
        ("start", "后台启动后端服务"),
        ("reload", "重载后端配置"),
        ("status", "查看后端状态"),
        ("stop", "停止后端服务"),
        ("restart", "重启后端服务"),
    ):
        p = subparsers.add_parser(name, help=help_text)
        add_config_flag(p)
        add_api_flags(p)
    # 后端控制命令: satrap start / reload / status / stop / restart

    p_cfg = subparsers.add_parser("config", help="配置文件管理")
    # satrap config 命令
    cfg_sub = p_cfg.add_subparsers(dest="action", help="操作")
    p = cfg_sub.add_parser("init", help="创建默认配置文件")
    add_mode_flags(p)
    cfg_sub.add_parser("path", help="显示当前配置文件路径")
    p = cfg_sub.add_parser("show", help="显示解析后的配置 (密钥脱敏)")
    add_config_flag(p)
    cfg_sub.add_parser("raw", help="显示原始配置文本 (不脱敏)")
    p = cfg_sub.add_parser("validate", help="校验配置文件 (不落盘)")
    add_config_flag(p)
    p = cfg_sub.add_parser("set", help="设置顶层配置字段")
    p.add_argument("--set", action="append", nargs="+", required=True, help="设置参数: key=value, 支持 api.host")
    add_mode_flags(p)

    p_sess = subparsers.add_parser("session", help="Session 类与实例管理")
    # satrap session 命令
    sess_sub = p_sess.add_subparsers(dest="action", help="操作")

    p = sess_sub.add_parser("list", help="列出所有 session 类")
    add_config_flag(p)
    p = sess_sub.add_parser("enable", help="启用 session 类")
    p.add_argument("name"); add_config_flag(p)
    add_mode_flags(p)
    p = sess_sub.add_parser("disable", help="停用 session 类")
    p.add_argument("name"); add_config_flag(p)
    add_mode_flags(p)
    p = sess_sub.add_parser("register", help="注册 session 类")
    p.add_argument("name"); p.add_argument("--class-path"); p.add_argument("--from-scan", dest="from_scan")
    p.add_argument("--description", default="")
    p.add_argument("--context-key", default="", help="指定 params 中哪个字段作为上下文区分键")
    p.add_argument("--model-key", default="", help="指定 params 中哪个字段引用 ModelConfigManager 中的 LLM 配置名称")
    add_config_flag(p)
    add_mode_flags(p)
    p = sess_sub.add_parser("scan", help="扫描 Session 类")
    p.add_argument("--path", action="append", help="扫描目录, 默认来自配置 session_scan_paths")
    add_config_flag(p)
    p = sess_sub.add_parser("unregister", help="注销 session 类")
    p.add_argument("name"); add_config_flag(p)
    add_mode_flags(p)
    p = sess_sub.add_parser("create", help="创建 session 实例")
    p.add_argument("name"); p.add_argument("--id"); add_config_flag(p)
    p.add_argument("--context-value", default="", help="上下文区分值, 作为 session_id 的一部分")
    p.add_argument("--llm", default="", help="ModelConfigManager 中的 LLM 配置名称")
    p.add_argument("--adapter-id", default="", help="写入会话初始化参数的平台适配器实例 ID")
    p = sess_sub.add_parser("config", help="配置 session 类参数")
    p.add_argument("name")
    p.add_argument("--set", action="append", nargs="+", help="设置参数: key=value")
    p.add_argument("--from-json", help="从 JSON 设置参数")
    p.add_argument("--show", action="store_true", help="查看当前参数")
    add_config_flag(p)
    add_mode_flags(p)

    p_inst = sess_sub.add_parser("instance", help="持久化会话实例管理")
    # satrap session instance 命令
    inst_sub = p_inst.add_subparsers(dest="instance_action", help="操作")
    p = inst_sub.add_parser("list", help="列出会话实例")
    p.add_argument("--platform-id", default="", help="只看指定平台实例")
    add_config_flag(p)
    p = inst_sub.add_parser("delete", help="删除会话实例")
    p.add_argument("session_id")
    p.add_argument("--platform-id", default="", help="平台实例 ID, 默认 local")
    add_config_flag(p)
    add_mode_flags(p)
    p = inst_sub.add_parser("bulk-delete", help="批量删除会话实例")
    p.add_argument("session_ids", nargs="*", help="selected 模式下的会话 ID")
    p.add_argument("--mode", required=True, choices=["empty", "single", "selected"], help="empty=无消息, single=仅一条消息, selected=显式指定")
    p.add_argument("--platform-id", default="", help="selected 模式下的平台实例 ID, 默认 local")
    add_config_flag(p)
    add_mode_flags(p)
    p = inst_sub.add_parser("restart", help="按冷配置重启会话实例 (仅在线)")
    p.add_argument("session_id")
    p.add_argument("--platform-id", default="", help="平台实例 ID, 默认 local")
    add_config_flag(p)

    p_mod = subparsers.add_parser("model", help="模型配置管理")
    # satrap model 命令
    mod_sub = p_mod.add_subparsers(dest="action", help="操作")
    p = mod_sub.add_parser("list", help="列出模型配置")
    p.add_argument("type", nargs="?", default="all", choices=["llm", "embedding", "rerank", "all"])
    add_config_flag(p)
    p = mod_sub.add_parser("show", help="查看模型配置详情")
    p.add_argument("type", choices=["llm", "embedding", "rerank"])
    p.add_argument("name", nargs="?", default="default")
    p.add_argument("--show-key", action="store_true"); add_config_flag(p)
    p = mod_sub.add_parser("set", help="设置模型配置")
    p.add_argument("type", choices=["llm", "embedding", "rerank"])
    p.add_argument("name", nargs="?", default="default")
    p.add_argument("--set", action="append", nargs="+", help="设置参数: key=value")
    p.add_argument("--from-json", help="从 JSON 设置完整配置"); add_config_flag(p)
    add_mode_flags(p)
    p = mod_sub.add_parser("remove", help="删除模型配置")
    p.add_argument("type", choices=["llm", "embedding", "rerank"])
    p.add_argument("name", nargs="?", default="default"); add_config_flag(p)
    add_mode_flags(p)

    p_plat = subparsers.add_parser("platform", help="平台适配器管理")
    # satrap platform 命令
    plat_sub = p_plat.add_subparsers(dest="action", help="操作")
    p = plat_sub.add_parser("list", help="列出平台适配器")
    add_config_flag(p)
    p = plat_sub.add_parser("show", help="查看平台配置")
    p.add_argument("id"); add_config_flag(p)
    for action in ("add", "update"):
        p = plat_sub.add_parser(action, help=("新增平台配置" if action == "add" else "更新平台配置"))
        p.add_argument("id")
        p.add_argument("--type", required=True, choices=["misskey", "onebot", "aiocqhttp"])
        p.add_argument("--session-type", default="", help="入站消息使用的会话类配置名称")
        p.add_argument("--set", action="append", nargs="+", help="设置 settings: key=value")
        p.add_argument("--from-json", help="settings JSON 对象")
        add_config_flag(p)
        add_mode_flags(p)
    p = plat_sub.add_parser("remove", help="删除平台配置")
    p.add_argument("id"); add_config_flag(p)
    add_mode_flags(p)

    p_plug = subparsers.add_parser("plugin", help="聊天插件管理")
    # satrap plugin 命令
    plug_sub = p_plug.add_subparsers(dest="action", help="操作")
    plug_sub.add_parser("list", help="列出聊天插件")
    p = plug_sub.add_parser("show", help="查看插件详情")
    p.add_argument("name")
    for action in ("enable", "disable"):
        p = plug_sub.add_parser(action, help=("启用插件" if action == "enable" else "停用插件"))
        p.add_argument("name")
        add_mode_flags(p)
    p = plug_sub.add_parser("capability", help="设置插件单项能力启停")
    p.add_argument("name")
    p.add_argument("kind", help="能力类别: tools/skills/handlers/commands/mcp")
    p.add_argument("cap", help="能力名称")
    p.add_argument("state", choices=["on", "off"], help="on=启用, off=停用")
    add_mode_flags(p)
    p = plug_sub.add_parser("config", help="查看或修改插件全局配置")
    p.add_argument("name")
    p.add_argument("--set", action="append", nargs="+", help="设置参数: key=value")
    p.add_argument("--from-json", help="从 JSON 设置完整配置")
    add_mode_flags(p)

    p_edi = subparsers.add_parser("edictum", help="Edictum 命名配置管理")
    # satrap edictum 命令
    edi_sub = p_edi.add_subparsers(dest="action", help="操作")
    p = edi_sub.add_parser("types", help="列出 Edictum 类型")
    add_config_flag(p)
    p = edi_sub.add_parser("list", help="列出命名配置")
    add_config_flag(p)
    p = edi_sub.add_parser("show", help="查看命名配置详情")
    p.add_argument("name"); add_config_flag(p)
    p = edi_sub.add_parser("create", help="创建命名配置")
    p.add_argument("name")
    p.add_argument("--type", required=True, help="Edictum 类型, 见 edictum types")
    p.add_argument("--model", default="", help="模型配置名称")
    p.add_argument("--description", default="")
    p.add_argument("--set", action="append", nargs="+", help="设置 params: key=value")
    p.add_argument("--params-json", help="params JSON 对象")
    p.add_argument("--plugin", action="append", default=[], help="声明插件, 可重复")
    add_config_flag(p)
    add_mode_flags(p)
    p = edi_sub.add_parser("update", help="更新命名配置 (支持改名)")
    p.add_argument("name")
    p.add_argument("--rename", default="", help="新名称 (自动迁移会话引用)")
    p.add_argument("--description", default=None)
    p.add_argument("--model", default="")
    p.add_argument("--set", action="append", nargs="+", help="更新 params: key=value")
    p.add_argument("--params-json", help="params JSON 对象")
    add_config_flag(p)
    add_mode_flags(p)
    for action in ("enable", "disable"):
        p = edi_sub.add_parser(action, help=("启用命名配置" if action == "enable" else "停用命名配置"))
        p.add_argument("name"); add_config_flag(p)
        add_mode_flags(p)
    p = edi_sub.add_parser("delete", help="删除命名配置 (被引用时拒绝)")
    p.add_argument("name"); add_config_flag(p)
    add_mode_flags(p)
    p = edi_sub.add_parser("preview", help="预览运行时配置变更影响 (仅在线)")
    p.add_argument("name", nargs="?", default="")
    add_config_flag(p)
    p = edi_sub.add_parser("apply", help="应用运行时配置变更 (仅在线)")
    p.add_argument("name", nargs="?", default="")
    add_config_flag(p)

    p_ckpt = subparsers.add_parser("checkpoint", help="状态检查点管理")
    # satrap checkpoint 命令
    ckpt_sub = p_ckpt.add_subparsers(dest="action", help="操作")

    def add_platform_scope_flag(p: argparse.ArgumentParser):
        p.add_argument("--platform-id", default="local", help="平台实例 ID, 默认 local")
        p.add_argument("--data-root", default=None, help="运行数据根目录, 默认 .satrap/data")

    p = ckpt_sub.add_parser("create", help="为对话创建检查点")
    p.add_argument("conversation_id")
    p.add_argument("--name", default="")
    p.add_argument("--description", default="")
    add_platform_scope_flag(p)
    p = ckpt_sub.add_parser("list", help="列出对话检查点")
    p.add_argument("conversation_id")
    add_platform_scope_flag(p)
    p = ckpt_sub.add_parser("rollback", help="回滚对话到检查点")
    p.add_argument("conversation_id")
    p.add_argument("checkpoint_id")
    add_platform_scope_flag(p)
    p = ckpt_sub.add_parser("retry", help="从检查点重试 (保留未来检查点)")
    p.add_argument("conversation_id")
    p.add_argument("checkpoint_id")
    add_platform_scope_flag(p)
    p = ckpt_sub.add_parser("fork", help="从检查点分支新对话线")
    p.add_argument("conversation_id")
    p.add_argument("branch_name")
    p.add_argument("--checkpoint", default=None, help="源检查点 ID, 默认最近一个")
    add_platform_scope_flag(p)
    p = ckpt_sub.add_parser("lineage", help="查看检查点血缘链 (根在前)")
    p.add_argument("checkpoint_id")
    add_platform_scope_flag(p)
    p = ckpt_sub.add_parser("branches", help="列出对话 fork 出的全部分支")
    p.add_argument("conversation_id")
    add_platform_scope_flag(p)
    p = ckpt_sub.add_parser("audit", help="查看对话检查点变更记录 (source/reason)")
    p.add_argument("conversation_id")
    add_platform_scope_flag(p)

    p_user = subparsers.add_parser("user", help="用户管理")
    # satrap user 命令
    user_sub = p_user.add_subparsers(dest="action", help="操作")

    def add_user_platform_flag(p: argparse.ArgumentParser):
        p.add_argument("--platform-id", default="local", help="平台实例 ID, 默认 local")
        p.add_argument("--data-root", default=None, help="运行数据根目录, 默认 .satrap/data")

    p = user_sub.add_parser("list", help="列出全部用户")
    p.add_argument("--limit", type=int, default=200)
    add_user_platform_flag(p)
    p = user_sub.add_parser("info", help="查看用户详情")
    p.add_argument("user_id")
    add_user_platform_flag(p)
    p = user_sub.add_parser("create", help="创建用户 (已存在则更新平台/昵称)")
    p.add_argument("user_id")
    p.add_argument("--platform", default="")
    p.add_argument("--nickname", default="")
    add_user_platform_flag(p)
    p = user_sub.add_parser("update", help="更新用户昵称/平台")
    p.add_argument("user_id")
    p.add_argument("--nickname", default=None)
    p.add_argument("--platform", default=None)
    add_user_platform_flag(p)
    p = user_sub.add_parser("delete", help="删除用户信息 (不删除会话本身)")
    p.add_argument("user_id")
    add_user_platform_flag(p)
    p = user_sub.add_parser("bind", help="绑定会话到用户")
    p.add_argument("user_id")
    p.add_argument("session_id")
    add_user_platform_flag(p)
    p = user_sub.add_parser("unbind", help="解绑会话")
    p.add_argument("user_id")
    p.add_argument("session_id")
    add_user_platform_flag(p)
    p = user_sub.add_parser("sessions", help="列出用户绑定的会话")
    p.add_argument("user_id")
    add_user_platform_flag(p)

    return parser


def main():
    """运行 Satrap 命令行入口"""
    argv = sys.argv[1:]
    # --json 可放在任意位置: 预剥离后再交给 argparse, 避免逐个子解析器重复声明
    json_requested = "--json" in argv
    if json_requested:
        argv = [item for item in argv if item != "--json"]
        output.set_json_mode(True)

    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        asyncio.run(cmd_run(args))
        return
    if args.command == "reload":
        output.run_cli_action(lambda: cmd_reload(args))
        return

    dispatch_map = {
        "status": dispatch_control,
        "start": dispatch_control,
        "stop": dispatch_control,
        "restart": dispatch_control,
        "config": dispatch_config,
        "session": dispatch_session,
        "model": dispatch_model,
        "platform": dispatch_platform,
        "plugin": dispatch_plugin,
        "edictum": dispatch_edictum,
        "checkpoint": dispatch_checkpoint,
        "user": dispatch_user,
    }
    handler = dispatch_map.get(args.command)
    if handler is None:
        output.error(f"未知命令: {args.command}")
        sys.exit(output.EXIT_USAGE)
    handler(args)


if __name__ == "__main__":
    main()
