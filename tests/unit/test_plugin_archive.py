"""插件 ZIP 安装的边界与临时文件清理测试"""
import io
from pathlib import Path
import stat
import zipfile

import pytest

from satrap.edictum import plugin_archive
from satrap.edictum.plugin_archive import PluginArchiveInstaller
from satrap.edictum.plugin_catalog import PluginCatalog


def archive_bytes(files):
    """
    构造内存 ZIP 测试文件

    参数:
    - files: 文件名或 ZipInfo 与内容组成的序列

    返回:
    - ZIP 字节内容
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files:
            if isinstance(name, str):
                info = zipfile.ZipInfo("placeholder")
                info.filename = name
                info.orig_filename = name
                info.compress_type = zipfile.ZIP_DEFLATED
                name = info
            archive.writestr(name, value)
    return buffer.getvalue()


@pytest.fixture
def installer(tmp_path):
    return PluginArchiveInstaller(PluginCatalog(tmp_path / "builtin", tmp_path / "data" / "plugins"))


@pytest.mark.parametrize("prefix", ["", "package/"])
def test_preview_install_without_execution_or_enable(installer, prefix):
    content = archive_bytes([(prefix + "meta.yaml", "name: probe\nversion: '1'\ntools:\n  probe_tool: 测试"), (prefix + "tools.py", "raise RuntimeError('不得执行')")])
    result = installer.preview(content)
    assert result["plugin"]["capabilities"]["tools"] == {"probe_tool": "测试"}
    assert not installer.catalog.user_dir.exists()
    installed = installer.install(result["token"])
    assert installed["plugin"]["name"] == "probe"
    assert (installer.catalog.user_dir / "probe" / "tools.py").is_file()
    assert not (installer.catalog.user_dir.parent / "chat_plugins.json").exists()
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())
    with pytest.raises(ValueError, match="过期或已使用"):
        installer.install(result["token"])


@pytest.mark.parametrize("path", ["../escape", "/escape", "C:/escape", "x\\escape", "CON.txt", "a/../escape", "a//escape", "x.", "LPT¹", "name:stream"])
def test_unsafe_paths_rejected_and_cleaned(installer, path):
    with pytest.raises(ValueError, match="非法路径"):
        installer.preview(archive_bytes([("meta.yaml", "name: probe"), (path, "payload")]))
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())


@pytest.mark.parametrize("files", [
    [("a/meta.yaml", "name: a"), ("b/meta.yaml", "name: b")],
    [("a/b/meta.yaml", "name: probe")],
    [("a/meta.yaml", "name: probe"), ("outside.txt", "x")],
    [("meta.yaml", "name: ../escape")],
    [("meta.yaml", "name: [broken")],
    [("meta.yaml", "name: true")],
    [("meta.yaml", "name: ' padded '")],
    [("meta.yaml", "name: probe"), ("Tools.py", "x"), ("tools.py", "x")],
    [("meta.yaml", "name: probe"), ("A/x", "x"), ("a/y", "x")],
])
def test_invalid_layout_manifest_and_case_conflict(installer, files):
    with pytest.raises(ValueError):
        installer.preview(archive_bytes(files))
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())


def test_link_rejected(installer):
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="链接或特殊文件"):
        installer.preview(archive_bytes([("meta.yaml", "name: probe"), (link, "../outside")]))


@pytest.mark.parametrize("source", ["builtin", "user"])
def test_existing_plugin_preserved(installer, source):
    base = installer.catalog.preset_dir if source == "builtin" else installer.catalog.user_dir
    existing = base / "existing"
    existing.mkdir(parents=True)
    (existing / "meta.yaml").write_text("name: probe\nversion: original", encoding="utf-8")
    with pytest.raises(ValueError, match="同名插件"):
        installer.preview(archive_bytes([("meta.yaml", "name: probe")]))
    assert "original" in (existing / "meta.yaml").read_text(encoding="utf-8")


def test_conflict_created_after_preview_cleans_staging(installer):
    result = installer.preview(archive_bytes([("meta.yaml", "name: probe")]))
    target = installer.catalog.user_dir / "probe"
    target.mkdir(parents=True)
    (target / "keep").write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="同名插件"):
        installer.install(result["token"])
    assert (target / "keep").read_text(encoding="utf-8") == "original"
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())


def test_limits_cancellation_and_expiry(installer, monkeypatch):
    content = archive_bytes([("meta.yaml", "name: probe")])
    monkeypatch.setattr(plugin_archive, "MAX_ARCHIVE_FILES", 1)
    with pytest.raises(ValueError, match="条目数量"):
        installer.preview(archive_bytes([("meta.yaml", "name: probe"), ("extra", "x")]))
    monkeypatch.setattr(plugin_archive, "MAX_EXPANDED_BYTES", 5)
    with pytest.raises(ValueError, match="大小"):
        installer.preview(content)
    monkeypatch.setattr(plugin_archive, "MAX_EXPANDED_BYTES", 1000)
    preview = installer.preview(content)
    installer.discard(preview["token"])
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())
    preview = installer.preview(content)
    monkeypatch.setattr(plugin_archive.time, "monotonic", lambda: float("inf"))
    with pytest.raises(ValueError, match="过期"):
        installer.install(preview["token"])
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())


def test_corrupt_archive_and_move_failure_cleanup(installer, monkeypatch):
    with pytest.raises(zipfile.BadZipFile):
        installer.preview(b"invalid zip")
    preview = installer.preview(archive_bytes([("meta.yaml", "name: probe")]))
    def fail_move(*args):
        raise OSError("测试移动失败")
    monkeypatch.setattr(plugin_archive, "_move_no_replace", fail_move)
    with pytest.raises(OSError, match="测试移动失败"):
        installer.install(preview["token"])
    assert not (installer.catalog.user_dir / "probe").exists()
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())


def test_atomic_move_refuses_existing_empty_directory(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "keep").write_text("source", encoding="utf-8")
    with pytest.raises(OSError):
        plugin_archive._move_no_replace(source, target)
    assert (source / "keep").is_file()
    assert target.is_dir()
    assert not list(target.iterdir())


def test_nested_private_metadata_is_not_a_second_plugin(installer):
    result = installer.preview(archive_bytes([("package/meta.yaml", "name: probe"), ("package/resources/private/meta.yaml", "resource: example")]))
    installer.install(result["token"])
    assert (installer.catalog.user_dir / "probe" / "resources" / "private" / "meta.yaml").is_file()


def test_invalid_existing_metadata_does_not_break_other_installations(installer):
    broken = installer.catalog.preset_dir / "broken"
    broken.mkdir(parents=True)
    (broken / "meta.yaml").write_text("name: [invalid", encoding="utf-8")
    result = installer.preview(archive_bytes([("meta.yaml", "name: probe")]))
    installer.discard(result["token"])
    with pytest.raises(ValueError, match="同名插件"):
        installer.preview(archive_bytes([("meta.yaml", "name: broken")]))
