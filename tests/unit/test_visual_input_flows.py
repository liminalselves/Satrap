"""业务链路场景与集中边界契约"""
from __future__ import annotations

from dataclasses import replace
from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot

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
from types import SimpleNamespace
import os
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.utils.vision import DEFAULT_IMAGE_TOKEN_COST, build_multimodal_content, content_text_projection
from satrap.core.utils import normalize_openai_base_url
import io
from PIL import Image
import pypdfium2 as pdfium
from satrap.expend.plugins.base_take.tools.async_ import AsyncReadDocumentTool
from satrap.expend.plugins.base_take.tools.sync import ReadDocumentTool
from satrap.core.utils.pdf_pages import read_pdf_pages


def _small_video(seconds=2):
    """生成可解码的 MP4, 用于验证完整视觉输入链路"""
    av = pytest.importorskip("av")
    output = io.BytesIO()
    with av.open(output, "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 32, 24, "yuv420p"
        for index in range(seconds * 10):
            frame = av.VideoFrame.from_image(Image.new("RGB", (32, 24), (index % 255, 30, 90)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_video_sampling_history_cache_and_native_switch(tmp_path, monkeypatch, asynchronous):
    """真实视频经过预算前抽帧, 历史保留引用, 缓存复用且切换后只发送原生视频"""
    from satrap.core.utils import media_frames
    from satrap.core.utils.media_store import REFERENCE_PREFIX, store_for

    path = tmp_path / "clip.mp4"
    path.write_bytes(_small_video())
    model = AsyncVisualModel() if asynchronous else VisualModel()
    model.video_frame_interval = 0.5
    cls = AsyncSimpleSession if asynchronous else SimpleSession
    session = cls("sample-video", model, db_path=str(tmp_path / "platform.db"), enable_checkpoint=False)

    async def run(text, **kwargs):
        return await session.run(text, **kwargs) if asynchronous else session.run(text, **kwargs)

    await run("查看画面", video_urls=[str(path)])
    parts = model.requests[-1][0]["content"]
    images = [part for part in parts if part["type"] == "image_url"]
    assert len(images) == 5
    assert "0.500 秒" in str(parts)
    assert not any(part["type"] == "video_url" for part in parts)
    reference = session.ctx.get_context()[0]["content"][1]["video_url"]["url"]
    assert reference.startswith(REFERENCE_PREFIX)
    assert store_for(session.ctx.db_path, session.ctx.conversation_id).read(reference)[0] == path.read_bytes()
    path.unlink()

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("相同媒体和采样参数不应重复解码")

    monkeypatch.setattr(media_frames, "_decode", unexpected_decode)
    await run("继续解释")
    assert sum(part["type"] == "image_url" for part in model.requests[-1][0]["content"]) == 5
    model.supports_native_video_input = True
    model.supports_visual_input = False
    await run("再次查看")
    native_parts = model.requests[-1][0]["content"]
    assert sum(part["type"] == "video_url" for part in native_parts) == 1
    assert not any(part["type"] == "image_url" for part in native_parts)
    assert reference in str(session.ctx.get_context())


def test_video_range_budget_and_scope(tmp_path):
    """长视频明确要求分段, 合法区间按原密度采样, 跨会话不能读取引用"""
    from satrap.core.utils.media_store import current_store, media_scope
    from satrap.core.utils.media_frames import sample_frames
    from satrap.expend.plugins.base_take.tools.sync import ReadMediaTool

    data = _small_video(seconds=10)
    model = SimpleNamespace(supports_visual_input=True, video_frame_interval=0.5)
    database = str(tmp_path / "platform.db")
    with media_scope(database, "owner_main"):
        url = "data:video/mp4;base64," + base64.b64encode(data).decode()
        content = user_media_content("长视频", None, [url], model)
        reference = content[1]["video_url"]["url"]
        projected = project_messages([{"role": "user", "content": content}], True, model)
        assert "尚未读取画面" in str(projected)
        assert "21 帧" in str(projected)
        assert "read_media" in str(projected)
        tool = ReadMediaTool()
        tool._bind(SimpleNamespace(ctx=SimpleNamespace(db_path=database, conversation_id="owner_main"), llm=model))
        result = tool.execute(reference.split("://")[1], 2, 4)
        frozen = freeze_tool_result(result, model)
        sampled = project_messages([{"role": "user", "content": frozen["media"]}], True, model)
        assert sum(part["type"] == "image_url" for part in sampled[0]["content"]) >= 4
        assert "2.000 秒" in str(sampled)
        source = current_store()
    with media_scope(database, "other_main"):
        with pytest.raises(ValueError, match="不属于当前会话"):
            current_store().read(reference)
        source.copy_to(current_store())
        assert current_store().read(reference)[0] == data
    with media_scope(database, "owner_main"):
        with pytest.raises(ValueError, match="缺少视频解码依赖|decode|无法|媒体|file|Invalid"):
            sample_frames(b"invalid-video", "video", 2)
    with media_scope(str(tmp_path / "different.db"), "owner_main"):
        with pytest.raises(ValueError, match="不属于当前会话"):
            current_store().read(reference)


@pytest.mark.parametrize("platform", ["onebot", "misskey"])
def test_platform_media_transport_is_normalized_by_actual_bytes(tmp_path, platform):
    """保留平台图片分类, 真 GIF 抽帧, 图片标签下的真实 MP4 按视频能力路由"""
    from satrap.core.platform.onebot.onebot_utils import onebot_segments_to_components
    from satrap.core.platform.misskey.misskey_utils import create_file_component
    from satrap.core.utils.media import freeze_media
    from satrap.core.utils.media_store import media_scope

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (8, 8), "blue")], duration=500)
    model = SimpleNamespace(supports_visual_input=True, supports_native_video_input=True, video_frame_interval=0.5)
    with media_scope(str(tmp_path / "platform.db"), platform):
        for data, mime, expected in ((buffer.getvalue(), "image/gif", "image_url"), (_small_video(), "video/mp4", "video_url")):
            url = f"data:{mime};base64," + base64.b64encode(data).decode()
            if platform == "onebot":
                components, _ = onebot_segments_to_components([{"type": "image", "data": {"file": "animation.gif", "url": url}}])
                component = components[0]
            else:
                component, _ = create_file_component({"type": "image/gif", "name": "animation.gif", "url": url})
            frozen = freeze_media(component.url, "image")
            assert frozen["type"] == expected
            prepared = project_messages([{"role": "user", "content": [frozen]}], True, model)[0]["content"]
            assert any(part["type"] == expected for part in prepared)
            assert not any(part["type"] == ("video_url" if expected == "image_url" else "image_url") for part in prepared)


