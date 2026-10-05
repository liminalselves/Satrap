"""媒体来源边界的路径, URI 与平台调用链回归"""
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace
import asyncio
import base64

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.components import File, Image, Node, Nodes, Video
from satrap.core.components.message import get_satrap_temp_path
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.onebot.onebot_utils import _normalize_file_source
from satrap.core.platform.misskey.adapter import MisskeyAdapter
from satrap.core.platform.misskey.misskey_utils import resolve_component_url_or_path
from satrap.core.platform.event import MessageChain
from satrap.core.platform import PlatformConfig
from satrap.core.storage import StorageLayout
from satrap.core.utils import paths


@pytest.fixture
def media_policy(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(paths, "_configured_media_roots", (str(allowed),))
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    return allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["picture#1.png", "picture%20name.png", "中文 图片.png"])
async def test_standard_uri_round_trip(media_policy, filename):
    picture = media_policy / filename
    picture.write_bytes(b"png")
    image = Image.fromFileSystem(str(picture))
    assert image.file == picture.as_uri()
    assert paths.file_uri_to_path(image.file) == str(picture).replace("\\", "/")
    assert await image.convert_to_file_path() == str(picture.resolve())
    assert await image.convert_to_base64() == base64.b64encode(b"png").decode()
    assert _normalize_file_source(str(picture)) == picture.as_uri()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "file_url", "missing", "video_forward", "uppercase", "single_slash"])
async def test_onebot_denies_before_platform_call(tmp_path, media_policy, kind):
    outside = tmp_path / "outside.png"
    source = outside.as_uri()
    component = File("outside", file=source)
    if kind == "file_url":
        component = File("outside", url=source)
    elif kind == "missing":
        component = File("outside", file=str(outside))
    elif kind == "video_forward":
        component = Nodes([Node(content=[Video(source)])])
    elif kind == "uppercase":
        component = Image(file="FILE:" + source[5:])
    elif kind == "single_slash":
        component = Image(file=source.replace("file:///", "file:/", 1))
    adapter = OneBotAdapter(PlatformConfig(id="test", type="onebot", settings={"host": "127.0.0.1"}))
    adapter._bot = AsyncMock()
    adapter._dispatch_action = AsyncMock()
    receipt = await adapter.send_message("group%456", MessageChain([component]))
    assert receipt.status == "failed" and receipt.reason == "media_source_denied"
    adapter._dispatch_action.assert_not_awaited()
    assert adapter._bot.method_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "file", "file_url", "duck"])
async def test_misskey_denies_before_upload(tmp_path, media_policy, kind):
    source = str(tmp_path / "outside.png")
    components = {
        "image": Image(file=source),
        "file": File("outside", file=source),
        "file_url": File("outside", url=Path(source).as_uri()),
        "duck": SimpleNamespace(source=source),
    }
    adapter = MisskeyAdapter(PlatformConfig(id="test", type="misskey", settings={
        "misskey_instance_url": "https://misskey.example", "misskey_token": "token",
    }))
    adapter._client = AsyncMock()
    with pytest.raises(paths.MediaSourcePermissionError):
        await adapter._upload_component(components[kind], asyncio.Semaphore(1))
    assert adapter._client.method_calls == []


