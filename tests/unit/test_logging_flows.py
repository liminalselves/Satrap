"""业务链路场景与集中边界契约"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import asyncio
import json
import logging
import subprocess
import sys
import time
import os
import pytest
from satrap.core.log.managed import ManagedDailyHandler
from satrap.core.log.policy import LoggingPolicy, LoggingPolicyStore, LoggingPolicyConflict, LogMaintenance
from satrap.core.log.managed import cleanup_logs, active_runtimes, management_lock
from typing import Any, cast
import io
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.log.stream import StandardLogStream, StandardStreamCapture, standard_log_stream
from satrap.core.log import logger

def store_for(tmp_path):
    return LoggingPolicyStore(tmp_path / "logging.json", tmp_path / "logs")


def test_advanced_debug_object_policy_survives_context_replacement_and_reload(tmp_path):
    """对象开关独立持久化, 新上下文与跨进程读取保留覆盖并核验修订和身份隔离"""
    from dataclasses import replace
    from satrap.core.call_context import CallOrigin
    from satrap.core.config.document import ConfigRevisionConflict
    from satrap.core.log.debug.contracts import DebugObject
    from satrap.core.log.debug.policy import DebugPolicy, DebugPolicyStore

    origin = CallOrigin("qq", "bot1", "GroupMessage", "group1", "member1", "msg1", "req1", conversation_kind="group")
    obj = DebugObject.from_origin(origin)
    assert DebugObject.from_origin(replace(origin, actor_id="member2", request_id="req2", conversation_id="new-context")) == obj
    store = DebugPolicyStore(tmp_path / "config" / "debug.json")
    second_process = DebugPolicyStore(store.path)
    initial = store.read()
    assert not initial.resolve(obj).enabled and initial.resolve(obj).mode == "inherit"
    assert not store.path.exists()
    store.set_object(obj, "on", initial.revision)
    active = second_process.read().resolve(obj)
    assert active.enabled and active.mode == "on"
    assert DebugPolicyStore(store.path).read().resolve(obj) == active
    for changed in (replace(obj, platform_id="other"), replace(obj, self_id="bot2"),
                    replace(obj, conversation_kind="private"), replace(obj, target_id="group2")):
        assert not second_process.read().resolve(changed).enabled
    with pytest.raises(ConfigRevisionConflict):
        second_process.set_default(True, initial.revision)
    store.set_default(True, active.revision)
    assert second_process.read().resolve(obj).mode == "on"
    other = replace(obj, target_id="group2")
    assert second_process.read().resolve(other).enabled
    store.set_object(other, "off", store.read().revision)
    assert not second_process.read().resolve(other).enabled
    store.set_object(obj, "inherit", store.read().revision)
    assert second_process.read().resolve(obj).mode == "inherit" and second_process.read().resolve(obj).enabled
    store.set_default(False, store.read().revision)
    assert not second_process.read().resolve(obj).enabled
    assert active.enabled   # 已受理请求继续使用冻结快照
    valid = DebugPolicy().payload()
    for broken in ({**valid, "default_enabled": "false"}, {**valid, "max_body_mb": 100, "max_request_mb": 1},
                   {**valid, "retention_days": False}, {**valid, "objects": [{"object": obj.payload(), "mode": "inherit"}]},
                   {**valid, "objects": [{"object": obj.payload(), "mode": "on"}]*2}):
        with pytest.raises(ValueError):
            DebugPolicy.parse(broken)
    store.path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        second_process.read()


def test_advanced_debug_chain_store_preserves_shared_bodies_and_event_ownership(tmp_path):
    """诊断链路分页不解码正文, 多请求共享及清理保留活跃引用, 损坏内容明确拒绝"""
    from dataclasses import replace
    from satrap.core.log.debug.contracts import DebugEvent, DebugObject
    from satrap.core.log.debug.store import DebugStore

    store = DebugStore(tmp_path / "debug" / "backend.sqlite", "backend")
    assert store.contexts()["contexts"] == [] and not store.path.exists()
    obj = DebugObject("qq", "bot", "group", "group")
    old = time.time() - 9 * 86400
    first = DebugEvent("request-old", obj, "context-before-new", "群标题", "event-old", 0, "model", "start",
                       "model1", "", old, "running", {}, {"input": {"model": "example", "messages": [{"role": "system", "content": "稳定提示"*200}, {"role": "user", "content": "问题"}]}}, "revision")
    store.append([first, replace(first, event_id="end-old", sequence=1, category="request", phase="end", status="success", bodies={})])
    second = replace(first, request_id="request-new", context_id="context-after-new", event_id="event-new", recorded_at=time.time())
    store.append([second])
    assert len(store.contexts()["contexts"]) == 2
    assert store.events("request-new")["events"][0]["body_slots"] == ["input"]
    assert store.body("request-new", "event-new", "input") == second.bodies["input"]
    with pytest.raises(ValueError, match="不属于"):
        store.body("request-old", "event-new", "input")
    with store.connect() as db:
        roots = db.execute("SELECT count(DISTINCT body_key),count(*) FROM debug_event_bodies").fetchone()
        assert tuple(roots) == (1, 2)
    result = store.cleanup(retention_days=7, target_bytes=100_000_000)
    assert result["deleted_requests"] == 1
    assert store.requests()["requests"][0]["request_id"] == "request-new"
    assert store.body("request-new", "event-new", "input") == second.bodies["input"]
    with store.connect(write=True) as db:
        key = db.execute("SELECT body_key FROM debug_event_bodies WHERE event_id='event-new'").fetchone()[0]
        db.execute("UPDATE debug_bodies SET body=? WHERE key=?", (b"broken", key))
    with pytest.raises(ValueError):
        store.body("request-new", "event-new", "input")


def test_advanced_debug_ordered_message_prefixes_share_bounded_sequence_nodes(tmp_path):
    """连续增长的模型消息保留真实顺序, 新前缀只增量保存正文块和目录"""
    import sqlite3
    from satrap.core.log.debug.codec import DebugBodyCodec

    with sqlite3.connect(":memory:") as db:
        db.execute("PRAGMA foreign_keys=ON")
        DebugBodyCodec.initialize(db)
        messages = []
        keys = []
        for index in range(256):
            messages.append({"role": "user", "content": f"消息{index}"})
            codec = DebugBodyCodec(db)
            keys.append(codec.put({"model": "example", "messages": messages}))
        codec = DebugBodyCodec(db)
        for length in (1, 31, 32, 33, 127, 128, 255, 256):
            assert codec.read(keys[length-1]) == {"model": "example", "messages": messages[:length]}
        count = db.execute("SELECT count(*) FROM debug_bodies").fetchone()[0]
        assert count < 256 * 5
        assert codec.read(codec.put({"messages": []})) == {"messages": []}
        escaped = {"content": "\x01" * 1_500_000}
        assert codec.read(codec.put(escaped)) == escaped   # 原始正文未超默认预算, JSON 转义不能导致已采集正文无法读取
        with pytest.raises(ValueError, match="预算"):
            codec.read(keys[-1], max_bytes=10)


def test_advanced_debug_existing_store_maintenance_restarts_without_new_capture(tmp_path, monkeypatch):
    """重启后已有库继续清理, 默认关闭且没有库时不创建诊断线程或文件"""
    from dataclasses import replace
    from unittest.mock import Mock
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.contracts import DebugEvent, DebugObject
    from satrap.core.log.debug.store import DebugStore
    store = DebugStore(tmp_path / "backend.sqlite", "backend")
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setattr(capture, "debug_store", lambda service=None: store)
    writer_factory = Mock()
    monkeypatch.setattr(capture, "debug_writer", writer_factory)
    capture.maintain_existing_debug("backend")
    writer_factory.assert_not_called()
    assert not store.path.exists()
    event = DebugEvent("expired", DebugObject("qq", "bot", "group", "room"), "context", "", "start", 0,
                       "request", "start", "", "", time.time() - 9 * 86400, "", {}, {}, "v1")
    store.append([event, replace(event, event_id="end", sequence=1, phase="end", status="success")])
    capture.maintain_existing_debug("backend")
    writer_factory.assert_called_once_with("backend")
    writer = capture.DebugWriter(store)
    try:
        writer._maintain()
        assert store.requests()["requests"] == []
    finally:
        writer.close()


def test_advanced_debug_capture_freezes_policy_redacts_and_keeps_tools_across_new_contexts(tmp_path, monkeypatch):
    """高级采集冻结设置, 脱敏正文, 保留工具和新上下文, 关闭后下一请求不再写入"""
    from dataclasses import replace
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.contracts import DebugObject
    from satrap.core.log.debug.policy import policy_store
    from satrap.core.log.debug.store import DebugStore

    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    obj = DebugObject("qq", "bot", "group", "group")
    store = DebugStore(tmp_path / "logs" / "debug.sqlite", "backend")
    writer = capture.DebugWriter(store)
    monkeypatch.setattr(capture, "debug_writer", lambda: writer)
    settings = policy_store()
    try:
        with capture.debug_request(obj, "off-request", input_data="不应保存") as trace:
            assert trace is None
        assert not store.path.exists()
        settings.set_object(obj, "on", settings.read().revision)
        with capture.debug_request(obj, "first-request", context_id="first-context", input_data={"text": "第一条消息"}) as trace:
            assert trace is not None
            with capture.debug_span("tool", metadata={"name": "read_file"}, bodies={"arguments": {"path": "example.txt"}}):
                capture.record_debug("tool_result", "end", status="completed", bodies={"result": {"ok": True, "text": "返回正文"}})
        with capture.debug_request(obj, "second-request", context_id="new-context", input_data="/new 之后的消息") as trace:
            assert trace is not None
            settings.set_object(obj, "off", settings.read().revision)
            capture.record_debug("model", "end", bodies={"output": {"api_key": "should-not-appear", "text": "Bearer secret-token"}})
        with capture.debug_request(obj, "third-request", input_data="关闭之后") as trace:
            assert trace is None
        assert writer.flush()
        requests = store.requests()["requests"]
        assert {row["request_id"] for row in requests} == {"first-request", "second-request"}
        model = next(row for row in store.events("second-request")["events"] if row["category"] == "model")
        body = store.body("second-request", model["event_id"], "output")
        assert body == {"api_key": "[REDACTED]", "text": "[REDACTED]"}
        assert {row["context_id"] for row in store.contexts()["contexts"]} == {"first-context", "new-context"}
        selection = replace(settings.read().resolve(obj), enabled=True, max_body_mb=1, max_request_mb=1)
        oversized = capture.DebugTrace(selection, "large-request", writer=writer)
        oversized.emit("request", "start", bodies={"input": "中"*1_000_000})
        oversized.finish()
        assert writer.flush()
        large = next(row for row in store.requests()["requests"] if row["request_id"] == "large-request")
        assert large["incomplete"]
        assert capture.current_trace() is None
    finally:
        writer.close()

def expired(root, identity="123-aaaaaaaaaaaaaaaa"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"SATRAP-test-20200101-{identity}.log"
    path.write_text("过期", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_advanced_debug_observes_sdk_streams_without_changing_results(tmp_path, monkeypatch):
    """真实 SDK 数据类型覆盖同步, 异步, 流式和异常, 诊断故障不能触发额外模型调用"""
    from openai.types.chat import ChatCompletion, ChatCompletionChunk
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.model import complete_sync, complete_async
    from satrap.core.log.debug.contracts import DebugObject
    from satrap.core.log.debug.policy import DebugPolicy
    from satrap.core.log.debug.store import DebugStore
    from dataclasses import replace

    obj = DebugObject("qq", "bot", "group", "group")
    writer = capture.DebugWriter(DebugStore(tmp_path / "debug.sqlite", "backend"))
    monkeypatch.setattr(capture, "debug_writer", lambda: writer)
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    capture.policy_store().save(DebugPolicy(default_enabled=True), DebugPolicy().revision)
    response = ChatCompletion.model_validate({"id": "response", "created": 1, "model": "model", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "真实答案"}}]})
    chunks = [ChatCompletionChunk.model_validate({"id": "stream", "created": 1, "model": "model", "object": "chat.completion.chunk",
              "choices": [{"index": 0, "delta": {"content": text, "reasoning_content": f"公开思考{index}"}, "finish_reason": "stop" if index else None}]}) for index, text in enumerate(("真实", "分块"))]
    calls = []
    def create(**params):
        calls.append(params)
        return iter(chunks) if params.get("stream") else response
    async def async_create(**params):
        calls.append(params)
        async def stream():
            for chunk in chunks:
                yield chunk
        return stream() if params.get("stream") else response
    try:
        with capture.debug_request(obj, "sdk", context_id="context"):
            assert complete_sync(create, {"messages": [], "model": "model"}, 60) is response
            assert list(complete_sync(create, {"messages": [], "stream": True}, 60)) == chunks
            assert await complete_async(async_create, {"messages": []}, 60) is response
            stream = await complete_async(async_create, {"messages": [], "stream": True}, 60)
            assert [chunk async for chunk in stream] == chunks
        assert len(calls) == 4 and writer.flush()
        events = writer.store.events("sdk")["events"]
        assert len([entry for entry in events if entry["category"] == "model" and entry["phase"] == "end"]) == 4
        assert next(entry for entry in events if entry["category"] == "model")["metadata"]["model"] == "model"
        output = [entry for entry in events if "output" in entry["body_slots"]][0]
        assert writer.store.body("sdk", output["event_id"], "output")["choices"][0]["message"]["content"] == "真实答案"
        partial = [entry for entry in events if "output_chunk" in entry["body_slots"]][0]
        assert writer.store.body("sdk", partial["event_id"], "output_chunk") == [{"index": 0, "content": "真实分块", "reasoning_content": "公开思考0公开思考1", "tool_calls": []}]
        with capture.debug_request(obj, "failed-sdk"):
            def fail(**params):
                raise TimeoutError("不得泄露 sk-secret-credentials-value")
            with pytest.raises(TimeoutError):
                complete_sync(fail, {"messages": []}, 60)
        assert writer.flush()
        failed = [entry for entry in writer.store.events("failed-sdk")["events"] if entry["category"] == "model"][-1]
        assert failed["status"] == "timeout" and "sk-secret" not in str(failed)
        assert failed["metadata"]["diagnostic_level"] == "error"
        assert writer.store.request_info("failed-sdk")["diagnostic_level"] == "error"
        broken = capture.DebugTrace(replace(DebugPolicy().resolve(obj), enabled=True), "broken", writer=writer)
        monkeypatch.setattr(writer, "submit", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("故障")))
        broken.emit("request", "start", bodies={"input": "实际输入"})
        broken.finish()
        assert broken.incomplete
    finally:
        writer.close()


def test_advanced_debug_cold_catalog_and_stable_tied_pagination(tmp_path):
    """已有路由无需日志即可选择, 同刻分页不遗漏, 旧式身份不猜测且不创建数据库"""
    from satrap.core.log.debug.catalog import available_contexts
    from satrap.core.storage.layout import StorageLayout
    from satrap.core.framework.UserManager import UserInfoStore
    from satrap.core.conversation import ConversationRoute
    from satrap.core.log.debug.store import DebugStore
    from satrap.core.log.debug.contracts import DebugObject, DebugEvent
    from dataclasses import replace
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("qq", platform_type="onebot")
    users = UserInfoStore(layout.platform_db("qq"))
    route = ConversationRoute("user", "qq", "async_simple", scope="group", self_id="bot", group_id="group")
    with users._connect() as db:
        db.execute("INSERT INTO context_sessions VALUES(?,?,?,?,?,?,?,?)", (route.key, "", "qq", "edictum", "async_simple", "old-context", 1, 2))
    page = available_contexts(layout, "qq", limit=1)
    assert page["contexts"][0]["object"]["target_id"] == "group"
    assert page["contexts"][0]["context_id"] == "old-context"
    assert available_contexts(layout, "chat")["contexts"] == []
    assert not layout.platform_db("chat").exists()
    store = DebugStore(tmp_path / "debug.sqlite", "backend")
    event = DebugEvent("req0", DebugObject("qq", "bot", "group", "group"), "context", "标题", "e0", 0, "request", "start", "", "", 1.0, "", {}, {}, "rev")
    for index in range(7):
        store.append([replace(event, request_id=f"req{index}", event_id=f"e{index}")])
    first = store.requests(limit=3)
    second = store.requests(limit=3, cursor=first["next_cursor"])
    third = store.requests(limit=3, cursor=second["next_cursor"])
    assert [item["request_id"] for page in (first, second, third) for item in page["requests"]] == [f"req{index}" for index in range(7)]
    assert not third["has_more"]


@pytest.mark.asyncio
async def test_advanced_debug_http_auth_settings_export_and_source_isolation(tmp_path, monkeypatch):
    """真实 HTTP 分派验证三服务认证隔离, 配置 CAS, 懒读正文及逐行导出"""
    from satrap.core.backend import control_server as control
    from satrap.core.server_auth import ServerAuth
    from satrap.core.utils.minihttp import MiniHTTPServer
    from satrap.core.log.debug.capture import debug_store
    from satrap.core.log.debug.policy import DebugPolicy
    from satrap.core.log.debug.contracts import DebugObject, DebugEvent
    from dataclasses import replace
    monkeypatch.setenv("SATRAP_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    obj = DebugObject("qq", "bot", "group", "group")
    for source in ("control", "backend", "chat"):
        event = DebugEvent(source+"-request", obj, "context", "标题", source+"-event", 0, "request", "start", "", "", 1.0, "", {}, {"input": source+"正文"}, "rev")
        debug_store(source).append([event, replace(event, sequence=1, event_id=source+"-end", phase="end", status="success", bodies={})])
    servers, endpoints = [], []
    async def request(port, token, path, method="GET", payload=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        raw_body = json.dumps(payload).encode() if payload is not None else b""
        try:
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(raw_body)}\r\n\r\n".encode() + raw_body)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 10)
            head, body = raw.split(b"\r\n\r\n", 1)
            return int(head.split()[1]), body
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        server = await asyncio.start_server(control._handle_request, "127.0.0.1", 0)
        servers.append(server); endpoints.append(("control", server.sockets[0].getsockname()[1], control._CONTROL_AUTH.token))
        for source in ("backend", "chat"):
            token = source+"-debug-token-at-least-32-characters"
            http = MiniHTTPServer("127.0.0.1", 0, session_namespace=source, auth=ServerAuth.create("127.0.0.1", 0, token=token))
            server = await asyncio.start_server(http._handle_connection, "127.0.0.1", 0)
            servers.append(server); endpoints.append((source, server.sockets[0].getsockname()[1], token))
        for source, port, token in endpoints:
            assert (await request(port, "invalid", "/api/debug/contexts"))[0] == 401
            assert (await request(port, "invalid", "/api/debug/export?request_id="+source+"-request"))[0] == 401
            status, raw = await request(port, token, "/api/debug/contexts")
            assert status == 200 and json.loads(raw)["contexts"][0]["service"] == source
            status, raw = await request(port, token, f"/api/debug/body?request_id={source}-request&event_id={source}-event&slot=input")
            assert status == 200 and json.loads(raw)["body"] == source+"正文"
            assert (await request(port, token, "/api/debug/body?request_id=foreign&event_id="+source+"-event&slot=input"))[0] == 400
            status, raw = await request(port, token, f"/api/debug/export?request_id={source}-request")
            lines = [json.loads(line) for line in raw.splitlines()]
            assert status == 200 and lines[0]["request"]["request_id"] == source+"-request"
            assert lines[1]["bodies"]["input"] == source+"正文" and lines[-1]["export_complete"]
            assert (await request(port, token, "/api/debug/contexts?limit=101"))[0] == 400
            assert (await request(port, token, "/api/debug/contexts?service=foreign"))[0] == 400
        _, port, token = endpoints[0]
        status, raw = await request(port, token, "/config/debug-logging")
        snapshot = json.loads(raw)
        assert status == 200 and not snapshot["policy"]["default_enabled"]
        payload = {"policy": DebugPolicy(default_enabled=True).payload(), "expected_revision": snapshot["revision"]}
        assert (await request(port, token, "/config/debug-logging", "PUT", payload))[0] == 200
        assert (await request(port, token, "/config/debug-logging", "PUT", payload))[0] == 409
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_advanced_debug_tool_outcomes_keep_warning_and_hard_error_after_recovery(tmp_path, monkeypatch, asynchronous):
    """
    真实工具调用区分参数错误和内部异常, 后续成功不抹除诊断级别

    参数:
    - tmp_path: 独立配置和诊断库目录
    - monkeypatch: 隔离策略和写入器
    - asynchronous: 是否使用异步工具管理器
    """
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.contracts import DebugObject
    from satrap.core.log.debug.policy import DebugPolicy
    from satrap.core.log.debug.store import DebugStore
    from satrap.core.utils.TCBuilder import Tool, AsyncTool, ToolsManager, AsyncToolsManager
    from satrap.core.utils.TCBuilder.utils import tool_error

    def outcome(value: str):
        """
        提供有明确错误边界的同一工具行为

        参数:
        - value: 控制本次测试动作

        返回:
        - 原始业务返回, crash 分支抛出内部 TypeError
        """
        if value == "crash":
            raise TypeError("工具内部错误, 不是参数绑定错误")
        if value == "denied":
            return tool_error("sample", "当前用户没有权限", "permission_denied")
        if value == "pending":
            return {"ok": True, "status": "pending"}
        if value == "unclassified":
            return {"ok": False, "error_type": "custom_failure", "diagnostic_level": "info"}
        return {"ok": True, "data": {"ok": False, "error_type": "execution_error"}}

    class SyncSample(Tool):
        tool_name = "sample"
        description = "诊断集成场景"
        params_dict = {"value": ("string", "测试动作")}

        def execute(self, value: str):
            return outcome(value)

    class AsyncSample(AsyncTool):
        tool_name = "sample"
        description = "诊断集成场景"
        params_dict = {"value": ("string", "测试动作")}

        async def execute(self, value: str):
            return outcome(value)

    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    capture.policy_store().save(DebugPolicy(default_enabled=True), DebugPolicy().revision)
    store = DebugStore(tmp_path / "backend.sqlite", "backend")
    writer = capture.DebugWriter(store)
    monkeypatch.setattr(capture, "debug_writer", lambda: writer)
    manager = AsyncToolsManager() if asynchronous else ToolsManager()
    manager.register_tool(AsyncSample() if asynchronous else SyncSample())
    obj = DebugObject("qq", "bot", "group", "room")

    async def call(identity, name="sample", arguments=None):
        """通过对应管理器透传同一真实工具请求与结果"""
        info = {"id": identity, "name": name, "arguments": {} if arguments is None else arguments}
        return await manager.execute_tool_call(info) if asynchronous else manager.execute_tool_call(info)

    try:
        with capture.debug_request(obj, "corrected", context_id="context"):
            _, bad = await call("missing-value")
            assert bad["error_type"] == "invalid_arguments"
            await call("unknown-tool", name="missing")
            await call("malformed", arguments='not-json')
            _, good = await call("fixed", arguments={"value": "good"})
            assert good["ok"] is True
        with capture.debug_request(obj, "hard-error", context_id="context"):
            _, failed = await call("crashed", arguments={"value": "crash"})
            assert failed["error_type"] == "execution_error"
            await call("later-fixed", arguments={"value": "good"})
        with capture.debug_request(obj, "neutral", context_id="context"):
            await call("waiting", arguments={"value": "pending"})
        with capture.debug_request(obj, "business", context_id="context"):
            await call("denied", arguments={"value": "denied"})
            await call("unclassified", arguments={"value": "unclassified"})
        with capture.debug_request(obj, "normal", context_id="context"):
            await call("normal", arguments={"value": "good"})
        with pytest.raises(asyncio.CancelledError):
            with capture.debug_request(obj, "cancelled", context_id="context"):
                raise asyncio.CancelledError()
        with pytest.raises(TimeoutError):
            with capture.debug_request(obj, "timed-out", context_id="context"):
                raise TimeoutError()
        assert writer.flush()
        rows = {row["request_id"]: row for row in store.requests()["requests"]}
        assert rows["corrected"]["status"] == "success" and rows["corrected"]["diagnostic_level"] == "warning"
        assert rows["hard-error"]["status"] == "success" and rows["hard-error"]["diagnostic_level"] == "error"
        assert rows["normal"]["diagnostic_level"] == "info" and rows["neutral"]["diagnostic_level"] == "info"
        assert rows["cancelled"]["status"] == "cancelled" and rows["cancelled"]["diagnostic_level"] == "info"
        assert rows["timed-out"]["diagnostic_level"] == "error"
        assert rows["business"]["diagnostic_level"] == "warning"
        events = store.events("corrected")["events"]
        assert any(event["metadata"].get("error_kind") == "model_input" for event in events)
        assert next(event for event in events if event["span_id"] == "missing-value" and event["category"] == "tool" and event["phase"] == "end")["metadata"]["diagnostic_level"] == "warning"
        hard = store.events("hard-error")["events"]
        assert next(event for event in hard if event["category"] == "tool_result" and event["span_id"] == "crashed")["metadata"]["diagnostic_level"] == "error"
        assert any(event["metadata"].get("result_status") == "pending" for event in store.events("neutral")["events"])
    finally:
        writer.close()


def test_advanced_debug_v1_migration_keeps_legacy_unknown_without_reading_bodies(tmp_path, monkeypatch):
    """
    旧库只升级汇总列, 旧记录分类不依赖解码正文, 新请求累积最高级别

    参数:
    - tmp_path: 独立诊断库目录
    - monkeypatch: 阻止读取任何正文以验证摘要读取边界
    """
    import sqlite3
    from satrap.core.log.debug.store import DebugStore
    from satrap.core.log.debug.contracts import DebugEvent, DebugObject
    from satrap.core.log.debug.codec import DebugBodyCodec
    obj = DebugObject("qq", "bot", "group", "room")
    path = tmp_path / "v1.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE debug_requests (request_id TEXT PRIMARY KEY,object_key TEXT,context_id TEXT,title TEXT,parent_request_id TEXT,
                started_at REAL,updated_at REAL,status TEXT,incomplete INTEGER,settings_revision TEXT);
            CREATE TABLE debug_events (event_id TEXT PRIMARY KEY,request_id TEXT,sequence INTEGER,category TEXT,phase TEXT,
                span_id TEXT,parent_span_id TEXT,recorded_at REAL,status TEXT,metadata_json TEXT,UNIQUE(request_id,sequence));
            CREATE TABLE debug_event_bodies (event_id TEXT,slot TEXT,body_key TEXT,PRIMARY KEY(event_id,slot));
            PRAGMA user_version=1;
        """)
        db.execute("INSERT INTO debug_requests VALUES('legacy',?,'context','','',1,1,'success',0,'v1')", (obj.key,))
        db.execute("INSERT INTO debug_events VALUES('old','legacy',0,'tool_result','end','','',1,'returned','{}')")
        db.execute("INSERT INTO debug_requests VALUES('known-error',?,'context','','',1,1,'failed',0,'v1')", (obj.key,))
        db.execute("INSERT INTO debug_events VALUES('old-error','known-error',0,'model','end','','',1,'failed','{\"code\":\"provider_error\"}')")
    store = DebugStore(path, "backend")
    monkeypatch.setattr(DebugBodyCodec, "read", lambda *args, **kwargs: pytest.fail("摘要读取不能解码正文"))
    assert store.request_info("legacy")["diagnostic_level"] == "unknown"
    assert store.events("legacy")["events"][0]["metadata"]["diagnostic_level"] == "unknown"
    assert store.events("known-error")["events"][0]["metadata"]["diagnostic_level"] == "error"
    event = DebugEvent("new", obj, "context", "", "new-event", 0, "request", "end", "", "", 2, "success", {"code": "invalid_arguments"}, {}, "v2")
    store.append([event, event])
    assert store.request_info("new")["diagnostic_level"] == "warning"
    assert store.request_info("legacy")["diagnostic_level"] == "unknown"
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT metadata_json FROM debug_events WHERE event_id='old'").fetchone()[0] == '{}'
        assert db.execute("SELECT diagnostic_level FROM debug_requests WHERE request_id='legacy'").fetchone()[0] is None


