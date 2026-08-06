"""SessionManager 检查点集成 (P3-1 收尾) 单元测试

覆盖:
- default_checkpoint / default_checkpoint_db 注入会话构造
- 显式配置 (session_config) 优先于默认值
- 默认关闭时行为不变
- 真实 Session 子类端到端可用检查点
- BackendConfig 解析 session_checkpoint 字段
"""
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.framework.Base import Session
from satrap.core.framework.SessionManager import SessionConfig, SessionManager
from satrap.core.framework.command import CommandHandler
from satrap.core.state import StateStore
from satrap.core.utils.context import ContextManager


class _RecordingSession(Session):
    """记录收到的检查点构造参数"""

    received: dict[str, Any] = {}

    def __init__(
        self,
        session_id: str,
        content_callback: Optional[Callable[[str], None]] | None = None,
        command_handler: Optional[CommandHandler] | None = None,
        *,
        db_path: str = ".satrap/chat_history.db",
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        **kw: Any,
    ):
        type(self).received = {
            "db_path": db_path,
            "enable_checkpoint": enable_checkpoint,
            "state_store": state_store,
        }
        super().__init__(
            session_id,
            content_callback,
            command_handler,
            db_path=db_path,
            state_store=state_store,
            enable_checkpoint=enable_checkpoint,
        )


class _CkptSession(Session):
    """真实可用检查点的会话子类 (带一个工作流上下文)"""

    def __init__(
        self,
        session_id: str,
        content_callback: Optional[Callable[[str], None]] | None = None,
        command_handler: Optional[CommandHandler] | None = None,
        *,
        db_path: str = ".satrap/chat_history.db",
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        **kw: Any,
    ):
        super().__init__(
            session_id,
            content_callback,
            command_handler,
            db_path=db_path,
            state_store=state_store,
            enable_checkpoint=enable_checkpoint,
        )
        self.wf_ctx = ContextManager(
            self.workflow_id_assign("main"),
            db_path=db_path,
            state_store=self._state_store,
            enable_checkpoint=True,
            auto_checkpoint=False,
        )
        self._track_workflow_context("main", self.wf_ctx)


def _make_cfg(session_id: str, params: dict[str, Any] | None = None) -> SessionConfig:
    now = time.time()
    return SessionConfig(
        session_id=session_id,
        session_type_name="rec",
        created_at=now,
        last_used_at=now,
        message_count=0,
        session_config=params or {},
    )


def _make_mgr(tmp_path: Path, **kw: Any) -> SessionManager:
    sm = SessionManager(db_path=str(tmp_path / "session_config.db"), **kw)
    sm.registry.register("rec", _RecordingSession)
    return sm


def test_default_checkpoint_injected(tmp_path: Path):
    """default_checkpoint=True 时, 未显式配置的会话收到注入的开关与库路径"""
    db = str(tmp_path / "chat_history.db")
    sm = _make_mgr(tmp_path, default_checkpoint=True, default_checkpoint_db=db)

    entry = sm._create_entry(_make_cfg("sid-1"))

    assert entry is not None
    received = _RecordingSession.received
    assert received["enable_checkpoint"] is True
    assert received["db_path"] == db


def test_explicit_config_overrides_default(tmp_path: Path):
    """session_config 显式配置优先于 SessionManager 默认值"""
    db = str(tmp_path / "chat_history.db")
    db_custom = str(tmp_path / "custom.db")
    sm = _make_mgr(tmp_path, default_checkpoint=True, default_checkpoint_db=db)

    entry = sm._create_entry(
        _make_cfg("sid-2", {"enable_checkpoint": False, "db_path": db_custom})
    )

    assert entry is not None
    received = _RecordingSession.received
    assert received["enable_checkpoint"] is False
    assert received["db_path"] == db_custom


def test_default_off_keeps_behavior(tmp_path: Path):
    """default_checkpoint=False (默认) 时行为不变: 收到 False"""
    sm = _make_mgr(tmp_path)

    entry = sm._create_entry(_make_cfg("sid-3"))

    assert entry is not None
    assert _RecordingSession.received["enable_checkpoint"] is False


