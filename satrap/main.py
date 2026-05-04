#!/usr/bin/env python3
"""Satrap CLI 入口"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_proj_root = str(Path(__file__).resolve().parent.parent)
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Satrap 后端管理工具")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--api-host", default=os.getenv("SATRAP_API_HOST", "127.0.0.1"))
    parser.add_argument("--api-port", type=int, default=int(os.getenv("SATRAP_API_PORT", "19870")))

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # satrap run
    p = subparsers.add_parser("run", help="启动后端服务")
    p.add_argument("--config")
    p.add_argument("--log-level", default="INFO")

    # satrap reload
    subparsers.add_parser("reload", help="重载后端配置")

    # satrap session
    p_sess = subparsers.add_parser("session", help="Session 类管理")
    sess_sub = p_sess.add_subparsers(dest="action", help="操作")

    p = sess_sub.add_parser("list", help="列出所有 session 类")
    p.add_argument("--config")
    p = sess_sub.add_parser("enable", help="启用 session 类")
    p.add_argument("name"); p.add_argument("--config")
    p = sess_sub.add_parser("disable", help="停用 session 类")
    p.add_argument("name"); p.add_argument("--config")
    p = sess_sub.add_parser("register", help="注册 session 类")
    p.add_argument("name"); p.add_argument("--class-path", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--context-key", default="", help="指定 params 中哪个字段作为上下文区分键")
    p.add_argument("--model-key", default="", help="指定 params 中哪个字段引用 ModelConfigManager 中的 LLM 配置名称")
    p.add_argument("--config")
    p = sess_sub.add_parser("unregister", help="注销 session 类")
    p.add_argument("name"); p.add_argument("--config")
    p = sess_sub.add_parser("create", help="创建 session 实例")
    p.add_argument("name"); p.add_argument("--id"); p.add_argument("--config")
    p.add_argument("--context-value", default="", help="上下文区分值, 作为 session_id 的一部分")
    p.add_argument("--llm", default="", help="ModelConfigManager 中的 LLM 配置名称")
    p = sess_sub.add_parser("config", help="配置 session 类参数")
    p.add_argument("name")
    p.add_argument("--set", action="append", nargs="+", help="设置参数: key=value")
    p.add_argument("--from-json", help="从 JSON 设置参数")
    p.add_argument("--show", action="store_true", help="查看当前参数")
    p.add_argument("--config")

    # satrap model
    p_mod = subparsers.add_parser("model", help="模型配置管理")
    mod_sub = p_mod.add_subparsers(dest="action", help="操作")
    p = mod_sub.add_parser("list", help="列出模型配置")
    p.add_argument("type", nargs="?", default="all", choices=["llm", "embedding", "rerank", "all"])
    p.add_argument("--config")
    p = mod_sub.add_parser("show", help="查看模型配置详情")
    p.add_argument("type", choices=["llm", "embedding", "rerank"])
    p.add_argument("name", nargs="?", default="default")
    p.add_argument("--show-key", action="store_true"); p.add_argument("--config")
    p = mod_sub.add_parser("set", help="设置模型配置")
    p.add_argument("type", choices=["llm", "embedding", "rerank"])
    p.add_argument("name", nargs="?", default="default")
    p.add_argument("--set", action="append", nargs="+", help="设置参数: key=value")
    p.add_argument("--from-json", help="从 JSON 设置完整配置"); p.add_argument("--config")
    p = mod_sub.add_parser("remove", help="删除模型配置")
    p.add_argument("type", choices=["llm", "embedding", "rerank"])
    p.add_argument("name", nargs="?", default="default"); p.add_argument("--config")

    # satrap platform
    p_plat = subparsers.add_parser("platform", help="平台适配器管理")
    plat_sub = p_plat.add_subparsers(dest="action", help="操作")
    p = plat_sub.add_parser("list", help="列出平台适配器")
    p.add_argument("--config")

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        from satrap.cli.cmd_run import cmd_run
        asyncio.run(cmd_run(args))
    elif args.command == "reload":
        from satrap.cli.cmd_reload import cmd_reload
        cmd_reload(args)
    elif args.command == "session":
        from satrap.cli.cmd_session import dispatch as dispatch_session
        dispatch_session(args)
    elif args.command == "model":
        from satrap.cli.cmd_model import dispatch as dispatch_model
        dispatch_model(args)
    elif args.command == "platform":
        from satrap.cli.cmd_platform import dispatch as dispatch_platform
        dispatch_platform(args)
    else:
        print(f"未知命令: {args.command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