@pytest.mark.asyncio
async def test_misskey_keeps_remote_and_converts_inline(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    assert await resolve_component_url_or_path(Image(file="HTTPS://example.com/a.png")) == (
        "https://example.com/a.png", None,
    )
    assert Image.fromURL("HTTPS://example.com/a.png").file == "https://example.com/a.png"
    image = Image.fromBase64(base64.b64encode(b"png").decode())
    remote, local = await resolve_component_url_or_path(image)
    assert remote is None and Path(local).read_bytes() == b"png"


@pytest.mark.parametrize("relative", [
    "platforms/p/cache/temp/a.png",
    "platforms/p/sessions/s/uploads/a.png",
    "platforms/p/sessions/s/artifacts/a.png",
    "platforms/p/sessions/s/sandbox/a.png",
    "platforms/p/sessions/s/cache/a.png",
])
def test_default_layout_media_only(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    assert paths.ensure_allowed_media_path(str(tmp_path / relative)) == str((tmp_path / relative).resolve())


@pytest.mark.parametrize("relative", [
    "platforms/p/trash/sessions/old/uploads/private.png",
    "platforms/p/users/u/cache/private.png",
    "platforms/p/projects/a/uploads/private.png",
    "platforms/p/sessions/s/indexes/cache/private.png",
    "platforms/p/platform.db",
])
def test_default_layout_denies_named_ancestors(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path)
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(tmp_path / relative))


def test_default_policy_denies_other_app_temp(tmp_path, monkeypatch):
    import tempfile
    other_app_file = Path(tempfile.gettempdir()) / "satrap-policy-probe-other-app" / "private.tmp"
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    assert paths.get_allowed_media_roots() == [(paths.get_data_dir() / "sandbox").resolve()]
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(other_app_file))
    sandbox = paths.get_data_dir() / "sandbox" / "picture.png"
    assert paths.ensure_allowed_media_path(str(sandbox)) == str(sandbox.resolve())


@pytest.mark.parametrize("mode", ["configured", "environment"])
def test_temp_subdirectory_requires_explicit_authorization(tmp_path, monkeypatch, mode):
    allowed = tmp_path / "chosen-media"
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    if mode == "configured":
        paths.set_media_allowed_roots([str(allowed)])
    else:
        monkeypatch.setenv("SATRAP_EXTRA_MEDIA_ROOTS", str(allowed))
    assert paths.ensure_allowed_media_path(str(allowed / "a.png")) == str((allowed / "a.png").resolve())
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(tmp_path / "unselected" / "private.tmp"))


def test_explicit_roots_preserve_semicolon_and_replace_temp(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed;media"
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    paths.set_media_allowed_roots([str(allowed)])
    assert paths.get_allowed_media_roots() == [allowed.resolve()]
    assert paths.ensure_allowed_media_path(str(allowed / "a.png")) == str((allowed / "a.png").resolve())
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(tmp_path / "other.png"))


def test_backend_root_controls_generated_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", None)
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    backend = BackendManager(BackendConfig(data_root=str(tmp_path / "custom-data")))
    expected = backend.storage_layout.platform_cache("local") / "temp"
    assert Path(get_satrap_temp_path()) == expected
    assert paths.ensure_allowed_media_path(str(expected / "a.png")) == str(expected / "a.png")
    old = StorageLayout().platform_cache("local") / "temp" / "a.png"
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.ensure_allowed_media_path(str(old))


@pytest.mark.parametrize("value", ["D:/media", [1], {"path": "media"}, False])
def test_backend_rejects_invalid_media_config(value):
    with pytest.raises(ValueError, match="media_allowed_roots"):
        BackendConfig.from_dict({"media_allowed_roots": value})


@pytest.mark.parametrize("source", ["file:relative", "file:///a#b", "file:///a?b", "file:///a%00b"])
def test_invalid_file_uri_rejected(source):
    with pytest.raises(paths.MediaSourcePermissionError):
        paths.file_uri_to_path(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["file:///tmp/%FF.png", "file:///tmp/%E4.png", "file://%FF/share/a.png"])
async def test_invalid_utf8_uri_is_media_denial(source):
    with pytest.raises(paths.MediaSourcePermissionError) as caught:
        paths.file_uri_to_path(source)
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)
    adapter = OneBotAdapter(PlatformConfig(id="test", type="onebot", settings={"host": "127.0.0.1"}))
    adapter._bot = AsyncMock()
    adapter._dispatch_action = AsyncMock()
    receipt = await adapter.send_message("group%456", MessageChain([Image(file=source)]))
    assert receipt.status == "failed" and receipt.reason == "media_source_denied"
    adapter._dispatch_action.assert_not_awaited()
    assert adapter._bot.method_calls == []