def test_media_worker_timeout_reaps_process_and_cache_is_bounded(tmp_path, monkeypatch):
    """真实解码进程超时后被回收, 处理槽位可继续使用, 缓存按容量淘汰旧结果"""
    from satrap.core.utils import media_frames
    from satrap.core.utils.media_store import media_scope, current_store

    data = _small_video()
    processes = []
    actual_popen = media_frames.subprocess.Popen

    def capture(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(media_frames.subprocess, "Popen", capture)
    with media_scope(str(tmp_path / "platform.db"), "worker"):
        monkeypatch.setattr(media_frames, "DECODE_TIMEOUT", 0)
        with pytest.raises(ValueError, match="超时"):
            media_frames.sample_frames(data, "video", 0.5)
        assert processes and all(process.poll() is not None for process in processes)
        monkeypatch.setattr(media_frames, "DECODE_TIMEOUT", 30)
        first = media_frames.sample_frames(data, "video", 0.5)
        assert len(first["frames"]) == 5
        started = len(processes)
        assert media_frames.sample_frames(data, "video", 0.5) == first
        assert len(processes) == started
        monkeypatch.setattr(media_frames, "MAX_CACHE_BYTES", 1)
        media_frames.sample_frames(data, "video", 1.0)
        caches = [entry for entry in (current_store().root / "frames").iterdir() if entry.is_dir() and not entry.name.startswith(".")]
        assert len(caches) == 1


@pytest.mark.asyncio
async def test_cancelled_media_task_stops_real_worker(tmp_path, monkeypatch):
    """异步任务取消会传播到后台解码进程, 不留下持续运行的子进程"""
    import asyncio
    import threading
    from satrap.core.utils import media_frames
    from satrap.core.utils.media_store import media_scope

    data = _small_video()
    started = threading.Event()
    processes = []
    actual_popen = media_frames.subprocess.Popen

    def capture(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(media_frames.subprocess, "Popen", capture)
    with media_scope(str(tmp_path / "platform.db"), "cancel"), media_frames.cancellable_media():
        task = asyncio.create_task(asyncio.to_thread(media_frames.sample_frames, data, "video", 0.5))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for process in processes:
        await asyncio.to_thread(process.wait, timeout=5)
        assert process.poll() is not None


def test_new_media_budget_preserves_current_segment_without_rewriting_history(tmp_path):
    """连续读取优先保留最新完整区间, 旧画面明确占位且原始历史仍可重读"""
    from satrap.core.utils.media import limit_history_media

    part = {"type": "image_url", "image_url": {"url": image_url()}}
    context = ContextManager("budget", db_path=str(tmp_path / "platform.db"))
    context.add_turn_messages([{"role": "user", "content": [part] * 16}])
    pending = [{"role": "user", "content": [{"type": "text", "text": "下一段"}, part, part]}]
    prepared = context.prepare_model_context(llm=VisualModel(), pending_messages=pending)
    assert sum(item["type"] == "image_url" for message in prepared.messages for item in message["content"] if isinstance(message["content"], list)) == 2
    assert "历史画面本轮未附加" in str(prepared.messages)
    assert len(context.get_context()[0]["content"]) == 16
    previous = {"role": "tool", "tool_call_id": "before", "content": [part] * 16}
    current = {"role": "tool", "tool_call_id": "current", "content": [part] * 16}
    _, projected = limit_history_media([], [previous, current])
    assert "历史画面本轮未附加" in str(projected[0])
    assert projected[1] == current
    with pytest.raises(ValueError, match="本轮新读取"):
        limit_history_media([], [{"role": "user", "content": [part] * 17}])
    context.close()


@pytest.mark.parametrize("format_name", ["GIF", "WEBP", "PNG"])
def test_animated_image_sampling_respects_container_durations(tmp_path, format_name):
    """动画按真实播放时长取样, APNG 默认静态封面不混入动画时间线"""
    from satrap.core.utils.media_store import media_scope
    from satrap.core.utils.media_frames import sample_frames

    output = io.BytesIO()
    red, blue = Image.new("RGBA", (8, 8), "red"), Image.new("RGBA", (8, 8), "blue")
    if format_name == "PNG":
        Image.new("RGBA", (8, 8), "green").save(output, format="PNG", save_all=True,
            append_images=[red, blue], duration=500, default_image=True)
    else:
        red.save(output, format=format_name, save_all=True, append_images=[blue], duration=500, lossless=True)
    mime = {"GIF": "image/gif", "WEBP": "image/webp", "PNG": "image/png"}[format_name]
    url = f"data:{mime};base64," + base64.b64encode(output.getvalue()).decode()
    model = SimpleNamespace(supports_visual_input=True, video_frame_interval=0.5)
    with media_scope(str(tmp_path / "platform.db"), "animation"):
        content = user_media_content("动画", [url], None, model)
        prepared = project_messages([{"role": "user", "content": content}], True, model)
        segment = sample_frames(output.getvalue(), "animation", 0.5, 0.25, 0.4)
    assert len(segment["frames"]) == 1 and segment["frames"][0]["time"] == 0.25
    with Image.open(io.BytesIO(segment["frames"][0]["data"])) as frame:
        red_value, green_value, blue_value = frame.getpixel((0, 0))
        assert red_value > 200 and green_value < 30 and blue_value < 30
    assert "总时长 1.00 秒" in str(prepared)
    assert "0.500 秒" in str(prepared)
    images = [part for part in prepared[0]["content"] if part["type"] == "image_url"]
    assert len(images) == 2
    with Image.open(io.BytesIO(base64.b64decode(images[0]["image_url"]["url"].split(",")[1]))) as first:
        red_value, green_value, blue_value = first.getpixel((0, 0))
        assert red_value > 200 and green_value < 30 and blue_value < 30

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
    from satrap.core.utils.media_store import current_store
    assert current_store().read(content[1]["video_url"]["url"])[0] == b"video-before"

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

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_context_manager_persists_multimodal_content(tmp_path: Path, asynchronous: bool):
    """同步与异步上下文均把多模态内容持久化并按图片计费"""
    db_path = tmp_path / "history.db"
    if asynchronous:
        ctx = AsyncContextManager("vision", db_path=str(db_path))
        await ctx.initialize()
        await ctx.add_user_message("图片里有什么", img_urls=[_tiny_png_data_url()])
        loaded: ContextManager | AsyncContextManager = AsyncContextManager("vision", db_path=str(db_path))
        await loaded.initialize()
    else:
        ctx = ContextManager("vision", db_path=str(db_path))
        ctx.add_user_message("图片里有什么", img_urls=[_tiny_png_data_url()])
        loaded = ContextManager("vision", db_path=str(db_path))

    message = loaded.get_context()[0]
    assert message["role"] == "user"
    assert isinstance(message["content"], list)
    if asynchronous:
        assert message["content"][1]["type"] == "image_url"
    else:
        assert message["content"][0]["text"] == "图片里有什么"
        assert loaded.estimate_token(method="experience") >= DEFAULT_IMAGE_TOKEN_COST

def test_context_manager_loads_legacy_text_rows(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    ctx = ContextManager("legacy", db_path=str(db_path))
    ctx.add_user_message("旧消息")

    loaded = ContextManager("legacy", db_path=str(db_path))

    assert loaded.get_context()[0]["content"] == "旧消息"

class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs: Any):
        self.kwargs = kwargs
        return {"choices": [{"message": {"content": "ok"}}]}

class _FakeAsyncCompletions:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs: Any):
        self.kwargs = kwargs
        return {"choices": [{"message": {"content": "ok"}}]}

def _make_llm(fake_completions: _FakeCompletions) -> LLM:
    llm = LLM.__new__(LLM)
    # 测试替身非真实 OpenAI 客户端, 边界断言集中在工厂内
    llm.client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=fake_completions)))
    llm.model = "mock"
    llm.temperature = 0.7
    llm.top_p = 0.95
    llm.max_tokens = 1000
    llm.suppress_error = True
    llm.return_false = False
    llm.thinking_field_name = "reasoning_content"
    llm.thinking_fields = None
    llm.omit_none_thinking_fields = False
    llm.supports_visual_input = True
    return llm