@pytest.mark.asyncio
async def test_advanced_debug_real_session_model_tool_approval_and_runtime_interruption(tmp_path, monkeypatch):
    """真实会话执行两次模型调用和一次审批拒绝, 保留父子步骤, 工作流归属及中断证据"""
    from unittest.mock import AsyncMock
    from openai.types.chat import ChatCompletion
    from satrap import AsyncLLM
    from satrap.edictum import AsyncSimpleSession
    from satrap.core.utils.TCBuilder import AsyncTool
    from satrap.core.approval.coordinator import approve_async
    from satrap.core.approval.policy import RiskLevel
    from satrap.core.approval.state import session_approval
    from satrap.core.approval.contracts import ApprovalResult
    from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.policy import DebugPolicy
    from satrap.core.log.debug.store import DebugStore
    from satrap.core.log.debug.contracts import DebugObject
    from dataclasses import replace
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    capture.policy_store().save(DebugPolicy(default_enabled=True), DebugPolicy().revision)
    store = DebugStore(tmp_path / "chat.sqlite", "chat")
    writer = capture.DebugWriter(store)
    monkeypatch.setattr(capture, "debug_writer", lambda: writer)
    monkeypatch.setattr(capture, "debug_store", lambda service=None: store)
    llm = AsyncLLM(api_key="synthetic-key", base_url="http://127.0.0.1:9/v1", model="synthetic", supports_visual_input=False)
    first = ChatCompletion.model_validate({"id": "m1", "created": 1, "model": "synthetic", "object": "chat.completion",
             "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call-guard", "type": "function", "function": {"name": "guarded_action", "arguments": "{}"}}]}}]})
    final = ChatCompletion.model_validate({"id": "m2", "created": 1, "model": "synthetic", "object": "chat.completion",
             "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "审批拒绝, 未执行操作"}}]})
    create = AsyncMock(side_effect=[first, final])
    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    session = AsyncSimpleSession("actual-chat", llm, db_path=str(tmp_path / "platform.db"))
    bind_environment(session, replace(environment_snapshot(session), session_root=tmp_path / "session"))
    engine = session_approval(session)
    engine.set_mode("user")
    session.approval_provider = lambda request: ApprovalResult.for_request(request, "deny", "本次操作未授权", "user")
    class Guard(AsyncTool):
        tool_name = "guarded_action"
        description = "集成场景中的审批动作"
        params_dict = {}

        async def execute(self):
            """
            验证真实审批分派, 不执行外部副作用

            返回:
            - 放行状态及拒绝原因
            """
            allowed, reason = await approve_async(session, engine, "shell", RiskLevel.HIGH, "验证审批", action={"command": "synthetic"}, tool_name=self.tool_name)
            return {"ok": allowed, "reason": reason}
    obj = DebugObject("chat", "local", "chat", "actual-chat")
    try:
        await session.initialize()
        session.tools_manager.register_tool(Guard())
        with capture.debug_request(obj, "actual-request", context_id="actual-chat"):
            capture.record_debug("chat", "start", metadata={"reply_version": {"turn_id": 1, "variant_index": 0}})
            answer = await session.run("执行测试动作")
        assert answer == "审批拒绝, 未执行操作" and create.call_count == 2 and writer.flush()
        events = store.events("actual-request")["events"]
        assert {"context_preparation", "model", "tool", "tool_result", "approval"} <= {event["category"] for event in events}
        approval = next(event for event in events if event["category"] == "approval" and event["phase"] == "end")
        assert approval["status"] == "deny" and approval["parent_span_id"] == "call-guard"
        assert store.find_link("turn", json.dumps([obj.key, "actual-chat", 1, 0])) == "actual-request"
        assert capture.previous_chat_request(obj, "actual-chat", {"turn_id": 1, "variant_index": 1}, None) == "actual-request"
        assert any(item["context_kind"] == "workflow" for item in store.contexts()["contexts"])
        unfinished = capture.DebugTrace(capture.policy_store().read().resolve(obj), "unfinished", context_id="actual-chat", writer=writer)
        unfinished.emit("request", "start", bodies={"input": "未完成的请求"})
        assert writer.flush()
        assert next(item for item in store.requests()["requests"] if item["request_id"] == "unfinished")["status"] == "running"
        writer.close()
        row = next(item for item in store.requests()["requests"] if item["request_id"] == "unfinished")
        assert row["status"] == "interrupted" and row["incomplete"]
    finally:
        writer.close()
        await llm.client.close()


def test_advanced_debug_concurrent_objects_and_capacity_pause_keep_business_running(tmp_path, monkeypatch):
    """并发对象不串正文, 容量耗尽暂停采集并显式标记缺口, 业务仍可完成"""
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.policy import DebugPolicy
    from satrap.core.log.debug.store import DebugStore
    from satrap.core.log.debug.contracts import DebugObject
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    capture.policy_store().save(DebugPolicy(default_enabled=True), DebugPolicy().revision)
    store = DebugStore(tmp_path / "backend.sqlite", "backend")
    writer = capture.DebugWriter(store)
    monkeypatch.setattr(capture, "debug_writer", lambda: writer)
    obj = DebugObject("qq", "bot", "group", "room0")
    def execute(index):
        with capture.debug_request(replace(obj, target_id=f"room{index%2}"), f"thread{index}", context_id=f"context{index}", input_data=f"input{index}"):
            capture.record_debug("tool_result", "end", bodies={"result": f"result{index}"})
            assert capture.current_trace().request_id == f"thread{index}"
        assert capture.current_trace() is None
        return index * 2
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(execute, range(16))) == [index*2 for index in range(16)]
        assert writer.flush()
        for index in range(16):
            events = store.events(f"thread{index}")["events"]
            assert [entry["sequence"] for entry in events] == [0, 1, 2]
            assert store.body(f"thread{index}", events[0]["event_id"], "input") == f"input{index}"
            assert store.body(f"thread{index}", events[1]["event_id"], "result") == f"result{index}"
        assert len(store.contexts()["contexts"]) == 16
        assert len(store.contexts(query="context1")["contexts"]) == 7
        assert not store.contexts(query="%")["contexts"]
        assert not store.contexts(context_kind="workflow")["contexts"]
        assert len(store.contexts(recording_keys=[obj.key])["contexts"]) == 8
        assert len(store.contexts(recording_keys=[obj.key], exclude_keys=True)["contexts"]) == 8
        with capture.debug_request(obj, "capacity-active", context_id="capacity", input_data="继续执行") as trace:
            assert trace is not None and writer.flush()
            monkeypatch.setattr(writer, "_quota", lambda: 1)
            writer._maintain()
            assert writer.quota_blocked
            monkeypatch.setattr(writer, "_maintain", lambda: None)
            capture.record_debug("tool_result", "end", bodies={"result": "容量不足时省略"})
            with capture.debug_request(obj, "capacity-paused") as paused:
                assert paused is None
                from satrap.core.log.debug.model import complete_sync
                result = {"answer": "业务仍能正常返回"}
                assert complete_sync(lambda **kwargs: result, {"messages": []}, 60) is result
        assert writer.flush()
        row = next(item for item in store.requests()["requests"] if item["request_id"] == "capacity-active")
        assert row["status"] == "success" and row["incomplete"]
        assert writer.dropped_events == 1 and writer.dropped_requests == 1
    finally:
        writer.close()


def test_advanced_debug_deferred_group_and_friend_decisions_link_to_original_object(tmp_path, monkeypatch):
    """群和好友审批在原请求结束后仍关联真实来源, 相同动作 ID 不跨业务命名空间串链"""
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.policy import DebugPolicy
    from satrap.core.log.debug.store import DebugStore
    from satrap.core.log.debug.contracts import DebugObject
    from satrap.core.config.group_actions import GroupActionStore
    from satrap.core.friends.store import FriendStore
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    capture.policy_store().save(DebugPolicy(default_enabled=True), DebugPolicy().revision)
    store = DebugStore(tmp_path / "backend.sqlite", "backend")
    writer = capture.DebugWriter(store)
    monkeypatch.setattr(capture, "debug_writer", lambda: writer)
    monkeypatch.setattr(capture, "debug_store", lambda service=None: store)
    groups = GroupActionStore(tmp_path / "platform.db")
    friends = FriendStore(tmp_path / "platform.db")
    obj = DebugObject("qq", "100", "group", "200")
    try:
        with capture.debug_request(obj, "action-initial", context_id="before-new"):
            with capture.debug_span("tool", span_id="action-tool"):
                groups.submit("shared-action", "100", "200", "set_group_name", {"name": "测试群"}, "model", 1, approval_required=True)
                friends.register("100", "shared-action", "delete_friend", {"user_id": "300"}, "model", actor_id="400")
        assert writer.flush()
        assert groups.decide("100", "200", "shared-action", approve=True, policy_revision=1)["state"] == "executing"
        assert writer.flush()
        assert groups.settle("100", "200", "shared-action", "succeeded", "platform_confirmed")["state"] == "succeeded"
        assert friends.transition("100", "shared-action", "pending", "rejected")
        assert writer.flush()
        requests = store.requests()["requests"]
        assert len(requests) == 4 and {item["object_key"] for item in requests} == {obj.key}
        decisions = {}
        for request in requests:
            for event in store.events(request["request_id"])["events"]:
                if event["category"] == "platform_approval" and event["status"] != "pending":
                    decisions[(event["metadata"]["domain"], event["status"])] = request
                    assert event["parent_span_id"] == ""
        approved = decisions[("group", "executing")]
        assert approved["parent_request_id"] == "action-initial"
        assert decisions[("group", "succeeded")]["parent_request_id"] == approved["request_id"]
        assert decisions[("friend", "rejected")]["parent_request_id"] == "action-initial"
        assert {item["context_id"] for item in requests} == {"before-new"}
    finally:
        writer.close()


def test_advanced_debug_end_frame_cannot_overtake_queued_events(tmp_path, monkeypatch):
    """阻塞首批写入后排入跨批次事件, 收尾落盘前必须已保存全部更早序号"""
    import threading
    from satrap.core.log.debug import capture
    from satrap.core.log.debug.policy import DebugPolicy
    from satrap.core.log.debug.store import DebugStore
    from satrap.core.log.debug.contracts import DebugObject
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    store = DebugStore(tmp_path / "backend.sqlite", "backend")
    entered, release = threading.Event(), threading.Event()
    original = store.append
    closed = []
    def append(events):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        original(events)
        for event in events:
            if event.category == "request" and event.phase == "end":
                with store.connect() as db:
                    sequences = [row[0] for row in db.execute("SELECT sequence FROM debug_events WHERE request_id=? ORDER BY sequence", (event.request_id,))]
                assert sequences == list(range(event.sequence + 1))
                closed.append(event.request_id)
    monkeypatch.setattr(store, "append", append)
    writer = capture.DebugWriter(store, capacity=256)
    trace = capture.DebugTrace(DebugPolicy(default_enabled=True).resolve(DebugObject("qq", "bot", "group", "room")), "ordered", writer=writer)
    try:
        trace.emit("request", "start")
        assert entered.wait(10)
        for index in range(100):
            trace.emit("model", "update", metadata={"part": index})
        trace.finish()
        release.set()
        assert writer.flush(20) and closed == ["ordered"]
        first = store.events("ordered", limit=64)
        second = store.events("ordered", after=first["events"][-1]["sequence"], limit=64)
        assert [item["sequence"] for page in (first, second) for item in page["events"]] == list(range(102))
    finally:
        release.set()
        writer.close()

def test_default_policy_and_compare_and_swap(tmp_path):
    store = store_for(tmp_path)
    assert store.read() == (LoggingPolicy(True, 30), "missing")
    assert not store.path.exists()
    saved = store.save({"enabled": False, "retention_days": 90}, "missing")
    assert saved["policy"] == {"enabled": False, "retention_days": 90}
    before = store.path.read_bytes()
    with pytest.raises(LoggingPolicyConflict):
        store.save({"enabled": True, "retention_days": 1}, "missing")
    assert store.path.read_bytes() == before
    with pytest.raises(ValueError):
        store.save({"enabled": False, "retention_days": True}, saved["revision"])
    assert store.path.read_bytes() == before

def test_hourly_cleanup_policy_reload_disable_and_manual(tmp_path):
    store = store_for(tmp_path)
    old = expired(store.root)
    result = store.cleanup(automatic=True, now=10000)
    assert result["deleted"] == [old.name]
    old = expired(store.root)
    assert store.cleanup(automatic=True, now=13599) is None and old.exists()
    assert store.cleanup(automatic=True, now=13600)["deleted"] == [old.name]
    saved = store.save({"enabled": False, "retention_days": 30}, "missing")
    old = expired(store.root)
    assert store.cleanup(automatic=True, now=17200) is None and old.exists()
    assert store.cleanup(expected_revision=saved["revision"])["deleted"] == [old.name]
    with pytest.raises(LoggingPolicyConflict):
        store.cleanup(expected_revision="stale")
    saved = store.save({"enabled": True, "retention_days": 1}, saved["revision"])
    old = expired(store.root)
    assert store.cleanup(automatic=True, now=17201)["deleted"] == [old.name]

def test_maintenance_runs_while_idle_and_stops(tmp_path):
    store = store_for(tmp_path)
    current = [date(2020, 1, 1)]
    handler = ManagedDailyHandler(store.root, clock=lambda: current[0])
    handler.handle(logging.LogRecord("test", 30, "", 0, "旧日期", (), None))
    active = handler.current_file
    current[0] = date.today()
    maintenance = LogMaintenance(handler, store, interval=0.03)
    try:
        maintenance.start()
        deadline = time.monotonic() + 3
        while active.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not active.exists() and handler.stream is None
        new = expired(store.root)
        maintenance.clock = lambda: time.time() + 3601
        deadline = time.monotonic() + 3
        while new.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not new.exists()
    finally:
        maintenance.close()
        handler.close()
    assert not maintenance._thread.is_alive()

def test_corrupt_config_disables_deletion_until_repaired_and_reports(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    handler = ManagedDailyHandler(store.root)
    failures = []
    monkeypatch.setattr("satrap.core.log.policy.report_failure", failures.append)
    store.path.write_text("broken", encoding="utf-8")
    old = expired(store.root)
    maintenance = LogMaintenance(handler, store)
    try:
        maintenance.step()
        assert old.exists() and failures
        store.path.unlink()
        assert store.snapshot()["maintenance_error"]
        maintenance.step()
        assert not old.exists() and store.snapshot()["maintenance_error"] is None
    finally:
        maintenance.close()
        handler.close()

def test_save_failure_preserves_original_and_retry_succeeds(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    original_replace = os.replace
    def fail(source, destination):
        if Path(destination) == store.path:
            raise OSError("磁盘失败")
        return original_replace(source, destination)
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.save({"enabled": False, "retention_days": 60}, "missing")
    assert store.read() == (LoggingPolicy(), "missing")
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(os, "replace", original_replace)
    assert store.save({"enabled": False, "retention_days": 60}, "missing")["policy"]["retention_days"] == 60

@pytest.mark.parametrize("stage", ["path-resolution", "file-open"])
def test_logger_initialization_failure_keeps_console_usable(tmp_path, monkeypatch, capsys, stage):
    from satrap.core.log import Logger
    failures = []
    monkeypatch.setattr("satrap.core.log.report_failure", failures.append)
    monkeypatch.setattr("satrap.core.log.managed.report_failure", failures.append)
    if stage == "path-resolution":
        monkeypatch.setattr("satrap.core.log.log_root", lambda: (_ for _ in ()).throw(OSError("日志目录不可解析")))
    logger = Logger("invalid-root", std_out=True, file_name=str(tmp_path / "invalid\0path") if stage == "file-open" else None)
    try:
        logger.warning("业务仍可运行")
        assert failures and logger.log_file is None
        assert "业务仍可运行" in capsys.readouterr().err
    finally:
        logger.close()

def test_corrupt_cleanup_result_is_visible_without_breaking_config(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    store.root.mkdir()
    (store.root / ".cleanup-result.json").write_text('{"created_at": 1, "deleted": [], "skipped": [null], "errors": []}', encoding="utf-8")
    failures = []
    monkeypatch.setattr("satrap.core.log.policy.report_failure", failures.append)
    snapshot = store.snapshot()
    assert snapshot["status_error"] and snapshot["last_cleanup"] is None and failures
    assert snapshot["policy"]["retention_days"] == 30

def test_manual_global_failure_is_saved_and_retry_clears_it(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    from satrap.core.log import policy
    original_cleanup = policy.cleanup_logs
    monkeypatch.setattr(policy, "cleanup_logs", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("维护目录不可写")))
    with pytest.raises(PermissionError):
        store.cleanup(expected_revision="missing")
    assert store.snapshot()["maintenance_error"]["error"] == "维护目录不可写"
    monkeypatch.setattr(policy, "cleanup_logs", original_cleanup)
    assert store.cleanup(expected_revision="missing") is not None
    assert store.snapshot()["maintenance_error"] is None

def test_two_processes_coordinate_cleanup_and_keep_concurrent_records(tmp_path):
    store = store_for(tmp_path)
    old = expired(store.root)
    script = """
from pathlib import Path
import logging, json, sys
from satrap.core.log.managed import ManagedDailyHandler
from satrap.core.log.policy import LoggingPolicyStore
store = LoggingPolicyStore(Path(sys.argv[1]), Path(sys.argv[2]))
handler = ManagedDailyHandler(store.root, 'parallel')
for i in range(50):
    handler.handle(logging.LogRecord('probe', 30, '', 0, f'中文-{i}\\n尾行-{i}', (), None))
result = store.cleanup(automatic=True, now=10000)
print(json.dumps({'file': handler.current_file.name, 'cleaned': result is not None}), flush=True)
handler.close()
"""
    children = [subprocess.Popen([sys.executable, "-c", script, str(store.path), str(store.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}) for _ in range(2)]
    try:
        outputs = []
        for child in children:
            out, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            outputs.append(json.loads(out))
        assert sum(item["cleaned"] for item in outputs) == 1
        assert len({item["file"] for item in outputs}) == 1 and not old.exists()
        for item in outputs:
            lines = (store.root / item["file"]).read_text(encoding="utf-8").splitlines()
            assert len(lines) == 200
            pairs = list(zip(lines[::2], lines[1::2]))
            assert sorted(pairs) == sorted([(f"中文-{i}", f"尾行-{i}") for i in range(50)] * 2)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)

@pytest.mark.asyncio
async def test_control_api_read_save_cleanup_validation_and_failures(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control
    store = store_for(tmp_path)
    monkeypatch.setattr(control, "LoggingPolicyStore", lambda: store)
    logs = []
    monkeypatch.setattr(control.logger, "warning", logs.append)
    monkeypatch.setattr(control.logger, "error", logs.append)
    async def request(method, path="/config/logging", payload=None):
        content = json.dumps(payload).encode("utf-8") if payload is not None else b""
        reader = asyncio.StreamReader()
        reader.feed_data(content)
        reader.feed_eof()
        headers = f"{method} {path} HTTP/1.1\r\nContent-Length: {len(content)}\r\n\r\n".encode("utf-8")
        return await control._route_logging(control._RouteContext(method, path, path, reader, headers))
    status, data = await request("GET")
    assert status == 200 and data["policy"]["retention_days"] == 30
    status, saved = await request("PUT", payload={"policy": {"enabled": True, "retention_days": 2}, "expected_revision": data["revision"]})
    assert status == 200 and saved["policy"]["retention_days"] == 2
    assert (await request("PUT", payload={"policy": {"enabled": True, "retention_days": 2}, "expected_revision": "missing"}))[0] == 409
    assert (await request("PUT", payload={"policy": {"enabled": True, "retention_days": 0}, "expected_revision": saved["revision"]}))[0] == 400
    old = expired(store.root)
    status, result = await request("POST", "/config/logging/cleanup", {"expected_revision": saved["revision"]})
    assert status == 200 and result["ok"] and old.name in result["result"]["deleted"]
    old = expired(store.root)
    original = Path.unlink
    def deny(path, *args, **kwargs):
        if path == old:
            raise PermissionError("权限错误")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", deny)
    status, result = await request("POST", "/config/logging/cleanup", {"expected_revision": saved["revision"]})
    assert status == 200 and not result["ok"] and result["result"]["errors"]
    monkeypatch.setattr(store, "save", lambda *args: (_ for _ in ()).throw(OSError("故障")))
    assert (await request("PUT", payload={"policy": {}, "expected_revision": saved["revision"]}))[0] == 500
    assert (await request("DELETE"))[0] == 405
    assert logs

@pytest.mark.asyncio
async def test_http_dispatch_keeps_logging_api_authenticated(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control
    store = store_for(tmp_path)
    monkeypatch.setattr(control, "LoggingPolicyStore", lambda: store)
    server = await asyncio.start_server(control._handle_request, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        for authorized in (False, True):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            token = control._CONTROL_AUTH.token if authorized else "invalid"
            writer.write(f"GET /config/logging HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n\r\n".encode("utf-8"))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()
            await writer.wait_closed()
            assert response.startswith(b"HTTP/1.1 200" if authorized else b"HTTP/1.1 401")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_independent_log_http_sources_authentication_and_incremental_queries(monkeypatch):
    from satrap.core.backend import control_server as control
    from satrap.core.log import api
    from satrap.core.server_auth import ServerAuth
    from satrap.core.utils.minihttp import MiniHTTPServer
    stream = StandardLogStream()
    stream.publish("独立日志, 无需 Backend 管理器", "DEBUG")
    monkeypatch.setattr(api, "standard_log_stream", stream)
    servers = []
    endpoints = []
    try:
        control_server = await asyncio.start_server(control._handle_request, "127.0.0.1", 0)
        servers.append(control_server)
        endpoints.append((control_server.sockets[0].getsockname()[1], control._CONTROL_AUTH.token))
        for label in ("backend", "chat"):
            token = f"independent-{label}-token-at-least-32-characters"
            service = MiniHTTPServer("127.0.0.1", 0, auth=ServerAuth.create("127.0.0.1", 0, token=token))
            server = await asyncio.start_server(service._handle_connection, "127.0.0.1", 0)
            servers.append(server)
            endpoints.append((server.sockets[0].getsockname()[1], token))
        async def request(port, token, path="/api/logs"):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n\r\n".encode("utf-8"))
                await writer.drain()
                raw = await asyncio.wait_for(reader.read(), 5)
                head, body = raw.split(b"\r\n\r\n", 1)
                status = int(head.split(b" ")[1])
                return status, json.loads(body) if status == 200 else {}
            finally:
                writer.close()
                await writer.wait_closed()
        for index, (port, token) in enumerate(endpoints):
            assert (await request(port, "invalid"))[0] == 401
            other = endpoints[(index + 1) % len(endpoints)][1]
            assert (await request(port, other))[0] == 401
            status, initial = await request(port, token)
            assert status == 200 and initial["entries"][0]["level"] == "DEBUG"
            status, empty = await request(port, token, f"/api/logs?after={initial['next_cursor']}&stream_id={initial['stream_id']}")
            assert status == 200 and empty["entries"] == []
            assert (await request(port, token, "/api/logs?limit=501"))[0] == 400
            assert (await request(port, token, "/api/logs?after=1"))[0] == 400
        servers[1].close()
        await servers[1].wait_closed()
        stream.publish("平台 Backend 停止后仍可查看", "INFO")
        for port, token in (endpoints[0], endpoints[2]):
            assert (await request(port, token))[1]["entries"][-1]["content"] == "平台 Backend 停止后仍可查看"
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()

def _record(text):
    return logging.LogRecord("probe", logging.WARNING, __file__, 1, text, (), None)

def test_utf8_daily_rotation_and_idle_close(tmp_path):
    now = [date(2026, 10, 3)]
    handler = ManagedDailyHandler(tmp_path, "backend", clock=lambda: now[0])
    try:
        handler.handle(_record("中文消息"))
        first = handler.current_file
        assert first.read_text(encoding="utf-8") == "中文消息\n"
        now[0] = date(2026, 10, 4)
        handler.tick()
        assert handler.current_file is None and handler.stream is None
        handler.handle(_record("次日消息"))
        assert handler.current_file != first
        assert "20261004" in handler.current_file.name
        assert first.read_text(encoding="utf-8") == "中文消息\n"
    finally:
        handler.close()

def test_date_boundary_and_active_file_protection(tmp_path):
    handler = ManagedDailyHandler(tmp_path, "chat", clock=lambda: date(2026, 9, 1))
    try:
        handler.handle(_record("还在写入"))
        with management_lock(tmp_path):
            assert active_runtimes(tmp_path)[0]["current_file"] == handler.current_file.name
        old = tmp_path / "SATRAP-test-20260903-123-aaaaaaaaaaaaaaaa.log"
        boundary = tmp_path / "SATRAP-test-20260904-123-aaaaaaaaaaaaaaaa.log"
        old.write_text("过期", encoding="utf-8")
        boundary.write_text("边界", encoding="utf-8")
        foreign = tmp_path / "foreign-20200101.log"
        foreign.write_text("其它程序", encoding="utf-8")
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert result["deleted"] == [old.name]
        assert handler.current_file.exists() and boundary.exists() and foreign.exists()
        assert result["skipped"][0]["file"] == handler.current_file.name
        saved = handler.current_file
    finally:
        handler.close()
    result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
    assert saved.name in result["deleted"]


def test_shared_file_stays_active_until_last_writer_exits_and_restarts_append(tmp_path):
    first = ManagedDailyHandler(tmp_path, "chat", clock=lambda: date(2020, 1, 1))
    second = ManagedDailyHandler(tmp_path, "chat", clock=lambda: date(2020, 1, 1))
    try:
        first.handle(_record("第一进程"))
        second.handle(_record("第二进程"))
        path = first.current_file
        assert path == second.current_file == tmp_path / "SATRAP-chat-20200101.log"
        first.close()
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 25))
        assert path.exists() and path.name not in result["deleted"]
        second.handle(_record("退出一个后继续"))
    finally:
        first.close()
        second.close()
    restarted = ManagedDailyHandler(tmp_path, "chat", clock=lambda: date(2020, 1, 1))
    try:
        restarted.handle(_record("重启后追加"))
        assert path.read_text(encoding="utf-8").splitlines() == ["第一进程", "第二进程", "退出一个后继续", "重启后追加"]
    finally:
        restarted.close()
    assert path.name in cleanup_logs(tmp_path, 30, today=date(2026, 10, 25))["deleted"]


def test_incremental_log_snapshots_reset_boundaries_and_utf8_budget():
    stream = StandardLogStream(history_size=5000)
    stream.service = "chat"
    for index in range(5010):
        stream.publish(f"中文-{index}", "DEBUG")
    initial = stream.snapshot(limit=100)
    assert initial["service"] == "chat" and len(initial["entries"]) == 100
    assert initial["next_cursor"] == 5010 and not initial["reset"]
    stream.publish("新日志", "INFO")
    increment = stream.snapshot(after=initial["next_cursor"], stream_id=initial["stream_id"])
    assert [entry["content"] for entry in increment["entries"]] == ["新日志"]
    assert stream.snapshot(after=0, stream_id=initial["stream_id"])["reset"]
    assert stream.snapshot(after=5011, stream_id="old-process")["reset"]
    assert stream.snapshot(after=9999, stream_id=initial["stream_id"])["reset"]
    for _ in range(40):
        stream.publish("中文" * 10000, "WARNING")
    batch = stream.snapshot(after=5011, stream_id=initial["stream_id"], limit=500)
    assert batch["has_more"] and sum(len(entry["content"].encode("utf-8")) for entry in batch["entries"]) <= 256 * 1024
    assert all(entry["truncated"] and "\ufffd" not in entry["content"] for entry in batch["entries"])
    rest = stream.snapshot(after=batch["next_cursor"], stream_id=batch["stream_id"], limit=500)
    assert rest["entries"][0]["sequence"] == batch["next_cursor"] + 1
    while rest["has_more"]:
        rest = stream.snapshot(after=rest["next_cursor"], stream_id=rest["stream_id"], limit=500)
    assert rest["next_cursor"] == 5051
    with pytest.raises(ValueError):
        stream.snapshot(after=1)
    with pytest.raises(ValueError):
        stream.snapshot(limit=501)

def test_multiprocess_lease_and_crash_release(tmp_path):
    script = """
from datetime import date
from pathlib import Path
import logging, sys
from satrap.core.log.managed import ManagedDailyHandler
handler = ManagedDailyHandler(Path(sys.argv[1]), 'worker', clock=lambda: date(2020, 1, 1))
handler.handle(logging.LogRecord('probe', 30, '', 0, '子进程日志', (), None))
print(handler.current_file.name, flush=True)
sys.stdin.readline()
"""
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}) for _ in range(2)]
    try:
        filename = children[0].stdout.readline().strip()
        assert children[1].stdout.readline().strip() == filename
        assert filename.startswith("SATRAP-worker-")
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert (tmp_path / filename).exists() and result["skipped"]
        children[0].kill()
        children[0].communicate(timeout=10)
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert (tmp_path / filename).exists() and filename not in result["deleted"]
        children[1].kill()
        children[1].communicate(timeout=10)
        assert filename in cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))["deleted"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)

