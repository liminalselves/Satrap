from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.edictum.simple_session import AsyncSimpleSession, SimpleSession
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.core.framework.providers import EdictumProvider
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.type import SessionConfig, UserCall


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


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_platform_settings_reach_sdk_and_survive_model_reload(tmp_path, monkeypatch, asynchronous, stream):
    """平台入口的参数覆盖进入真实请求构造, 流式和模型重载保持一致"""
    requests: list[dict[str, Any]] = []
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="回复"), finish_reason="stop")])
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="回复", tool_calls=None))])

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        return iter([chunk]) if kwargs.get("stream") else response

    async def chunks():
        yield chunk

    async def acreate(**kwargs):
        requests.append(deepcopy(kwargs))
        return chunks() if kwargs.get("stream") else response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=acreate if asynchronous else create)))
    monkeypatch.setattr("satrap.core.APICall.LLMCall.async_.AsyncOpenAI" if asynchronous else "satrap.core.APICall.LLMCall.sync.OpenAI", lambda **kwargs: client)
    llm_type = AsyncLLM if asynchronous else LLM
    llm = llm_type(api_key="test", model="probe", temperature=0.7, thinking_fields=["reasoning_effort"], suppress_error=False)
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    configs.create("assistant", {"edictum_type": "async_simple" if asynchronous else "simple", "params": {
        "thinking": "high", "temperature": 0, "model_params": {"top_p": 0.8, "max_tokens": 2048}, "stream": stream,
    }})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(tmp_path / "context.db"))
    session = provider.create_session(SessionConfig(session_id="request", session_type_name="assistant", provider_name="edictum"), llm)
    call = UserCall(session_id="request", message="测试参数")
    if isinstance(session, AsyncSimpleSession):
        await session.initialize()
        assert await SessionManager._invoke_async_session(session, call) == "回复"
        await session.run("本次关闭思考", thinking="off")
        session.reload_llm(cast(AsyncLLM, llm_type(api_key="test", thinking_fields=["reasoning_effort"], suppress_error=False)))
        await SessionManager._invoke_async_session(session, call)
    else:
        assert isinstance(session, SimpleSession)
        assert SessionManager._invoke_sync_session(session, call) == "回复"
        session.run("本次关闭思考", thinking="off")
        session.reload_llm(cast(LLM, llm_type(api_key="test", thinking_fields=["reasoning_effort"], suppress_error=False)))
        SessionManager._invoke_sync_session(session, call)
    assert len(requests) == 3
    assert [request["extra_body"]["reasoning_effort"] for request in requests] == ["high", "none", "high"]
    for request in requests:
        assert request["temperature"] == 0
        assert request["top_p"] == 0.8
        assert request["max_tokens"] == 2048
        assert bool(request.get("stream")) == stream


@pytest.mark.parametrize("params", [
    {"thinking": "invalid"}, {"model_params": []}, {"model_params": {"api_key": "bad"}},
    {"model_params": {"temperature": True}}, {"model_params": {"temperature": float("nan")}},
    {"model_params": {"temperature": 3}}, {"model_params": {"top_p": 0}},
    {"model_params": {"max_tokens": 2.5}}, {"temperature": 0, "model_params": {"temperature": 1}},
])
def test_invalid_settings_rejected_before_persistence(tmp_path, params):
    """非法子配置应明确报错而不是保存后静默丢弃"""
    configs = EdictumConfigManager(create_default_edictum_type_registry(), tmp_path / "edictum.json")
    with pytest.raises(ValueError):
        configs.create("bad", {"edictum_type": "async_simple", "params": params})
    assert configs.list_configs() == {}
