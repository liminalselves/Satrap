from typing import cast

import pytest

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.edictum.simple_session import AsyncSimpleSession, SimpleSession


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("prompt", [None, "新的提示词", ""])
async def test_prompt_restart_preserves_history_and_supports_explicit_clear(tmp_path, asynchronous, prompt):
    """重启会话只替换明确配置的提示词, 清空和保留都不影响对话历史"""
    db_path = str(tmp_path / "context.db")
    if asynchronous:
        old = AsyncSimpleSession("prompt", cast(AsyncLLM, object()), system_prompt="旧提示词", db_path=db_path, enable_checkpoint=False)
        await old.initialize()
        assert old._wf is not None
        await old._wf.ctx.add_user_message("已有消息")
        session = AsyncSimpleSession("prompt", cast(AsyncLLM, object()), system_prompt=prompt, db_path=db_path, enable_checkpoint=False)
        await session.initialize()
        assert session._wf is not None
        messages = session._wf.ctx.get_context()
    else:
        old = SimpleSession("prompt", cast(LLM, object()), system_prompt="旧提示词", db_path=db_path, enable_checkpoint=False)
        old._wf.ctx.add_user_message("已有消息")
        session = SimpleSession("prompt", cast(LLM, object()), system_prompt=prompt, db_path=db_path, enable_checkpoint=False)
        messages = session._wf.ctx.get_context()
    assert [message["content"] for message in messages if message["role"] == "system"] == ["旧提示词" if prompt is None else prompt]
    assert [message["content"] for message in messages if message["role"] == "user"] == ["已有消息"]
