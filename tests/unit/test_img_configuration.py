"""图片模型配置的存储, 客户端, 引用保护和接口契约"""
from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from satrap import AsyncImageGen, ImageGen, NovelAIParams
from satrap.core.APICall.ImageCall import build_imagegen_from_config
from satrap.core.config.img_references import ImgReferenceScanError, list_img_config_references
from satrap.core.config.model_service import ModelConfigService
from satrap.core.config.model_snapshot import ModelConfigSnapshot
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.framework.BackGroundManager import ConfigInUseError, ConfigReferenceScanError, ModelConfigManager
from satrap.core.storage import StorageLayout
from satrap.core.type import ImgConfig, LLMConfig
from satrap.edictum.plugin_config import ConfigField
from satrap.edictum.plugin_resources import PluginResources, build_model_client, named_model_config
from satrap.edictum.plugin_settings import model_options


def payload() -> dict:
    return {"model": "nai-diffusion-4-5-full", "api_type": "novelai", "api_key": "private-img-key",
            "base_url": "https://image.test/exact/generate/?version=1", "edit_base_url": "https://image.test/exact/edit",
            "default_params": {"size": [512, 768], "seed": 0, "steps": 20,
                               "characters": [{"prompt": "woman", "centers": [[0.2, 0.5]]}], "native": {"parameters": {"future": True}}}}


def test_novelai_prompt_injection_flags_roundtrip_without_changing_negative_input(tmp_path):
    manager = ModelConfigManager(tmp_path / "models.json")
    config = {**payload(), "default_params": {"negative_prompt": "custom negative", "inject_default_positive_prompt": True,
                                               "inject_default_negative_prompt": False}}
    ModelConfigService(manager).create("img", "paint", config)
    saved = ModelConfigManager(manager.storage_path).get_img_config("paint")
    assert saved.default_params == config["default_params"]
    with build_imagegen_from_config(saved) as client:
        assert client.default_params.inject_default_positive_prompt is True
        assert client.default_params.inject_default_negative_prompt is False
        assert client.default_params.negative_prompt == "custom negative"


