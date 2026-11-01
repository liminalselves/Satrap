"""生图插件的动态契约, 托管模型调用与当前会话图片交付"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from io import BytesIO
import inspect
import json
from pathlib import Path
import threading
from unittest.mock import AsyncMock, MagicMock

import httpx
from PIL import Image
import pytest

from satrap import AsyncLLM, LLM
from satrap.core.approval.state import session_approval
from satrap.core.config.model_service import ModelConfigService
from satrap.core.file_access.artifacts import bind_file_receiver
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap.edictum.plugin import load_plugin_meta
from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema
from satrap.expend.plugins.image_generation.artifacts import load_image
from satrap.expend.plugins.image_generation.policy import ImagePolicy


PLUGIN = Path(__file__).resolve().parents[2] / "satrap/expend/plugins/image_generation"


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _png() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (8, 12), "orange").save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture(params=[False, True], ids=["sync", "async"])
async def image_host(request, tmp_path, monkeypatch):
    """真实安装插件并执行工具, 仅把上游 HTTP 替换为本地响应"""
    from satrap.edictum import simple_session as session_module

    asynchronous = request.param
    monkeypatch.setattr(session_module, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    llm = MagicMock(spec=AsyncLLM if asynchronous else LLM)
    llm.chat = AsyncMock() if asynchronous else MagicMock()
    session = (AsyncSimpleSession if asynchronous else SimpleSession)("images", llm, db_path=str(tmp_path / "chat.db"))
    manager = ModelConfigManager(tmp_path / "models.json")
    for name, api_type, model, defaults in (
        ("nai", "novelai", "nai-diffusion-4-5-full", {"size": [512, 768], "n": 3, "steps": 20, "seed": 0,
                                                   "negative_prompt": "bad quality", "native": {"parameters": {"future": False}}}),
        ("openai", "openai_images", "gpt-image", {}),
        ("seedream", "seedream", "seedream", {"native": {"sequential_image_generation": "auto", "sequential_image_generation_options": {"max_images": 4}}}),
    ):
        ModelConfigService(manager).create("img", name, {"api_type": api_type, "model": model, "api_key": "plugin-test-secret",
                                                        "base_url": "https://images.test/exact/generate?version=1", "default_params": defaults})
    session.plugin_model_manager = manager
    root = tmp_path / "sessions" / "images"
    bind_environment(session, replace(environment_snapshot(session), session_root=root, artifacts_root=root / "artifacts"))
    requests, received = [], []
    body = {"images": [{"image": base64.b64encode(_png()).decode("ascii"), "seed": 0}]}

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    async def install(config=None):
        plugin = await _await(session.install_plugin(str(PLUGIN), config={"img_model": "nai", **(config or {})}))
        client = plugin.resources["img_model"]
        if asynchronous:
            await client.client.aclose()
            client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        else:
            client.client.close()
            client.client = httpx.Client(transport=httpx.MockTransport(handle))
        return client

    async def execute(name="generate_image", **arguments):
        if asynchronous:
            return await session.tools_manager.execute_tool(name, arguments)
        return await asyncio.to_thread(session.tools_manager.execute_tool, name, arguments)

    async def receive(artifact):
        artifact.verify()
        received.append(artifact)
        return {"ok": True, "status": "available", "file": artifact.public()}

    with bind_file_receiver(receive):
        try:
            yield session, install, execute, requests, received, body, asynchronous
        finally:
            for plugin in reversed(session.list_plugins()):
                await _await(session.uninstall_plugin(plugin.name))


def test_meta_defaults_match_policy_and_img_resource_type():
    schema = parse_config_schema(load_plugin_meta(PLUGIN))
    defaults = {key: definition.default for key, definition in schema.items()}
    assert schema["img_model"].type == "img" and schema["img_model"].required
    assert ImagePolicy.from_config(defaults) == ImagePolicy()


@pytest.mark.parametrize("natural,keywords,expected", [(True, False, ["自然语言"]), (False, True, ["关键词"]), (True, True, ["自然语言", "关键词", "混合"])])
def test_prompt_styles_are_independent_and_guidance_is_in_schema(natural, keywords, expected):
    policy = ImagePolicy.from_config({"accept_natural_language": natural, "accept_keywords": keywords, "prompt_guidance": "使用英文"})
    description = policy.properties()[policy.prompt_field]["description"]
    assert all(value in description for value in [*expected, "使用英文"])
    assert "prompt_type" not in policy.properties()


@pytest.mark.parametrize("natural,keywords,required,forbidden", [
    (True, False, ["仅允许完整", "禁止关键词或标签列表", "禁止用逗号堆砌标签", "必须先将关键词组织成"], ["禁止自然语言句子"]),
    (False, True, ["仅允许关键词", "禁止自然语言句子", "必须先提取主体", "禁止把整句话塞进单个数组项"], ["禁止关键词或标签列表"]),
    (True, True, ["可以任选一种或混合使用"], ["禁止关键词或标签列表", "禁止自然语言句子"]),
])
async def test_tool_and_positive_negative_schemas_explicitly_enforce_selected_style(image_host, natural, keywords, required, forbidden):
    session, install, _, requests, _, _, _ = image_host
    await install({"accept_natural_language": natural, "accept_keywords": keywords,
                   "allow_negative_prompt": True, "prompt_guidance": "使用英文"})
    tool = session.tools_manager.tools["generate_image"].get_tool_defined()["function"]
    properties = tool["parameters"]["properties"]
    positive = "prompt" if natural else "keywords"
    negative = "negative_prompt" if natural else "negative_keywords"
    for description in (tool["description"], properties[positive]["description"], properties[negative]["description"]):
        assert all(fragment in description for fragment in [*required, "使用英文", "不得覆盖上述禁止规则"])
        assert all(fragment not in description for fragment in forbidden)
    assert not requests


def test_keywords_only_uses_array_contract_and_preserves_valid_tag_syntax():
    policy = ImagePolicy.from_config({"accept_natural_language": False, "allow_negative_prompt": True})
    schema = policy.properties()
    assert policy.prompt_field == "keywords" and "prompt" not in schema and "negative_prompt" not in schema
    assert schema["keywords"]["type"] == "array" and schema["keywords"]["minItems"] == 1
    assert schema["keywords"]["maxItems"] == 128 and schema["keywords"]["items"]["maxLength"] == 120
    prompt, values = policy.prepare_prompt({"keywords": [" orange cat ", "窗台", "1.2::blue hair::", "artist_name (artist)"],
                                            "negative_keywords": ["bad anatomy", "blurry"]})
    assert prompt == "orange cat, 窗台, 1.2::blue hair::, artist_name (artist)"
    assert policy.resolve(values).negative_prompt == "bad anatomy, blurry"
    assert policy.resolve({}).negative_prompt is None
    assert policy.resolve({"negative_keywords": []}).negative_prompt == ""


@pytest.mark.parametrize("arguments", [
    {}, {"prompt": "A cat is sitting beside a window."},
    {"prompt": "cat", "keywords": ["cat"]}, {"keywords": "cat, window"}, {"keywords": []},
    {"keywords": None}, {"keywords": [1]}, {"keywords": [""]}, {"keywords": ["   "]},
    {"keywords": ["cat, window"]}, {"keywords": ["cat，window"]}, {"keywords": ["cat; window"]},
    {"keywords": ["cat\nwindow"]}, {"keywords": ["cat\twindow"]}, {"keywords": ["a" * 121]},
    {"keywords": ["cat"] * 129}, {"keywords": ["cat"], "negative_prompt": "blurry"},
    {"keywords": ["cat"], "negative_keywords": "blurry"}, {"keywords": ["cat"], "negative_keywords": ["bad, blurry"]},
])
def test_keywords_only_rejects_free_text_and_malformed_arrays(arguments):
    policy = ImagePolicy.from_config({"accept_natural_language": False, "allow_negative_prompt": True})
    with pytest.raises(ValueError):
        policy.prepare_prompt(arguments)


@pytest.mark.parametrize("keywords", [False, True])
def test_natural_language_modes_keep_existing_prompt_contract(keywords):
    policy = ImagePolicy.from_config({"accept_keywords": keywords})
    assert policy.prompt_field == "prompt" and "keywords" not in policy.properties()
    assert policy.prepare_prompt({"prompt": "An orange cat by the window."}) == ("An orange cat by the window.", {})


@pytest.mark.parametrize("config", [
    {"accept_natural_language": False, "accept_keywords": False}, {"accept_keywords": "true"},
    {"size_mode": "invalid"}, {"count_mode": "invalid"}, {"size_choices": ""},
    {"size_choices": "832x1216", "size_default": "1024x1024"}, {"size_step": 0},
    {"count_min": 3, "count_default": 2}, {"count_max": 33}, {"count_fixed": True},
    {"width_min": 3000}, {"height_min": 3000}, {"max_pixels": 40000001},
    {"size_mode": "range", "size_default": "2K"}, {"size_mode": "range", "size_default": "513x768"},
    {"size_mode": "fixed", "size_fixed": "9000x9000"}, {"size_mode": "fixed", "size_fixed": " "},
])
def test_invalid_policy_rejected_before_network(config):
    with pytest.raises(ValueError):
        ImagePolicy.from_config(config)


@pytest.mark.parametrize("arguments", [{"size": "512x512"}, {"size": None}, {"n": 1}, {"native": {}}, {"negative_prompt": "bad"}])
def test_hidden_and_unlisted_arguments_cannot_override_policy(arguments):
    with pytest.raises(ValueError):
        ImagePolicy.from_config({"size_mode": "fixed"}).resolve(arguments)


@pytest.mark.parametrize("value", [0, 5, True, 1.5, "2", None])
def test_count_range_rejects_outside_or_non_integer_values(value):
    policy = ImagePolicy.from_config({"count_mode": "range"})
    with pytest.raises(ValueError):
        policy.resolve({"n": value})


def test_range_schema_and_runtime_share_bounds_steps_and_pixel_limit():
    policy = ImagePolicy.from_config({"size_mode": "range", "max_pixels": 1048576})
    schema = policy.properties()
    assert schema["width"]["minimum"] == 512 and schema["width"]["maximum"] == 2048
    assert schema["height"]["multipleOf"] == 64
    assert policy.resolve({}).size == (1024, 1024)
    assert policy.resolve({"width": 512, "height": 768}).size == (512, 768)
    for arguments in ({"width": 512}, {"height": 768}, {"width": 513, "height": 768},
                      {"width": 2048, "height": 2048}, {"width": 256, "height": 768},
                      {"width": True, "height": 768}):
        with pytest.raises(ValueError):
            policy.resolve(arguments)


async def test_generate_uses_model_defaults_policy_overrides_and_current_receiver(image_host):
    session, install, execute, requests, received, body, _ = image_host
    client = await install({"count_mode": "range", "count_default": 2, "allow_negative_prompt": True,
                            "size_choices": "512x768\n768x512", "size_default": "512x768"})
    definition = session.tools_manager.tools["generate_image"].get_tool_defined()
    assert definition["function"]["parameters"]["required"] == ["prompt"]
    assert definition["function"]["parameters"]["additionalProperties"] is False
    result = await execute(prompt="an orange cat")
    assert result["ok"] and result["generation_status"] == "succeeded" and result["delivery_status"] == "succeeded"
    assert result["requested_count"] == 2 and result["actual_count"] == 1 and len(requests) == 1
    assert requests[0].url == "https://images.test/exact/generate?version=1"
    parameters = json.loads(requests[0].content)["parameters"]
    assert (parameters["width"], parameters["height"], parameters["n_samples"], parameters["steps"], parameters["seed"]) == (512, 768, 2, 20, 0)
    assert parameters["negative_prompt"] == "bad quality" and parameters["future"] is False
    assert client.default_params.n == 3 and client.default_params.size == (512, 768)
    assert received[0].path.read_bytes() == _png()
    encoded = json.dumps(result)
    assert "plugin-test-secret" not in encoded and str(received[0].root) not in encoded and body["images"][0]["image"] not in encoded
    root = environment_snapshot(session).artifacts_root
    artifact, record = load_image(root, result["images"][0]["file"]["file_id"])
    assert artifact.sha256 == received[0].sha256 and record["width"] == 8 and record["height"] == 12


async def test_keywords_only_blocks_old_prompt_before_http_and_serializes_arrays(image_host):
    session, install, execute, requests, _, _, _ = image_host
    await install({"accept_natural_language": False, "allow_negative_prompt": True})
    tool = session.tools_manager.tools["generate_image"]
    definition = tool.get_tool_defined()["function"]
    assert definition["parameters"]["required"] == ["keywords"]
    assert "prompt" not in definition["parameters"]["properties"]
    assert "仅允许关键词" in definition["description"]
    for arguments in ({"prompt": "An orange cat is sitting beside a window."}, {"keywords": "cat, window"},
                      {"keywords": ["cat\nwindow"]}, {"keywords": ["cat"], "negative_prompt": "bad quality"}):
        result = await execute(**arguments)
        assert not result["ok"] and result["error_type"] == "invalid_arguments"
    assert not requests
    result = await execute(keywords=["orange cat", "window", "1.2::daylight::"], negative_keywords=["blurry", "bad anatomy"])
    assert result["ok"] and len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["input"] == "orange cat, window, 1.2::daylight::"
    assert body["parameters"]["negative_prompt"] == "blurry, bad anatomy"
    assert body["parameters"]["steps"] == 20 and body["parameters"]["seed"] == 0
    assert (await execute(keywords=["cat"], negative_keywords=[]))["ok"]
    assert json.loads(requests[-1].content)["parameters"]["negative_prompt"] == ""
    assert (await execute(keywords=["cat"]))["ok"]
    assert json.loads(requests[-1].content)["parameters"]["negative_prompt"] == "bad quality"


async def test_keywords_plugin_inherits_nai_injection_once_after_preflight(image_host):
    session, install, execute, requests, _, _, _ = image_host
    manager = session.plugin_model_manager
    defaults = manager.get_img_config("nai").default_params
    manager.update_img_config("nai", default_params={**defaults, "inject_default_positive_prompt": True,
                                                     "inject_default_negative_prompt": True})
    await install({"accept_natural_language": False, "allow_negative_prompt": True})
    result = await execute(keywords=["orange cat", "window"], negative_keywords=["blur"])
    assert result["ok"] and len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["input"] == "orange cat, window, very aesthetic, masterpiece, no text"
    assert body["input"].count("very aesthetic") == 1
    assert body["parameters"]["negative_prompt"].startswith("lowres, artistic error,")
    assert body["parameters"]["negative_prompt"].endswith("blank page, blur")
    assert manager.get_img_config("nai").default_params["negative_prompt"] == "bad quality"


async def test_fixed_values_are_hidden_and_rejected(image_host):
    session, install, execute, requests, _, _, _ = image_host
    await install({"size_mode": "fixed", "size_fixed": "512x768", "count_fixed": 2})
    tool = session.tools_manager.tools["generate_image"]
    properties = tool.get_tool_defined()["function"]["parameters"]["properties"]
    assert set(properties) == {"prompt"}
    for argument in ({"n": 1}, {"size": "512x768"}, {"width": 512, "height": 768}, {"native": {"n": 5}}, {"prompt": " "}):
        assert not (await execute(**{"prompt": "cat", **argument}))["ok"]
    assert not requests
    result = await execute(prompt="cat")
    assert result["requested_count"] == 2 and json.loads(requests[0].content)["parameters"]["n_samples"] == 2


async def test_negative_prompt_empty_string_clears_inherited_value(image_host):
    _, install, execute, requests, _, _, _ = image_host
    await install({"allow_negative_prompt": True})
    assert (await execute(prompt="cat", negative_prompt=""))["ok"]
    assert json.loads(requests[0].content)["parameters"]["negative_prompt"] == ""


async def test_failed_delivery_can_be_retried_after_reinstall_without_regenerating_or_copying(image_host):
    session, install, execute, requests, received, _, _ = image_host
    await install()
    async def fail(artifact):
        return {"ok": False, "status": "failed", "reason": "upload_failed"}
    with bind_file_receiver(fail):
        result = await execute(prompt="cat")
    assert not result["ok"] and result["generation_status"] == "succeeded" and not result["retry_generation"]
    file_id = result["images"][0]["file"]["file_id"]
    await _await(session.uninstall_plugin("image_generation"))
    await install()
    resent = await execute("send_generated_image", file_id=file_id)
    assert resent["ok"] and resent["generation_status"] == "reused" and len(requests) == 1
    assert len(list(received[-1].root.glob("sf_*"))) == 1
    assert resent["images"][0]["file"] == result["images"][0]["file"]


async def test_unconfirmed_delivery_does_not_discard_image_or_regenerate(image_host):
    _, install, execute, requests, _, _, _ = image_host
    await install()
    async def fail(artifact):
        raise RuntimeError("plugin-test-secret")
    with bind_file_receiver(fail):
        result = await execute(prompt="cat")
    assert result["delivery_status"] == "unknown" and result["generation_status"] == "succeeded"
    assert "plugin-test-secret" not in json.dumps(result)
    assert len(requests) == 1


async def test_resend_rejects_missing_foreign_changed_images_and_paths(image_host, tmp_path):
    session, install, execute, requests, received, _, _ = image_host
    await install()
    result = await execute(prompt="cat")
    root = environment_snapshot(session).artifacts_root
    file_id = result["images"][0]["file"]["file_id"]
    with pytest.raises(ValueError):
        load_image(tmp_path / "other-session", file_id)
    for invalid in ("../" + file_id, str(received[0].path), "sf_" + "0" * 32):
        assert not (await execute("send_generated_image", file_id=invalid))["ok"]
    received[0].path.write_bytes(b"changed")
    assert not (await execute("send_generated_image", file_id=file_id))["ok"]
    assert len(requests) == 1


async def test_plan_mode_blocks_generation_and_resend_before_network(image_host):
    session, install, execute, requests, _, _, _ = image_host
    await install()
    session_approval(session).set_plan_mode(True)
    assert not (await execute(prompt="cat"))["ok"]
    assert not (await execute("send_generated_image", file_id="sf_" + "0" * 32))["ok"]
    assert not requests


async def test_seedream_disables_inherited_group_generation_and_posts_once(image_host):
    _, install, execute, requests, _, _, _ = image_host
    await install({"img_model": "seedream", "size_mode": "fixed", "size_fixed": "2K"})
    result = await execute(prompt="cat")
    assert result["ok"] and len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["size"] == "2K" and body["sequential_image_generation"] == "disabled"


@pytest.mark.parametrize("config", [{"img_model": "openai", "allow_negative_prompt": True},
                                    {"img_model": "seedream", "count_mode": "range"},
                                    {"size_choices": "2K", "size_default": "2K"}])
async def test_incompatible_model_policy_fails_install_without_http(image_host, config):
    session, install, _, requests, _, _, _ = image_host
    with pytest.raises(ValueError):
        await install(config)
    assert not requests and not session.list_plugins()


async def test_upstream_failure_has_no_payload_or_secret_and_no_retry(image_host):
    _, install, execute, requests, _, _, asynchronous = image_host
    client = await install()
    def fail(request):
        requests.append(request)
        return httpx.Response(500, json={"error": "plugin-test-secret"})
    await _await(client.client.aclose() if asynchronous else client.client.close())
    client.client = (httpx.AsyncClient if asynchronous else httpx.Client)(transport=httpx.MockTransport(fail))
    result = await execute(prompt="cat")
    assert not result["ok"] and result["error_type"] == "http_error"
    assert result["generation_status"] == "unknown" and not result["retry_generation"] and len(requests) == 1
    assert "plugin-test-secret" not in json.dumps(result)


async def test_async_cancellation_propagates_without_another_http_request(image_host):
    _, install, execute, requests, _, _, asynchronous = image_host
    if not asynchronous:
        pytest.skip("仅异步客户端支持任务取消")
    client = await install()
    started = asyncio.Event()
    async def pending(request):
        requests.append(request)
        started.set()
        await asyncio.Event().wait()
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(pending))
    task = asyncio.create_task(execute(prompt="cat"))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(requests) == 1


async def test_openai_and_pixel_range_use_same_dynamic_contract(image_host):
    session, install, execute, requests, _, _, _ = image_host
    await install({"img_model": "openai", "size_mode": "range", "count_mode": "range"})
    schema = session.tools_manager.tools["generate_image"].get_tool_defined()["function"]["parameters"]
    assert set(schema["properties"]) == {"prompt", "width", "height", "n"}
    assert not (await execute(prompt="cat", width=768))["ok"]
    assert not requests
    result = await execute(prompt="cat", width=768, height=512, n=2)
    assert result["ok"] and len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["size"] == "768x512" and body["n"] == 2


async def test_multiple_images_keep_individual_partial_delivery_states(image_host):
    _, install, execute, requests, received, body, _ = image_host
    await install({"count_fixed": 2})
    body["images"].append(dict(body["images"][0]))
    calls = []
    async def receive(artifact):
        calls.append(artifact)
        return {"ok": len(calls) == 1, "status": "available" if len(calls) == 1 else "failed"}
    with bind_file_receiver(receive):
        result = await execute(prompt="cat")
    assert not result["ok"] and result["delivery_status"] == "partial" and result["actual_count"] == 2
    assert len(requests) == 1 and len(list(calls[0].root.glob("sf_*"))) == 2
    assert calls[0].file_id != calls[1].file_id
    assert all(artifact.path.read_bytes() == _png() for artifact in calls)


async def test_full_artifact_directory_rejects_before_billed_request(image_host):
    session, install, execute, requests, _, _, _ = image_host
    await install()
    root = environment_snapshot(session).artifacts_root
    root.mkdir(parents=True)
    for index in range(128):
        (root / ("sf_" + f"{index:032x}")).write_bytes(b"x")
    assert not (await execute(prompt="cat"))["ok"]
    assert not requests


async def test_async_cancellation_during_storage_finishes_save_and_releases_lease(image_host, monkeypatch):
    session, install, execute, requests, _, _, asynchronous = image_host
    if not asynchronous:
        pytest.skip("仅异步客户端支持任务取消")
    from satrap.expend.plugins.image_generation import artifacts
    from satrap.core.storage.file_lock import FileLock
    await install()
    started, release = threading.Event(), threading.Event()
    original = artifacts.freeze_file
    def slow_freeze(*args):
        started.set()
        if not release.wait(5):
            raise TimeoutError("测试未释放文件写入")
        return original(*args)
    monkeypatch.setattr(artifacts, "freeze_file", slow_freeze)
    task = asyncio.create_task(execute(prompt="cat"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    root = environment_snapshot(session).artifacts_root
    files = list(root.glob("sf_*"))
    assert len(files) == 1 and len(requests) == 1
    load_image(root, files[0].name)
    with FileLock(root.parent.parent.parent / "locks" / (root.parent.name + ".lock"), timeout=.05):
        pass
