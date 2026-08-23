from pathlib import Path
from typing import Any

import pytest

from satrap.core.config_document import (
    create_default_config,
    delete_platform,
    load_config_document,
    save_config_document,
    upsert_platform,
)


def test_save_config_document_validates_and_replaces_atomically(tmp_path: Path):
    """
    配置保存应规范化平台并且不残留临时文件

    参数:
    - tmp_path: 临时目录
    """
    path = tmp_path / "config.yaml"
    config_data: dict[str, Any] = {
        "platforms": [{"id": " demo ", "type": " misskey ", "settings": {}}],
    }
    saved = save_config_document(
        path,
        config_data,
    )

    assert saved["platforms"][0]["id"] == "demo"
    assert load_config_document(path) == saved
    assert list(tmp_path.glob(".*.tmp")) == []


def test_default_config_does_not_overwrite_existing_file(tmp_path: Path):
    """
    默认配置创建不应覆盖已有配置

    参数:
    - tmp_path: 临时目录
    """
    path = tmp_path / "config.yaml"
    save_config_document(path, {"api": {"port": 19999}, "platforms": []})

    result = create_default_config(path)

    assert result["api"]["port"] == 19999


def test_platform_crud_rejects_duplicates_and_missing_targets():
    """平台 CRUD 应拒绝重复 id 和不存在的更新或删除目标"""
    platforms = upsert_platform([], {"id": "main", "type": "misskey", "settings": {}})

    with pytest.raises(ValueError, match="已存在"):
        upsert_platform(platforms, {"id": "main", "type": "onebot", "settings": {}})
    with pytest.raises(ValueError, match="不存在"):
        upsert_platform(
            platforms,
            {"id": "renamed", "type": "misskey", "settings": {}},
            original_id="missing",
        )
    with pytest.raises(ValueError, match="不存在"):
        delete_platform(platforms, "missing")


def test_platform_update_preserves_list_position():
    """平台更新应保持原列表顺序"""
    platforms: list[dict[str, Any]] = [
        {"id": "first", "type": "misskey", "settings": {}},
        {"id": "second", "type": "onebot", "settings": {}},
    ]

    updated = upsert_platform(
        platforms,
        {"id": "first", "type": "misskey", "settings": {"base_url": "https://example.test"}},
        original_id="first",
    )

    assert [item["id"] for item in updated] == ["first", "second"]
    assert updated[0]["settings"]["base_url"] == "https://example.test"
