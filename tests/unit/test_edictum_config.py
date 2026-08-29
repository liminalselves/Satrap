from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from satrap.core.config.edictum_service import EdictumConfigService
from satrap.core.framework.Base import Session
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import (
    EdictumTypeDefinition,
    EdictumTypeRegistry,
    create_default_edictum_type_registry,
)


class _FutureEdictumSession(Session):
    """验证自定义类型无需修改注册表设计的测试会话"""

    def __init__(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.kwargs = kwargs


def test_default_registry_exposes_builtin_types_and_accepts_future_type() -> None:
    """类型注册表应内置 simple 系列并允许独立扩展新 Edictum 类型"""
    registry = create_default_edictum_type_registry()
    registry.register(
        EdictumTypeDefinition(
            name="future",
            factory=_FutureEdictumSession,
            is_async=False,
            description="未来类型",
        )
    )

    names = [item["name"] for item in registry.list_types()]
    created = registry.create("future", session_id="future-1", feature=True)

    assert names == ["async_simple", "future", "simple"]
    assert isinstance(created, _FutureEdictumSession)
    assert created.kwargs["feature"] is True


def test_edictum_cold_config_crud_is_named_and_has_no_placeholders(tmp_path: Path) -> None:
    """Edictum 冷配置应从空对象开始并支持创建, 重命名, 禁用和删除"""
    registry = create_default_edictum_type_registry()
    storage_path = tmp_path / "edictum.json"
    manager = EdictumConfigManager(registry, storage_path)
    service = EdictumConfigService(manager, registry)

    assert service.list_configs() == {}
    assert json.loads(storage_path.read_text(encoding="utf-8")) == {}

    payload: dict[str, Any] = {
            "name": "assistant",
            "edictum_type": "async_simple",
            "model_name": "default",
            "params": {"system_prompt": "你是助手"},
            "plugins": ["session_commands", {"name": "tools", "enabled": False}],
    }
    created = service.create(payload)
    final_name, renamed = service.update(
        "assistant",
        {"name": "platform-assistant", "description": "平台会话"},
    )
    disabled = service.set_enabled(final_name, False)

    assert created["provider"] == "edictum"
    assert created["edictum_type"] == "async_simple"
    assert all(isinstance(plugin, dict) for plugin in created["plugins"])
    assert created["plugins"][0]["name"] == "session_commands"
    assert created["plugins"][0]["capabilities"]["commands"]["about"] is True
    assert final_name == "platform-assistant"
    assert renamed["description"] == "平台会话"
    assert disabled["enabled"] is False
    assert service.get("assistant") is None

    reloaded = EdictumConfigManager(registry, storage_path)
    assert reloaded.get_config("platform-assistant") is not None
    assert service.delete("platform-assistant") is True
    assert service.list_configs() == {}


def test_edictum_cold_config_rejects_unknown_type_and_fields(tmp_path: Path) -> None:
    """Edictum 冷配置应拒绝未知类型和意义不明的请求字段"""
    registry = EdictumTypeRegistry()
    manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    service = EdictumConfigService(manager, registry)

    with pytest.raises(ValueError, match="未知 Edictum 类型"):
        service.create({"name": "bad", "edictum_type": "missing"})
    with pytest.raises(ValueError, match="未知 Edictum 配置字段"):
        service.create({"name": "bad", "edictum_type": "missing", "mystery": 1})
