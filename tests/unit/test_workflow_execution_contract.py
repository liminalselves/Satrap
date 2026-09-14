from collections.abc import AsyncIterator, Iterator, Callable, Sequence
from pathlib import Path
import asyncio
import sqlite3
from typing import Any, cast
import pytest
import copy

from satrap.core.framework.Base.execution.errors import ModelCallError, ModelProtocolError
from satrap.core.framework.Base.execution.store import RunStore
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent


CALL = {"id": "c1", "name": "noop", "arguments": {}}
TOOLS = LLMCallResponse("tools_call", "工具说明", tool_calls=[CALL], thinking="思考")
ANSWER = LLMCallResponse("message", "完成", thinking="思考")
Workflow = ModelWorkflowFramework | AsyncModelWorkflowFramework


class Script:
    """记录外部模型的动态请求, 替身边界沿用 JSON 消息类型"""

    def __init__(self, replies: Sequence[LLMCallResponse | bool | BaseException]) -> None:
        """
        保存测试脚本或初始化响应记录

        参数:
        - replies: 预设模型响应或异常序列
        """
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.observe: Callable[[], object] = lambda: None
        self.histories: list[object] = []

    def next(self, messages: list[dict[str, Any]], kwargs: dict[str, Any]) -> LLMCallResponse | bool:
        """
        记录请求及调用时历史, 返回预设响应或抛出预设异常

        参数:
        - messages: 本次模型请求消息
        - kwargs: 本次模型请求的动态 JSON 参数

        返回:
        - 预设模型响应或失败标记, 预设异常直接抛出
        """
        self.histories.append(copy.deepcopy(self.observe()))
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class SyncModel:
    model = "offline"

    def __init__(self, script: Script) -> None:
        """
        保存测试脚本或初始化响应记录

        参数:
        - script: 记录请求并提供预设结果的模型脚本
        """
        self.script = script

    def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCallResponse | bool:
        """
        使用预设脚本模拟非流式模型响应

        参数:
        - messages: 本次模型请求消息
        - kwargs: 本次模型请求的动态 JSON 参数

        返回:
        - 预设模型响应或失败标记
        """
        return self.script.next(messages, kwargs)

    def stream_call(self, messages: list[dict[str, Any]], **kwargs: Any) -> Iterator[LLMCallStreamEvent]:
        """
        使用预设脚本模拟思考, 内容和最终响应事件

        参数:
        - messages: 本次模型请求消息
        - kwargs: 本次模型请求的动态 JSON 参数

        返回:
        - 思考, 内容和最终响应事件迭代器, 失败标记不产生最终事件
        """
        reply = self.script.next(messages, kwargs)
        if isinstance(reply, LLMCallResponse):
            yield LLMCallStreamEvent("thinking_delta", delta=reply.thinking or "")
            yield LLMCallStreamEvent("content_delta", delta=reply.content)
            yield LLMCallStreamEvent("done", response=reply)


