"""
UserManager / UserInfoStore / 用户 API 单元测试

覆盖:
- UserInfoStore: 用户 CRUD + 会话绑定 + 上下文会话路由
- UserManager.auto_create 开关语义 (True 自动创建 / False 拒绝未知用户)
- UserManager: bind / unbind / get_user_sessions / resolve_session / route_call
- satrap.api.user: list / get / create / update / delete / bind / unbind / sessions
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from satrap.api import user as user_api
from satrap.core.framework.Base import Session
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.UserManager import UserInfoStore, UserManager
from satrap.core.type import UserCall, UserInfo


class _EchoSession(Session):
    """测试用同步会话"""

    def run(self, message: str) -> str:
        return message


def _session_class_mgr(tmp_path: Path, params: dict[str, Any] | None = None) -> SessionClassConfigManager:
    mgr = SessionClassConfigManager(storage_path=tmp_path / "session_classes.json")
    mgr.register("dummy", _EchoSession)
    if params:
        mgr.set_config("dummy", params)
    return mgr


def _session_manager(tmp_path: Path, scm: SessionClassConfigManager) -> SessionManager:
    sm = SessionManager(default_session_type="dummy", db_path=tmp_path / "sessions.db")
    sm.register_session_type("dummy", _EchoSession)
    sm.class_cfg_mgr = scm
    return sm


def _user_manager(tmp_path: Path, auto_create: bool = True) -> UserManager:
    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    return UserManager(sm, db_path=tmp_path / "users.db", auto_create=auto_create)


# ================= UserInfoStore 测试 =================


def test_store_upsert_get_delete_roundtrip(tmp_path: Path):
    """
    用户写入后可查询, 平台/昵称更新后覆盖, 删除后消失

    参数:
    - tmp_path: tmp路径
    """
    store = UserInfoStore(db_path=tmp_path / "users.db")
    info = UserInfo(user_id="u1", user_platform="misskey", user_nickname="小美", user_session=[])

    store.upsert(info)
    got = store.get("u1")
    assert got is not None
    assert got.user_platform == "misskey"
    assert got.user_nickname == "小美"

    got.user_nickname = "新昵称"
    store.upsert(got)
    renamed = store.get("u1")
    assert renamed is not None
    assert renamed.user_nickname == "新昵称"

    assert store.get("missing") is None
    store.delete("u1")
    assert store.get("u1") is None


def test_store_list_sorted_and_limited(tmp_path: Path):
    """
    list 按 user_id 升序且受 limit 限制

    参数:
    - tmp_path: tmp路径
    """
    store = UserInfoStore(db_path=tmp_path / "users.db")
    for uid in ("b", "a", "c"):
        store.upsert(UserInfo(user_id=uid, user_platform="", user_nickname="", user_session=[]))

    all_users = store.list()
    assert [u.user_id for u in all_users] == ["a", "b", "c"]
    assert [u.user_id for u in store.list(limit=2)] == ["a", "b"]


def test_store_add_remove_session_is_idempotent(tmp_path: Path):
    """
    会话绑定幂等, 解绑只移除目标会话

    参数:
    - tmp_path: tmp路径
    """
    store = UserInfoStore(db_path=tmp_path / "users.db")
    store.upsert(UserInfo(user_id="u1", user_platform="", user_nickname="", user_session=[]))

    store.add_session("u1", "sid-1")
    store.add_session("u1", "sid-1")   # 重复绑定不生效
    store.add_session("u1", "sid-2")
    assert store.list_user_sessions("u1") == ["sid-1", "sid-2"]

    store.remove_session("u1", "sid-1")
    assert store.list_user_sessions("u1") == ["sid-2"]
    assert store.list_user_sessions("missing") == []


def test_store_context_session_route(tmp_path: Path):
    """
    上下文会话路由: 创建/查询/删除

    参数:
    - tmp_path: tmp路径
    """
    store = UserInfoStore(db_path=tmp_path / "users.db")
    key = store.upsert_context_session("u1", "misskey", "chat", "sid-1")
    assert key == "chat:misskey:u1"

    got = store.get_context_session("u1", "misskey", "chat")
    assert got is not None
    assert got.session_id == "sid-1"
    assert got.user_id == "u1"

    store.upsert_context_session("u1", "misskey", "chat", "sid-2")   # 更新路由
    rerouted = store.get_context_session("u1", "misskey", "chat")
    assert rerouted is not None
    assert rerouted.session_id == "sid-2"

    store.delete_context_session("u1", "misskey", "chat")
    assert store.get_context_session("u1", "misskey", "chat") is None


# ================= auto_create 开关语义 =================


def test_auto_create_true_creates_missing_user(tmp_path: Path):
    """
    auto_create=True (默认): 未知用户自动创建并落库

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path, auto_create=True)
    info = um.get_or_create_user("u1", platform="misskey", nickname="小美")

    assert info is not None
    assert info.user_id == "u1"
    assert um.store.get("u1") is not None


def test_auto_create_false_rejects_missing_user(tmp_path: Path):
    """
    auto_create=False: 未知用户返回 None 且不落库

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path, auto_create=False)
    assert um.get_or_create_user("u1", platform="misskey") is None
    assert um.store.get("u1") is None


def test_auto_create_false_still_works_for_existing_user(tmp_path: Path):
    """
    auto_create=False: 已存在用户仍可查询与更新

    参数:
    - tmp_path: tmp路径
    """
    um_on = _user_manager(tmp_path, auto_create=True)
    um_on.get_or_create_user("u1", platform="misskey", nickname="小美")

    um_off = _user_manager(tmp_path, auto_create=False)
    info = um_off.get_or_create_user("u1", platform="onebot", nickname="新昵称")
    assert info is not None
    assert info.user_platform == "onebot"
    assert info.user_nickname == "新昵称"


def test_auto_create_false_resolve_session_returns_empty(tmp_path: Path):
    """
    auto_create=False: resolve_session 对未知用户返回空, 不创建会话

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path, auto_create=False)
    assert um.resolve_session("u1", "misskey", "dummy", um.sm.class_cfg_mgr) == ""
    assert um.store.get("u1") is None