def test_legacy_compatibility(tmp_path):
    old = tmp_path / "SATRAP-20200101.log"
    old.write_text("旧日志", encoding="utf-8")
    with old.open("a", encoding="utf-8"):
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert old.exists() and result["skipped"]
    result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
    if os.name == "nt":
        assert result["deleted"] == [old.name] and not result["errors"]
    else:
        assert result["skipped"] and old.exists()

def test_write_and_cleanup_failures_are_isolated_and_reported(tmp_path, monkeypatch):
    failures = []
    monkeypatch.setattr("satrap.core.log.managed.report_failure", failures.append)
    handler = ManagedDailyHandler(tmp_path)
    monkeypatch.setattr(handler, "tick", lambda **kwargs: (_ for _ in ()).throw(OSError("磁盘失败")))
    handler.handle(_record("不能写入"))
    assert "磁盘失败" in failures[0]
    handler.close()
    old = tmp_path / "SATRAP-test-20200101-123-aaaaaaaaaaaaaaaa.log"
    old.write_text("旧内容", encoding="utf-8")
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path == old:
            raise PermissionError("删除失败")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
    assert result["errors"][0]["file"] == old.name and old.exists()
    assert any("删除失败" in message for message in failures)

def test_write_failure_recovers_on_next_record(tmp_path, monkeypatch):
    failures = []
    monkeypatch.setattr("satrap.core.log.managed.report_failure", failures.append)
    handler = ManagedDailyHandler(tmp_path)
    try:
        handler.handle(_record("首条"))
        filename = handler.current_file
        handler.stream.close()
        handler.handle(_record("故障条"))
        assert failures and handler.stream is None
        handler.handle(_record("恢复条"))
        assert filename.read_text(encoding="utf-8") == "首条\n恢复条\n"
    finally:
        handler.close()