def _make_async_llm(fake_completions: _FakeAsyncCompletions) -> AsyncLLM:
    """异步 LLM 替身工厂: 边界断言集中在此"""
    llm = AsyncLLM.__new__(AsyncLLM)
    llm.client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=fake_completions)))
    llm.model = "mock"
    llm.temperature = 0.7
    llm.top_p = 0.95
    llm.max_tokens = 1000
    llm.suppress_error = True
    llm.return_false = False
    llm.thinking_field_name = "reasoning_content"
    llm.thinking_fields = None
    llm.omit_none_thinking_fields = False
    llm.supports_visual_input = True
    return llm

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_llm_call_appends_images_to_last_user_message(asynchronous: bool):
    """同步与异步 LLM 都把图片追加到最后一条 user 消息"""
    if asynchronous:
        fake = _FakeAsyncCompletions()
        response = await _make_async_llm(fake).call(
            [{"role": "user", "content": "看图"}], img_urls=[_tiny_png_data_url()],
        )
        assert isinstance(response, LLMCallResponse)
        assert fake.kwargs is not None
        assert fake.kwargs["messages"][0]["content"][1]["type"] == "image_url"
    else:
        fake = _FakeCompletions()
        response = _make_llm(fake).call(
            [{"role": "user", "content": "第一张"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "第二张"}],
            img_urls=[_tiny_png_data_url()],
        )
        assert isinstance(response, LLMCallResponse)
        assert fake.kwargs is not None
        messages = fake.kwargs["messages"]
        assert messages[0]["content"] == "第一张"
        assert isinstance(messages[-1]["content"], list)
        assert messages[-1]["content"][1]["type"] == "image_url"

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

