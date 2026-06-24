from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.type import LLMCallResponse
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.utils.vision import (
    DEFAULT_IMAGE_TOKEN_COST,
    build_multimodal_content,
    content_text_projection,
    normalize_openai_base_url,
)


def _tiny_png_data_url() -> str:
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    return "data:image/png;base64," + base64.b64encode(raw).decode("utf-8")


def test_build_multimodal_content_supports_urls_and_data_url():
    content = build_multimodal_content(
        "看图",
        ["https://example.com/a.png", _tiny_png_data_url()],
    )

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["image_url"]["url"] == "https://example.com/a.png"
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content_text_projection(content) == "看图 [图片] [图片]"


def test_build_multimodal_content_compresses_toolkit_image_when_available():
    image_path = Path(".toolkit/IMG_0114..JPG")
    if not image_path.exists():
        pytest.skip(".toolkit 测试图片不存在")

    content = build_multimodal_content("描述图片", [str(image_path)])

    assert isinstance(content, list)
    data_url = content[1]["image_url"]["url"]
    assert data_url.startswith("data:image/")
    payload = data_url.split(",", 1)[1]
    assert len(base64.b64decode(payload)) <= 4 * 1024 * 1024


def test_context_manager_persists_multimodal_content(tmp_path):
    db_path = tmp_path / "history.db"
    ctx = ContextManager("vision", db_path=str(db_path))
    ctx.add_user_message("图片里有什么", img_urls=[_tiny_png_data_url()])

    loaded = ContextManager("vision", db_path=str(db_path))
    message = loaded.get_context()[0]

    assert message["role"] == "user"
    assert isinstance(message["content"], list)
    assert message["content"][0]["text"] == "图片里有什么"
    assert loaded.estimate_token(method="experience") >= DEFAULT_IMAGE_TOKEN_COST


def test_async_context_manager_persists_multimodal_content(tmp_path):
    async def _run():
        db_path = tmp_path / "history.db"
        ctx = AsyncContextManager("vision", db_path=str(db_path))
        await ctx.initialize()
        await ctx.add_user_message("图片里有什么", img_urls=[_tiny_png_data_url()])

        loaded = AsyncContextManager("vision", db_path=str(db_path))
        await loaded.initialize()
        message = loaded.get_context()[0]

        assert isinstance(message["content"], list)
        assert message["content"][1]["type"] == "image_url"

    asyncio.run(_run())


def test_context_manager_loads_legacy_text_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    ctx = ContextManager("legacy", db_path=str(db_path))
    ctx.add_user_message("旧消息")

    loaded = ContextManager("legacy", db_path=str(db_path))

    assert loaded.get_context()[0]["content"] == "旧消息"


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return {"choices": [{"message": {"content": "ok"}}]}


class _FakeAsyncCompletions:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return {"choices": [{"message": {"content": "ok"}}]}


def _make_llm(fake_completions: _FakeCompletions) -> LLM:
    llm = LLM.__new__(LLM)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    llm.model = "mock"
    llm.temperature = 0.7
    llm.top_p = 0.95
    llm.max_tokens = 1000
    llm.suppress_error = True
    llm.return_false = False
    llm.reasoning_body = {"thinking": {"type": "enabled"}}
    llm.thinking_field_name = "reasoning_content"
    return llm


def test_llm_call_appends_images_to_last_user_message():
    fake = _FakeCompletions()
    llm = _make_llm(fake)

    response = llm.call(
        [{"role": "user", "content": "第一张"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "第二张"}],
        img_urls=[_tiny_png_data_url()],
    )

    assert isinstance(response, LLMCallResponse)
    messages = fake.kwargs["messages"]
    assert messages[0]["content"] == "第一张"
    assert isinstance(messages[-1]["content"], list)
    assert messages[-1]["content"][1]["type"] == "image_url"


def test_async_llm_call_appends_images_to_last_user_message():
    async def _run():
        fake = _FakeAsyncCompletions()
        llm = AsyncLLM.__new__(AsyncLLM)
        llm.client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
        llm.model = "mock"
        llm.temperature = 0.7
        llm.top_p = 0.95
        llm.max_tokens = 1000
        llm.suppress_error = True
        llm.return_false = False
        llm.reasoning_body = {"thinking": {"type": "enabled"}}
        llm.thinking_field_name = "reasoning_content"

        response = await llm.call([{"role": "user", "content": "看图"}], img_urls=[_tiny_png_data_url()])

        assert isinstance(response, LLMCallResponse)
        assert fake.kwargs["messages"][0]["content"][1]["type"] == "image_url"

    asyncio.run(_run())


def _load_toolkit_config() -> dict[str, str]:
    if os.getenv("SATRAP_RUN_TOOLKIT_VISION") != "1":
        pytest.skip("设置 SATRAP_RUN_TOOLKIT_VISION=1 后运行真实视觉模型测试")

    config_path = Path(".toolkit/apikey.txt")
    image_path = Path(".toolkit/IMG_0114..JPG")
    if not config_path.exists() or not image_path.exists():
        pytest.skip(".toolkit 模型配置或图片不存在")

    values: dict[str, str] = {}
    for line in config_path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    if not values.get("api key") or not values.get("model") or not values.get("base url"):
        pytest.skip(".toolkit/apikey.txt 字段不完整")
    return values


def test_normalize_openai_base_url_for_toolkit_endpoint():
    assert normalize_openai_base_url("https://api.siliconflow.cn/v1/chat/completions") == "https://api.siliconflow.cn/v1"


def test_real_toolkit_vision_call_when_available():
    values = _load_toolkit_config()
    llm = LLM(
        api_key=values["api key"],
        base_url=values["base url"],
        model=values["model"],
        max_tokens=200,
        reasoning_body=None,
    )

    response = llm.call(
        [{"role": "user", "content": "请用一句中文描述这张图片的主要内容"}],
        img_urls=[str(Path(".toolkit/IMG_0114..JPG"))],
    )

    assert isinstance(response, LLMCallResponse)
    assert response.content.strip()