def test_img_roundtrip_masking_rename_clear_and_preserve_other_model_edits(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    first = ModelConfigManager(path)
    second = ModelConfigManager(path)
    service = ModelConfigService(first)
    service.create("img", "paint", payload())
    second.set_llm_config(LLMConfig(model="chat"), "chat")
    assert second.get_img_config("paint").default_params == payload()["default_params"]
    assert first.get_llm_config("chat").model == "chat"
    assert service.list_configs("img")["paint"]["api_key"] != "private-img-key"
    assert service.reveal_api_key("img", "paint") == "private-img-key"
    service.update("img", "paint", {"name": "drawing", "api_key": "*********key", "edit_base_url": None, "default_params": None})
    loaded = ModelConfigManager(path).get_img_config("drawing")
    assert loaded.api_key == "private-img-key" and loaded.default_params is None and loaded.edit_base_url is None
    assert not first.has_config("img", "paint")
    assert service.delete("img", "drawing")
    assert not first.has_config("img", "drawing")
    first.reset("img")
    assert first.get_llm_config("chat").model == "chat"


def test_img_factory_nested_parameters_and_independent_runtime_snapshot(tmp_path: Path) -> None:
    manager = ModelConfigManager(tmp_path / "models.json")
    ModelConfigService(manager).create("img", "paint", payload())
    before = manager.storage_path.read_bytes()
    snapshot = ModelConfigSnapshot.read(manager)
    assert manager.storage_path.read_bytes() == before
    config = snapshot.get_img_config("paint")
    with build_imagegen_from_config(config) as client:
        assert isinstance(client, ImageGen) and client.base_url == payload()["base_url"]
        assert client.edit_base_url == payload()["edit_base_url"]
        assert isinstance(client.default_params, NovelAIParams)
        assert client.default_params.size == (512, 768) and client.default_params.seed == 0
        assert client.default_params.characters[0].centers == [(0.2, 0.5)]
    manager.update_img_config("paint", model="new-model")
    assert snapshot.get_img_config("paint").model == "nai-diffusion-4-5-full"
    config.default_params["steps"] = 99
    assert snapshot.get_img_config("paint").default_params["steps"] == 20


@pytest.mark.asyncio
async def test_img_plugin_resources_selector_and_close(tmp_path: Path) -> None:
    manager = ModelConfigManager(tmp_path / "models.json")
    ModelConfigService(manager).create("img", "paint", payload())
    config = named_model_config(manager, "img", "paint")
    assert isinstance(config, ImgConfig)
    assert model_options(manager)["img"][1] == {"value": "paint", "label": "paint", "model": config.model}
    schema = {"painter": ConfigField(name="painter", type="img")}
    resources = PluginResources(manager, schema, {"painter": "paint"}, async_=True)
    client = resources["painter"]
    assert isinstance(client, AsyncImageGen) and not client.suppress_error
    frozen = resources.snapshot_models()
    assert frozen.get_img_config("paint").api_key == "private-img-key"
    await resources.aclose()
    assert client.client.is_closed
    with build_model_client("img", config) as sync:
        assert isinstance(sync, ImageGen) and not sync.suppress_error


@pytest.mark.parametrize("change", [
    {"api_type": "gemini"}, {"timeout": 0}, {"timeout": True}, {"base_url": "/images"},
    {"default_params": {"size": [1]}}, {"default_params": {"steps": 0}},
    {"default_params": {"characters": [{"prompt": "woman", "centers": [[2, 0]]}]}},
    {"default_params": {"native": {"parameters": {"seed": 3}}}},
    {"default_params": {"native": {"parameters": {"image": "base64"}}}},
    {"default_params": {"vibes": []}}, {"default_params": {"precise_references": []}},
    {"default_params": {"steps": float("nan")}}, {"default_params": {"unknown": 1}},
    {"default_params": {"inject_default_positive_prompt": "true"}},
    {"default_params": {"inject_default_negative_prompt": 1}},
])
def test_invalid_img_configuration_is_rejected_before_file_changes(tmp_path: Path, change: dict) -> None:
    manager = ModelConfigManager(tmp_path / "models.json")
    before = manager.storage_path.read_bytes()
    with pytest.raises(ValueError):
        ModelConfigService(manager).create("img", "invalid", {**payload(), **change})
    assert manager.storage_path.read_bytes() == before


def test_img_reference_scanner_covers_global_named_session_and_group(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugin = plugins / "paint"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("name: paint\nversion: 0.1.0\nconfig_schema:\n  painter:\n    type: img\n", encoding="utf-8")
    globals = tmp_path / "globals"
    globals.mkdir()
    (globals / "paint.json").write_text(json.dumps({"painter": "drawing"}), encoding="utf-8")
    named = tmp_path / "agents.json"
    named.write_text(json.dumps({"agent": {"plugins": [{"name": "paint", "config": {"painter": "drawing"}}]}}), encoding="utf-8")
    layout = StorageLayout(tmp_path / "data")
    database = layout.platform_db("bot")
    database.parent.mkdir(parents=True)
    SessionOverrideStore(database).replace("session", "plugins.paint", {"painter": "drawing"}, expected_revision=0)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO group_configs(self_id, group_id, config_json, updated_at) VALUES (?, ?, ?, ?)", ("bot", "group", json.dumps({"session": {"plugins": {
            "mode": "value", "value": [{"name": "paint", "config": {"painter": "drawing"}}]}}}), 0))
    before = database.read_bytes()

    def check(name: str) -> list[dict[str, str]]:
        return list_img_config_references(name, platforms=[{"id": "bot"}], layout=layout, plugins_dir=plugins,
                                          plugin_config_dir=globals, edictum_config_path=named)

    assert {entry["kind"] for entry in check("drawing")} == {"plugin_global", "named_plugin", "session_override", "group_plugin"}
    assert database.read_bytes() == before
    manager = ModelConfigManager(tmp_path / "models.json", img_in_use_checker=check)
    service = ModelConfigService(manager)
    service.create("img", "drawing", payload())
    with pytest.raises(ConfigInUseError):
        service.delete("img", "drawing")
    with pytest.raises(ConfigInUseError):
        service.update("img", "drawing", {"name": "renamed"})
    named.write_text("broken", encoding="utf-8")
    with pytest.raises(ConfigReferenceScanError):
        service.delete("img", "drawing")


