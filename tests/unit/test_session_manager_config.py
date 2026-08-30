"""
SessionManager / SessionClassConfigManager 配置管理单元测试

覆盖:
- SessionClassConfigManager: register / list / list_types / get_class / get_params / delete
- SessionManager: register_session / get_session_config / list_session_configs
- update_session_config 存在与不存在分支
- remove_session / remove_session_async (仅内存 / 连配置删除)
- register_session_from_class_config 从类级配置派生实例配置
- reload_model_configs 无 manager 跳过与有 manager 刷新
"""
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from satrap.core.framework.Base import Session
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionConfig, SessionManager


class _EchoSession(Session):
    """测试用最小会话子类"""

    def __init__(self, session_id: str, **kw: Any):
        super().__init__(session_id, **kw)


def _make_sm(tmp_path: Path) -> SessionManager:
    return SessionManager(
        default_session_type="echo",
        default_session_class=_EchoSession,
        db_path=tmp_path / "session_config.db",
    )


# ================= SessionClassConfigManager 测试 =================

def test_class_config_register_list_and_types(tmp_path: Path):
    """
    类级配置: 注册后可列出, 类型名/参数正确

    参数:
    - tmp_path: tmp路径
    """
    mgr = SessionClassConfigManager(storage_path=tmp_path / "classes.json")
    mgr.register("dummy", _EchoSession)
    names = list(mgr.list_configs().keys())
    assert names == ["dummy"]
    assert mgr.get_class("dummy") is _EchoSession
    assert isinstance(mgr.get_params("dummy"), dict)
    assert mgr.has_config("dummy") is True


def test_class_config_persists_to_storage_file(tmp_path: Path):
    """
    类级配置写入 storage_path JSON 文件, 重建可恢复

    参数:
    - tmp_path: tmp路径
    """
    storage = tmp_path / "classes.json"
    mgr = SessionClassConfigManager(storage_path=storage)
    mgr.register("dummy", _EchoSession)
    mgr.set_config("dummy", {"model": "m1"})
    mgr2 = SessionClassConfigManager(storage_path=storage)
    assert list(mgr2.list_configs().keys()) == ["dummy"]
    assert mgr2.get_params("dummy") == {"model": "m1"}


def test_class_config_delete(tmp_path: Path):
    """
    类级配置 remove_config 后不再列出

    参数:
    - tmp_path: tmp路径
    """
    mgr = SessionClassConfigManager(storage_path=tmp_path / "classes.json")
    mgr.register("dummy", _EchoSession)
    assert mgr.remove_config("dummy") is True
    assert mgr.has_config("dummy") is False


# ================= SessionManager 配置 =================

def test_register_session_persists_config(tmp_path: Path):
    """
    register_session 生成 SessionConfig 并落库

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    cfg = sm.register_session(
        session_class=_EchoSession,
        session_type_name="echo",
        session_config={"model_name": "m1"},
        session_id="s-1",
    )
    assert cfg.session_id == "s-1"
    assert cfg.session_type_name == "echo"
    assert cfg.session_config == {"model_name": "m1"}
    assert sm.get_session_config("s-1") is not None


def test_get_session_config_missing_returns_none(tmp_path: Path):
    """
    不存在的会话 ID 返回 None

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    assert sm.get_session_config("nope") is None


def test_list_session_configs_ordered_by_last_used(tmp_path: Path):
    """
    list_session_configs 按最后使用时间倒序, 支持 limit

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    sm.register_session(_EchoSession, "echo", session_id="s-old")
    old_cfg = sm.get_session_config("s-old")
    assert old_cfg is not None
    sm.store.update_runtime_fields("s-old", last_used_at=time.time() - 1000, message_count=0)
    sm.register_session(_EchoSession, "echo", session_id="s-new")
    configs = sm.list_session_configs()
    assert [c.session_id for c in configs] == ["s-new", "s-old"]
    assert len(sm.list_session_configs(limit=1)) == 1


def test_update_session_config_existing_and_missing(tmp_path: Path):
    """
    update_session_config: 存在时更新并返回, 不存在返回 None

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    sm.register_session(_EchoSession, "echo", session_config={"a": 1}, session_id="s-1")
    updated = sm.update_session_config("s-1", session_config={"a": 2})
    assert updated is not None
    assert updated.session_config == {"a": 2}
    assert sm.update_session_config("missing", session_config={"a": 3}) is None


