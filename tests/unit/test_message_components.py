import base64
import os

import pytest

from satrap.core.components import (
    At,
    AtAll,
    ComponentTypes,
    Dice,
    Face,
    File,
    Forward,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Poke,
    Record,
    Reply,
    RPS,
    Shake,
    Unknown,
    Video,
    file_token_service,
    set_callback_api_base,
)


def test_core_components_can_serialize():
    components = [
        Plain("hello"),
        Face(id=1),
        At(qq=123),
        AtAll(),
        Reply(id="msg-1"),
        Poke(id=456),
        Forward(id="forward-1"),
        Json({"ok": True}),
        Unknown(text="?"),
        RPS(),
        Dice(),
        Shake(),
    ]

    assert components[0].toDict() == {"type": "text", "data": {"text": "hello"}}
    for comp in components[1:]:
        assert isinstance(comp.toDict(), dict)


def test_special_serializers():
    assert At(qq=123).toDict() == {"type": "at", "data": {"qq": "123"}}
    assert AtAll().toDict() == {"type": "at", "data": {"qq": "all"}}
    assert Poke(id="42").toDict() == {"type": "poke", "data": {"type": "126", "id": "42"}}
    assert Json('{"a": 1}').data == {"a": 1}


@pytest.mark.asyncio
async def test_node_and_nodes_to_dict():
    node = Node(content=[Plain("hello")], name="tester", uin="10001")
    node_dict = await node.to_dict()

    assert node_dict == {
        "type": "node",
        "data": {
            "user_id": "10001",
            "nickname": "tester",
            "content": [{"type": "text", "data": {"text": "hello"}}],
        },
    }
    assert await Nodes([node]).to_dict() == {"messages": [node_dict]}


@pytest.mark.asyncio
async def test_image_and_record_base64_file_conversion(tmp_path):
    raw = b"satrap"
    encoded = base64.b64encode(raw).decode("utf-8")

    image = Image.fromBase64(encoded)
    image_path = await image.convert_to_file_path()
    try:
        assert os.path.exists(image_path)
        assert await image.convert_to_base64() == encoded
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

    record_path = tmp_path / "record.bin"
    record_path.write_bytes(raw)
    record = Record.fromFileSystem(str(record_path))
    assert await record.convert_to_base64() == encoded
    assert await record.convert_to_file_path() == os.path.abspath(record_path)


@pytest.mark.asyncio
async def test_file_get_file_local_and_async_guard(tmp_path):
    path = tmp_path / "demo.txt"
    path.write_text("hello", encoding="utf-8")

    file_seg = File(name="demo.txt", file=str(path))
    assert file_seg.file == os.path.abspath(path)
    assert await file_seg.get_file() == os.path.abspath(path)

    url_file = File(name="demo.txt", url="https://example.com/demo.txt")
    assert await url_file.get_file(allow_return_url=True) == "https://example.com/demo.txt"
    assert url_file.file == ""


@pytest.mark.asyncio
async def test_file_register_to_file_service(tmp_path):
    path = tmp_path / "demo.txt"
    path.write_text("hello", encoding="utf-8")
    set_callback_api_base("https://callback.example/")
    try:
        url = await File(name="demo.txt", file=str(path)).register_to_file_service()
        token = url.rsplit("/", 1)[-1]
        assert url.startswith("https://callback.example/api/file/")
        assert file_token_service.get_file(token) == os.path.abspath(path)
    finally:
        set_callback_api_base("")


@pytest.mark.asyncio
async def test_video_and_file_to_dict_without_callback(tmp_path):
    path = tmp_path / "video.bin"
    path.write_bytes(b"video")

    assert await Video.fromFileSystem(str(path)).to_dict() == {
        "type": "video",
        "data": {"file": f"file:///{os.path.abspath(path)}"},
    }
    assert await File(name="video.bin", file=str(path)).to_dict() == {
        "type": "file",
        "data": {"name": "video.bin", "file": os.path.abspath(path)},
    }
    assert File(name="video.bin", file=str(path)).toDict() == {
        "type": "file",
        "data": {"name": "video.bin", "file": str(path)},
    }


def test_component_types_mapping_contains_astrbot_keys():
    for key in [
        "plain",
        "text",
        "image",
        "record",
        "video",
        "file",
        "face",
        "at",
        "rps",
        "dice",
        "shake",
        "share",
        "contact",
        "location",
        "music",
        "reply",
        "poke",
        "forward",
        "node",
        "nodes",
        "json",
        "unknown",
    ]:
        assert key in ComponentTypes