@pytest.mark.parametrize("content", ["broken", "missing_current_file", "wrong_process"])
def test_corrupt_live_state_protects_all_process_files(tmp_path, monkeypatch, content):
    failures = []
    monkeypatch.setattr("satrap.core.log.managed.report_failure", failures.append)
    handler = ManagedDailyHandler(tmp_path, clock=lambda: date(2020, 1, 1))
    try:
        handler.handle(_record("保留"))
        import json
        if content == "missing_current_file":
            content = json.dumps({"id": handler.identity})
        elif content == "wrong_process":
            content = json.dumps({"id": handler.identity, "current_file": "SATRAP-process-20200101-123-aaaaaaaaaaaaaaaa.log"})
        (tmp_path / ".runtime" / f"{handler.identity}.json").write_text(content, encoding="utf-8")
        result = cleanup_logs(tmp_path, 30, today=date(2026, 10, 3))
        assert handler.current_file.exists() and result["skipped"] and failures
    finally:
        handler.close()

@pytest.mark.parametrize("days", [0, -1, 3651, True, 1.5, "30"])
def test_invalid_retention_cannot_delete_files(tmp_path, days):
    old = tmp_path / "SATRAP-test-20200101-123-aaaaaaaaaaaaaaaa.log"
    old.write_text("保留", encoding="utf-8")
    with pytest.raises(ValueError):
        cleanup_logs(tmp_path, days)
    assert old.exists()

