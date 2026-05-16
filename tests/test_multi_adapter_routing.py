from __future__ import annotations

from pathlib import Path

from satrap.cli.cmd_session import _configured_adapter_ids
from satrap.core.framework.Base import Session
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformAdapter, PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig
from satrap.core.platform.event import MessageEvent, PlatformMetadata
from satrap.core.type import MessageMember, PlatformMessage, PlatformMessageType
from satrap.core.backend.BackendManager import BackendConfig


class _EchoSession(Session):
    """测试用同步会话"""

    def run(self, message: str) -> str:
        return message


class _DummyAdapter(PlatformAdapter):
    adapter_type = "dummy"

    async def run(self) -> None:
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)


def _session_class_mgr(tmp_path: Path, params: dict | None = None) -> SessionClassConfigManager:
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


def _message_event(adapter_id: str = "misskey1") -> MessageEvent:
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.session_id = "chat%user-1"
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="user-1", nickname="User")
    message.sender.user_id = "user-1"
    message.sender.nickname = "User"
    message.message = []
    message.message_str = "hello"
    message.raw_message = {}
    event = MessageEvent(
        message_str="hello",
        platform_message=message,
        platform_meta=PlatformMetadata(name=adapter_id, id=adapter_id),
        session_id=message.session_id,
        adapter=None,
        session_type="dummy",
    )
    return event


def test_same_adapter_type_can_register_multiple_instances():
    """同一 adapter type 可以注册多个不同实例 ID"""
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    mgr = PlatformAdapterManager(registry=registry)

    first = mgr.add_adapter(PlatformConfig(id="dummy1", type="dummy"))
    second = mgr.add_adapter(PlatformConfig(id="dummy2", type="dummy"))

    assert first is not None
    assert second is not None
    assert mgr.list_adapters() == ["dummy1", "dummy2"]


def test_user_manager_routes_same_user_to_different_adapter_sessions(tmp_path):
    """同一用户在不同 adapter_id 下应拥有不同上下文"""
    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")

    first = um.resolve_session("user-1", "misskey1", "dummy", scm)
    second = um.resolve_session("user-1", "misskey2", "dummy", scm)

    assert first == "dummy:misskey1:user-1"
    assert second == "dummy:misskey2:user-1"
    assert first != second


def test_pipeline_uses_configured_adapter_override(tmp_path):
    """类级 adapter_id 存在且有效时, 管线使用该适配器绑定上下文"""
    scm = _session_class_mgr(tmp_path, {"adapter_id": "misskey2"})
    sm = _session_manager(tmp_path, scm)
    scheduler = PipelineScheduler(sm)
    scheduler.set_adapter_ids({"misskey1", "misskey2"})

    platform_id, extra = scheduler._resolve_route_adapter(_message_event("misskey1"))

    assert platform_id == "misskey2"
    assert extra == {"adapter_id": "misskey2"}


def test_pipeline_falls_back_when_adapter_override_is_missing(tmp_path):
    """显式 adapter_id 不存在时回退事件来源适配器"""
    scm = _session_class_mgr(tmp_path, {"adapter_id": "missing"})
    sm = _session_manager(tmp_path, scm)
    scheduler = PipelineScheduler(sm)
    scheduler.set_adapter_ids({"misskey1"})

    platform_id, extra = scheduler._resolve_route_adapter(_message_event("misskey1"))

    assert platform_id == "misskey1"
    assert extra is None


def test_configured_adapter_ids_reads_backend_config_platforms():
    """CLI 创建会话时可从配置中读取 adapter_id 候选"""
    config = BackendConfig(
        platforms=[
            {"id": "misskey1", "type": "misskey", "settings": {}},
            {"id": "misskey2", "type": "misskey", "settings": {}},
        ],
    )

    assert _configured_adapter_ids(config) == {"misskey1", "misskey2"}