def test_normalize_openai_base_url_variants() -> None:
    """归一化常见端点路径; 远程默认要求 HTTPS, 回环/显式例外放行, 其它协议或带凭据拒绝"""
    assert normalize_openai_base_url("https://api.siliconflow.cn/v1/chat/completions") == "https://api.siliconflow.cn/v1"
    assert normalize_openai_base_url("https://api.siliconflow.cn/v1/audio/transcriptions") == "https://api.siliconflow.cn/v1"
    assert normalize_openai_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    assert normalize_openai_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert normalize_openai_base_url("http://model.internal/v1", allow_insecure=True) == "http://model.internal/v1"

    with pytest.raises(ValueError, match="必须使用 https"):
        normalize_openai_base_url("http://model.internal/v1")
    with pytest.raises(ValueError, match="仅支持 http 或 https"):
        normalize_openai_base_url("ftp://model.internal/v1")
    with pytest.raises(ValueError, match="用户名或密码"):
        normalize_openai_base_url("https://user:secret@model.example/v1")

@pytest.mark.integration
@pytest.mark.requires_api
def test_real_toolkit_vision_call_when_available():
    values = _load_toolkit_config()
    llm = LLM(
        api_key=values["api key"],
        base_url=values["base url"],
        model=values["model"],
        max_tokens=200,
        thinking_fields=None,
    )

    response = llm.call(
        [{"role": "user", "content": "请用一句中文描述这张图片的主要内容"}],
        img_urls=[str(Path(".toolkit/IMG_0114..JPG"))],
    )

    assert isinstance(response, LLMCallResponse)
    assert response.content.strip()

