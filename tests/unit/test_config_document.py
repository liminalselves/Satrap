from pathlib import Path
import subprocess
import sys
import pytest
from typing import Any

from satrap.core.config.document import (
    ConfigRevisionConflict,
    config_document_revision,
    create_default_config,
    delete_platform,
    load_config_document,
    save_config_document,
    upsert_platform,
)


def test_stale_config_revision_preserves_latest_document(tmp_path: Path):
    """过期写入必须保留最新文档, 且错误不回显配置凭据"""
    path = tmp_path / "config.json"
    original = save_config_document(path, {"platforms": []})
    revision = config_document_revision(original)
    latest = save_config_document(path, {"platforms": [], "secret": "test-secret"}, expected_revision=revision)
    with pytest.raises(ConfigRevisionConflict) as error:
        save_config_document(path, original, expected_revision=revision)
    assert "test-secret" not in str(error.value)
    assert load_config_document(path) == latest
    assert config_document_revision({"a": 1, "b": 2}) == config_document_revision({"b": 2, "a": 1})


def test_config_revision_serializes_independent_processes(tmp_path: Path):
    """相同修订的跨进程保存只能有一个成功"""
    path = tmp_path / "config.json"
    revision = config_document_revision(save_config_document(path, {"platforms": []}))
    script = """
import sys
from satrap.core.config.document import ConfigRevisionConflict, save_config_document
try:
    save_config_document(sys.argv[1], {"platforms": [], "writer": sys.argv[3]}, expected_revision=sys.argv[2])
except ConfigRevisionConflict:
    print("conflict")
else:
    print("saved")
"""
    processes = [subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", script, str(path), revision, str(index)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    ) for index in range(2)]
    try:
        outputs = [process.communicate(timeout=30) for process in processes]
        assert all(process.returncode == 0 for process in processes), outputs
        assert sorted(output.strip() for output, _ in outputs) == ["conflict", "saved"]
        assert load_config_document(path)["writer"] in {"0", "1"}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_save_config_document_validates_and_replaces_atomically(tmp_path: Path):
    """
    配置保存应规范化平台并且不残留临时文件

    参数:
    - tmp_path: 临时目录
    """
    path = tmp_path / "config.yaml"
    config_data: dict[str, Any] = {
        "platforms": [
            {
                "id": " demo ",
                "type": " misskey ",
                "session_type": " assistant ",
                "settings": {},
            }
        ],
    }
    saved = save_config_document(
        path,
        config_data,
    )

    assert saved["platforms"][0]["id"] == "demo"
    assert saved["platforms"][0]["session_provider"] == "session_class"
    assert saved["platforms"][0]["session_type"] == "assistant"
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


def test_replace_with_retry_retries_transient_busy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """EACCES/EPERM/EBUSY 类瞬时占用会重试直至成功"""
    import errno
    import os
    from satrap.core.config import document

    attempts: list[int] = []
    real_replace = os.replace

    def flaky(source: str, target: str) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError(errno.EBUSY, "busy")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    source = tmp_path / "a.tmp"
    target = tmp_path / "b.json"
    source.write_text("x", encoding="utf-8")
    document._replace_with_retry(source, target, attempts=5, interval=0)
    assert target.read_text(encoding="utf-8") == "x" and len(attempts) == 3


def test_replace_with_retry_does_not_retry_other_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """非占用类错误立即抛出, 不做无谓重试"""
    import errno
    import os
    from satrap.core.config import document

    def always_missing(source: str, target: str) -> None:
        raise OSError(errno.ENOENT, "missing")

    monkeypatch.setattr(os, "replace", always_missing)
    with pytest.raises(OSError):
        document._replace_with_retry(tmp_path / "a.tmp", tmp_path / "b.json", attempts=5, interval=0)
