"""用户管理 HTTP API handlers

与 checkpoint API 同模式: 直接构造 UserInfoStore 操作用户库,
与运行时 Session 解耦; 会话级绑定关系通过 user_session 字段维护,
运行时注入请使用 UserManager (resolve_session / route_call)。
"""
from __future__ import annotations

from typing import Any

from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.type import UserInfo


def _user_to_dict(info: UserInfo) -> dict[str, Any]:
    """UserInfo -> JSON 可序列化 dict"""
    return {
        "user_id": info.user_id,
        "user_platform": info.user_platform,
        "user_nickname": info.user_nickname,
        "user_session": list(info.user_session or []),
    }


def list_users(db_path: str, limit: int = 200) -> dict[str, Any]:
    """列出全部用户 (按 user_id 升序)"""
    store = UserInfoStore(db_path=db_path)
    users = store.list(limit=limit)
    return {"users": [_user_to_dict(u) for u in users], "count": len(users)}


def get_user(db_path: str, user_id: str) -> dict[str, Any]:
    """查询单个用户详情"""
    store = UserInfoStore(db_path=db_path)
    info = store.get(user_id)
    if info is None:
        return {"ok": False, "error": f"user_id 不存在: {user_id}"}
    return {"ok": True, "user": _user_to_dict(info)}


def create_user(
    db_path: str,
    user_id: str,
    platform: str = "",
    nickname: str = "",
) -> dict[str, Any]:
    """创建用户 (已存在则更新平台/昵称, 幂等)"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return {"ok": False, "error": "user_id 不能为空"}
    store = UserInfoStore(db_path=db_path)
    info = store.get(user_id)
    if info is not None:
        changed = False
        if platform and info.user_platform != platform:
            info.user_platform = platform
            changed = True
        if nickname and info.user_nickname != nickname:
            info.user_nickname = nickname
            changed = True
        if changed:
            store.upsert(info)
        return {"ok": True, "user": _user_to_dict(info), "created": False}
    created = UserInfo(
        user_id=user_id,
        user_platform=platform or "",
        user_nickname=nickname or "",
        user_session=[],
    )
    store.upsert(created)
    return {"ok": True, "user": _user_to_dict(created), "created": True}


def update_user(
    db_path: str,
    user_id: str,
    nickname: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """更新用户昵称/平台"""
    store = UserInfoStore(db_path=db_path)
    info = store.get(user_id)
    if info is None:
        return {"ok": False, "error": f"user_id 不存在: {user_id}"}
    changed = False
    if nickname is not None and str(nickname).strip() and info.user_nickname != nickname:
        info.user_nickname = nickname
        changed = True
    if platform is not None and info.user_platform != platform:
        info.user_platform = platform
        changed = True
    if changed:
        store.upsert(info)
    return {"ok": True, "user": _user_to_dict(info)}


def delete_user(db_path: str, user_id: str) -> dict[str, Any]:
    """删除用户信息 (不删除绑定的会话本身)"""
    store = UserInfoStore(db_path=db_path)
    info = store.get(user_id)
    if info is None:
        return {"ok": False, "error": f"user_id 不存在: {user_id}"}
    store.delete(user_id)
    return {"ok": True}


def bind_session(db_path: str, user_id: str, session_id: str) -> dict[str, Any]:
    """给用户绑定一个 session_id (幂等)"""
    store = UserInfoStore(db_path=db_path)
    if store.get(user_id) is None:
        return {"ok": False, "error": f"user_id 不存在: {user_id}"}
    store.add_session(user_id, session_id)
    return {"ok": True, "session_ids": store.list_user_sessions(user_id)}


def unbind_session(db_path: str, user_id: str, session_id: str) -> dict[str, Any]:
    """从用户解绑一个 session_id"""
    store = UserInfoStore(db_path=db_path)
    if store.get(user_id) is None:
        return {"ok": False, "error": f"user_id 不存在: {user_id}"}
    store.remove_session(user_id, session_id)
    return {"ok": True, "session_ids": store.list_user_sessions(user_id)}


def list_user_sessions(db_path: str, user_id: str) -> dict[str, Any]:
    """获取用户绑定的 session_id 列表"""
    store = UserInfoStore(db_path=db_path)
    session_ids = store.list_user_sessions(user_id)
    return {"user_id": user_id, "session_ids": session_ids, "count": len(session_ids)}
