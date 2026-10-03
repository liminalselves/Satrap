"""逐群配置资源引用扫描的保守语义"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.config.group_references import GroupReferenceScanError, list_group_references
from satrap.core.config.group_store import GroupConfigStore
from satrap.core.storage import StorageLayout
from satrap.core.framework.BackGroundManager import ConfigInUseError, ModelConfigManager
from satrap.core.type import LLMConfig
from satrap.core.config.session_class_service import SessionClassConfigService
from satrap.core.config.edictum_service import EdictumConfigService


def test_group_binding_and_model_references(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path)
    store = GroupConfigStore(layout.platform_db("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.patch_group("100", "123", "session", {
        "binding": {"mode": "value", "value": {"provider": "edictum", "config_name": "simple"}},
        "model": {"mode": "value", "value": "chat"},
    }, expected_revision=0)
    binding = list_group_references("edictum", "simple", layout=layout, platform_ids=["bot"])
    model = list_group_references("llm", "chat", layout=layout, platform_ids=["bot"])
    assert len(binding) == 1 and binding[0]["kind"] == "group_binding"
    assert len(model) == 1 and model[0]["kind"] == "group_model"
    assert list_group_references("llm", "other", layout=layout, platform_ids=["bot"]) == []


def test_group_reference_scan_rejects_corrupt_json(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path)
    store = GroupConfigStore(layout.platform_db("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.patch_group("100", "123", "session", {
        "model": {"mode": "value", "value": "chat"},
    }, expected_revision=0)
    with sqlite3.connect(layout.platform_db("bot")) as connection:
        connection.execute("UPDATE group_configs SET config_json='invalid' WHERE group_id='123'")
    with pytest.raises(GroupReferenceScanError, match="无法解析"):
        list_group_references("llm", "chat", layout=layout, platform_ids=["bot"])


def test_group_model_reference_blocks_delete_and_rename(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path)
    store = GroupConfigStore(layout.platform_db("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.patch_group("100", "123", "session", {
        "model": {"mode": "value", "value": "chat"},
    }, expected_revision=0)
    manager = ModelConfigManager(
        storage_path=tmp_path / "models.json",
        llm_in_use_checker=lambda name: list_group_references(
            "llm", name, layout=layout, platform_ids=["bot"],
        ),
    )
    manager.set_llm_config(LLMConfig(name="chat", model="example", api_key="test"), name="chat")
    with pytest.raises(ConfigInUseError):
        manager.remove_llm_config("chat")
    with pytest.raises(ConfigInUseError):
        manager.update_named_config("llm", "chat", {}, new_name="new")
    assert manager.get_llm_config("chat").model == "example"


def test_named_session_services_refuse_bound_delete() -> None:
    reference = lambda _: [{"summary": "群 123 的会话绑定", "kind": "group_binding"}]
    cls = SessionClassConfigService(cast(Any, SimpleNamespace(remove_config=lambda _: True)), reference)
    with pytest.raises(ConfigInUseError):
        cls.delete("assistant")
    edictum = EdictumConfigService(
        cast(Any, SimpleNamespace(delete=lambda _: True)), cast(Any, SimpleNamespace()),
        reference_checker=reference,
    )
    with pytest.raises(ConfigInUseError):
        edictum.delete("assistant")