def test_register_session_from_class_config(tmp_path: Path):
    """
    从类级配置派生实例配置 (extra_params 覆盖)

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    mgr = SessionClassConfigManager(storage_path=tmp_path / "classes.json")
    mgr.register("dummy", _EchoSession)
    mgr.set_config("dummy", {"model": "m1", "k": "v"})
    cfg = sm.register_session_from_class_config(
        "dummy", mgr, session_id="s-cls", extra_params={"k": "override"}
    )
    assert cfg.session_type_name == "dummy"
    assert cfg.session_config["model"] == "m1"
    assert cfg.session_config["k"] == "override"


def test_list_registered_session_types(tmp_path: Path):
    """
    list_registered_session_types 返回注册的类型名

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    sm.register_session_type("extra", _EchoSession)
    types = sm.list_registered_session_types()
    assert "echo" in types
    assert "extra" in types


# ================= remove_session 测试 =================

def _register_active_session(sm: SessionManager, session_id: str) -> None:
    """
    注册一个会话并放入活跃池

    参数:
    - sm: sm 输入值
    - session_id: 会话 ID
    """
    cfg = sm.register_session(_EchoSession, "echo", session_id=session_id)
    session = _EchoSession(session_id)
    sm.pool.put(session_id, session, session_type=cfg.session_type_name or "echo")


def test_remove_session_keeps_config_by_default(tmp_path: Path):
    """
    remove_session 默认仅移除内存活跃实例, 保留 SQL 配置

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    _register_active_session(sm, "s-1")
    assert len(sm.list_sessions()) == 1
    sm.remove_session("s-1")
    assert len(sm.list_sessions()) == 0
    assert sm.get_session_config("s-1") is not None   # 配置保留


def test_remove_session_with_config_deletes_both(tmp_path: Path):
    """
    remove_session(remove_config=True) 同时删除内存实例与 SQL 配置

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    _register_active_session(sm, "s-1")
    sandbox_path = sm.storage_layout.session_sandbox("local", "s-1")
    (sandbox_path / "result.txt").write_text("data", encoding="utf-8")

    sm.remove_session("s-1", remove_config=True)

    assert sm.get_session_config("s-1") is None
    assert not sandbox_path.exists()
    assert any((sm.storage_layout.trash_root("local") / "sessions").iterdir())


def test_remove_session_missing_id_no_error(tmp_path: Path):
    """
    移除不存在的会话不报错

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    sm.remove_session("missing")
    sm.remove_session("missing", remove_config=True)


@pytest.mark.asyncio
async def test_remove_session_async(tmp_path: Path):
    """
    异步移除会话, 行为与同步一致

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    _register_active_session(sm, "s-1")
    sandbox_path = sm.storage_layout.session_sandbox("local", "s-1")
    (sandbox_path / "result.txt").write_text("data", encoding="utf-8")

    await sm.remove_session_async("s-1", remove_config=True)

    assert sm.get_session_config("s-1") is None
    assert not sandbox_path.exists()


# ================= reload_model_configs 测试 =================

def test_reload_model_configs_skips_without_manager(tmp_path: Path):
    """
    无 ModelConfigManager 时跳过, 不报错

    参数:
    - tmp_path: tmp路径
    """
    sm = _make_sm(tmp_path)
    sm.reload_model_configs()


def test_reload_model_configs_refreshes_active_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    有 manager 时刷新活跃会话的 LLM 实例

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    sm = _make_sm(tmp_path)
    _register_active_session(sm, "s-1")

    fake_session = MagicMock()
    fake_entry = SimpleNamespace(
        session=fake_session,
        session_type="echo",
        created_at=time.time(),
        last_used=time.time(),
    )
    monkeypatch.setattr(sm.pool, "list_entries", lambda: {"s-1": fake_entry})

    mgr = MagicMock()
    mgr.get_llm_config.return_value = SimpleNamespace(
        api_key="key", base_url="http://x", model="m",
        temperature=0.5, max_tokens=1024,
    )
    sm._model_cfg_mgr = mgr
    sm.reload_model_configs()
    fake_session.reload_llm.assert_called_once()
    fake_session.apply_context_config.assert_called_once_with(mgr.get_llm_config.return_value)
