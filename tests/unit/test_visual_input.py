"""视觉配置, 多轮历史, 工具图片及恢复输入回归"""
from pathlib import Path
from typing import Any, cast
import base64
import copy

import pytest

from satrap.core.APICall.LLMCall import LLM, AsyncLLM, build_llm_from_config
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, LLMConfig
from satrap.core.utils.media import user_media_content, project_messages, freeze_tool_result
from satrap.core.APICall.LLMCall.utils import prepare_call_messages
from satrap.core.utils.context import add_tools_call_flow
from satrap.edictum import SimpleSession, AsyncSimpleSession


class VisualModel(LLM):
    def __init__(self):
        self.supports_visual_input = True
        self.requests = []

    def call(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        return LLMCallResponse(type="answer", content="已读取")

    def stream_call(self, messages, **kwargs):
        yield LLMCallStreamEvent(kind="done", response=self.call(messages, **kwargs))


class AsyncVisualModel(AsyncLLM):
    def __init__(self):
        self.supports_visual_input = True
        self.requests = []

    async def call(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        return LLMCallResponse(type="answer", content="已读取")

    async def stream_call(self, messages, **kwargs):
        yield LLMCallStreamEvent(kind="done", response=await self.call(messages, **kwargs))


def image_url():
    return "data:image/png;base64," + base64.b64encode(b"test-image").decode("ascii")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("recoverable", [False, True])
async def test_visual_history_survives_followup(tmp_path, asynchronous, stream, recoverable):
    if asynchronous:
        model = AsyncVisualModel()
        session = AsyncSimpleSession("visual", model, stream=stream, recoverable=recoverable, db_path=str(tmp_path / "context.db"))
        await session.run("看图", img_urls=[image_url()])
        await session.run("继续解释")
    else:
        model = VisualModel()
        session = SimpleSession("visual", model, stream=stream, recoverable=recoverable, db_path=str(tmp_path / "context.db"))
        session.run("看图", img_urls=[image_url()])
        session.run("继续解释")
    first_user = next(m for m in model.requests[1] if m["role"] == "user")
    assert first_user["content"][1]["image_url"]["url"] == image_url()
    assert session.ctx.get_context()[0]["content"] == first_user["content"]


def test_visual_capability_requires_boolean_and_factory_propagates():
    assert LLMConfig().supports_visual_input is False
    with pytest.raises(ValueError):
        LLMConfig(supports_visual_input=cast(Any, "false"))
    llm = build_llm_from_config(LLMConfig(api_key="fake", supports_visual_input=True), async_=False)
    assert llm.supports_visual_input is True
    llm.client.close()


def test_disabled_new_media_rejected_and_history_projection_is_non_destructive():
    model = VisualModel()
    content = user_media_content("图片", [image_url()], None, model)
    model.supports_visual_input = False
    with pytest.raises(ValueError, match="未启用"):
        user_media_content("图片", [image_url()], None, model)
    messages = [{"role": "user", "content": content}]
    assert project_messages(messages, False)[0]["content"] == "图片 [图片]"
    assert isinstance(messages[0]["content"], list)


def test_local_video_is_frozen(tmp_path):
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"video-before")
    content = user_media_content("视频", None, [str(path)], VisualModel())
    assert isinstance(content, list)
    path.write_bytes(b"video-after")
    assert content[1]["type"] == "video_url"
    assert base64.b64decode(content[1]["video_url"]["url"].split(",")[1]) == b"video-before"


def test_media_tool_results_follow_all_tool_responses():
    messages: list[dict[str, Any]] = []
    calls = [{"id": "a", "type": "function", "function": {"name": "read_document", "arguments": "{}"}},
             {"id": "b", "type": "function", "function": {"name": "other", "arguments": "{}"}}]
    result = {"satrap_media_result": 1, "text": "第 1 页", "media": [{"type": "image_url", "image_url": {"url": image_url()}}]}
    add_tools_call_flow(messages, "", calls, [result, "普通结果"])
    assert [m["role"] for m in messages] == ["assistant", "tool", "tool"]
    assert messages[1]["content"][0]["text"] == "第 1 页"
    prepared = prepare_call_messages(messages, None, None, supports_visual_input=True)
    assert [m["role"] for m in prepared] == ["assistant", "tool", "tool", "user"]
    assert prepared[-1]["content"][-1]["image_url"]["url"] == image_url()
    assert isinstance(messages[1]["content"], list)
    projected = project_messages(messages, False)
    assert projected[1]["content"] == "第 1 页 [图片]"
    assert [m["role"] for m in projected] == ["assistant", "tool", "tool"]


def test_tool_media_is_fixed_before_storage(tmp_path):
    path = tmp_path / "page.png"
    path.write_bytes(b"original")
    result = {"satrap_media_result": 1, "text": "页面", "media": [
        {"type": "image_url", "image_url": {"url": str(path)}},
    ]}
    frozen = freeze_tool_result(result, VisualModel())
    path.unlink()
    data = frozen["media"][0]["image_url"]["url"]
    assert base64.b64decode(data.split(",")[1]) == b"original"
    assert result["media"][0]["image_url"]["url"] == str(path)
    assert freeze_tool_result("普通结果", object()) == "普通结果"
    with pytest.raises(ValueError, match="未启用"):
        freeze_tool_result(frozen, object())
