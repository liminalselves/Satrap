from collections.abc import AsyncIterator, Iterator, Callable, Sequence
from pathlib import Path
import asyncio
import sqlite3
from typing import Any, cast
import pytest
import copy

from satrap.core.framework.Base.execution.errors import ModelCallError, ModelProtocolError
from satrap.core.framework.Base.execution.flow import agent_flow
from satrap.core.framework.Base.execution.store import RunStore
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_model_failure_preserves_error_and_does_not_commit_success(tmp_path, monkeypatch, asynchronous, stream):
    """结构化失败与流中断保留旧历史, 记录失败而非提交错误为回答"""
    from satrap.core.type import LLMErrorInfo

    info = LLMErrorInfo("network_timeout", "等待模型响应超时")
    script = Script([LLMCallResponse("error", "", error_info=info)])
    wf, callbacks = await make_workflow(tmp_path, script, asynchronous, True)
    before = copy.deepcopy(wf.ctx.get_context())

    def failed_stream(self, messages, **kwargs):
        yield LLMCallStreamEvent("content_delta", delta="未完成内容")
        yield LLMCallStreamEvent("error", error=info.text, error_info=info)
        raise AssertionError("错误事件后不应继续消费流")

    async def failed_async_stream(self, messages, **kwargs):
        for event in failed_stream(self, messages, **kwargs):
            yield event

    monkeypatch.setattr(SyncModel, "stream_call", failed_stream)
    monkeypatch.setattr(AsyncModel, "stream_call", failed_async_stream)
    with pytest.raises(ModelCallError, match="network_timeout") as failure:
        await invoke(wf, stream)
    assert failure.value.info == info
    assert wf.ctx.get_context() == before
    assert "等待模型响应超时" not in str(stored_history(wf))
    if stream:
        assert "未完成内容" in callbacks
    assert RunStore(wf.ctx.db_path, wf.ctx.conversation_id).get(wf.last_run_id)["status"] == "failed"
from satrap.core.type import RequestContextBlock
from satrap.core.utils.context.request import FINAL_RESPONSE_PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_temporary_agent_model_failure_keeps_structured_error(tmp_path, asynchronous, stream):
    from satrap.core.type import LLMErrorInfo
    info = LLMErrorInfo("rate_limit", "服务商限流", status=429)
    wf, _ = await make_workflow(tmp_path, Script([LLMCallResponse("error", info.text, error_info=info)]), asynchronous, False)
    method = wf.stream_tools_agent if stream else wf.tools_agent
    with pytest.raises(ModelCallError, match="429") as failure:
        if asynchronous:
            await method("临时任务")
        else:
            method("临时任务")
    assert failure.value.info == info
    assert not any(message.get("role") != "system" for message in wf.ctx.get_context())


CALL = {"id": "c1", "name": "noop", "arguments": {}}
TOOLS = LLMCallResponse("tools_call", "工具说明", tool_calls=[CALL], thinking="思考")
ANSWER = LLMCallResponse("message", "完成", thinking="思考")
Workflow = ModelWorkflowFramework | AsyncModelWorkflowFramework