class AsyncModel:
    model = "offline"

    def __init__(self, script: Script) -> None:
        """
        保存测试脚本或初始化响应记录

        参数:
        - script: 记录请求并提供预设结果的模型脚本
        """
        self.script = script

    async def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCallResponse | bool:
        """
        使用预设脚本模拟非流式模型响应

        参数:
        - messages: 本次模型请求消息
        - kwargs: 本次模型请求的动态 JSON 参数

        返回:
        - 预设模型响应或失败标记
        """
        return self.script.next(messages, kwargs)

    async def stream_call(self, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[LLMCallStreamEvent]:
        """
        使用预设脚本模拟思考, 内容和最终响应事件

        参数:
        - messages: 本次模型请求消息
        - kwargs: 本次模型请求的动态 JSON 参数

        返回:
        - 思考, 内容和最终响应事件迭代器, 失败标记不产生最终事件
        """
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
        """
        返回固定工具结果以验证执行轮数

        返回:
        - 固定工具结果文本
        """
        return "工具结果"


class AsyncNoop(AsyncTool):
    tool_name = "noop"
    description = "返回固定测试结果"
    params_dict = {}

    async def execute(self) -> str:
        """
        返回固定工具结果以验证执行轮数

        返回:
        - 固定工具结果文本
        """
        return "工具结果"


async def make_workflow(tmp_path: Path, script: Script, asynchronous: bool, recoverable: bool) -> tuple[Workflow, list[str]]:
    """
    构造带有既存历史的工作流并收集回调

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - script: 记录请求并提供预设结果的模型脚本
    - asynchronous: 是否使用原生异步工作流
    - recoverable: 是否启用执行记录与恢复

    返回:
    - 工作流及回调输出列表
    """
    events: list[str] = []
    database = str(tmp_path / "history.db")
    if asynchronous:
        async def callback(text: str) -> None:
            """
            收集异步模型回调

            参数:
            - text: 模型回调输出
            """
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
            """
            收集同步模型回调

            参数:
            - text: 模型回调输出
            """
            events.append(text)
        wf = ModelWorkflowFramework(cast(LLM, SyncModel(script)), "test", db_path=database,
                                    recoverable=recoverable, content_callback=sync_callback, return_thinking=True)
        wf.tools_manager.register_tool(Noop())
        wf.ctx.add_user_message("旧输入")
        wf.ctx.add_bot_message("旧回答")
        wf.ctx.load_context()
    script.observe = wf.ctx.get_context
    return wf, events


async def invoke(wf: Workflow, stream: bool, iterations: int = 1) -> str:
    """
    通过指定入口调用统一工作流

    参数:
    - wf: 待验证的工作流
    - stream: 是否使用流式模型入口
    - iterations: 最大工具轮数, 默认 1

    返回:
    - 最终回答, 执行异常向外传播
    """
    if isinstance(wf, AsyncModelWorkflowFramework):
        if stream:
            return await wf.stream_full_agent("新输入", thinking="high", max_iterations=iterations)
        return await wf.full_agent("新输入", thinking="high", max_iterations=iterations)
    if stream:
        return wf.stream_full_agent("新输入", thinking="high", max_iterations=iterations)
    return wf.full_agent("新输入", thinking="high", max_iterations=iterations)


def stored_history(wf: Workflow) -> list[tuple[str, str]]:
    """
    读取实际落库消息以验证原子提交

    参数:
    - wf: 待验证的工作流

    返回:
    - 按写入顺序排列的角色和内容列表
    """
    with sqlite3.connect(wf.ctx.db_path) as db:
        return db.execute("SELECT role,content FROM chat_history WHERE conversation_id=? ORDER BY id", (wf.ctx.conversation_id,)).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("recoverable", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("iterations", [0, 1, 2])
async def test_iterations_thinking_and_atomic_history(tmp_path: Path, asynchronous: bool, recoverable: bool, stream: bool, iterations: int) -> None:
    """
    验证各入口共用轮数上限, 思考参数与整轮提交约定

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - asynchronous: 是否使用原生异步工作流
    - recoverable: 是否启用执行记录与恢复
    - stream: 是否使用流式模型入口
    - iterations: 最大工具轮数, 默认 1
    """
    rounds = max(1, iterations)
    script = Script([TOOLS] * rounds + [ANSWER])
    wf, events = await make_workflow(tmp_path, script, asynchronous, recoverable)
    original = copy.deepcopy(wf.ctx.get_context())
    assert await invoke(wf, stream, iterations) == "完成"
    assert len(script.requests) == rounds + 1
    assert all(request["thinking"] == "high" for request in script.requests)
    assert all(request["tools"] for request in script.requests[:-1])
    assert script.requests[-1]["tools"] == []
    assert script.requests[-1]["messages"][-1]["role"] == "user"
    assert "已达到最大工具调用尝试次数" in script.requests[-1]["messages"][-1]["content"]
    assert all(history == original for history in script.histories)
    assert [text for role, text in stored_history(wf) if role == "user"].count("新输入") == 1
    assert stored_history(wf)[-1] == ("assistant", "完成")
    assert events == [value for _ in range(rounds) for value in ("思考", "工具说明")] + ["思考", "完成"]
    assert (wf.last_run_id is not None) == recoverable


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("recoverable", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["first", "later", "final_tools", "cancel"])
async def test_failures_never_commit_partial_history(tmp_path: Path, asynchronous: bool, recoverable: bool, stream: bool, failure: str) -> None:
    """
    验证失败或取消向外传播且不留下部分历史

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - asynchronous: 是否使用原生异步工作流
    - recoverable: 是否启用执行记录与恢复
    - stream: 是否使用流式模型入口
    - failure: 待模拟的失败阶段或取消类型
    """
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
    """
    验证普通内存模式成功后仍不写入数据库

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - asynchronous: 是否使用原生异步工作流
    """
    script = Script([ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, False)
    wf.ctx.keep_in_memory = True
    assert await invoke(wf, False) == "完成"
    assert stored_history(wf) == [("user", "旧输入"), ("assistant", "旧回答")]
    assert wf.ctx.get_context()[-1]["content"] == "完成"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sql_failure_rolls_back_complete_turn(tmp_path: Path, asynchronous: bool) -> None:
    """
    通过真实数据库写入失败验证内存和持久化历史回滚

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - asynchronous: 是否使用原生异步工作流
    """
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
    """
    验证兼容执行器仍使用二元组报告失败

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - asynchronous: 是否使用原生异步工作流
    """
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
    """
    验证会话非流式入口向模型传递思考强度

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - asynchronous: 是否使用原生异步工作流
    - recoverable: 是否启用执行记录与恢复
    """
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
    """
    验证新增思考参数不改变原图片位置参数含义

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - asynchronous: 是否使用原生异步工作流
    """
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
    """
    验证恢复执行沿用原任务的思考强度

    参数:
    - tmp_path: 隔离测试数据库的临时目录
    - asynchronous: 是否使用原生异步工作流
    """
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
    assert isinstance(users[-1]["content"], str)
    assert "已达到最大" in users[-1]["content"]
    assert script.requests[0]["img_urls"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_temporary_tools_agent_accepts_video(tmp_path, asynchronous, stream):
    script = Script([ANSWER])
    wf, _ = await make_workflow(tmp_path, script, asynchronous, False)
    wf.llm.supports_visual_input = True
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
