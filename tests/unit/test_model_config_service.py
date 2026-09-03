"""模型配置共享领域服务测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.model_service import ModelConfigService
from satrap.core.type import LLMConfig


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
            "context_window": 64000,
            "history_ratio": 0.75,
            "context_strategy": "summarize",
            "context_threshold": 0.85,
            "truncation_floor": 0.35,
            "summary_keep_recent_turns": 4,
        },
    )
    listed = service.list_configs("llm")

    assert listed["demo"]["model"] == "gpt-demo"
    assert listed["demo"]["api_key"] != "secret-key"
    assert str(listed["demo"]["api_key"]).endswith("-key")
    assert listed["demo"]["thinking_fields"] == ["thinking.type", "reasoning_effort"]
    assert listed["demo"]["thinking_levels"] == ["low", "high", "xhigh"]
    assert listed["demo"]["omit_none_thinking_fields"] is True
    assert listed["demo"]["context_strategy"] == "summarize"
    assert listed["demo"]["context_threshold"] == 0.85
    assert listed["demo"]["truncation_floor"] == 0.35
    assert listed["demo"]["summary_keep_recent_turns"] == 4

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
    with pytest.raises(ValueError, match="context_strategy"):
        service.create("llm", "demo", {"context_strategy": "unknown"})
    with pytest.raises(ValueError, match="truncation_floor"):
        service.create(
            "llm",
            "demo",
            {"context_threshold": 0.4, "truncation_floor": 0.8},
        )
    with pytest.raises(ValueError, match="summary_keep_recent_turns"):
        service.create("llm", "demo", {"summary_keep_recent_turns": -1})
    with pytest.raises(ValueError, match="context_threshold"):
        service.create("llm", "demo", {"context_threshold": "0.8"})
    with pytest.raises(ValueError, match="context_window"):
        service.create("llm", "demo", {"context_window": True})


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


def test_masking_does_not_depend_on_lock_api_key(tmp_path: Path) -> None:
    """
    展示接口即使允许运行时更新 API Key 也必须始终脱敏

    参数:
    - tmp_path: 临时目录
    """
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("llm", "unlocked", {
        "model": "demo",
        "api_key": "unlocked-secret-key",
        "lock_api_key": False,
    })

    listed = service.list_configs("llm")
    assert listed["unlocked"]["api_key"] != "unlocked-secret-key"
    assert listed["unlocked"]["api_key"].endswith("-key")


def test_dump_uses_storage_key_as_config_name(tmp_path: Path):
    """
    序列化输出的 name 字段恒为存储键 (写入时已强制对齐)

    参数:
    - tmp_path: 临时目录
    """
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    manager.set_llm_config(LLMConfig(name="original", model="gpt-demo"), name="alias")

    listed = manager.list_llm_configs(mask_api_key=False)

    assert listed["alias"]["name"] == "alias"