def test_default_db_only_when_configured(tmp_path: Path):
    """default_checkpoint_db=None 时 db_path 使用会话自身默认"""
    sm = _make_mgr(tmp_path, default_checkpoint=True)

    entry = sm._create_entry(_make_cfg("sid-4"))

    assert entry is not None
    assert _RecordingSession.received["db_path"] == ".satrap/chat_history.db"


def test_injection_ignored_when_constructor_rejects(tmp_path: Path):
    """构造器不接受 enable_checkpoint 时注入被忽略, 不抛错"""

    class _StrictSession(Session):
        kw: dict[str, Any] = {}

        def __init__(self, session_id: str):
            self.session_id = session_id
            self.kw = {}

    sm = SessionManager(
        db_path=str(tmp_path / "session_config.db"),
        default_checkpoint=True,
        default_checkpoint_db=str(tmp_path / "x.db"),
    )
    sm.registry.register("strict", _StrictSession)

    now = time.time()
    cfg = SessionConfig(
        session_id="sid-5",
        session_type_name="strict",
        created_at=now,
        last_used_at=now,
        message_count=0,
        session_config={},
    )
    entry = sm._create_entry(cfg)

    assert entry is not None
    assert isinstance(entry.session, _StrictSession)
    assert entry.session.kw == {}
    assert not hasattr(entry.session, "enable_checkpoint")


def test_session_config_constructor_case_injected(tmp_path: Path):
    """case 1 (session_config 入参) 同样注入检查点默认值"""

    class _ConfigSession(Session):
        def __init__(
            self, session_config: SessionConfig, session_id: str | None = None, **kw: Any
        ):
            self.session_id = session_id
            self.received = kw

    sm = SessionManager(
        db_path=str(tmp_path / "session_config.db"),
        default_checkpoint=True,
        default_checkpoint_db=str(tmp_path / "ctx.db"),
    )
    sm.registry.register("cfg", _ConfigSession)

    now = time.time()
    cfg = SessionConfig(
        session_id="sid-6",
        session_type_name="cfg",
        created_at=now,
        last_used_at=now,
        message_count=0,
        session_config={},
    )
    entry = sm._create_entry(cfg)

    assert entry is not None
    assert isinstance(entry.session, _ConfigSession)
    assert entry.session.received["enable_checkpoint"] is True
    assert entry.session.received["db_path"] == str(tmp_path / "ctx.db")


def test_end_to_end_real_session_checkpoint(tmp_path: Path):
    """真实会话子类: 注入开启后会话级检查点端到端可用"""
    db = str(tmp_path / "chat_history.db")
    sm = SessionManager(
        db_path=str(tmp_path / "session_config.db"),
        default_checkpoint=True,
        default_checkpoint_db=db,
    )
    sm.registry.register("ckpt", _CkptSession)

    now = time.time()
    cfg = SessionConfig(
        session_id="sid-7",
        session_type_name="ckpt",
        created_at=now,
        last_used_at=now,
        message_count=0,
        session_config={},
    )
    entry = sm._create_entry(cfg)

    assert entry is not None
    session = entry.session
    assert isinstance(session, _CkptSession)
    assert session._state_store is not None

    session.session_ctx.add_user_message("会话消息")
    session.wf_ctx.add_user_message("工作流消息")
    batch_id = session.create_checkpoint(name="起点")
    manuals = [cp for cp in session.list_checkpoints() if cp.checkpoint_kind == "manual"]
    assert len(manuals) == 1   # session_ctx 自动 stable 检查点不计入

    session.wf_ctx.add_user_message("后续")
    session.rollback(batch_id)
    assert session.wf_ctx.get_context() == [{"role": "user", "content": "工作流消息"}]


def test_backend_config_parses_checkpoint_fields():
    """BackendConfig 解析 session_checkpoint / session_checkpoint_db"""
    cfg = BackendConfig.from_dict(
        {"session_checkpoint": True, "session_checkpoint_db": ".satrap/ctx.db"}
    )
    assert cfg.session_checkpoint is True
    assert cfg.session_checkpoint_db == ".satrap/ctx.db"

    default = BackendConfig.from_dict({})
    assert default.session_checkpoint is False
    assert default.session_checkpoint_db is None