@pytest.mark.asyncio
async def test_default_cache_keeps_download_and_inline_media(tmp_path, monkeypatch):
    from satrap.core.components import message
    monkeypatch.setattr(paths, "_configured_media_roots", ())
    monkeypatch.setattr(paths, "_media_storage_root", tmp_path / "data")
    monkeypatch.delenv("SATRAP_EXTRA_MEDIA_ROOTS", raising=False)
    cache = Path(get_satrap_temp_path())

    async def download(url, target):
        Path(target).write_bytes(b"downloaded")
        return target

    monkeypatch.setattr(message, "download_file", download)
    downloaded = Path(await File("a.png", url="https://example.com/a.png").get_file())
    inline = Path(await Image.fromBase64(base64.b64encode(b"inline").decode()).convert_to_file_path())
    assert downloaded.parent == inline.parent == cache
    assert downloaded.read_bytes() == b"downloaded"
    assert inline.read_bytes() == b"inline"


@pytest.mark.asyncio
async def test_file_preserves_url_priority_and_missing_local_fallback(media_policy, monkeypatch):
    from satrap.core.components import message
    existing = media_policy / "existing.txt"
    existing.write_text("local", encoding="utf-8")
    component = File("file", file=str(existing), url="https://example.com/a.txt")
    assert await component.get_file(True) == "https://example.com/a.txt"
    assert await component.get_file() == str(existing)
    assert component.file == str(existing)
    missing = media_policy / "missing.txt"
    component.file_ = str(missing)
    monkeypatch.setattr(message, "get_satrap_temp_path", lambda: str(media_policy))
    download = AsyncMock(return_value=str(existing))
    monkeypatch.setattr(message, "download_file", download)
    assert await component.get_file() == str(existing)
    assert download.await_args.args[0] == "https://example.com/a.txt"


@pytest.mark.parametrize("denied", [False, True])
def test_sync_file_keeps_download_failure_contract(media_policy, monkeypatch, denied):
    from satrap.core.components import message
    error = paths.MediaSourcePermissionError("denied") if denied else OSError("download failed")
    monkeypatch.setattr(message, "get_satrap_temp_path", lambda: str(media_policy))
    monkeypatch.setattr(message, "download_file", AsyncMock(side_effect=error))
    component = File("file", url="https://example.com/a.txt")
    if denied:
        with pytest.raises(paths.MediaSourcePermissionError):
            component.file
    else:
        assert component.file == ""


@pytest.mark.asyncio
async def test_allowed_onebot_paths_reach_platform_even_if_remote_only(media_policy):
    adapter = OneBotAdapter(PlatformConfig(id="test", type="onebot", settings={"host": "127.0.0.1"}))
    adapter._bot = AsyncMock()
    adapter._dispatch_action = AsyncMock(return_value={"file_id": "f1"})
    local = media_policy / "remote-only.txt"
    assert not local.exists()
    result = await adapter.send_message("group%456", MessageChain([File("file", file=str(local))]))
    assert result.status == "success"
    assert adapter._dispatch_action.await_args.kwargs["file"] == str(local)


@pytest.mark.asyncio
async def test_misskey_duck_get_file_retains_contract(media_policy):
    getter = AsyncMock(return_value="https://example.com/a.png")
    assert await resolve_component_url_or_path(SimpleNamespace(get_file=getter)) == (
        "https://example.com/a.png", None,
    )
    getter.assert_awaited_once_with(True)
    denied = SimpleNamespace(get_file=AsyncMock(side_effect=paths.MediaSourcePermissionError("denied")), url="https://example.com/a.png")
    with pytest.raises(paths.MediaSourcePermissionError):
        await resolve_component_url_or_path(denied)


def test_file_authority_platform_contract(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "posix")
    with pytest.raises(paths.MediaSourcePermissionError):
        _normalize_file_source("file://server/share/a.png")
    assert paths.file_uri_to_path("FILE:/var/tmp/a%20b.png") == "/var/tmp/a b.png"
    monkeypatch.setattr(paths.os, "name", "nt")
    assert paths.file_uri_to_path("file://server/share/a.png") == r"\\server\share\a.png"
    assert paths.file_uri_to_path("file://localhost/C:/a.png") == "C:/a.png"
