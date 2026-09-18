"""
状态检查点 CLI: create / list / rollback / retry / fork / lineage / branches / audit

业务逻辑复用 satrap.api.checkpoint (与 HTTP API 同套实现),
按平台实例直接操作其唯一 platform.db, 与运行时 Session 解耦
"""
from __future__ import annotations

import argparse
from typing import Any

from satrap.api import checkpoint as checkpoint_api
from satrap.core.storage import StorageLayout
from satrap.cli.output import dispatch_action, ok, print_json, render_data


def _db_path(args: argparse.Namespace) -> str:
    """
    返回指定平台实例的唯一数据库路径

    参数:
    - args: 额外位置参数

    返回:
    - str: 平台实例数据库路径
    """
    return str(StorageLayout(args.data_root).platform_db(str(args.platform_id)))


def _fmt_checkpoint_line(cp: dict[str, Any]) -> str:
    """
    参数:
    - cp: 检查点数据

    返回:
    - str: 单行检查点摘要
    """
    return (
        f"{cp.get('checkpoint_id')}  [{cp.get('checkpoint_kind')}] {cp.get('name') or '-'} "
        f"(revision={cp.get('state_revision')}, position={cp.get('position')}, "
        f"batch={cp.get('batch_id') or '-'})"
    )


def cmd_checkpoint_create(args: argparse.Namespace):
    """
    为对话创建检查点

    参数:
    - args: 额外位置参数
    """
    result = checkpoint_api.create_checkpoint(
        _db_path(args), args.conversation_id, name=args.name, description=args.description,
    )
    ok(f"已创建检查点: {result['checkpoint_id']} (对话: {args.conversation_id})")


def cmd_checkpoint_list(args: argparse.Namespace):
    """
    列出对话的全部检查点

    参数:
    - args: 额外位置参数
    """
    result = checkpoint_api.list_checkpoints(_db_path(args), args.conversation_id)
    checkpoints = result["checkpoints"]

    def _human() -> None:
        if not checkpoints:
            print("没有检查点")
            return
        for cp in checkpoints:
            print(_fmt_checkpoint_line(cp))

    render_data(result, _human)


def cmd_checkpoint_rollback(args: argparse.Namespace):
    """
    回滚对话到指定检查点

    参数:
    - args: 额外位置参数
    """
    checkpoint_api.rollback_checkpoint(_db_path(args), args.conversation_id, args.checkpoint_id)
    ok(f"已回滚到检查点: {args.checkpoint_id}")


def cmd_checkpoint_retry(args: argparse.Namespace):
    """
    从指定检查点重试 (保留未来检查点)

    参数:
    - args: 额外位置参数
    """
    checkpoint_api.retry_checkpoint(_db_path(args), args.conversation_id, args.checkpoint_id)
    ok(f"已重试到检查点: {args.checkpoint_id} (未来检查点已保留)")


def cmd_checkpoint_fork(args: argparse.Namespace):
    """
    从检查点 fork 一条新对话线

    参数:
    - args: 额外位置参数
    """
    result = checkpoint_api.fork_checkpoint(
        _db_path(args), args.conversation_id, args.branch_name, checkpoint_id=args.checkpoint or None,
    )
    ok(f"已分支: {args.conversation_id} -> {result['conversation_id']}")


def cmd_checkpoint_lineage(args: argparse.Namespace):
    """
    查看检查点血缘链 (根在前)

    参数:
    - args: 额外位置参数
    """
    result = checkpoint_api.trace_lineage(_db_path(args), args.checkpoint_id)
    lineage = result["lineage"]

    def _human() -> None:
        print("血缘链 (根在前):")
        for cp in lineage:
            print(
                f"  {cp.get('checkpoint_id')}  [{cp.get('checkpoint_kind')}] {cp.get('name') or '-'} "
                f"(scope={cp.get('scope_id')}, batch={cp.get('batch_id') or '-'})"
            )

    render_data(result, _human)


def cmd_checkpoint_branches(args: argparse.Namespace):
    """
    列出对话 fork 出的全部分支

    参数:
    - args: 额外位置参数
    """
    result = checkpoint_api.list_branches(_db_path(args), args.conversation_id)
    branches = result["branches"]

    def _human() -> None:
        if not branches:
            print("没有分支")
            return
        for cp in branches:
            print(
                f"  {cp.get('checkpoint_id')}  {cp.get('name') or '-'} "
                f"(scope={cp.get('scope_id')}, parent={cp.get('parent_checkpoint_id') or '-'})"
            )

    render_data(result, _human)


def cmd_checkpoint_audit(args: argparse.Namespace):
    """
    查看对话的检查点变更记录 (含 source / reason)

    参数:
    - args: 额外位置参数
    """
    result = checkpoint_api.list_mutations(_db_path(args), args.conversation_id)
    mutations = result["mutations"]

    def _human() -> None:
        if not mutations:
            print("没有变更记录")
            return
        print("变更记录 (最新在前):")
        for cp in mutations:
            print(
                f"  {cp.get('created_at', 0):.0f}  {cp.get('checkpoint_id')}  [{cp.get('checkpoint_kind')}] "
                f"{cp.get('name') or '-'}  source={cp.get('source')} reason={cp.get('reason') or '-'}"
            )

    render_data(result, _human)


def dispatch(args: argparse.Namespace):
    """
    checkpoint 子命令分发 (统一异常兜底, 避免裸 traceback 退出)

    参数:
    - args: 命令参数
    """
    dispatch_action({
        "create": cmd_checkpoint_create,
        "list": cmd_checkpoint_list,
        "rollback": cmd_checkpoint_rollback,
        "retry": cmd_checkpoint_retry,
        "fork": cmd_checkpoint_fork,
        "lineage": cmd_checkpoint_lineage,
        "branches": cmd_checkpoint_branches,
        "audit": cmd_checkpoint_audit,
    }, args)
