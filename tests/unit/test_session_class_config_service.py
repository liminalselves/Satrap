"""会话类配置共享领域服务测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.config.session_class_service import SessionClassConfigService


def test_session_class_config_service_cold_crud_and_reload(tmp_path: Path):
    """
    共享服务应在不导入会话类时完成冷配置增删改查

    参数:
    - tmp_path: 临时目录
    """
    storage_path = tmp_path / "session-classes.json"
    manager = SessionClassConfigManager(storage_path=storage_path)
    service = SessionClassConfigService(manager)

    created = service.create(
        {
            "name": "cold",
            "class_path": "not_installed.future.FutureSession",
            "is_async": True,
            "description": "冷创建",
            "params": {"model_name": "default"},
        }
    )

    assert created["class_path"] == "not_installed.future.FutureSession"
    assert created["is_async"] is True
    assert service.list_configs()["cold"]["params"] == {"model_name": "default"}

    updated = service.update(
        "cold",
        {
            "name": "renamed",
            "class_path": "future.provider.ProviderSession",
            "description": "已重命名",
            "context_key": "room_id",
        },
    )
    service.set_enabled("renamed", False)

    assert updated["description"] == "已重命名"
    assert service.get("cold") is None
    assert service.get("renamed") is not None
    assert service.get("renamed")["enabled"] is False   # type: ignore[index]

    reloaded = SessionClassConfigService(
        SessionClassConfigManager(storage_path=storage_path)
    )
    assert reloaded.get("renamed") is not None
    assert reloaded.get("renamed")["class_path"] == "future.provider.ProviderSession"   # type: ignore[index]
    assert reloaded.delete("renamed") is True
    assert reloaded.list_configs() == {}


def test_session_class_config_service_rejects_invalid_and_conflicting_input(tmp_path: Path):
    """
    共享服务应拒绝非法字段和名称冲突

    参数:
    - tmp_path: 临时目录
    """
    service = SessionClassConfigService(
        SessionClassConfigManager(storage_path=tmp_path / "session-classes.json")
    )
    service.create({"name": "first", "class_path": "example.First"})
    service.create({"name": "second", "class_path": "example.Second"})

    with pytest.raises(ValueError, match="名称已存在"):
        service.update("first", {"name": "second"})
    with pytest.raises(ValueError, match="未知会话类配置字段"):
        service.update("first", {"unknown": True})
    with pytest.raises(ValueError, match="params 必须是对象"):
        service.create({"name": "bad", "class_path": "example.Bad", "params": []})

    assert service.get("first")["class_path"] == "example.First"   # type: ignore[index]
    assert service.get("second")["class_path"] == "example.Second"   # type: ignore[index]