def test_auto_create_false_create_user_session_returns_empty(tmp_path: Path):
    """
    auto_create=False: create_user_session 对未知用户返回空

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path, auto_create=False)
    assert um.create_user_session("u1", _EchoSession, "dummy") == ""


def test_auto_create_false_route_call_rejects(tmp_path: Path):
    """
    auto_create=False: route_call 对未知用户拒绝处理

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path, auto_create=False)
    assert um.route_call(UserCall(session_id="", message="hello"), "u1") == ""


# ================= 绑定与会话 =================


def test_bind_unbind_session_with_missing_user(tmp_path: Path):
    """
    绑定/解绑: 用户不存在返回 False, 存在时幂等操作

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path)
    assert um.bind_session("missing", "sid-1") is False
    assert um.unbind_session("missing", "sid-1") is False

    um.get_or_create_user("u1")
    assert um.bind_session("u1", "sid-1") is True
    assert um.bind_session("u1", "sid-1") is True   # 幂等
    assert um.get_user_session_ids("u1") == ["sid-1"]
    assert um.unbind_session("u1", "sid-1") is True
    assert um.get_user_session_ids("u1") == []


def test_get_user_sessions_returns_configs(tmp_path: Path):
    """
    get_user_sessions 返回绑定的 SessionConfig 列表

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path)
    sid = um.resolve_session("u1", "misskey", "dummy", um.sm.class_cfg_mgr)

    configs = um.get_user_sessions("u1")
    assert len(configs) == 1
    assert configs[0].session_id == sid


def test_unbind_orphan_sessions_removes_stale_bindings(tmp_path: Path):
    """
    unbind_orphan_sessions 清理已不存在的会话绑定

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path)
    um.get_or_create_user("u1")
    um.bind_session("u1", "ghost-sid")
    assert um.unbind_orphan_sessions("u1") == 1
    assert um.get_user_session_ids("u1") == []


# ================= 路由 =================


def test_resolve_session_reuses_same_context(tmp_path: Path):
    """
    同 user+platform+type 复用同一会话, 不同 platform 各自独立

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path)
    scm = um.sm.class_cfg_mgr

    first = um.resolve_session("u1", "misskey", "dummy", scm)
    second = um.resolve_session("u1", "misskey", "dummy", scm)
    assert first == second
    assert first.startswith("dummy:misskey:u1:")

    other = um.resolve_session("u1", "onebot", "dummy", scm)
    assert other != first
    assert other.startswith("dummy:onebot:u1:")


def test_route_call_full_flow(tmp_path: Path):
    """
    route_call: 自动创建用户与会话, 消息路由返回会话回复

    参数:
    - tmp_path: tmp路径
    """
    um = _user_manager(tmp_path)
    um.resolve_session("u1", "misskey", "dummy", um.sm.class_cfg_mgr)

    result = um.route_call(UserCall(session_id="", message="hello"), "u1")
    assert result == "hello"
    assert um.store.get("u1") is not None


# ================= API 层 =================


def _api_db(tmp_path: Path) -> str:
    return str(tmp_path / "users.db")


def test_api_list_and_get(tmp_path: Path):
    """
    API: 创建后可列表与详情查询

    参数:
    - tmp_path: tmp路径
    """
    db = _api_db(tmp_path)
    result = user_api.create_user(db, "u1", platform="misskey", nickname="小美")
    assert result["ok"] is True
    assert result["created"] is True

    listed = user_api.list_users(db)
    assert listed["count"] == 1
    assert listed["users"][0]["user_id"] == "u1"

    got = user_api.get_user(db, "u1")
    assert got["ok"] is True
    assert got["user"]["user_nickname"] == "小美"
    assert user_api.get_user(db, "missing")["ok"] is False


def test_api_create_update_delete(tmp_path: Path):
    """
    API: 创建幂等更新, 更新字段, 删除

    参数:
    - tmp_path: tmp路径
    """
    db = _api_db(tmp_path)
    user_api.create_user(db, "u1", platform="misskey", nickname="小美")
    again = user_api.create_user(db, "u1", platform="onebot")
    assert again["created"] is False
    assert again["user"]["user_platform"] == "onebot"

    updated = user_api.update_user(db, "u1", nickname="新昵称")
    assert updated["user"]["user_nickname"] == "新昵称"
    assert user_api.update_user(db, "missing", nickname="x")["ok"] is False

    assert user_api.delete_user(db, "u1")["ok"] is True
    assert user_api.delete_user(db, "u1")["ok"] is False
    assert user_api.list_users(db)["count"] == 0


def test_api_bind_unbind_sessions(tmp_path: Path):
    """
    API: 绑定/解绑会话, 会话列表

    参数:
    - tmp_path: tmp路径
    """
    db = _api_db(tmp_path)
    user_api.create_user(db, "u1")
    assert user_api.bind_session(db, "missing", "sid-1")["ok"] is False

    bound = user_api.bind_session(db, "u1", "sid-1")
    assert bound["ok"] is True
    assert bound["session_ids"] == ["sid-1"]

    sessions = user_api.list_user_sessions(db, "u1")
    assert sessions["session_ids"] == ["sid-1"]
    assert sessions["count"] == 1

    unbound = user_api.unbind_session(db, "u1", "sid-1")
    assert unbound["ok"] is True
    assert unbound["session_ids"] == []
