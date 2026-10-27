"""业务链路场景与集中边界契约"""
from __future__ import annotations

import aiohttp
import asyncio
import pytest
from typing import cast
from types import SimpleNamespace
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.core.server_auth import ServerAuth
from satrap.display.recorder import DisplayRecorder as _display_stream_DisplayRecorder
from satrap.display.service import ChatService, _SubscriberQueue, _Conversation
from satrap.display.server import ChatHTTPServer
import threading
from pathlib import Path
from typing import Any, Iterator
import json
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import AsyncToolsManager, Tool, ToolsManager
from satrap.display.recorder import _truncate_arguments
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, TokenUsage
from satrap.display import DisplayRecorder as _display_recorder_DisplayRecorder
from satrap.edictum import SimpleSession

async def test_slow_websocket_recovers_reply_and_pending_question(tmp_path):
    recorder = _display_stream_DisplayRecorder(str(tmp_path / "display.db"), "audit")
    conv = cast(_Conversation, SimpleNamespace(conversation_id="audit", model="default", recorder=recorder, subscribers=set(), pending_user_inputs={}, task=None))
    service = ChatService.__new__(ChatService)
    service._conversations = {"audit": conv}
    service._orphan_queues = {}
    service._operations = {}
    service._event_sequences = {}
    service._stream_id = "server-test"
    service._ask_user_timeout_seconds = 5
    from satrap.core.storage import StorageLayout
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    service._storage = StorageLayout(tmp_path / "data")
    service._platform_id = "chat"
    service._model_cfg = ModelConfigManager(str(tmp_path / "models.json"), auto_create=False)
    token = "audit-only-token-that-is-at-least-32-characters"
    server = ChatHTTPServer.__new__(ChatHTTPServer)
    MiniHTTPServer.__init__(server, host="127.0.0.1", port=0, auth=ServerAuth.create("127.0.0.1", 0, token=token))
    server.service = service
    from satrap.core.telemetry.health import ServiceRuntime
    server._service_runtime = ServiceRuntime("chat")
    question_task = None
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as client:
            url = f"http://127.0.0.1:{port}/ws/chat?conversation=audit"
            async with client.ws_connect(url) as slow:
                snapshot = await slow.receive_json(timeout=2)
                assert snapshot["type"] == "snapshot" and snapshot["state"] == "idle"
                fast = service.subscribe("audit")
                recorder.start_turn("question")
                for _ in range(1005):
                    recorder.on_content("x")
                    service._broadcast(conv, {"type": "content_delta", "delta": "x"})
                    assert fast.get_nowait()["type"] == "content_delta"
                completed = recorder.end_turn()
                assert completed is not None
                service._broadcast(conv, {"type": "turn_done", "answer": "x" * 1005, **completed})
                assert fast.get_nowait()["type"] == "turn_done"
                assert not fast.invalidated
                event = await slow.receive_json(timeout=2)
                assert event == {"type": "resync_required"}
                assert (await slow.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
                service.unsubscribe("audit", fast)
            recorder.start_turn("approval")
            question_task = asyncio.create_task(service._request_user_input(conv, "请选择", ["继续", "取消"]))
            await asyncio.sleep(0)
            async with client.ws_connect(url) as recovered:
                snapshot = await recovered.receive_json(timeout=2)
                assert snapshot["turns"][0]["answer"] == "x" * 1005
                assert snapshot["state"] == "running"
                request = snapshot["pending_user_inputs"][0]
                assert request["question"] == "请选择"
                assert service.answer_user_input("audit", request["request_id"], "1")["ok"]
                assert not service.answer_user_input("audit", request["request_id"], "1")["ok"]
                assert await question_task == "1"
                ended = await recovered.receive_json(timeout=2)
                assert ended["type"] == "ask_user_end" and ended["seq"] == snapshot["seq"] + 1
                completed = recorder.end_turn("finished")
                assert completed is not None
                service._broadcast(conv, {"type": "turn_done", "answer": "finished", **completed})
                assert (await recovered.receive_json(timeout=2))["type"] == "turn_done"
                assert service.runtime_snapshot("audit")["state"] == "idle"
    finally:
        if question_task and not question_task.done():
            question_task.cancel()
            await asyncio.gather(question_task, return_exceptions=True)
        await server.stop()
        recorder.close()

def test_subscriber_byte_budget_is_independent_of_message_count():
    queue = _SubscriberQueue(maxsize=1000, max_bytes=128)
    queue.put_nowait({"type": "content_delta", "delta": "x" * 60})
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait({"type": "content_delta", "delta": "x" * 60})
    queue.invalidate()
    assert queue.qsize() == 1 and queue.buffered_bytes <= 128

def test_full_queue_requires_resynchronization():
    service = ChatService.__new__(ChatService)
    service._event_sequences = {}
    service._stream_id = "audit"
    queue = _SubscriberQueue(maxsize=1000)
    for _ in range(1000):
        queue.put_nowait({"type": "content", "delta": "old"})
    conv = cast(_Conversation, SimpleNamespace(conversation_id="audit", subscribers={queue}))
    service._broadcast(conv, {"type": "turn_done"})
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert events == [{"type": "resync_required"}]
    assert queue.invalidated and queue not in conv.subscribers

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "satrap_coding"

class _EchoTool(Tool):
    tool_name = "echo"
    description = "echo"
    params_dict = {"x": ("string", "input")}

    def execute(self, x: str = "") -> str:
        return f"echo:{x}"

def test_truncate_arguments_limits_each_value():
    """每个参数值截断到前 20 字符, 非 dict 输入返回空对象"""
    args: dict[str, Any] = {"short": "abc", "long": "x" * 50, "num": 42}
    out = json.loads(_truncate_arguments(args))
    assert out["short"] == "abc"
    assert out["long"] == "x" * 20 + "…"
    assert out["num"] == "42"
    assert _truncate_arguments(None) == "{}"
    assert _truncate_arguments("str") == "{}"

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_tool_observer_start_and_end_fired(asynchronous: bool):
    """挂接钩子后 start/end 均触发, end 带 success (同步与异步管理器镜像)"""
    if asynchronous:
        from satrap.core.utils.TCBuilder import AsyncTool

        class _AEcho(AsyncTool):
            tool_name = "aecho"
            description = "aecho"
            params_dict = {"x": ("string", "input")}

            async def execute(self, x: str = "") -> str:
                return f"echo:{x}"

        m = AsyncToolsManager()
        m.register_tool(_AEcho())
        async_events: list[str] = []
        m.tool_call_start = lambda e: async_events.append("start")
        m.tool_call_end = lambda e: async_events.append(f"end:{e['success']}")
        _, res = await m.execute_tool_call({"name": "aecho", "arguments": {"x": "hi"}, "id": "c2"})
        assert res == "echo:hi"
        assert async_events == ["start", "end:True"]
    else:
        m = ToolsManager()
        m.register_tool(_EchoTool())
        events: list[tuple[str, dict[str, Any]]] = []
        m.tool_call_start = lambda e: events.append(("start", e))
        m.tool_call_end = lambda e: events.append(("end", e))
        m.execute_tool_call({"name": "echo", "arguments": {"x": "hi"}, "id": "c2"})
        assert events[0][0] == "start"
        assert events[0][1]["name"] == "echo" and events[0][1]["call_id"] == "c2"
        assert "success" not in events[0][1]
        assert events[1][0] == "end" and events[1][1]["success"] is True

def test_tool_observer_failure_marks_success_false():
    """工具执行失败 (ok=False) 时 end 事件 success=False"""
    m = ToolsManager()
    ends: list[dict[str, Any]] = []
    m.tool_call_end = ends.append
    m.execute_tool_call({"name": "ghost", "arguments": {}, "id": "c3"})
    assert ends and ends[0]["success"] is False

def test_tool_observer_exception_isolated():
    """钩子异常被隔离, 不影响工具执行"""
    m = ToolsManager()
    m.register_tool(_EchoTool())

    def _boom(e: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    m.tool_call_start = _boom
    _, res = m.execute_tool_call({"name": "echo", "arguments": {"x": "ok"}, "id": "c4"})
    assert res == "echo:ok"

def test_recorder_turn_and_tool_calls(tmp_path: Any):
    """start_turn 提前插行 -> 工具状态实时 -> end_turn 回填 thinking/answer; 失败与兜底一并覆盖"""
    db = str(tmp_path / "display.db")
    rec = _display_recorder_DisplayRecorder(db, "conv-1")

    rec.start_turn("看看目录")
    # start_turn 后即有一行 answer 为空的 turn
    turns = rec.list_turns()
    assert len(turns) == 1 and turns[0]["user_input"] == "看看目录" and turns[0]["answer"] == ""

    rec.on_thinking("先")
    rec.on_thinking("思考")
    rec.on_content("最终")
    rec.on_content("回复")
    rec.on_tool_start({"name": "list_dir", "arguments": {"path": "."}, "call_id": "c1"})
    # 进行中: success 为 None
    turns = rec.list_turns()
    assert turns[0]["tool_calls"][0]["success"] is None
    rec.on_tool_end({"name": "list_dir", "call_id": "c1", "success": True})
    rec.end_turn()

    turns = rec.list_turns()
    t = turns[0]
    assert t["thinking"] == "先思考"
    assert t["answer"] == "最终回复"
    assert t["tool_calls"][0]["success"] is True
    assert t["tool_calls"][0]["name"] == "list_dir"
    rec.close()

    # 工具失败记录 success=False
    rec_fail = _display_recorder_DisplayRecorder(str(tmp_path / "fail.db"), "conv-fail")
    rec_fail.start_turn("x")
    rec_fail.on_tool_start({"name": "shell", "arguments": {"command": "rm"}, "call_id": "c9"})
    rec_fail.on_tool_end({"name": "shell", "call_id": "c9", "success": False})
    rec_fail.end_turn("done")
    assert rec_fail.list_turns()[0]["tool_calls"][0]["success"] is False
    rec_fail.close()

    # 回调流为空时 answer 用 end_turn 的 fallback 兜底
    rec_fallback = _display_recorder_DisplayRecorder(str(tmp_path / "fallback.db"), "conv-fallback")
    rec_fallback.start_turn("hi")
    rec_fallback.end_turn("兜底回复")
    assert rec_fallback.list_turns()[0]["answer"] == "兜底回复"
    rec_fallback.close()

def test_copy_turns_uses_consistent_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复制等待目标锁时源会话删除不得造成工具调用缺失"""
    class BlockingLock:
        """在进入目标临界区前提供确定性并发控制"""

        def __init__(self) -> None:
            """初始化进入和放行事件"""
            self.entered = threading.Event()
            self.release = threading.Event()

        def __enter__(self) -> BlockingLock:
            """记录锁请求并等待测试放行"""
            self.entered.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("等待目标锁放行超时")
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: object,
        ) -> None:
            """退出测试锁"""

    db_path = str(tmp_path / "copy.db")
    source = _display_recorder_DisplayRecorder(db_path, "source")
    target = _display_recorder_DisplayRecorder(db_path, "target")
    source.start_turn("问题")
    source.on_tool_start({"name": "shell", "arguments": {}, "call_id": "call-1"})
    source.on_tool_end({"call_id": "call-1", "success": True})
    source.end_turn("回答")
    blocking_lock = BlockingLock()
    monkeypatch.setattr(target, "_lock", blocking_lock)
    copied_counts: list[int] = []

    worker = threading.Thread(
        target=lambda: copied_counts.append(source.copy_turns_to(target, 1))
    )
    worker.start()
    assert blocking_lock.entered.wait(timeout=1)
    source.delete_conversation()
    blocking_lock.release.set()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert copied_counts == [1]
    copied_turns = target.list_turns()
    assert len(copied_turns) == 1
    assert [tool["name"] for tool in copied_turns[0]["tool_calls"]] == ["shell"]
    source.close()
    target.close()

def test_recorder_answer_fallback(tmp_path: Any):
    """回调流为空时 answer 用 end_turn 的 fallback 兜底"""
    db = str(tmp_path / "display.db")
    rec = _display_recorder_DisplayRecorder(db, "conv-1")
    rec.start_turn("hi")
    rec.end_turn("兜底回复")
    assert rec.list_turns()[0]["answer"] == "兜底回复"

def test_recorder_turn_index_continues_across_restart(tmp_path: Any):
    """turn_index 跨重启连续 (启动查 max+1)"""
    db = str(tmp_path / "display.db")
    rec1 = _display_recorder_DisplayRecorder(db, "conv-1")
    rec1.start_turn("一")
    rec1.end_turn("r1")
    rec1.close()

    rec2 = _display_recorder_DisplayRecorder(db, "conv-1")
    rec2.start_turn("二")
    rec2.end_turn("r2")
    turns = rec2.list_turns()
    assert [t["turn_index"] for t in turns] == [0, 1]

def test_recorder_tool_failure_recorded(tmp_path: Any):
    """工具失败记录 success=False"""
    db = str(tmp_path / "display.db")
    rec = _display_recorder_DisplayRecorder(db, "conv-1")
    rec.start_turn("x")
    rec.on_tool_start({"name": "shell", "arguments": {"command": "rm"}, "call_id": "c9"})
    rec.on_tool_end({"name": "shell", "call_id": "c9", "success": False})
    rec.end_turn("done")
    assert rec.list_turns()[0]["tool_calls"][0]["success"] is False

def test_recorder_retry_variants_are_persisted_and_selectable(tmp_path: Any):
    """Retry 追加回复版本并隔离工具调用, 切换后同步当前展示值"""
    db = str(tmp_path / "display.db")
    rec = _display_recorder_DisplayRecorder(db, "conv-variants")
    started = rec.start_turn("问题")
    assert started["turn_index"] == 0 and started["variant_index"] == 0
    rec.on_content("第一版")
    rec.on_tool_start({"name": "first", "arguments": {}, "call_id": "first-call"})
    rec.on_tool_end({"call_id": "first-call", "success": True})
    rec.end_turn(
        context_messages=[
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "第一版"},
        ],
        context_stats={"request_count": 1, "last_request": {"strategy": "sliding"}},
    )

    retry = rec.start_retry_variant()
    assert retry is not None and retry["variant_index"] == 1
    rec.on_content("第二版")
    rec.on_tool_start({"name": "second", "arguments": {}, "call_id": "second-call"})
    rec.on_tool_end({"call_id": "second-call", "success": False})
    rec.end_turn(
        context_messages=[
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "第二版"},
        ],
        context_stats={"request_count": 2, "last_request": {"strategy": "summarize"}},
    )

    turn = rec.list_turns()[0]
    assert turn["variant_count"] == 2 and turn["active_variant"] == 1
    assert turn["answer"] == "第二版"
    assert [item["name"] for item in turn["tool_calls"]] == ["second"]
    assert [item["answer"] for item in turn["variants"]] == ["第一版", "第二版"]
    assert turn["context_stats"]["request_count"] == 2
    assert [item["context_stats"]["request_count"] for item in turn["variants"]] == [1, 2]

    selected = rec.activate_variant(0, 0)
    assert selected is not None and selected["answer"] == "第一版"
    assert selected["context_stats"]["last_request"]["strategy"] == "sliding"
    assert [item["name"] for item in selected["tool_calls"]] == ["first"]
    context = rec.variant_context(0, 0)
    assert context is not None and context[-1]["content"] == "第一版"

def test_recorder_isolated_from_conversations(tmp_path: Any):
    """不同 conversation_id 数据隔离"""
    db = str(tmp_path / "display.db")
    ra = _display_recorder_DisplayRecorder(db, "conv-a")
    rb = _display_recorder_DisplayRecorder(db, "conv-b")
    ra.start_turn("a1")
    ra.end_turn("ra")
    rb.start_turn("b1")
    rb.end_turn("rb")
    assert len(ra.list_turns()) == 1 and ra.list_turns()[0]["user_input"] == "a1"
    assert len(rb.list_turns()) == 1 and rb.list_turns()[0]["user_input"] == "b1"

class _ToolThenAnswerLLM(LLM):
    """第一轮流式返回工具调用, 第二轮流式返回最终答案"""

    def __init__(self) -> None:
        self.n = 0

    def call(self, messages: list[dict[str, Any]], **kw: Any) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content="x")

    def stream_call(self, messages: list[dict[str, Any]], **kw: Any) -> Iterator[LLMCallStreamEvent]:
        self.n += 1
        if self.n == 1:
            yield LLMCallStreamEvent(kind="thinking_delta", delta="先列目录")
            yield LLMCallStreamEvent(
                kind="done",
                response=LLMCallResponse(
                    type="tools_call",
                    content="",
                    # LLMCall 层已将 OpenAI 嵌套格式转为扁平 call_info: {name, id, arguments(dict)}
                    tool_calls=[{"name": "list_dir", "id": "c1", "arguments": {"path": "."}}],
                    usage=TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12),
                ),
            )
        else:
            yield LLMCallStreamEvent(kind="thinking_delta", delta="整理结果")
            yield LLMCallStreamEvent(kind="content_delta", delta="目录已列出")
            yield LLMCallStreamEvent(
                kind="done",
                response=LLMCallResponse(
                    type="answer",
                    content="目录已列出",
                    usage=TokenUsage(input_tokens=15, output_tokens=5, total_tokens=20),
                ),
            )

def test_end_to_end_session_with_plugin(tmp_path: Any, monkeypatch: Any):
    """真实 SimpleSession + satrap_coding 插件 + recorder 全链路"""
    import satrap.expend.plugins.satrap_coding.tools as tools_mod

    monkeypatch.setattr(tools_mod, "WORKSPACE_ROOT", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    db = str(tmp_path / "display.db")
    rec = _display_recorder_DisplayRecorder(db, "conv-1")
    s = SimpleSession(
        "conv-1", _ToolThenAnswerLLM(), db_path=str(tmp_path / "chat.db"),
        stream=True, return_thinking=True,
        content_callback=rec.on_content, thinking_callback=rec.on_thinking,
        enable_checkpoint=False,
    )
    s.install_plugin(str(PLUGIN_DIR))
    s.tools_manager.tool_call_start = rec.on_tool_start
    s.tools_manager.tool_call_end = rec.on_tool_end

    rec.start_turn("看看目录")
    ans = s.run("看看目录", thinking="medium")
    assert isinstance(ans, str)
    stats = s.get_context_stats()
    assert stats is not None and stats["request_count"] == 2
    assert stats["total_api_tokens"] == 32
    rec.end_turn(ans, context_stats=stats)

    turns = rec.list_turns()
    assert len(turns) == 1
    t = turns[0]
    assert t["user_input"] == "看看目录"
    assert "先列目录" in (t["thinking"] or "")
    assert "整理结果" in (t["thinking"] or "")
    assert t["answer"] == "目录已列出"
    assert len(t["tool_calls"]) == 1
    assert t["tool_calls"][0]["name"] == "list_dir"
    assert t["tool_calls"][0]["success"] is True
    assert t["context_stats"]["request_count"] == 2
    assert t["context_stats"]["last_request"]["api_input_tokens"] == 15

def test_recorder_meta_roundtrip_and_schema(tmp_path: Any):
    """conversation_meta 保存/读取 think, 新库默认思考模式与项目归属"""
    from satrap.display.recorder import get_conversation_meta

    db = str(tmp_path / "display.db")
    rec = _display_recorder_DisplayRecorder(db_path=db, conversation_id="c1")
    rec.save_meta("default", think="high")
    rec.close()

    meta = get_conversation_meta("c1", db_path=db)
    assert meta is not None
    assert meta["model"] == "default"
    assert meta["think"] == "high"
    assert meta["project_id"] is None

    rec2 = _display_recorder_DisplayRecorder(db_path=db, conversation_id="c2")
    rec2.save_meta("default")
    rec2.close()
    meta2 = get_conversation_meta("c2", db_path=db)
    assert meta2 is not None
    assert meta2["think"] == "off"
    assert meta2["project_id"] is None

def test_history_batch_queries_preserve_full_variants(tmp_path):
    """批量查询历史: 每条 turn 带全量 variants 与工具调用, 无逐行 N+1 查询"""
    recorder = _display_recorder_DisplayRecorder(str(tmp_path / "history.db"), "audit")
    try:
        for turn in range(8):
            recorder.start_turn(f"问题 {turn}")
            for variant in range(3):
                if variant:
                    recorder.start_retry_variant()
                recorder.on_tool_start({"name": "read", "arguments": "{}", "call_id": f"{turn}-{variant}"})
                recorder.on_tool_end({"name": "read", "call_id": f"{turn}-{variant}", "success": variant != 1})
                recorder.end_turn(f"回复 {turn}-{variant}")
        conn = recorder._get_conn()
        statements = []
        conn.set_trace_callback(statements.append)
        result = recorder.list_turns(limit=3, offset=2)
        conn.set_trace_callback(None)
        select_count = len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")])
        assert select_count == 3
        assert [row["turn_index"] for row in result] == [2, 3, 4]
        for row in result:
            turn = row["turn_index"]
            assert row["variant_count"] == 3
            assert [item["answer"] for item in row["variants"]] == [f"回复 {turn}-{v}" for v in range(3)]
            assert [item["name"] for item in row["tool_calls"]] == ["read"]
    finally:
        recorder.close()


@pytest.mark.parametrize("answer,expected", [("批准本次", "allow"), ("拒绝", "deny"), ("y", "deny"), ("all", "deny")])
async def test_coding_approval_ui_result_binds_exact_pending_request(answer, expected):
    """Chat 使用待回答 ID 回填单次审批, 普通肯定文本不获得权限"""
    from satrap.core.approval.contracts import ApprovalRequest
    request = ApprovalRequest.create(session_id="approval-ui", turn_id="turn", actor_id="", tool="write_file",
        operation="file_write", risk=1, description="修改文件", mode="user", revision=0,
        action={"path": "test.txt", "before": "before", "after": "after"},
        evidence=[{"source": "main_history", "content": "不应显示给审批卡片的历史正文"}])
    service = ChatService.__new__(ChatService)
    service._ask_user_timeout_seconds = 1
    conv = cast(_Conversation, SimpleNamespace(conversation_id="approval-ui", pending_user_inputs={}))
    service._conversations = {"approval-ui": conv}
    events = []
    service._broadcast = lambda conv, event: events.append(event)
    task = asyncio.create_task(service._request_approval(conv, request))
    await asyncio.sleep(0)
    pending_id = events[0]["request_id"]
    assert events[0]["options"] == ["批准本次", "拒绝"]
    assert events[0]["approval"]["action"]["before"] == "before"
    assert events[0]["approval"]["action"]["after"] == "after"
    assert "main_history" not in json.dumps(events[0], ensure_ascii=False)
    assert conv.pending_user_inputs[pending_id].approval == events[0]["approval"]
    assert not service.answer_user_input("approval-ui", "another-request", "批准本次")["ok"]
    assert not task.done()
    if answer not in {"批准本次", "拒绝"}:
        assert not service.answer_user_input("approval-ui", pending_id, answer)["ok"]
        assert not task.done()
        answer = "拒绝"
    assert service.answer_user_input("approval-ui", pending_id, answer)["ok"]
    result = await task
    assert (result.request_id, result.digest, result.reviewer, result.decision) == (request.request_id, request.digest, "user", expected)
    assert not conv.pending_user_inputs
    assert not service.answer_user_input("approval-ui", pending_id, "批准本次")["ok"]


async def test_coding_approval_ui_timeout_and_cancel_remove_pending_requests():
    """审批超时或取消清理待回填, 迟到批准不能复活"""
    from satrap.core.approval.contracts import ApprovalRequest
    request = ApprovalRequest.create(session_id="approval-ui", turn_id="", actor_id="", tool="shell",
        operation="shell", risk=2, description="命令", mode="user", revision=0,
        action={"command": "echo audit"}, evidence=[])
    service = ChatService.__new__(ChatService)
    service._ask_user_timeout_seconds = 0.01
    conv = cast(_Conversation, SimpleNamespace(conversation_id="approval-ui", pending_user_inputs={}))
    service._conversations = {"approval-ui": conv}
    events = []
    service._broadcast = lambda conv, event: events.append(event)
    with pytest.raises(TimeoutError):
        await service._request_approval(conv, request)
    assert events[-1]["status"] == "timeout" and not conv.pending_user_inputs
    service._ask_user_timeout_seconds = 1
    task = asyncio.create_task(service._request_approval(conv, request))
    await asyncio.sleep(0)
    pending_id = events[-1]["request_id"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events[-1]["status"] == "cancelled" and not conv.pending_user_inputs
    assert not service.answer_user_input("approval-ui", pending_id, "批准本次")["ok"]
