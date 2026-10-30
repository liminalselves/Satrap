"""Responses 的 SDK, 工具循环, 存储和独立库调用整合验收"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys
import textwrap
from typing import Any

import httpx
from openai import OpenAI, AsyncOpenAI
import pytest

from satrap import LLM, AsyncLLM, ModelWorkflowFramework, AsyncModelWorkflowFramework, Tool, AsyncTool, ToolsManager, AsyncToolsManager
from satrap.core.APICall.LLMCall import build_llm_from_config
from satrap.core.state import StateStore
from satrap.core.type import LLMConfig, StateScope
from satrap.core.utils.context import ContextManager, AsyncContextManager
from satrap.core.utils.model_history import attach_protocol_payload


def response(output: list[dict[str, object]] | None = None, **changes: object) -> dict[str, object]:
    """
    构造真实 SDK 可解析的服务响应

    参数:
    - output: 本次输出项, 默认普通文本
    - changes: 终态或错误字段覆盖

    返回:
    - 不包含认证资料的协议响应
    """
    return {"id": "resp_test", "object": "response", "created_at": 1, "status": "completed", "model": "resolved-version",
            "output": output if output is not None else [{"type": "message", "id": "msg_final", "role": "assistant", "status": "completed",
                "phase": "final_answer", "content": [{"type": "output_text", "text": "结果是 3", "annotations": []}]}],
            "usage": {"input_tokens": 21, "output_tokens": 8, "total_tokens": 29, "input_tokens_details": {"cached_tokens": 7}}, **changes}


def tool_output() -> list[dict[str, object]]:
    """
    提供带推理和说明的工具调用

    返回:
    - 调用 ID 与输出项 ID 不同的有序输出
    """
    return [{"type": "reasoning", "id": "rs_test", "summary": [{"type": "summary_text", "text": "需要计算"}], "encrypted_content": "encrypted-test"},
            {"type": "message", "id": "msg_comment", "role": "assistant", "status": "completed", "phase": "commentary",
             "content": [{"type": "output_text", "text": "正在计算", "annotations": []}]},
            {"type": "function_call", "id": "fc_item", "call_id": "call_test", "name": "add", "arguments": '{"value":2}', "status": "completed"}]


def sse(result: dict[str, object], *, terminal: bool = True) -> bytes:
    """
    生成 SDK 实际消费的 SSE 字节

    参数:
    - result: 完整终态
    - terminal: 是否提供终态, False 用于模拟中途断流

    返回:
    - 流式响应字节
    """
    events: list[dict[str, object]] = [{"type": "response.created", "response": {**result, "status": "in_progress", "output": []}}]
    for index, item in enumerate(result["output"]):
        events.append({"type": "response.output_item.added", "output_index": index, "item": item})
        if item["type"] == "message":
            for part in item["content"]:
                events.append({"type": "response.output_text.delta", "output_index": index, "item_id": item["id"], "content_index": 0, "delta": part["text"]})
        if item["type"] == "function_call":
            for delta in ('{"value":', '2}'):
                events.append({"type": "response.function_call_arguments.delta", "output_index": index, "item_id": item["id"], "delta": delta})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    if terminal:
        events.append({"type": "response." + str(result["status"]), "response": result})
    return "".join(f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': number}, ensure_ascii=False)}\n\n" for number, event in enumerate(events)).encode("utf-8")


@pytest.fixture
def clients(monkeypatch):
    """提供经过真实 SDK 编解码的离线客户端, 不启动任何 Satrap 服务"""
    from satrap.core.APICall.LLMCall import sync, async_

    requests: list[dict[str, Any]] = []
    state: dict[str, Any] = {"result": None, "terminal": True, "http_status": 200}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        requests.append(body)
        if state.get("fail_on_request") == len(requests):
            raise httpx.ReadTimeout("模拟中断", request=request)
        if state["http_status"] != 200:
            return httpx.Response(state["http_status"], json={"error": {"code": "invalid_request", "message": "test"}})
        if state.get("event_error"):
            return httpx.Response(200, content=b'event: error\ndata: {"type":"error","code":"server_error","message":"test"}\n\n', headers={"Content-Type": "text/event-stream"})
        result = state["result"] or response(tool_output() if body.get("tools") and not any(item.get("type") == "function_call_output" for item in body["input"]) else None)
        return httpx.Response(200, content=sse(result, terminal=state["terminal"]), headers={"Content-Type": "text/event-stream"}) if body.get("stream") else httpx.Response(200, json=result)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(sync, "OpenAI", lambda **kwargs: OpenAI(**kwargs, http_client=httpx.Client(transport=transport), max_retries=0))
    monkeypatch.setattr(async_, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport), max_retries=0))
    return requests, state


class AddTool(Tool):
    """用于完整 Agent 调用的普通工具"""

    tool_name = "add"
    description = "加一"
    params_dict = {"value": ("integer", "数值")}

    def execute(self, value: int) -> dict[str, int]:
        """
        执行确定性计算

        参数:
        - value: 输入整数

        返回:
        - 加一结果
        """
        return {"result": value + 1}


class AsyncAddTool(AsyncTool):
    """异步 Agent 调用的加一工具"""

    tool_name = "add"
    description = "加一"
    params_dict = {"value": ("integer", "数值")}

    async def execute(self, value: int) -> dict[str, int]:
        """
        执行确定性异步计算

        参数:
        - value: 输入整数

        返回:
        - 加一结果
        """
        return {"result": value + 1}


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_agent_tool_loop_replays_items_and_survives_reload(clients, tmp_path, async_mode, stream):
    requests, _ = clients
    config = LLMConfig(api_key="test", model="example", base_url="https://models.test/v1", api_type="responses")
    llm = build_llm_from_config(config, async_=async_mode)
    manager = AsyncToolsManager() if async_mode else ToolsManager()
    manager.register_tool(AsyncAddTool() if async_mode else AddTool())
    cls = AsyncModelWorkflowFramework if async_mode else ModelWorkflowFramework
    workflow = cls(llm, "independent", manager, db_path=str(tmp_path/"history.sqlite"), recoverable=True)
    if async_mode:
        await workflow.initialize()
    try:
        if async_mode:
            from satrap.core.framework.Base.execution.engine import run_async
            result = await run_async(workflow, user_input="计算 2 加一", stream=stream)
        else:
            from satrap.core.framework.Base.execution.engine import run_sync
            result = run_sync(workflow, user_input="计算 2 加一", stream=stream)
        assert result == "结果是 3"
        assert len(requests) == 2
        request = requests[1]
        assert request["store"] is False and request["truncation"] == "disabled"
        assert request["tools"][0]["strict"] is False
        assert "messages" not in request and "stream_options" not in request
        assert "temperature" not in request and "top_p" not in request
        assert any(item.get("encrypted_content") == "encrypted-test" for item in request["input"])
        assert any(item.get("phase") == "commentary" for item in request["input"])
        assert any(item.get("type") == "function_call_output" and item["call_id"] == "call_test" for item in request["input"])
        if async_mode:
            loaded = AsyncContextManager("independent", db_path=str(tmp_path/"history.sqlite"))
            await loaded.initialize()
        else:
            loaded = ContextManager("independent", db_path=str(tmp_path/"history.sqlite"))
        payloads = [message["protocol_payload"] for message in loaded.get_context() if message.get("protocol_payload")]
        assert len(payloads) == 2
        assert payloads[0]["model"] == "example"
        assert payloads[0]["items"][0]["encrypted_content"] == "encrypted-test"
        await llm.call(loaded.get_context()) if async_mode else llm.call(loaded.get_context())
        assert any(item.get("phase") == "final_answer" for item in requests[-1]["input"])
    finally:
        await llm.client.close() if async_mode else llm.client.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("method", ["chat", "call", "structured_output", "stream_chat", "stream_call"])
async def test_public_entry_points_and_media_preserve_input(clients, async_mode, method):
    requests, state = clients
    state["result"] = response()
    llm = (AsyncLLM if async_mode else LLM)(api_key="test", model="example", base_url="https://models.test/v1", api_type="responses")
    messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "问题"}]
    original = copy.deepcopy(messages)
    kwargs = {"format": {"result": "str"}} if method == "structured_output" else {"thinking": "high"}
    if method in {"call", "stream_call"}:
        llm.supports_visual_input = True
        kwargs["img_urls"] = ["data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="]
    try:
        pending = getattr(llm, method)(messages, **kwargs)
        result = [item async for item in pending] if async_mode and method.startswith("stream") else list(pending) if method.startswith("stream") else await pending if async_mode else pending
        assert messages == original
        assert requests[-1]["max_output_tokens"] == 1000
        if method == "structured_output":
            assert requests[-1]["text"]["format"] == {"type": "json_object"}
            assert "result" in requests[-1]["input"][0]["content"]
        else:
            assert requests[-1]["reasoning"] == {"effort": "high", "summary": "auto"}
        if method in {"call", "stream_call"}:
            assert requests[-1]["input"][-1]["content"][-1]["type"] == "input_image"
            parsed = result[-1].response if method.startswith("stream") else result
            assert parsed.content == "结果是 3" and parsed.usage.cached_tokens == 7
            image_url = requests[-1]["input"][-1]["content"][-1]["image_url"]
            tool_history = [{"role": "user", "content": "读取图片"},
                            {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "id": "image-call", "function": {"name": "read_image", "arguments": "{}"}}]},
                            {"role": "tool", "tool_call_id": "image-call", "content": [{"type": "text", "text": "工具图片"}, {"type": "image_url", "image_url": {"url": image_url}}]}]
            await llm.call(tool_history) if async_mode else llm.call(tool_history)
            assert requests[-1]["input"][-1]["type"] == "function_call_output"
            assert requests[-1]["input"][-1]["output"][-1]["type"] == "input_image"
        else:
            assert result == ["结果是 3"] if method.startswith("stream") else result == "结果是 3"
        for fields, omit_none in ((None, False), (["reasoning_effort"], False), (["reasoning_effort"], True), (["thinking.type", "enable_thinking"], True)):
            llm.thinking_fields = fields
            llm.omit_none_thinking_fields = omit_none
            off_kwargs = {key: value for key, value in kwargs.items() if key != "thinking"}
            pending = getattr(llm, method)(messages, **off_kwargs, **({} if method == "structured_output" else {"thinking": "off"}))
            result = [item async for item in pending] if async_mode and method.startswith("stream") else list(pending) if method.startswith("stream") else await pending if async_mode else pending
            assert requests[-1].get("reasoning") == (None if method == "structured_output" else {"effort": "none"})
            assert "extra_body" not in requests[-1] and "reasoning_effort" not in requests[-1]
            assert "thinking" not in requests[-1] and "enable_thinking" not in requests[-1]
            if method in {"call", "stream_call"}:
                parsed = result[-1].response if method.startswith("stream") else result
                assert parsed.type == "message" and parsed.content == "结果是 3"
            else:
                assert result == ["结果是 3"] if method.startswith("stream") else result == "结果是 3"
        pending = getattr(llm, method)(messages, **{key: value for key, value in kwargs.items() if key != "thinking"})
        _ = [item async for item in pending] if async_mode and method.startswith("stream") else list(pending) if method.startswith("stream") else await pending if async_mode else pending
        assert "reasoning" not in requests[-1]
    finally:
        await llm.client.close() if async_mode else llm.client.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("failure", ["http", "failed", "limit", "disconnect", "arguments"])
async def test_failures_do_not_become_successful_tool_calls(clients, async_mode, failure):
    _, state = clients
    result = response(tool_output())
    if failure == "http":
        state["http_status"] = 400
    elif failure == "failed":
        result.update(status="failed", error={"code": "server_error", "message": "credential must never escape"})
    elif failure == "limit":
        result.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    elif failure == "disconnect":
        state["terminal"] = False
    else:
        result["output"][-1]["arguments"] = '{"value":'
    state["result"] = result
    llm = (AsyncLLM if async_mode else LLM)(api_key="test", model="example", base_url="https://models.test/v1", api_type="responses")
    try:
        pending = llm.stream_call([{"role": "user", "content": "执行"}])
        events = [item async for item in pending] if async_mode else list(pending)
        parsed = events[-1].response
        if failure == "arguments":
            assert parsed.tool_calls[0]["argument_error"]
        else:
            assert parsed.error_info is not None and parsed.type == "error"
            assert parsed.tool_calls is None
            assert "credential" not in parsed.content
    finally:
        await llm.client.close() if async_mode else llm.client.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_video_wire_transport_history_and_tool_projection(clients, tmp_path, monkeypatch, async_mode, stream):
    """原生视频实际进入 SDK 请求, 两种传输均保持归属且工具视频位于用户输入"""
    from types import SimpleNamespace
    from satrap.core.utils import media, outbound
    from satrap.core.utils.media_store import media_scope, current_store

    requests, state = clients
    state["result"] = response()
    data = b'\x00\x00\x00\x18ftypisom' + bytes(32)
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    downloads = []
    def download(url, **kwargs):
        downloads.append(url)
        return SimpleNamespace(content=data, url=url, headers={"content-type": "video/mp4"}, raise_for_status=lambda: None)
    monkeypatch.setattr(media, "safe_sync_get", download)
    monkeypatch.setattr(outbound, "validate_outbound_http_url", lambda url: url if url.startswith("https://cdn.test/") else (_ for _ in ()).throw(ValueError("拒绝非公网来源")))
    llm = (AsyncLLM if async_mode else LLM)(api_key="test", model="example", base_url="https://models.test/v1", api_type="responses", supports_native_video_input=True)

    async def invoke(messages, **kwargs):
        pending = llm.stream_call(messages, **kwargs) if stream else llm.call(messages, **kwargs)
        if stream:
            events = [event async for event in pending] if async_mode else list(pending)
            return events[-1].response
        return await pending if async_mode else pending

    try:
        result = await invoke([{"role": "user", "content": "看视频"}], video_urls=[str(path)])
        assert result.content == "结果是 3"
        part = requests[-1]["input"][-1]["content"][-1]
        assert part["type"] == "input_video" and part["video_url"].startswith("data:video/mp4;base64,")
        assert "fps" not in part and "file_id" not in part
        with media_scope(str(tmp_path / "history.sqlite"), "owner"):
            frozen = media.user_media_content("看视频", None, ["https://cdn.test/clip.mp4"], llm)
            original = copy.deepcopy(frozen)
            llm.set_parameters(native_video_transport="url")
            projected = media.project_context_media([{"role": "user", "content": frozen}], llm, str(tmp_path / "history.sqlite"), "owner")
            assert (await invoke(projected)).content == "结果是 3"
            assert frozen == original and downloads == ["https://cdn.test/clip.mp4"]
            part = requests[-1]["input"][-1]["content"][-1]
            assert part == {"type": "input_video", "video_url": "https://cdn.test/clip.mp4"}
            tool_history = [{"role": "user", "content": "读取视频"},
                            {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "id": "video-call", "function": {"name": "read_video", "arguments": "{}"}}]},
                            {"role": "tool", "tool_call_id": "video-call", "content": frozen}]
            await invoke(tool_history)
            items = requests[-1]["input"]
            assert items[-2]["type"] == "function_call_output"
            assert all(part.get("type") != "input_video" for part in items[-2]["output"])
            assert items[-1]["role"] == "user" and "video-call" in items[-1]["content"][0]["text"]
            assert items[-1]["content"][-1]["type"] == "input_video"
            assert len(downloads) == 1
            path.write_bytes(data + b"local-only")
            with pytest.raises(ValueError, match="公网"):
                await invoke([{"role": "user", "content": "本地视频"}], video_urls=[str(path)])
            reference = frozen[1]["video_url"]["url"]
            from satrap.core.utils.media_store import MediaStore
            other = MediaStore(current_store().root, "fork")
            current_store().copy_to(other)
            assert other.public_url(reference) == "https://cdn.test/clip.mp4"
            with media_scope(str(tmp_path / "history.sqlite"), "unrelated"):
                with pytest.raises(ValueError):
                    await invoke([{"role": "user", "content": frozen}])
    finally:
        await llm.client.close() if async_mode else llm.client.close()


def test_protocol_payload_checkpoint_fork_and_edit(clients, tmp_path):
    _, state = clients
    state["result"] = response(tool_output())
    llm = LLM(api_key="test", model="example", base_url="https://models.test/v1", api_type="responses")
    try:
        parsed = llm.call([{"role": "user", "content": "计算"}])
        context = ContextManager("original", db_path=str(tmp_path/"checkpoint.sqlite"), enable_checkpoint=True)
        context.add_bot_message(parsed.content, protocol_payload=parsed.protocol_payload)
        context.save_context()
        checkpoint = context.create_checkpoint(name="原始")
        branch = context.state_store.fork(checkpoint.checkpoint_id, new_scope_id="branch")
        loaded = ContextManager("branch", db_path=str(tmp_path/"checkpoint.sqlite"))
        assert loaded.get_context()[0]["protocol_payload"]["items"] == tool_output()
        item = copy.deepcopy(loaded.get_context()[0])
        item["content"] = "编辑后的内容"
        encoded = llm.protocol.encode_messages([item], "example")
        assert not any(output.get("type") == "reasoning" for output in encoded)
        assert llm.protocol.encode_messages(loaded.get_context(), "different-model")[0]["content"] == parsed.content
        snapshot = context.state_store.create_checkpoint(StateScope("conversation", "original"), materialize=True)
        context.state_store.fork(snapshot.checkpoint_id, new_scope_id="snapshot-branch")
        snapshot_context = ContextManager("snapshot-branch", db_path=str(tmp_path/"checkpoint.sqlite"))
        assert snapshot_context.get_context()[0]["protocol_payload"] == loaded.get_context()[0]["protocol_payload"]
        from satrap.core.storage.database import snapshot_session_domain, delete_session_domain_rows, restore_session_domain
        archived = snapshot_session_domain(tmp_path/"checkpoint.sqlite", "original")
        context.close()
        delete_session_domain_rows(tmp_path/"checkpoint.sqlite", "original")
        restore_session_domain(tmp_path/"checkpoint.sqlite", "original", archived)
        restored = ContextManager("original", db_path=str(tmp_path/"checkpoint.sqlite"))
        assert restored.get_context()[0]["protocol_payload"] == loaded.get_context()[0]["protocol_payload"]
    finally:
        llm.client.close()


@pytest.mark.parametrize("async_mode", [False, True])
async def test_responses_debug_and_metering_track_terminal_failures(clients, tmp_path, monkeypatch, async_mode):
    from satrap.core.telemetry import model
    from satrap.core.telemetry.capture import TelemetryWriter, bind_scope
    from satrap.core.telemetry.contracts import MetricScope
    from satrap.core.telemetry.health import ServiceRuntime
    from satrap.core.telemetry.store import TelemetryStore, SettingsStore
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.contracts import DebugObject
    from satrap.core.log.debug.policy import DebugPolicy
    from satrap.core.log.debug.store import DebugStore

    requests, state = clients
    state["result"] = response()
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path/"config"))
    debug_writer = capture.DebugWriter(DebugStore(tmp_path/"debug.sqlite", "backend"))
    meter = TelemetryStore(tmp_path/"meter.sqlite", "backend")
    writer = TelemetryWriter(meter, ServiceRuntime("backend"), settings_store=SettingsStore(tmp_path/"settings.json"))
    assert writer.ready.wait(3)
    monkeypatch.setattr(model, "current_writer", lambda: writer)
    monkeypatch.setattr(capture, "debug_writer", lambda: debug_writer)
    capture.policy_store().save(DebugPolicy(default_enabled=True), DebugPolicy().revision)
    obj = DebugObject("qq", "bot", "group", "room")
    llm = (AsyncLLM if async_mode else LLM)(api_key="test", model="example", base_url="https://models.test/v1", api_type="responses")
    try:
        with bind_scope(MetricScope(platform_id="qq", session_id="session")):
            for name, status, terminal in (("success", "completed", True), ("failed", "failed", True), ("disconnect", "completed", False), ("event-error", "completed", False), ("cancelled", "completed", True)):
                state["result"] = response(status=status)
                state["terminal"] = terminal
                state["event_error"] = name == "event-error"
                with capture.debug_request(obj, name):
                    pending = llm.stream_call([{"role": "user", "content": name}])
                    if name == "cancelled":
                        first = await anext(pending) if async_mode else next(pending)
                        assert first.kind == "content_delta"
                        await pending.aclose() if async_mode else pending.close()
                        continue
                    events = [event async for event in pending] if async_mode else list(pending)
                    assert (events[-1].response.error_info is None) == (name == "success")
        assert len(requests) == 5
        assert debug_writer.flush()
        ends = [[event for event in debug_writer.store.events(name)["events"] if event["category"] == "model" and event["phase"] == "end"] for name in ("success", "failed", "disconnect", "event-error", "cancelled")]
        assert [events[0]["status"] for events in ends] == ["completed", "failed", "failed", "failed", "cancelled"]
        assert ends[0][0]["metadata"]["usage"]["total_tokens"] == 29
        entries = debug_writer.store.events("success")["events"]
        start = next(event for event in entries if event["category"] == "model" and event["phase"] == "start")
        assert debug_writer.store.body("success", start["event_id"], "input")["input"] == requests[0]["input"]
        chunks = [event for event in entries if "output_chunk" in event["body_slots"]]
        assert "".join(debug_writer.store.body("success", event["event_id"], "output_chunk")[0]["content"] for event in chunks) == "结果是 3"
    finally:
        writer.close()
        debug_writer.close()
        await llm.client.close() if async_mode else llm.client.close()
    with meter.connect() as connection:
        rows = connection.execute("SELECT status,usage_json FROM model_calls ORDER BY started_at").fetchall()
        assert [row["status"] for row in rows] == ["succeeded", "failed", "failed", "failed", "cancelled"]
        assert json.loads(rows[0]["usage_json"])["cached_tokens"] == 7


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_interrupted_agent_resumes_without_repeating_tool(clients, tmp_path, async_mode, stream):
    from satrap.core.framework.Base.execution.engine import run_sync, run_async
    from satrap import ModelCallError

    requests, state = clients
    executions: list[int] = []

    class CountedTool(AddTool):
        def execute(self, value: int) -> dict[str, int]:
            executions.append(value)
            return super().execute(value)

    class CountedAsyncTool(AsyncAddTool):
        async def execute(self, value: int) -> dict[str, int]:
            executions.append(value)
            return await super().execute(value)

    manager = AsyncToolsManager() if async_mode else ToolsManager()
    manager.register_tool(CountedAsyncTool() if async_mode else CountedTool())
    model_class = AsyncLLM if async_mode else LLM
    workflow_class = AsyncModelWorkflowFramework if async_mode else ModelWorkflowFramework
    llm = model_class(api_key="test", model="example", base_url="https://models.test/v1", api_type="responses")
    workflow = workflow_class(llm, "resume", manager, db_path=str(tmp_path/"resume.sqlite"), recoverable=True)
    if async_mode:
        await workflow.initialize()
    state["fail_on_request"] = 2
    try:
        with pytest.raises(ModelCallError):
            await run_async(workflow, user_input="计算", stream=stream) if async_mode else run_sync(workflow, user_input="计算", stream=stream)
        assert executions == [2]
        run_id = workflow.last_run_id
        assert run_id
        if not async_mode:
            workflow.ctx.close()
        recovered = workflow_class(llm, "resume", manager, db_path=str(tmp_path/"resume.sqlite"), recoverable=True)
        if async_mode:
            await recovered.initialize()
        state["fail_on_request"] = None
        result = await run_async(recovered, run_id=run_id, callback=False) if async_mode else run_sync(recovered, run_id=run_id, callback=False)
        assert result == "结果是 3" and executions == [2]
        assert len(requests) == 3
        assert any(item.get("encrypted_content") == "encrypted-test" for item in requests[-1]["input"])
        assert sum(message["role"] == "user" for message in recovered.ctx.get_context()) == 1
    finally:
        await llm.client.close() if async_mode else llm.client.close()


def test_components_run_without_backend_or_telemetry(tmp_path):
    source = textwrap.dedent('''
        import asyncio, importlib.abc, json, sys
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        class NoHost(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith(("satrap.core.backend", "satrap.core.telemetry", "satrap.display.server")):
                    raise AssertionError("底层组件依赖了服务模块: " + fullname)
        sys.meta_path.insert(0, NoHost())
        import httpx
        from openai import OpenAI, AsyncOpenAI
        from satrap import LLM, AsyncLLM, Tool, AsyncTool, ToolsManager, AsyncToolsManager
        from satrap import Session, AsyncSession, ModelWorkflowFramework, AsyncModelWorkflowFramework
        from satrap.core.APICall.LLMCall import sync, async_
        requests = []
        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            if request.url.path.endswith('/responses'):
                done = any(item.get('type') == 'function_call_output' for item in body['input'])
                output = [{'type':'message','id':'m','role':'assistant','status':'completed','phase':'final_answer','content':[{'type':'output_text','text':'3','annotations':[]}]}] if done else [{'type':'function_call','id':'f','call_id':'c','name':'add','arguments':'{"value":2}'}]
                value = {'id':'r','created_at':1,'object':'response','status':'completed','model':'example','output':output}
            else:
                done = any(item.get('role') == 'tool' for item in body['messages'])
                message = {'role':'assistant','content':'3'} if done else {'role':'assistant','content':'','tool_calls':[{'id':'c','type':'function','function':{'name':'add','arguments':'{"value":2}'}}]}
                value = {'id':'r','created':1,'object':'chat.completion','model':'example','choices':[{'index':0,'finish_reason':'stop' if done else 'tool_calls','message':message}]}
            return httpx.Response(200,json=value)
        sync.OpenAI = lambda **kw: OpenAI(**kw,http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        async_.AsyncOpenAI = lambda **kw: AsyncOpenAI(**kw,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        class Add(Tool):
            tool_name='add'
            description='加一'
            params_dict={'value':('integer','数值')}
            def execute(self,value): return {'result':value+1}
        class AAdd(AsyncTool):
            tool_name='add'
            description='加一'
            params_dict={'value':('integer','数值')}
            async def execute(self,value): return {'result':value+1}
        class Chat(Session):
            def run(self,message): return self.workflow.full_agent(message,callback=False)
        class AChat(AsyncSession):
            async def run(self,message): return await self.workflow.full_agent(message,callback=False)
        async def exercise():
            for protocol in ('chat_completions','responses'):
                llm=LLM(api_key='test',base_url='https://models.test/v1',model='example',api_type=protocol)
                tools=ToolsManager(); tools.register_tool(Add())
                workflow=ModelWorkflowFramework(llm,'sync-'+protocol,tools,db_path='unused.sqlite',persist_context=False)
                chat=Chat('session-'+protocol,db_path='explicit-session.sqlite')
                chat.workflow=workflow
                assert chat.run('计算') == '3'
                chat.session_ctx.close(); llm.client.close()
                llm=AsyncLLM(api_key='test',base_url='https://models.test/v1',model='example',api_type=protocol)
                tools=AsyncToolsManager(); tools.register_tool(AAdd())
                workflow=AsyncModelWorkflowFramework(llm,'async-'+protocol,tools,db_path='unused.sqlite',persist_context=False)
                await workflow.initialize()
                chat=AChat('async-session-'+protocol,db_path='explicit-async-session.sqlite')
                await chat.initialize(); chat.workflow=workflow
                assert await chat.run('计算') == '3'
                await llm.client.close()
            assert len(requests)==8
        asyncio.run(exercise())
        assert not any(name.startswith(('satrap.core.backend','satrap.core.telemetry','satrap.display.server')) for name in sys.modules)
        assert not Path('unused.sqlite').exists()
        assert not list(Path('.').rglob('config.yaml'))
        print('standalone components passed')
    ''')
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "SATRAP_CONFIG_ROOT": str(tmp_path/"config"), "SATRAP_DATA_ROOT": str(tmp_path/"data")}
    completed = subprocess.run([sys.executable, "-c", source, str(Path(__file__).resolve().parents[2])], cwd=tmp_path,
                               env=environment, text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "standalone components passed" in completed.stdout


def test_plaintext_reasoning_without_summary_is_shown_as_thinking():
    """不生成摘要的服务商只返回 reasoning_text, 流式与终态都作为思考内容展示"""
    from satrap.core.APICall.LLMCall.protocols.responses import ResponsesProtocol
    decoder = ResponsesProtocol("https://models.test/v1").new_stream_decoder()
    events = [{"type": "response.reasoning_text.delta", "item_id": "rs", "delta": "先想"},
              {"type": "response.output_text.delta", "delta": "结果"},
              {"type": "response.completed", "response": {"id": "r", "status": "completed", "model": "m", "output": [
                  {"type": "reasoning", "id": "rs", "summary": [], "content": [{"type": "reasoning_text", "text": "先想"}]},
                  {"type": "message", "id": "m", "role": "assistant", "content": [{"type": "output_text", "text": "结果"}]}]}}]
    deltas = [(event.kind, event.delta) for chunk in events for event in decoder.consume(chunk)]
    assert deltas == [("thinking_delta", "先想"), ("content_delta", "结果")]
    result = decoder.response()
    assert result.thinking == "先想" and result.content == "结果"