@pytest.mark.asyncio
async def test_img_test_uses_saved_key_one_image_and_releases_client(tmp_path: Path, monkeypatch) -> None:
    output = BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, format="PNG")
    data = output.getvalue()
    requests = []
    clients = []

    def create_client(self):
        def handler(request):
            requests.append(request)
            assert request.headers["authorization"] == "Bearer private-img-key"
            body = json.loads(request.content)
            assert body["parameters"]["n_samples"] == 1
            return httpx.Response(201, json={"images": [{"image": base64.b64encode(data).decode()}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(AsyncImageGen, "_create_client", create_client)
    manager = ModelConfigManager(tmp_path / "models.json")
    service = ModelConfigService(manager)
    raw = payload()
    raw["default_params"]["n"] = 4
    service.create("img", "paint", raw)
    before = manager.storage_path.read_bytes()
    result = await service.test_img_config("paint", "a cat")
    assert result["images"][0]["data_url"].startswith("data:image/png;base64,")
    assert "private-img-key" not in json.dumps(result)
    assert manager.storage_path.read_bytes() == before and len(requests) == 1 and clients[0].is_closed
    with pytest.raises(ValueError):
        await service.test_img_config("paint", "")
    with pytest.raises(ValueError):
        await service.test_img_config("missing", "cat")


@pytest.mark.asyncio
async def test_hot_backend_img_crud(tmp_path: Path) -> None:
    from satrap.core.backend.http_api import BackendHTTPServer

    manager = ModelConfigManager(tmp_path / "models.json")
    server = object.__new__(BackendHTTPServer)
    server.backend = SimpleNamespace(model_config_manager=manager)
    status, response = await server._route_models("POST", "/api/config/models/img/paint", json.dumps(payload()).encode())
    assert status == 200 and response["ok"]
    status, response = await server._route_models("GET", "/api/config/models?type=img", b"")
    assert status == 200 and response["paint"]["api_key"] != "private-img-key"
    status, _ = await server._route_models("PATCH", "/api/config/models/img/paint", b'{"edit_base_url":null}')
    assert status == 200 and manager.get_img_config("paint").edit_base_url is None
    status, _ = await server._route_models("DELETE", "/api/config/models/img/paint", b"")
    assert status == 200 and not manager.has_config("img", "paint")


@pytest.mark.asyncio
async def test_seedream_test_disables_saved_group_generation(tmp_path: Path, monkeypatch) -> None:
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    data = output.getvalue()
    bodies = []

    def create_client(self):
        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(AsyncImageGen, "_create_client", create_client)
    manager = ModelConfigManager(tmp_path / "models.json")
    service = ModelConfigService(manager)
    service.create("img", "seedream", {"api_type": "seedream", "model": "seedream-model", "api_key": "test",
        "base_url": "https://seedream.test/exact", "default_params": {"native": {"sequential_image_generation": "auto",
            "sequential_image_generation_options": {"max_images": 3}}}})
    assert (await service.test_img_config("seedream", "cat"))["images"]
    assert bodies[0]["sequential_image_generation"] == "disabled"
    assert manager.get_img_config("seedream").default_params["native"]["sequential_image_generation"] == "auto"