@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "scan.pdf"
    with pdfium.PdfDocument.new() as document:
        for _ in range(3):
            page = document.new_page(1200, 1800)
            page.close()
        document.save(str(path))
    return path

def test_pdf_visual_pages_are_real_images_with_page_metadata(pdf_path):
    result = read_pdf_pages(pdf_path, visual=True, start_page=2, page_count=1)
    assert isinstance(result, dict)
    assert result["metadata"] == {
        "total_pages": 3, "start_page": 2, "end_page": 2,
        "next_page": 3, "text_truncated": False,
    }
    assert "没有可提取的文本层" in result["text"]
    assert "第 2 页" in result["media"][0]["text"]
    data = base64.b64decode(result["media"][1]["image_url"]["url"].split(",")[1])
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 1600
        image.verify()

@pytest.mark.parametrize("start,count", [(0, 1), (4, 1), (1, 0), (1, 6), (True, 1), (1, 1.5)])
def test_pdf_invalid_page_selection_fails(pdf_path, start, count):
    with pytest.raises(ValueError):
        read_pdf_pages(pdf_path, visual=True, start_page=start, page_count=count)

@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
async def test_document_modes_follow_current_model(pdf_path, asynchronous, enabled, monkeypatch):
    cls = AsyncReadDocumentTool if asynchronous else ReadDocumentTool
    tool = cls(pdf_path.parent)
    monkeypatch.setattr(tool, "_session", SimpleNamespace(llm=SimpleNamespace(supports_visual_input=enabled)), raising=False)

    async def execute(mode):
        if isinstance(tool, AsyncReadDocumentTool):
            return await tool.execute(str(pdf_path), mode=mode, start_page=1, page_count=1)
        return tool.execute(str(pdf_path), mode=mode, start_page=1, page_count=1)

    automatic = await execute("auto")
    assert isinstance(automatic, dict) is enabled
    text = await execute("text")
    assert isinstance(text, str)
    assert "没有可提取的文本层" in text
    visual = await execute("visual")
    if enabled:
        assert isinstance(visual, dict)
    else:
        assert "未启用图像与视频输入" in visual