def test_logger_integration_uses_independent_handlers_and_levels(tmp_path):
    from satrap.core.log import Logger
    first = Logger("same", std_out=False, output_dir=str(tmp_path), file_level=logging.WARNING)
    second = Logger("same", std_out=False, output_dir=str(tmp_path), file_level=logging.WARNING)
    try:
        first.set_service("custom-service")
        first.info("过滤")
        first.warning("第一实例")
        second.warning("第二实例")
        assert first.log_file != second.log_file
        content = Path(first.log_file).read_text(encoding="utf-8")
        assert "第一实例" in content and "第二实例" not in content and "过滤" not in content
        assert "custom-service" in first.log_file
    finally:
        first.close()
        second.close()

def test_symlink_and_foreign_file_are_preserved(tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}.log"
    outside.write_text("外部数据", encoding="utf-8")
    link = tmp_path / "SATRAP-test-20200101-123-aaaaaaaaaaaaaaaa.log"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("系统没有创建符号链接权限")
    result = cleanup_logs(tmp_path, 30)
    assert outside.read_text(encoding="utf-8") == "外部数据"
    assert link.is_symlink() and result["skipped"]

@pytest.mark.parametrize("name", [".runtime", ".locks"])
def test_redirected_management_directory_cannot_touch_outside_files(tmp_path, name):
    root = tmp_path / "logs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "123-aaaaaaaaaaaaaaaa.json"
    sentinel.write_text("不能删除", encoding="utf-8")
    (outside / "runtime-123-aaaaaaaaaaaaaaaa.lock").write_bytes(b"\0")
    target = root / name
    if os.name == "nt":
        command = "$taskUtf8 = [System.Text.UTF8Encoding]::new($false); [Console]::InputEncoding = $taskUtf8; [Console]::OutputEncoding = $taskUtf8; $OutputEncoding = $taskUtf8; chcp 65001 > $null; New-Item -ItemType Junction -Path $env:SATRAP_TEST_LINK -Target $env:SATRAP_TEST_TARGET | Out-Null"
        completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], env={**os.environ, "SATRAP_TEST_LINK": str(target), "SATRAP_TEST_TARGET": str(outside)}, text=True, encoding="utf-8", capture_output=True, timeout=15)
        assert completed.returncode == 0, completed.stderr
    else:
        target.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="重定向"):
            cleanup_logs(root, 30)
        assert sentinel.read_text(encoding="utf-8") == "不能删除"
        assert (outside / "runtime-123-aaaaaaaaaaaaaaaa.lock").exists()
    finally:
        if os.name == "nt":
            assert target.absolute().parent == root.resolve()
            os.rmdir(target)
        else:
            target.unlink()

