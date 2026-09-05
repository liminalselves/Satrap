"""
检查点管理 HTTP API handlers

与 CLI (satrap checkpoint) 同套逻辑: 直接构造 ContextManager / StateStore
操作上下文库, 与运行时 Session 解耦; 会话级聚合操作请在运行时通过
Session.create_checkpoint / rollback / fork 使用
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from satrap.core.utils.context import ContextManager
from satrap.core.state import StateStore
from satrap.core.type import StateCheckpoint, StateScope


def _cp_to_dict(cp: StateCheckpoint) -> dict[str, Any]:
    """
    StateCheckpoint -> JSON 可序列化 dict

    参数:
    - cp: cp 输入值

    返回:
    - dict[str, Any]: StateCheckpoint -> JSON 可序列化 dict
    """
    return {
        "checkpoint_id": cp.checkpoint_id,
        "namespace": cp.namespace,
        "scope_id": cp.scope_id,
        "branch_id": cp.branch_id,
        "name": cp.name,
        "description": cp.description,
        "snapshot_id": cp.snapshot_id or None,
        "batch_id": cp.batch_id or None,
        "state_revision": cp.state_revision,
        "position": cp.position,
        "checkpoint_kind": cp.checkpoint_kind,
        "parent_checkpoint_id": cp.parent_checkpoint_id,
        "source": cp.source,
        "reason": cp.reason,
        "created_at": cp.created_at,
    }


@contextmanager
def _open_ctx(db_path: str, conversation_id: str) -> Iterator[ContextManager]:
    """
    构造启用检查点的对话上下文, 用完释放复用连接

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID

    返回:
    - Iterator[ContextManager]: 构造启用检查点的对话上下文, 用完释放复用连接
    """
    ctx = ContextManager(conversation_id, db_path=db_path, enable_checkpoint=True)
    try:
        yield ctx
    finally:
        ctx.close()


def list_checkpoints(db_path: str, conversation_id: str) -> dict[str, Any]:
    """
    列出对话的检查点与全部分支

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID

    返回:
    - dict[str, Any]: 列出对话的检查点与全部分支
    """
    with _open_ctx(db_path, conversation_id) as ctx:
        checkpoints = ctx.list_checkpoints()
    store = StateStore(db_path=db_path)
    branches = store.list_branches(f"{conversation_id}:fork:")
    return {
        "conversation_id": conversation_id,
        "checkpoints": [_cp_to_dict(cp) for cp in checkpoints],
        "branches": [_cp_to_dict(cp) for cp in branches],
    }


def create_checkpoint(
    db_path: str,
    conversation_id: str,
    name: str = "",
    description: str = "",
) -> dict[str, Any]:
    """
    为对话创建手动检查点

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID
    - name: 名称
    - description: 说明文本

    返回:
    - dict[str, Any]: 为对话创建手动检查点
    """
    with _open_ctx(db_path, conversation_id) as ctx:
        cp = ctx.create_checkpoint(name=name, description=description)
    return {"checkpoint_id": cp.checkpoint_id, "ok": True}


def rollback_checkpoint(
    db_path: str, conversation_id: str, checkpoint_id: str
) -> dict[str, Any]:
    """
    回滚对话到指定检查点

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID
    - checkpoint_id: 检查点 ID

    返回:
    - dict[str, Any]: 回滚对话到指定检查点
    """
    with _open_ctx(db_path, conversation_id) as ctx:
        ctx.rollback(checkpoint_id)
    return {"checkpoint_id": checkpoint_id, "ok": True}


def retry_checkpoint(
    db_path: str, conversation_id: str, checkpoint_id: str
) -> dict[str, Any]:
    """
    从指定检查点重试 (保留未来检查点)

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID
    - checkpoint_id: 检查点 ID

    返回:
    - dict[str, Any]: 从指定检查点重试 (保留未来检查点)
    """
    with _open_ctx(db_path, conversation_id) as ctx:
        ctx.retry(checkpoint_id)
    return {"checkpoint_id": checkpoint_id, "ok": True}


def fork_checkpoint(
    db_path: str,
    conversation_id: str,
    branch_name: str,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    """
    从检查点 fork 一条新对话线

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID
    - branch_name: 分支名称
    - checkpoint_id: 检查点 ID

    返回:
    - dict[str, Any]: 从检查点 fork 一条新对话线
    """
    with _open_ctx(db_path, conversation_id) as ctx:
        new_ctx = ctx.fork(branch_name, checkpoint_id=checkpoint_id)
        new_id = new_ctx.conversation_id
        new_ctx.close()
    return {"conversation_id": new_id, "ok": True}


def list_branches(db_path: str, conversation_id: str) -> dict[str, Any]:
    """
    列出对话 fork 出的全部分支

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID

    返回:
    - dict[str, Any]: 列出对话 fork 出的全部分支
    """
    store = StateStore(db_path=db_path)
    branches = store.list_branches(f"{conversation_id}:fork:")
    return {
        "conversation_id": conversation_id,
        "branches": [_cp_to_dict(cp) for cp in branches],
    }


def trace_lineage(db_path: str, checkpoint_id: str) -> dict[str, Any]:
    """
    查看检查点血缘链 (根在前)

    参数:
    - db_path: 数据库路径
    - checkpoint_id: 检查点 ID

    返回:
    - dict[str, Any]: 查看检查点血缘链 (根在前)
    """
    store = StateStore(db_path=db_path)
    lineage = store.trace_lineage(checkpoint_id)
    return {
        "checkpoint_id": checkpoint_id,
        "lineage": [_cp_to_dict(cp) for cp in lineage],
    }


def list_mutations(db_path: str, conversation_id: str) -> dict[str, Any]:
    """
    查看对话的检查点变更记录 (含 source / reason, 最新在前)

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID

    返回:
    - dict[str, Any]: 查看对话的检查点变更记录 (含 source / reason, 最新在前)
    """
    store = StateStore(db_path=db_path)
    mutations = store.list_mutations(StateScope("conversation", conversation_id))
    return {
        "conversation_id": conversation_id,
        "mutations": [_cp_to_dict(cp) for cp in mutations],
    }
