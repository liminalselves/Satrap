"""模型配置共享领域服务测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.model_service import ModelConfigService


def test_model_config_service_crud_and_masking(tmp_path: Path):
    """
    共享领域服务应完成增删改查并对展示密钥脱敏

    参数:
    - tmp_path: 临时目录
    """
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)

    service.create(
        "llm",
        "demo",
        {
            "model": "gpt-demo",
            "api_key": "secret-key",
            "temperature": 0.5,
            "thinking_fields": ["thinking.type", "reasoning_effort"],
            "thinking_levels": ["low", "high", "xhigh"],
            "omit_none_thinking_fields": True,
        },
    )
    listed = service.list_configs("llm")

    assert listed["demo"]["model"] == "gpt-demo"
    assert listed["demo"]["api_key"] != "secret-key"
    assert str(listed["demo"]["api_key"]).endswith("-key")
    assert listed["demo"]["thinking_fields"] == ["thinking.type", "reasoning_effort"]
    assert listed["demo"]["thinking_levels"] == ["low", "high", "xhigh"]
    assert listed["demo"]["omit_none_thinking_fields"] is True

    service.update(
        "llm",
        "demo",
        {"name": "renamed", "temperature": 0.2, "api_key": listed["demo"]["api_key"]},
    )

    runtime = manager.get_llm_config("renamed")
    assert runtime.temperature == 0.2
    assert runtime.api_key == "secret-key"
    assert manager.has_config("llm", "demo") is False
    assert service.delete("llm", "renamed") is True
    assert manager.has_config("llm", "renamed") is False


def test_model_config_service_rejects_invalid_input(tmp_path: Path):
    """
    共享领域服务应拒绝未知类型, 字段和脱敏创建密钥

    参数:
    - tmp_path: 临时目录
    """
    service = ModelConfigService(ModelConfigManager(storage_path=tmp_path / "models.json"))

    with pytest.raises(ValueError, match="未知模型类型"):
        service.list_configs("unknown")
    with pytest.raises(ValueError, match="未知模型配置字段"):
        service.create("llm", "demo", {"unknown": True})
    with pytest.raises(ValueError, match="未知模型配置字段"):
        service.create("llm", "demo", {"reasoning_body": {}})
    with pytest.raises(ValueError, match="不支持的思考强度"):
        service.create("llm", "demo", {"thinking_levels": ["extreme"]})
    with pytest.raises(ValueError, match="脱敏"):
        service.create("llm", "demo", {"api_key": "*****abcd"})


def test_model_config_service_rejects_rename_collision(tmp_path: Path):
    """
    共享领域服务应拒绝重命名为已有配置

    参数:
    - tmp_path: 临时目录
    """
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("embedding", "first", {"model": "first-model"})
    service.create("embedding", "second", {"model": "second-model"})

    with pytest.raises(ValueError, match="名称已存在"):
        service.update("embedding", "first", {"name": "second"})

    assert manager.get_embedding_config("first").model == "first-model"
    assert manager.get_embedding_config("second").model == "second-model"
