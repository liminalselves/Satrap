"""状态检查点 CLI: create / list / rollback / retry / fork / lineage / branches

直接操作上下文库 (默认 .satrap/satrapdata/chat_history.db, 可用 --db 覆盖),
与运行时 Session 解耦; 会话级聚合操作请在运行时通过 Session.create_checkpoint / rollback / fork 使用
"""
from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from typing import Iterator

from satrap.core.state import StateStore
from satrap.core.type import StateScope
from satrap.core.utils.context import ContextManager
from satrap.core.utils.paths import get_db_path


DEFAULT_DB = get_db_path("chat_history.db")
"""上下文库默认路径 (与 ContextManager 默认一致)"""


def _db_path(args: argparse.Namespace) -> str:
    """上下文库路径: --db 优先, 否则默认路径"""
    return args.db or DEFAULT_DB


def _ctx(args: argparse.Namespace, conversation_id: str) -> ContextManager:
    """按上下文库路径构造对话上下文 (自动启用检查点)"""
    return ContextManager(
        conversation_id,
        db_path=_db_path(args),
        enable_checkpoint=True,
    )


@contextmanager
def _open_ctx(args: argparse.Namespace, conversation_id: str) -> Iterator[ContextManager]:
    """构造对话上下文, 用毕自动释放复用连接"""
    ctx = _ctx(args, conversation_id)
    try:
        yield ctx
    finally:
        ctx.close()


def _store(args: argparse.Namespace) -> StateStore:
    """按上下文库路径构造状态存储"""
    return StateStore(db_path=_db_path(args))


def cmd_checkpoint_create(args: argparse.Namespace):
    """为对话创建检查点"""
    with _open_ctx(args, args.conversation_id) as ctx:
        cp = ctx.create_checkpoint(name=args.name, description=args.description)
    label = cp.batch_id or cp.checkpoint_id
    print(f"已创建检查点: {label} (对话: {args.conversation_id})")


def cmd_checkpoint_list(args: argparse.Namespace):
    """列出对话的全部检查点"""
    with _open_ctx(args, args.conversation_id) as ctx:
        checkpoints = ctx.list_checkpoints()
    if not checkpoints:
        print("没有检查点")
        return
    for cp in checkpoints:
        print(
            f"{cp.checkpoint_id}  [{cp.checkpoint_kind}] {cp.name or '-'} "
            f"(revision={cp.state_revision}, position={cp.position}, "
            f"batch={cp.batch_id or '-'})"
        )


def cmd_checkpoint_rollback(args: argparse.Namespace):
    """回滚对话到指定检查点"""
    with _open_ctx(args, args.conversation_id) as ctx:
        ctx.rollback(args.checkpoint_id)
    print(f"已回滚到检查点: {args.checkpoint_id}")


def cmd_checkpoint_retry(args: argparse.Namespace):
    """从指定检查点重试 (保留未来检查点)"""
    with _open_ctx(args, args.conversation_id) as ctx:
        ctx.retry(args.checkpoint_id)
    print(f"已重试到检查点: {args.checkpoint_id} (未来检查点已保留)")


def cmd_checkpoint_fork(args: argparse.Namespace):
    """从检查点 fork 一条新对话线"""
    with _open_ctx(args, args.conversation_id) as ctx:
        new_ctx = ctx.fork(args.branch_name, checkpoint_id=args.checkpoint)
    try:
        print(f"已分支: {args.conversation_id} -> {new_ctx.conversation_id}")
    finally:
        new_ctx.close()


def cmd_checkpoint_lineage(args: argparse.Namespace):
    """查看检查点血缘链 (根在前)"""
    store = _store(args)
    lineage = store.trace_lineage(args.checkpoint_id)
    print("血缘链 (根在前):")
    for cp in lineage:
        print(
            f"  {cp.checkpoint_id}  [{cp.checkpoint_kind}] {cp.name or '-'} "
            f"(scope={cp.scope_id}, batch={cp.batch_id or '-'})"
        )


def cmd_checkpoint_branches(args: argparse.Namespace):
    """列出对话 fork 出的全部分支"""
    store = _store(args)
    branches = store.list_branches(f"{args.conversation_id}:fork:")
    if not branches:
        print("没有分支")
        return
    for cp in branches:
        print(
            f"  {cp.checkpoint_id}  {cp.name or '-'} "
            f"(scope={cp.scope_id}, parent={cp.parent_checkpoint_id or '-'})"
        )


def cmd_checkpoint_audit(args: argparse.Namespace):
    """查看对话的检查点变更记录 (含 source / reason)"""
    store = _store(args)
    mutations = store.list_mutations(StateScope("conversation", args.conversation_id))
    if not mutations:
        print("没有变更记录")
        return
    print("变更记录 (最新在前):")
    for cp in mutations:
        print(
            f"  {cp.created_at:.0f}  {cp.checkpoint_id}  [{cp.checkpoint_kind}] "
            f"{cp.name or '-'}  source={cp.source} reason={cp.reason or '-'}"
        )


def dispatch(args: argparse.Namespace):
    """checkpoint 子命令分发 (统一异常兜底, 避免裸 traceback 退出)"""
    action_map = {
        "create": cmd_checkpoint_create,
        "list": cmd_checkpoint_list,
        "rollback": cmd_checkpoint_rollback,
        "retry": cmd_checkpoint_retry,
        "fork": cmd_checkpoint_fork,
        "lineage": cmd_checkpoint_lineage,
        "branches": cmd_checkpoint_branches,
        "audit": cmd_checkpoint_audit,
    }
    handler = action_map.get(args.action)
    if handler is None:
        print(f"未知操作: {args.action}")
        sys.exit(2)
    try:
        handler(args)
    except ValueError as e:
        # 业务错误 (检查点不存在 / 批次冲突 / 目标作用域已有数据)
        print(f"错误: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"意外错误: {e}")
        sys.exit(2)
