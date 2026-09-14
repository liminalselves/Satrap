"""真实上下文存储上的中断恢复测试"""
import pytest

from satrap.core.framework.Base.execution.engine import run_sync, run_async
from satrap.core.framework.Base.execution.store import RunStore, RunNeedsAttention, RunConflictError
from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.type import LLMCallResponse
from satrap.core.utils.TCBuilder import Tool


class Crash(BaseException):
    pass


class Model:
    model = "offline"

    def __init__(self):
        self.calls = 0

    def call(self, messages, **kwargs):
        self.calls += 1
        if not any(message.get("role") == "tool" for message in messages):
            return LLMCallResponse("tools_call", "", tool_calls=[{"id": "c1", "name": "write", "arguments": {}}])
        return LLMCallResponse("message", "done")


class Writer(Tool):
    tool_name = "write"
    description = "写入测试记录"
    params_dict = {}

    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self.calls == 1:
            raise Crash("工具已经发生副作用")
        return "written"


def test_uncertain_tool_requires_explicit_retry_and_reuses_model(tmp_path):
    model = Model()
    wf = ModelWorkflowFramework(model, "test", db_path=str(tmp_path / "p.db"))
    tool = Writer()
    wf.tools_manager.register_tool(tool)
    try:
        with pytest.raises(Crash):
            run_sync(wf, user_input="write", callback=False)
        run = wf.last_run_id
        store = RunStore(wf.ctx.db_path, wf.ctx.conversation_id)
        assert model.calls == 1
        with pytest.raises(RunNeedsAttention):
            run_sync(wf, run_id=run, callback=False)
        assert tool.calls == 1
        store.authorize_retry(run, "tool:0:0")
        assert run_sync(wf, run_id=run, callback=False) == "done"
        assert model.calls == 2
        assert tool.calls == 2
        assert run_sync(wf, run_id=run, callback=False) == "done"
        assert model.calls == 2
        assert len([m for m in wf.ctx.get_context() if m["role"] == "user"]) == 1
    finally:
        wf.ctx.close()


@pytest.mark.asyncio
async def test_async_resume_reuses_saved_request(tmp_path):
    class AsyncModel:
        model = "offline"

        def __init__(self):
            self.calls = 0

        async def call(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise Crash("中断")
            return LLMCallResponse("message", "done")

    model = AsyncModel()
    wf = AsyncModelWorkflowFramework(model, "test", db_path=str(tmp_path / "p.db"))
    await wf.initialize()
    with pytest.raises(Crash):
        await run_async(wf, user_input="hello", callback=False)
    assert await run_async(wf, run_id=wf.last_run_id, callback=False) == "done"
    assert wf.ctx.get_context()[-1]["content"] == "done"


@pytest.mark.parametrize("change", ["history", "model"])
def test_resume_rejects_changed_inputs_before_tool_execution(tmp_path, change):
    model = Model()
    wf = ModelWorkflowFramework(model, "test", db_path=str(tmp_path / "p.db"))
    tool = Writer()
    wf.tools_manager.register_tool(tool)
    try:
        with pytest.raises(Crash):
            run_sync(wf, user_input="write", callback=False)
        if change == "history":
            wf.ctx.add_user_message("another turn")
        else:
            model.model = "different"
        with pytest.raises(RunConflictError):
            run_sync(wf, run_id=wf.last_run_id, callback=False)
        assert model.calls == tool.calls == 1
    finally:
        wf.ctx.close()


@pytest.mark.asyncio
async def test_async_session_cancel_is_terminal(tmp_path):
    import asyncio
    from satrap import AsyncSimpleSession

    class CancelModel:
        model = "offline"

        async def call(self, messages, **kwargs):
            raise asyncio.CancelledError()

    session = AsyncSimpleSession("cancel", CancelModel(), db_path=str(tmp_path / "p.db"), recoverable=True)
    with pytest.raises(asyncio.CancelledError):
        await session.run("hello")
    runs = await session.list_runs()
    assert runs[0]["status"] == "cancelled"
    with pytest.raises(RunConflictError):
        await session.resume_run(runs[0]["id"])


def test_sync_stream_and_completed_replay_preserve_usage(tmp_path):
    from satrap.core.type import LLMCallStreamEvent, TokenUsage

    class StreamingModel:
        model = "offline"
        calls = 0

        def stream_call(self, messages, **kwargs):
            self.calls += 1
            yield LLMCallStreamEvent("content_delta", delta="done")
            yield LLMCallStreamEvent("done", response=LLMCallResponse("message", "done", usage=TokenUsage(10, 2, 12)))

    model = StreamingModel()
    wf = ModelWorkflowFramework(model, "test", db_path=str(tmp_path / "p.db"), recoverable=True)
    try:
        assert wf.stream_full_agent("hello", callback=False) == "done"
        original = wf.get_context_stats()
        assert original is not None
        assert run_sync(wf, run_id=wf.last_run_id, callback=False) == "done"
        assert wf.get_context_stats() == original
        assert model.calls == 1
    finally:
        wf.ctx.close()