def test_truncated_tool_call_is_rejected_before_execution() -> None:
    flow = agent_flow("测试", 1)
    next(flow)
    with pytest.raises(ModelProtocolError, match="max_tokens"):
        flow.send({
            "type": "tools_call", "content": "", "finish_reason": "length",
            "tool_calls": [{"id": "partial", "name": "noop", "arguments": {}}],
        })


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("recoverable", [False, True])
async def test_request_environment_survives_tools_but_never_enters_history(tmp_path: Path, asynchronous: bool, stream: bool, recoverable: bool) -> None:
    """连续发言和工具循环只携带本轮快照, 原始历史与数据库均不累积资料"""
    script = Script([TOOLS, ANSWER, ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, recoverable)
    first = (RequestContextBlock("identity", "platform", "机器人当前身份"), RequestContextBlock("environment", "group_chat", "当前发言人甲"))
    second = (RequestContextBlock("environment", "group_chat", "当前发言人乙"),)
    assert await invoke(wf, stream, 2, first) == "完成"
    assert await invoke(wf, stream, 2, second) == "完成"
    for request in script.requests[:2]:
        body = str(request["messages"])
        assert body.count("机器人当前身份") == body.count("当前发言人甲") == 1
    body = str(script.requests[-1]["messages"])
    assert body.count("当前发言人乙") == 1 and "当前发言人甲" not in body and "机器人当前身份" not in body
    assert "当前发言人" not in str(wf.ctx.get_context())
    assert "机器人当前身份" not in str(stored_history(wf))


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_request_environment_recovery_uses_original_snapshot(tmp_path: Path, asynchronous: bool, stream: bool) -> None:
    """中断后不接受新来源资料覆盖原轮快照, 执行记录保留快照但历史不保留"""
    from satrap.core.framework.Base.execution.engine import run_async, run_sync
    script = Script([TOOLS, RuntimeError("临时失败"), ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, True)
    blocks = (RequestContextBlock("environment", "group_chat", "原轮发言人甲"),)
    with pytest.raises(RuntimeError, match="临时失败"):
        await invoke(wf, stream, 2, blocks)
    assert wf.last_run_id is not None
    restored = (RequestContextBlock("environment", "group_chat", "后来发言人乙"),)
    if isinstance(wf, AsyncModelWorkflowFramework):
        assert await run_async(wf, run_id=wf.last_run_id, request_blocks=restored) == "完成"
    else:
        assert run_sync(wf, run_id=wf.last_run_id, request_blocks=restored) == "完成"
    assert script.requests[1]["messages"] == script.requests[2]["messages"]
    assert "原轮发言人甲" in str(script.requests[-1]) and "后来发言人乙" not in str(script.requests[-1])
    assert "原轮发言人甲" not in str(stored_history(wf))


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_recovery_rejects_changed_default_agent_prompt(tmp_path, asynchronous):
    """切换提示后拒绝恢复旧轮次, 还原后继续使用原请求快照"""
    from satrap.core.framework.Base.execution.engine import run_async, run_sync
    from satrap.core.framework.Base.execution.store import RunConflictError
    script = Script([TOOLS, RuntimeError("临时失败"), ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, True)
    with pytest.raises(RuntimeError, match="临时失败"):
        await invoke(wf, False, 2)
    wf.default_agent_prompt_enabled = True
    with pytest.raises(RunConflictError, match="配置已改变"):
        if isinstance(wf, AsyncModelWorkflowFramework):
            await run_async(wf, run_id=wf.last_run_id)
        else:
            run_sync(wf, run_id=wf.last_run_id)
    wf.default_agent_prompt_enabled = False
    if isinstance(wf, AsyncModelWorkflowFramework):
        assert await run_async(wf, run_id=wf.last_run_id) == "完成"
    else:
        assert run_sync(wf, run_id=wf.last_run_id) == "完成"
    assert script.requests[1]["messages"] == script.requests[2]["messages"]


class Script:
    """记录外部模型的动态请求, 替身边界沿用 JSON 消息类型"""

    def __init__(self, replies: Sequence[LLMCallResponse | bool | BaseException]) -> None:
        """保存测试脚本或初始化响应记录"""
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.observe: Callable[[], object] = lambda: None
        self.histories: list[object] = []

    def next(self, messages: list[dict[str, Any]], kwargs: dict[str, Any]) -> LLMCallResponse | bool:
        """记录请求及调用时历史, 返回预设响应或抛出预设异常"""
        self.histories.append(copy.deepcopy(self.observe()))
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class SyncModel:
    model = "offline"

    def __init__(self, script: Script) -> None:
        """保存测试脚本或初始化响应记录"""
        self.script = script

    def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCallResponse | bool:
        """使用预设脚本模拟非流式模型响应"""
        return self.script.next(messages, kwargs)

    def stream_call(self, messages: list[dict[str, Any]], **kwargs: Any) -> Iterator[LLMCallStreamEvent]:
        """使用预设脚本模拟思考, 内容和最终响应事件"""
        reply = self.script.next(messages, kwargs)
        if isinstance(reply, LLMCallResponse):
            yield LLMCallStreamEvent("thinking_delta", delta=reply.thinking or "")
            yield LLMCallStreamEvent("content_delta", delta=reply.content)
            yield LLMCallStreamEvent("done", response=reply)


class AsyncModel:
    model = "offline"

    def __init__(self, script: Script) -> None:
        """保存测试脚本或初始化响应记录"""
        self.script = script

    async def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCallResponse | bool:
        """使用预设脚本模拟非流式模型响应"""
        return self.script.next(messages, kwargs)

    async def stream_call(self, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[LLMCallStreamEvent]:
        """使用预设脚本模拟思考, 内容和最终响应事件"""
        reply = self.script.next(messages, kwargs)
        if isinstance(reply, LLMCallResponse):
            yield LLMCallStreamEvent("thinking_delta", delta=reply.thinking or "")
            yield LLMCallStreamEvent("content_delta", delta=reply.content)
            yield LLMCallStreamEvent("done", response=reply)


class Noop(Tool):
    tool_name = "noop"
    description = "返回固定测试结果"
    params_dict = {}

    def execute(self) -> str:
        """返回固定工具结果以验证执行轮数"""
        return "工具结果"


class AsyncNoop(AsyncTool):
    tool_name = "noop"
    description = "返回固定测试结果"
    params_dict = {}

    async def execute(self) -> str:
        """返回固定工具结果以验证执行轮数"""
        return "工具结果"


async def make_workflow(tmp_path: Path, script: Script, asynchronous: bool, recoverable: bool) -> tuple[Workflow, list[str]]:
    """构造带有既存历史的工作流并收集回调"""
    events: list[str] = []
    database = str(tmp_path / "history.db")
    if asynchronous:
        async def callback(text: str) -> None:
            """收集异步模型回调"""
            events.append(text)
        wf = AsyncModelWorkflowFramework(cast(AsyncLLM, AsyncModel(script)), "test", db_path=database,
                                         recoverable=recoverable, content_callback=callback, return_thinking=True)
        await wf.initialize()
        wf.tools_manager.register_tool(AsyncNoop())
        await wf.ctx.add_user_message("旧输入")
        await wf.ctx.add_bot_message("旧回答")
        await wf.ctx.load_context()
    else:
        def sync_callback(text: str) -> None:
            """收集同步模型回调"""
            events.append(text)
        wf = ModelWorkflowFramework(cast(LLM, SyncModel(script)), "test", db_path=database,
                                    recoverable=recoverable, content_callback=sync_callback, return_thinking=True)
        wf.tools_manager.register_tool(Noop())
        wf.ctx.add_user_message("旧输入")
        wf.ctx.add_bot_message("旧回答")
        wf.ctx.load_context()
    script.observe = wf.ctx.get_context
    return wf, events


async def invoke(wf: Workflow, stream: bool, iterations: int = 1, request_blocks: tuple[RequestContextBlock, ...] = ()) -> str:
    """通过指定入口调用统一工作流"""
    if isinstance(wf, AsyncModelWorkflowFramework):
        if stream:
            return await wf.stream_full_agent("新输入", thinking="high", max_iterations=iterations, request_blocks=request_blocks)
        return await wf.full_agent("新输入", thinking="high", max_iterations=iterations, request_blocks=request_blocks)
    if stream:
        return wf.stream_full_agent("新输入", thinking="high", max_iterations=iterations, request_blocks=request_blocks)
    return wf.full_agent("新输入", thinking="high", max_iterations=iterations, request_blocks=request_blocks)


def stored_history(wf: Workflow) -> list[tuple[str, str]]:
    """读取实际落库消息以验证原子提交"""
    with sqlite3.connect(wf.ctx.db_path) as db:
        return db.execute("SELECT role,content FROM chat_history WHERE conversation_id=? ORDER BY id", (wf.ctx.conversation_id,)).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("recoverable", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("iterations", [1, 2])
async def test_iterations_thinking_and_atomic_history(tmp_path: Path, asynchronous: bool, recoverable: bool, stream: bool, iterations: int) -> None:
    """验证各入口共用轮数上限, 思考参数与整轮提交约定"""
    rounds = iterations
    script = Script([TOOLS] * rounds + [ANSWER])
    wf, events = await make_workflow(tmp_path, script, asynchronous, recoverable)
    original = copy.deepcopy(wf.ctx.get_context())
    assert await invoke(wf, stream, iterations) == "完成"
    assert len(script.requests) == rounds + 1
    assert all(request["thinking"] == "high" for request in script.requests)
    assert all(request["tools"] for request in script.requests[:-1])
    assert script.requests[-1]["tools"] is None
    final_messages = script.requests[-1]["messages"]
    assert FINAL_RESPONSE_PROMPT in next(message["content"] for message in final_messages if message["role"] == "system")
    assert all("已达到最大工具调用尝试次数" not in str(message) for message in final_messages)
    assert all(history == original for history in script.histories)
    assert [text for role, text in stored_history(wf) if role == "user"].count("新输入") == 1
    assert stored_history(wf)[-1] == ("assistant", "完成")
    assert events == [value for _ in range(rounds) for value in ("思考", "工具说明")] + ["思考", "完成"]
    assert (wf.last_run_id is not None) == recoverable


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("recoverable", [False, True])
@pytest.mark.parametrize("failure, stream", [
    ("first", False), ("later", True), ("final_tools", False), ("cancel", True),
])
async def test_failures_never_commit_partial_history(tmp_path: Path, asynchronous: bool, recoverable: bool, stream: bool, failure: str) -> None:
    """验证失败或取消向外传播且不留下部分历史"""
    replies: list[LLMCallResponse | bool | BaseException] = {
        "first": [False], "later": [TOOLS, False], "final_tools": [TOOLS, TOOLS],
        "cancel": [asyncio.CancelledError()],
    }[failure]
    script = Script(replies)
    wf, _ = await make_workflow(tmp_path, script, asynchronous, recoverable)
    original = copy.deepcopy(wf.ctx.get_context())
    expected = asyncio.CancelledError if failure == "cancel" else ModelProtocolError if failure == "final_tools" else ModelCallError
    with pytest.raises(expected):
        await invoke(wf, stream)
    assert wf.ctx.get_context() == original
    assert stored_history(wf) == [("user", "旧输入"), ("assistant", "旧回答")]
    if recoverable:
        assert wf.last_run_id is not None
        run = RunStore(wf.ctx.db_path, wf.ctx.conversation_id).get(wf.last_run_id)
        assert run["status"] == ("cancelled" if failure == "cancel" else "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_memory_only_execution_does_not_flush(tmp_path: Path, asynchronous: bool) -> None:
    """验证普通内存模式成功后仍不写入数据库"""
    script = Script([ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, False)
    wf.ctx.keep_in_memory = True
    assert await invoke(wf, False) == "完成"
    assert stored_history(wf) == [("user", "旧输入"), ("assistant", "旧回答")]
    assert wf.ctx.get_context()[-1]["content"] == "完成"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sql_failure_rolls_back_complete_turn(tmp_path: Path, asynchronous: bool) -> None:
    """通过真实数据库写入失败验证内存和持久化历史回滚"""
    script = Script([ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, False)
    original = copy.deepcopy(wf.ctx.get_context())
    with sqlite3.connect(wf.ctx.db_path) as db:
        db.execute("CREATE TRIGGER reject_answer BEFORE INSERT ON chat_history WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await invoke(wf, False)
    assert wf.ctx.get_context() == original
    assert stored_history(wf) == [("user", "旧输入"), ("assistant", "旧回答")]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_legacy_executor_returns_false_on_failure(tmp_path: Path, asynchronous: bool) -> None:
    """验证兼容执行器仍使用二元组报告失败"""
    script = Script([False])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, False)
    original = copy.deepcopy(wf.ctx.get_context())
    if isinstance(wf, AsyncModelWorkflowFramework):
        context, success = await wf.agent_executor(TOOLS, max_iterations=1)
    else:
        context, success = wf.agent_executor(TOOLS, max_iterations=1)
    assert not success
    assert context == original


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("recoverable", [False, True])
async def test_non_stream_session_supports_thinking(tmp_path: Path, asynchronous: bool, recoverable: bool) -> None:
    """验证会话非流式入口向模型传递思考强度"""
    from satrap import SimpleSession, AsyncSimpleSession

    script = Script([ANSWER])
    database = str(tmp_path / "session.db")
    if asynchronous:
        session = AsyncSimpleSession("session", cast(AsyncLLM, AsyncModel(script)), db_path=database,
                                     enable_checkpoint=False, recoverable=recoverable)
        assert await session.run("问题", thinking="high") == "完成"
    else:
        session = SimpleSession("session", cast(LLM, SyncModel(script)), db_path=database,
                                enable_checkpoint=False, recoverable=recoverable)
        assert session.run("问题", thinking="high") == "完成"
    assert script.requests[0]["thinking"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_existing_positional_images_remain_compatible(tmp_path: Path, asynchronous: bool) -> None:
    """验证新增思考参数不改变原图片位置参数含义"""
    script = Script([ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, False)
    wf.llm.supports_visual_input = True
    images = ["data:image/png;base64,aW1hZ2U="]
    if isinstance(wf, AsyncModelWorkflowFramework):
        assert await wf.full_agent("问题", False, 1, images, thinking="high") == "完成"
    else:
        assert wf.full_agent("问题", False, 1, images, thinking="high") == "完成"
    assert script.requests[0]["img_urls"] is None
    user = next(message for message in reversed(script.requests[0]["messages"]) if message["role"] == "user")
    assert user["content"][1]["image_url"]["url"] == images[0]
    assert script.requests[0]["thinking"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_resume_preserves_original_thinking(tmp_path: Path, asynchronous: bool) -> None:
    """验证恢复执行沿用原任务的思考强度"""
    from satrap.core.framework.Base.execution.engine import run_async, run_sync

    script = Script([False, ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, True)
    with pytest.raises(ModelCallError):
        await invoke(wf, False)
    assert wf.last_run_id is not None
    if isinstance(wf, AsyncModelWorkflowFramework):
        assert await run_async(wf, run_id=wf.last_run_id, thinking="low") == "完成"
    else:
        assert run_sync(wf, run_id=wf.last_run_id, thinking="low") == "完成"
    assert [request["thinking"] for request in script.requests] == ["high", "high"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_tool_media_recovery_reuses_frozen_result(tmp_path, monkeypatch, asynchronous, stream):
    from satrap.core.framework.Base.execution.engine import run_async, run_sync
    import base64

    source = tmp_path / "page.png"
    source.write_bytes(b"original-page")
    script = Script([TOOLS, RuntimeError("模型暂时失败"), ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, True)
    wf.llm.supports_visual_input = True
    executions = []

    def result():
        executions.append(1)
        return {"satrap_media_result": 1, "text": "页面文字", "media": [
            {"type": "image_url", "image_url": {"url": str(source)}},
        ]}

    async def async_result():
        return result()

    monkeypatch.setattr(wf.tools_manager.tools["noop"], "execute", async_result if asynchronous else result)
    with pytest.raises(RuntimeError, match="模型暂时失败"):
        await invoke(wf, stream, iterations=2)
    run_id = wf.last_run_id
    assert run_id is not None
    source.unlink()
    if isinstance(wf, AsyncModelWorkflowFramework):
        answer = await run_async(wf, run_id=run_id)
    else:
        answer = run_sync(wf, run_id=run_id)
    assert answer == "完成"
    assert executions == [1]
    tool = next(message for message in wf.ctx.get_context() if message["role"] == "tool")
    data = tool["content"][1]["image_url"]["url"]
    assert base64.b64decode(data.split(",")[1]) == b"original-page"
    assert script.requests[1]["messages"] == script.requests[2]["messages"]
    assert [m["content"] for m in wf.ctx.get_context() if m["role"] == "user"] == ["旧输入", "新输入"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["image", "video"])
async def test_user_media_recovery_survives_source_deletion(tmp_path, asynchronous, stream, kind):
    from satrap.core.framework.Base.execution.engine import run_async, run_sync
    import base64

    source = tmp_path / ("input.png" if kind == "image" else "input.mp4")
    source.write_bytes(b"original-input")
    script = Script([RuntimeError("模型暂时失败"), ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, True)
    wf.llm.supports_visual_input = True
    wf.llm.supports_native_video_input = kind == "video"
    images = [str(source)] if kind == "image" else None
    videos = [str(source)] if kind == "video" else None
    with pytest.raises(RuntimeError, match="模型暂时失败"):
        if isinstance(wf, AsyncModelWorkflowFramework):
            await run_async(wf, user_input="媒体问题", stream=stream, img_urls=images, video_urls=videos)
        else:
            run_sync(wf, user_input="媒体问题", stream=stream, img_urls=images, video_urls=videos)
    source.unlink()
    assert wf.last_run_id is not None
    if isinstance(wf, AsyncModelWorkflowFramework):
        await run_async(wf, run_id=wf.last_run_id)
    else:
        run_sync(wf, run_id=wf.last_run_id)
    assert script.requests[0]["messages"] == script.requests[1]["messages"]
    message = wf.ctx.get_context()[-2]
    assert message["role"] == "user"
    data = message["content"][1][kind + "_url"]["url"]
    if kind == "video":
        from satrap.core.utils.media_store import store_for
        assert store_for(wf.ctx.db_path, wf.ctx.conversation_id).read(data)[0] == b"original-input"
    else:
        assert base64.b64decode(data.split(",")[1]) == b"original-input"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_compat_executor_media_stays_on_original_user(tmp_path, asynchronous):
    script = Script([ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, False)
    wf.llm.supports_visual_input = True
    image = "data:image/png;base64,aW1hZ2U="
    if isinstance(wf, AsyncModelWorkflowFramework):
        _, success = await wf.agent_executor(TOOLS, max_iterations=1, img_urls=[image])
    else:
        _, success = wf.agent_executor(TOOLS, max_iterations=1, img_urls=[image])
    assert success
    users = [m for m in script.requests[0]["messages"] if m["role"] == "user"]
    assert users[0]["content"][1]["image_url"]["url"] == image
    assert len(users) == 1
    system_messages = [message for message in script.requests[0]["messages"] if message["role"] == "system"]
    assert system_messages and FINAL_RESPONSE_PROMPT in system_messages[0]["content"]
    assert all("已达到最大工具调用尝试次数" not in str(message) for message in users)
    assert script.requests[0]["img_urls"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_temporary_tools_agent_accepts_video(tmp_path, asynchronous, stream):
    script = Script([ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, False)
    wf.llm.supports_visual_input = True
    wf.llm.supports_native_video_input = True
    video = "data:video/mp4;base64,dmlkZW8="
    if isinstance(wf, AsyncModelWorkflowFramework):
        if stream:
            answer = await wf.stream_tools_agent("视频", video_urls=[video])
        else:
            answer = await wf.tools_agent("视频", video_urls=[video])
    else:
        if stream:
            answer = wf.stream_tools_agent("视频", video_urls=[video])
        else:
            answer = wf.tools_agent("视频", video_urls=[video])
    assert answer == "完成"
    user = next(m for m in script.requests[0]["messages"] if m["role"] == "user")
    assert user["content"][1]["video_url"]["url"] == video
    assert all(m["role"] == "system" for m in wf.ctx.get_context())
