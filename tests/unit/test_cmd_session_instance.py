"""
session instance CLI 离线路径测试 (cmd_session_instance)

覆盖:
- list / delete / bulk-delete 复用 SessionInstanceConfigService 冷管理
- restart 离线时统一报错
- 未知平台的统一错误
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any
import pytest

from satrap.cli import cmd_session_instance
from satrap.cli.output import CliError
from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config.session_instance_service import SessionInstanceConfigService
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.storage import StorageLayout
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager


class _DeadClient:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def is_alive(self) -> bool:
        return False


@pytest.fixture
def offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackendConfig:
    """强制离线并预置一个会话类与两个实例"""
    config = BackendConfig(
        data_root=str(tmp_path / "data"),
        session_class_config_path=str(tmp_path / "sessions.json"),
        edictum_config_path=str(tmp_path / "edictum.json"),
        session_scan_paths=[],
    )
    monkeypatch.setattr(cmd_session_instance, "daemon_client_from_args", _DeadClient)
    monkeypatch.setattr(cmd_session_instance, "load_cli_config", lambda args: config)

    scm = SessionClassConfigManager(storage_path=config.session_class_config_path, session_scan_paths=[])
    scm.register_by_class_path("dummy", "satrap.core.framework.Base.Session")
    layout = StorageLayout(config.data_root)
    database = layout.platform_db("local")
    service = SessionInstanceConfigService(
        SessionConfigStore(database),
        UserInfoStore(database),
        scm,
        EdictumConfigManager(create_default_edictum_type_registry(), storage_path=config.edictum_config_path),
        "local",
        layout,
    )
    service.create_instance("session_class", "dummy", session_id="inst-1")
    service.create_instance("session_class", "dummy", session_id="inst-2")
    return config


def _args(**overrides: Any) -> Namespace:
    base: dict[str, Any] = dict(
        instance_action="list", session_id="", session_ids=[], mode="selected",
        platform_id="", offline=False, force_offline=False,
    )
    base.update(overrides)
    return Namespace(**base)


def test_instance_list(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """列出持久化实例"""
    cmd_session_instance.cmd_instance_list(_args())
    out = capsys.readouterr().out
    assert "inst-1" in out and "inst-2" in out


def test_instance_delete(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """删除单个实例"""
    cmd_session_instance.cmd_instance_delete(_args(instance_action="delete", session_id="inst-1"))
    capsys.readouterr()
    cmd_session_instance.cmd_instance_list(_args())
    out = capsys.readouterr().out
    assert "inst-1" not in out
    assert "inst-2" in out


def test_instance_delete_missing_raises(offline_env: BackendConfig):
    """删除不存在实例抛 CliError"""
    with pytest.raises(CliError, match="不存在"):
        cmd_session_instance.cmd_instance_delete(_args(instance_action="delete", session_id="nope"))


def test_instance_bulk_delete_empty(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """empty 模式删除无消息实例"""
    cmd_session_instance.cmd_instance_bulk_delete(_args(instance_action="bulk-delete", mode="empty"))
    out = capsys.readouterr().out
    assert "inst-1" in out and "inst-2" in out
    cmd_session_instance.cmd_instance_list(_args())
    out = capsys.readouterr().out
    assert "没有持久化会话实例" in out


def test_instance_bulk_delete_selected_requires_ids(offline_env: BackendConfig):
    """selected 模式不带 ID 时给出用法提示"""
    with pytest.raises(CliError, match="至少选择一个"):
        cmd_session_instance.cmd_instance_bulk_delete(_args(instance_action="bulk-delete", mode="selected", session_ids=[]))


def test_instance_bulk_delete_selected(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """selected 模式删除指定实例"""
    cmd_session_instance.cmd_instance_bulk_delete(
        _args(instance_action="bulk-delete", mode="selected", session_ids=["inst-2"])
    )
    capsys.readouterr()
    cmd_session_instance.cmd_instance_list(_args())
    out = capsys.readouterr().out
    assert "inst-1" in out and "inst-2" not in out


def test_instance_restart_requires_backend(offline_env: BackendConfig):
    """restart 离线时统一报错"""
    with pytest.raises(CliError, match="需要后端运行"):
        cmd_session_instance.cmd_instance_restart(_args(instance_action="restart", session_id="inst-1"))


def test_unknown_platform_raises(offline_env: BackendConfig):
    """未知平台实例 ID 统一报错"""
    with pytest.raises(CliError, match="未知平台实例"):
        cmd_session_instance.cmd_instance_delete(
            _args(instance_action="delete", session_id="inst-1", platform_id="no-such-platform")
        )