def test_pdf_text_mode_does_not_render_pages(pdf_path, monkeypatch):
    def fail_render(*args, **kwargs):
        raise AssertionError("文本模式不应渲染图片")

    monkeypatch.setattr(pdfium.PdfPage, "render", fail_render)
    result = read_pdf_pages(pdf_path, visual=False, start_page=3)
    assert isinstance(result, str)
    assert "本次读取 3-3 页" in result
    assert "下一页" not in result


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_local_image_document_flow_preserves_visual_content_and_scope(tmp_path, monkeypatch, asynchronous):
    workspace = tmp_path / "project"
    workspace.mkdir()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    image_path = workspace / "plot.png"
    Image.new("RGBA", (30, 20), (255, 0, 64, 128)).save(image_path)
    external = tmp_path / "private.png"
    Image.new("RGB", (4, 4)).save(external)
    upload = uploads / "unique_upload.png"
    Image.new("RGB", (12, 8)).save(upload)
    model = SimpleNamespace(supports_visual_input=True)
    tool = (AsyncReadDocumentTool if asynchronous else ReadDocumentTool)(workspace)
    document_session = SimpleNamespace(llm=model)
    bind_environment(document_session, replace(environment_snapshot(document_session), upload_root=uploads))
    monkeypatch.setattr(tool, "_session", document_session, raising=False)

    async def read(path, mode="auto"):
        result = tool.execute(str(path), mode=mode)
        return await result if asynchronous else result

    result = await read(image_path)
    frozen = freeze_tool_result(result, model)
    assert frozen["satrap_media_result"] == 1
    assert "30 x 20" in frozen["text"]
    messages = []
    call = {"id": "local-image", "type": "function", "function": {"name": "read_document", "arguments": "{}"}}
    add_tools_call_flow(messages, "", [call], [frozen])
    prepared = prepare_call_messages(messages, None, None, supports_visual_input=True)
    assert [message["role"] for message in prepared] == ["assistant", "tool", "user"]
    assert prepared[-1]["content"][-1]["image_url"]["url"] == frozen["media"][0]["image_url"]["url"]
    raw = base64.b64decode(frozen["media"][0]["image_url"]["url"].split(",")[1])
    with Image.open(io.BytesIO(raw)) as picture:
        assert picture.size == (30, 20)
        assert picture.getpixel((0, 0)) == (255, 0, 64, 128)
    assert isinstance(await read("upload.png", "visual"), dict)
    assert "越出当前会话可见范围" in await read(external)
    assert "auto 或 visual" in await read(image_path, "text")
    assert "不存在" in await read(workspace / "missing.png")
    broken = workspace / "broken.jpg"
    broken.write_bytes(b"not an image")
    assert "图片读取失败" in await read(broken)
    gif = workspace / "animation.gif"
    Image.new("RGB", (8, 8), "red").save(gif, save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")])
    animation = freeze_tool_result(await read(gif), model)
    assert "按模型配置采样" in animation["text"]
    frames = project_messages([{"role": "user", "content": animation["media"]}], True, model)[0]["content"]
    assert sum(part["type"] == "image_url" for part in frames) == 2
    model.supports_visual_input = False
    assert "启用模型" in await read(image_path)
