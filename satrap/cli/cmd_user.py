"""用户管理 CLI: list / info / create / update / delete / bind / unbind / sessions

直接操作用户信息库 (默认 .satrap/user_info.db, 可用 --db 覆盖),
与运行时 Session 解耦; 运行时自动绑定请使用 UserManager (resolve_session / route_call)
"""
from __future__ import annotations

import argparse
import sys

from typing import Any, cast

from satrap.api import user as user_api


DEFAULT_DB = ".satrap/user_info.db"
"""用户信息库默认路径 (与 UserInfoStore 默认一致)"""


def _db_path(args: argparse.Namespace) -> str:
    """用户信息库路径: --db 优先, 否则默认路径"""
    return args.db or DEFAULT_DB


def _print_user(info: dict[str, Any]):
    """打印单个用户信息"""
    sessions = cast(list[Any], info.get("user_session") or [])
    print(f"user_id:    {info.get('user_id')}")
    print(f"platform:   {info.get('user_platform') or '-'}")
    print(f"nickname:   {info.get('user_nickname') or '-'}")
    print(f"sessions:   {', '.join(str(s) for s in sessions) if sessions else '-'}")


def cmd_user_list(args: argparse.Namespace):
    """列出全部用户"""
    result = user_api.list_users(_db_path(args), limit=args.limit)
    users = result["users"]
    if not users:
        print("(暂无用户)")
        return
    for u in users:
        sessions = u["user_session"]
        label = u["user_nickname"] or "-"
        print(f"{u['user_id']:<24} {u['user_platform'] or '-':<12} {label:<16} {len(sessions)} 个会话")
    print(f"\n共 {result['count']} 个用户")


def cmd_user_info(args: argparse.Namespace):
    """查看单个用户详情"""
    result = user_api.get_user(_db_path(args), args.user_id)
    if not result.get("ok"):
        print(f"错误: {result.get('error')}")
        sys.exit(1)
    _print_user(result["user"])


def cmd_user_create(args: argparse.Namespace):
    """创建用户 (已存在则更新平台/昵称)"""
    result = user_api.create_user(
        _db_path(args), args.user_id,
        platform=args.platform, nickname=args.nickname,
    )
    if not result.get("ok"):
        print(f"错误: {result.get('error')}")
        sys.exit(1)
    tag = "已创建" if result.get("created") else "已存在, 已更新"
    print(f"{tag}: {args.user_id}")


def cmd_user_update(args: argparse.Namespace):
    """更新用户昵称/平台"""
    result = user_api.update_user(
        _db_path(args), args.user_id,
        nickname=args.nickname, platform=args.platform,
    )
    if not result.get("ok"):
        print(f"错误: {result.get('error')}")
        sys.exit(1)
    print(f"已更新: {args.user_id}")


def cmd_user_delete(args: argparse.Namespace):
    """删除用户信息 (不删除会话本身)"""
    result = user_api.delete_user(_db_path(args), args.user_id)
    if not result.get("ok"):
        print(f"错误: {result.get('error')}")
        sys.exit(1)
    print(f"已删除用户: {args.user_id}")


def cmd_user_bind(args: argparse.Namespace):
    """绑定会话到用户"""
    result = user_api.bind_session(_db_path(args), args.user_id, args.session_id)
    if not result.get("ok"):
        print(f"错误: {result.get('error')}")
        sys.exit(1)
    print(f"已绑定: {args.user_id} -> {args.session_id}")


def cmd_user_unbind(args: argparse.Namespace):
    """解绑会话"""
    result = user_api.unbind_session(_db_path(args), args.user_id, args.session_id)
    if not result.get("ok"):
        print(f"错误: {result.get('error')}")
        sys.exit(1)
    print(f"已解绑: {args.user_id} -> {args.session_id}")


def cmd_user_sessions(args: argparse.Namespace):
    """列出用户绑定的会话"""
    result = user_api.list_user_sessions(_db_path(args), args.user_id)
    session_ids = result["session_ids"]
    if not session_ids:
        print(f"用户 {args.user_id} 未绑定任何会话")
        return
    for sid in session_ids:
        print(sid)
    print(f"\n共 {result['count']} 个会话")


def dispatch(args: argparse.Namespace):
    """user 命令分发 (统一异常处理, 不输出 traceback 退出)"""
    action_map = {
        "list": cmd_user_list,
        "info": cmd_user_info,
        "create": cmd_user_create,
        "update": cmd_user_update,
        "delete": cmd_user_delete,
        "bind": cmd_user_bind,
        "unbind": cmd_user_unbind,
        "sessions": cmd_user_sessions,
    }
    handler = action_map.get(args.action)
    if handler is None:
        print(f"未知操作: {args.action}")
        sys.exit(2)
    try:
        handler(args)
    except ValueError as e:
        # 业务错误 (user_id 为空 / 用户不存在)
        print(f"错误: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"执行出错: {e}")
        sys.exit(2)