def test_failure_diagnostics_cannot_raise_even_when_both_channels_fail(monkeypatch):
    from satrap.core.log import managed
    class BrokenStream:
        def write(self, content):
            raise OSError("stderr 故障")
    monkeypatch.setattr(sys, "__stderr__", BrokenStream())
    monkeypatch.setattr(managed.standard_log_stream, "publish", lambda *args: (_ for _ in ()).throw(RuntimeError("实时流故障")))
    messages = []
    monkeypatch.setattr(os, "write", lambda descriptor, content: messages.append(content))
    managed.report_failure("文件故障")
    assert "文件故障" in messages[0].decode("utf-8") and "实时流故障" in messages[0].decode("utf-8")
    monkeypatch.setattr(os, "write", lambda *args: (_ for _ in ()).throw(OSError("输出不可用")))
    managed.report_failure("全部通道故障")

@pytest.mark.asyncio
@pytest.mark.parametrize("history_limit", [2, 0])
async def test_standard_log_stream_history_and_live_entries(history_limit: int) -> None:
    """订阅原子获取历史日志 (limit=0 时为空) 并收到后续实时日志"""
    stream = StandardLogStream(history_size=3)
    stream.publish("first", "INFO")
    stream.publish("second", "WARNING")
    stream.publish("third", "ERROR")

    subscription = stream.subscribe(history_limit=history_limit, queue_size=2)
    try:
        assert [entry.content for entry in subscription.history] == (["second", "third"] if history_limit else [])
        stream.publish("live", "DEBUG")
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == ("live", "DEBUG")
    finally:
        stream.unsubscribe(subscription.subscription_id)
    stream.publish("after unsubscribe", "INFO")
    assert subscription.queue.empty()

