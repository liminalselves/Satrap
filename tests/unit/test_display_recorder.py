"""DisplayRecorder 展示层旁路记录测试"""
from __future__ import annotations

import threading
from pathlib import Path
import pytest
from typing import Any, Iterator
import json
import time

from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import AsyncToolsManager, Tool, ToolsManager
from satrap.display.recorder import _truncate_arguments
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, TokenUsage
from satrap.display import DisplayRecorder
from satrap.edictum import SimpleSession

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "satrap_coding"


class _EchoTool(Tool):
    tool_name = "echo"
    description = "echo"
    params_dict = {"x": ("string", "input")}

    def execute(self, x: str = "") -> str:
        return f"echo:{x}"


# ---------- 参数截断 ----------


def test_truncate_arguments_limits_each_value():
    """每个参数值截断到前 20 字符, 超长加省略号"""
    args: dict[str, Any] = {"short": "abc", "long": "x" * 50, "num": 42}
    out = json.loads(_truncate_arguments(args))
    assert out["short"] == "abc"
    assert out["long"] == "x" * 20 + "…"
    assert out["num"] == "42"


def test_truncate_arguments_non_dict_returns_empty():
    assert _truncate_arguments(None) == "{}"
    assert _truncate_arguments("str") == "{}"


# ---------- ToolsManager 观察钩子 ----------


def test_tool_observer_default_none_no_behavior_change():
    """默认 None: execute_tool_call 行为不变"""
    m = ToolsManager()
    m.register_tool(_EchoTool())
    _, res = m.execute_tool_call({"name": "echo", "arguments": {"x": "hi"}, "id": "c1"})
    assert res == "echo:hi"


def test_tool_observer_start_and_end_fired():
    """挂接钩子后 start/end 均触发, end 带 success"""
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


@pytest.mark.asyncio
async def test_async_tool_observer_fired():
    """异步 AsyncToolsManager 同样触发钩子"""
    from satrap.core.utils.TCBuilder import AsyncTool

    class _AEcho(AsyncTool):
        tool_name = "aecho"
        description = "aecho"
        params_dict = {"x": ("string", "input")}

        async def execute(self, x: str = "") -> str:
            return f"echo:{x}"

    m = AsyncToolsManager()
    m.register_tool(_AEcho())
    events: list[str] = []
    m.tool_call_start = lambda e: events.append("start")
    m.tool_call_end = lambda e: events.append(f"end:{e['success']}")
    _, res = await m.execute_tool_call({"name": "aecho", "arguments": {"x": "hi"}, "id": "c5"})
    assert res == "echo:hi"
    assert events == ["start", "end:True"]


# ---------- DisplayRecorder 落库 ----------


def test_recorder_turn_and_tool_calls(tmp_path: Any):
    """
    start_turn 提前插行 -> 工具状态实时 -> end_turn 回填 thinking/answer

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    rec = DisplayRecorder(db, "conv-1")

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


def test_copy_turns_uses_consistent_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    复制等待目标锁时源会话删除不得造成工具调用缺失

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
            """
            退出测试锁

            参数:
            - exc_type: 异常类型
            - exc_value: 异常实例
            - traceback: 回溯对象
            """

    db_path = str(tmp_path / "copy.db")
    source = DisplayRecorder(db_path, "source")
    target = DisplayRecorder(db_path, "target")
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
    """
    回调流为空时 answer 用 end_turn 的 fallback 兜底

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    rec = DisplayRecorder(db, "conv-1")
    rec.start_turn("hi")
    rec.end_turn("兜底回复")
    assert rec.list_turns()[0]["answer"] == "兜底回复"


def test_recorder_turn_index_continues_across_restart(tmp_path: Any):
    """
    turn_index 跨重启连续 (启动查 max+1)

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    rec1 = DisplayRecorder(db, "conv-1")
    rec1.start_turn("一")
    rec1.end_turn("r1")
    rec1.close()

    rec2 = DisplayRecorder(db, "conv-1")
    rec2.start_turn("二")
    rec2.end_turn("r2")
    turns = rec2.list_turns()
    assert [t["turn_index"] for t in turns] == [0, 1]


def test_recorder_tool_failure_recorded(tmp_path: Any):
    """
    工具失败记录 success=False

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    rec = DisplayRecorder(db, "conv-1")
    rec.start_turn("x")
    rec.on_tool_start({"name": "shell", "arguments": {"command": "rm"}, "call_id": "c9"})
    rec.on_tool_end({"name": "shell", "call_id": "c9", "success": False})
    rec.end_turn("done")
    assert rec.list_turns()[0]["tool_calls"][0]["success"] is False


def test_recorder_retry_variants_are_persisted_and_selectable(tmp_path: Any):
    """
    Retry 追加回复版本并隔离工具调用, 切换后同步当前展示值

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    rec = DisplayRecorder(db, "conv-variants")
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
    """
    不同 conversation_id 数据隔离

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    ra = DisplayRecorder(db, "conv-a")
    rb = DisplayRecorder(db, "conv-b")
    ra.start_turn("a1")
    ra.end_turn("ra")
    rb.start_turn("b1")
    rb.end_turn("rb")
    assert len(ra.list_turns()) == 1 and ra.list_turns()[0]["user_input"] == "a1"
    assert len(rb.list_turns()) == 1 and rb.list_turns()[0]["user_input"] == "b1"


# ---------- 端到端: SimpleSession + 插件 ----------


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
    """
    真实 SimpleSession + satrap_coding 插件 + recorder 全链路

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    import satrap.expend.plugins.satrap_coding.tools as tools_mod

    monkeypatch.setattr(tools_mod, "DATA_ROOT", tmp_path / "coding")
    monkeypatch.setattr(tools_mod, "WORKSPACE_ROOT", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    db = str(tmp_path / "display.db")
    rec = DisplayRecorder(db, "conv-1")
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


def test_recorder_meta_think_roundtrip(tmp_path: Any):
    """
    conversation_meta 保存/读取 think 默认思考强度

    参数:
    - tmp_path: tmp路径
    """
    from satrap.display.recorder import get_conversation_meta

    db = str(tmp_path / "display.db")
    rec = DisplayRecorder(db_path=db, conversation_id="c1")
    rec.save_meta("default", think="high")
    rec.close()

    meta = get_conversation_meta("c1", db_path=db)
    assert meta is not None
    assert meta["model"] == "default"
    assert meta["think"] == "high"


def test_recorder_meta_has_complete_schema(tmp_path: Any):
    """
    新库的会话元数据直接包含默认思考模式和项目归属

    参数:
    - tmp_path: tmp路径
    """
    from satrap.display.recorder import get_conversation_meta

    db = str(tmp_path / "display.db")
    rec = DisplayRecorder(db_path=db, conversation_id="c1")
    rec.save_meta("default")
    rec.close()

    meta = get_conversation_meta("c1", db_path=db)
    assert meta is not None
    assert meta["think"] == "off"
    assert meta["project_id"] is None


def test_history_batch_queries_preserve_full_variants(tmp_path):
    recorder = DisplayRecorder(str(tmp_path / "history.db"), "audit")
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
        assert len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]) == 3
        assert [row["turn_index"] for row in result] == [2, 3, 4]
        for row in result:
            assert row["variants"] == recorder._list_variants_locked(conn, row["id"])
            assert row["tool_calls"] == recorder._list_tool_calls_locked(conn, row["id"], 2)
            assert row["variant_count"] == 3
    finally:
        recorder.close()