@pytest.mark.asyncio
async def test_satrap_standard_logger_publishes_to_live_stream() -> None:
    """Satrap 控制台日志应同步到标准日志实时流"""
    standard_log_stream.clear()
    subscription = standard_log_stream.subscribe(history_limit=0)
    try:
        logger.info("standard stream probe", save_to_file=False)
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert entry.level == "INFO"
        assert "standard stream probe" in entry.content
    finally:
        standard_log_stream.unsubscribe(subscription.subscription_id)

@pytest.mark.asyncio
async def test_standard_stream_capture_preserves_output_and_publishes_lines() -> None:
    """标准流代理应保留原输出并按完整文本行发布"""
    wrapped = io.StringIO()
    stream = StandardLogStream()
    capture = StandardStreamCapture(wrapped, stream, "INFO")
    subscription = stream.subscribe(history_limit=0)
    try:
        assert capture.write("plain stdout") == len("plain stdout")
        assert subscription.queue.empty()
        capture.write("\n[ERROR]: failed\n")

        first = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        second = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (first.content, first.level) == ("plain stdout", "INFO")
        assert (second.content, second.level) == ("[ERROR]: failed", "ERROR")
        assert wrapped.getvalue() == "plain stdout\n[ERROR]: failed\n"
    finally:
        stream.unsubscribe(subscription.subscription_id)

@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
@pytest.mark.parametrize("template", [
    "[2026-09-28 21:27:17.222] [{level}]: 配置已刷新",
    "[2026-09-28 21:27:17,222] {level} in __init__: 配置已刷新",
    "[{level}]: 配置已刷新",
    "[2026-10-14 18:02:50 +0800] [24964] [{level}] Running on http://127.0.0.1:8080 (CTRL + C to quit)",
    "[2026-10-14 18:02:50 -0400] [24964] [{level}] 正文包含 [ERROR]",
])
async def test_capture_uses_header_level_on_stderr(level: str, template: str) -> None:
    """标准流中的日志头级别应覆盖 stderr 默认级别"""
    wrapped = io.StringIO()
    stream = StandardLogStream()
    capture = StandardStreamCapture(wrapped, stream, "ERROR")
    subscription = stream.subscribe(history_limit=0)
    content = template.format(level=level)
    try:
        capture.write(content + "\n")
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == (content, level)
        assert wrapped.getvalue() == content + "\n"
    finally:
        stream.unsubscribe(subscription.subscription_id)

@pytest.mark.asyncio
@pytest.mark.parametrize(("content", "default_level", "expected_level"), [
    ("[INFO]: 正文包含 [ERROR]", "ERROR", "INFO"),
    ("[2026-09-28 21:27:17.222] [INFO]: 正文包含 [CRITICAL]", "ERROR", "INFO"),
    ("[2026-09-28 21:27:17,222] WARNING in __init__: 正文包含 [ERROR]", "ERROR", "WARNING"),
    ("普通输出包含 [ERROR]", "INFO", "INFO"),
    ("普通输出包含 [INFO]", "ERROR", "ERROR"),
    ("[2026-09-28 21:27:17.222] 正文包含 [INFO]", "ERROR", "ERROR"),
    ("[UNKNOWN]: 正文包含 [INFO]", "ERROR", "ERROR"),
    ("[2026-10-14 18:02:50 +0800] [24964] [UNKNOWN] 正文包含 [INFO]", "ERROR", "ERROR"),
    ("[2026-10-14 18:02:50 +0800] [24964] 正文包含 [INFO]", "ERROR", "ERROR"),
    ("[2026-10-14 18:02:50 +0800] [24964] [INFO]suffix", "ERROR", "ERROR"),
    ("plain stdout", "INFO", "INFO"),
    ("plain stderr", "ERROR", "ERROR"),
    ('  File "[INFO]", line 1, in execute', "ERROR", "ERROR"),
    ("ConnectionResetError", "ERROR", "ERROR"),
])
async def test_capture_ignores_levels_in_body(
    content: str, default_level: str, expected_level: str,
) -> None:
    """正文和异常堆栈中的级别标记不应覆盖日志头或默认级别"""
    stream = StandardLogStream()
    capture = StandardStreamCapture(io.StringIO(), stream, default_level)
    subscription = stream.subscribe(history_limit=0)
    try:
        capture.write(content + "\n")
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == (content, expected_level)
    finally:
        stream.unsubscribe(subscription.subscription_id)

@pytest.mark.asyncio
async def test_capture_parses_colored_split_header_and_flushes_tail() -> None:
    """分段写入和 flush 均应在清理颜色后解析完整日志头"""
    wrapped = io.StringIO()
    stream = StandardLogStream()
    capture = StandardStreamCapture(wrapped, stream, "ERROR")
    subscription = stream.subscribe(history_limit=0)
    first = "\x1b[32m[2026-09-28 21:27:17.222] [INFO]: 配置已刷新\x1b[0m"
    tail = "[2026-09-28 21:27:18,568] WARNING in __init__: 应用失败"
    try:
        capture.write(first[:30])
        assert subscription.queue.empty()
        capture.write(first[30:] + "\r\n" + tail[:40])
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == (
            "[2026-09-28 21:27:17.222] [INFO]: 配置已刷新", "INFO",
        )
        capture.write(tail[40:])
        capture.flush()
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (entry.content, entry.level) == (tail, "WARNING")
        capture.flush()
        await asyncio.sleep(0)
        assert subscription.queue.empty()
        assert wrapped.getvalue() == first + "\r\n" + tail
    finally:
        stream.unsubscribe(subscription.subscription_id)

class _Reader:
    """可控制 EOF 状态的 WebSocket 测试读取器"""

    def __init__(self) -> None:
        self.eof = False

    def at_eof(self) -> bool:
        """
        返回:
        - bool: 当前是否已结束
        """
        return self.eof

@pytest.mark.asyncio
async def test_websocket_logs_use_standard_stream_without_log_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日志 WebSocket 应发送内存标准流的历史与实时输出"""
    standard_log_stream.clear()
    capture = StandardStreamCapture(io.StringIO(), standard_log_stream, "ERROR")
    history = "[2026-09-28 21:27:17,222] INFO in __init__: 配置已刷新"
    live = "[WARNING]: 应用失败, 正文包含 [ERROR]"
    capture.write(history + "\n")
    server = BackendHTTPServer(BackendManager())
    reader = _Reader()
    sent: list[dict[str, Any]] = []

    async def capture_send(_writer: object, message: dict[str, Any]) -> None:
        """
        参数:
        - _writer: 未使用的测试写入器
        - message: WebSocket 消息
        """
        sent.append(message)

    monkeypatch.setattr(server, "_ws_send", capture_send)
    task = asyncio.create_task(server._ws_log_handler(
        cast(asyncio.StreamReader, reader),
        cast(asyncio.StreamWriter, object()),
        history_lines=10,
    ))
    await asyncio.sleep(0)

    capture.write(live + "\n")
    for _ in range(10):
        if len(sent) >= 2:
            break
        await asyncio.sleep(0.01)

    reader.eof = True
    standard_log_stream.publish("wake", "DEBUG")
    await asyncio.wait_for(task, timeout=1)

    assert sent[0]["data"] == {"content": history, "level": "INFO"}
    assert sent[1]["data"] == {"content": live, "level": "WARNING"}
