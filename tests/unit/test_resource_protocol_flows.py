"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest
from openai import APIError
from satrap.core.APICall.ASRCall import ALLOWED_AUDIO_SUFFIXES, ASR, AsyncASR, build_asr_from_config
from satrap.core.APICall.ASRCall import async_ as async_module, sync as sync_module
from satrap.core.APICall.ASRCall.utils import MAX_AUDIO_BYTES, prepare_audio_input
from satrap.core.type import ASRConfig, ASRResponse
from typing import cast
from satrap.core.APICall.EmbedCall import AsyncEmbedding, Embedding, parse_embedding_response
from satrap.core.APICall.ReRankCall import AsyncReRank, ReRank
from satrap.core.APICall import ReRankCall as rerank_module
import asyncio
import hashlib
import json
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.expend.tools.mem0 import Mem0Memory

def _clients(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None, response: Any = None):
    """替换模块级 SDK 符号, 记录请求并返回固定转录响应"""
    calls: list[list[dict[str, Any]]] = [[], []]
    payload = response if response is not None else SimpleNamespace(text="你好", language="zh", duration=1.5)

    def create(**kwargs: Any) -> Any:
        calls[0].append(kwargs)
        if error is not None:
            raise error
        return payload

    async def acreate(**kwargs: Any) -> Any:
        calls[1].append(kwargs)
        if error is not None:
            raise error
        return payload

    def _fake_sync_client(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))

    def _fake_async_client(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=acreate)))

    monkeypatch.setattr(sync_module, "OpenAI", _fake_sync_client)
    monkeypatch.setattr(async_module, "AsyncOpenAI", _fake_async_client)
    return calls

def _pair(monkeypatch: pytest.MonkeyPatch, **options: Any):
    calls = _clients(monkeypatch, **{k: v for k, v in options.items() if k in {"error", "response"}})
    kwargs = {k: v for k, v in options.items() if k not in {"error", "response"}}
    return (
        ASR(api_key="test", model="whisper-1", **kwargs),
        AsyncASR(api_key="test", model="whisper-1", **kwargs),
        calls,
    )

def test_prepare_audio_input_accepts_path_and_bytes(tmp_path: Path):
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"fake-audio")
    name, data, mime = prepare_audio_input(audio)
    assert (name, data, mime) == ("voice.mp3", b"fake-audio", "audio/mpeg")
    name, data, mime = prepare_audio_input(b"fake-audio", filename="clip.wav")
    assert (name, data, mime) == ("clip.wav", b"fake-audio", "audio/wav")

def test_prepare_audio_input_rejects_bad_inputs(tmp_path: Path):
    with pytest.raises(ValueError, match="不存在"):
        prepare_audio_input(tmp_path / "missing.mp3")
    with pytest.raises(ValueError, match="文件名"):
        prepare_audio_input(b"data")
    with pytest.raises(ValueError, match="路径成分"):
        prepare_audio_input(b"data", filename="a/b.mp3")
    with pytest.raises(ValueError, match="不支持的音频格式"):
        prepare_audio_input(b"data", filename="voice.silk")
    with pytest.raises(ValueError, match="为空"):
        prepare_audio_input(b"", filename="voice.mp3")
    big = tmp_path / "big.wav"
    big.write_bytes(b"x" * (MAX_AUDIO_BYTES + 1))
    with pytest.raises(ValueError, match="上限"):
        prepare_audio_input(big)
    assert ".silk" not in ALLOWED_AUDIO_SUFFIXES and ".mp3" in ALLOWED_AUDIO_SUFFIXES

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_transcribe_builds_request_and_parity(monkeypatch: pytest.MonkeyPatch, asynchronous: bool):
    """同步转录取全字段构造请求; 异步版与同步语义一致且可覆盖模型"""
    if asynchronous:
        _, async_asr, calls = _pair(monkeypatch, language="zh")
        result = await async_asr.transcribe(b"audio-bytes", filename="voice.ogg", model="other-model")
        assert result is not None and result.text == "你好" and result.model == "other-model"
        request = calls[1][0]
        assert request["model"] == "other-model" and request["language"] == "zh"
        assert "prompt" not in request
    else:
        asr, _, calls = _pair(monkeypatch, language="zh", prompt="提示")
        result = asr.transcribe(b"audio-bytes", filename="voice.ogg")
        assert isinstance(result, ASRResponse)
        assert (result.text, result.model, result.language, result.duration) == ("你好", "whisper-1", "zh", 1.5)
        request = calls[0][0]
        assert request["file"][0] == "voice.ogg" and request["file"][2] == "audio/ogg"
        assert request["model"] == "whisper-1" and request["response_format"] == "json"
        assert request["language"] == "zh" and request["prompt"] == "提示"

def test_optional_parameters_omitted_when_empty(monkeypatch: pytest.MonkeyPatch):
    asr, _, calls = _pair(monkeypatch)
    asr.transcribe(b"audio-bytes", filename="voice.mp3", language="", prompt="")
    request = calls[0][0]
    assert "language" not in request and "prompt" not in request

def test_dict_response_shape_and_empty_text_kept(monkeypatch: pytest.MonkeyPatch):
    asr, _, _ = _pair(monkeypatch, response={"text": "", "language": "en"})
    result = asr.transcribe(b"audio-bytes", filename="voice.mp3")
    assert result is not None and result.text == "" and result.language == "en" and result.duration == 0.0

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_api_error_suppressed_or_raised(monkeypatch: pytest.MonkeyPatch, asynchronous: bool):
    import httpx
    error = APIError("boom", request=httpx.Request("POST", "http://x"), body=None)
    client, async_client, _ = _pair(monkeypatch, error=error)
    strict, async_strict, _ = _pair(monkeypatch, error=error, suppress_error=False)
    if asynchronous:
        assert await async_client.transcribe(b"audio-bytes", filename="voice.mp3") is None
        with pytest.raises(APIError):
            await async_strict.transcribe(b"audio-bytes", filename="voice.mp3")
    else:
        assert client.transcribe(b"audio-bytes", filename="voice.mp3") is None
        with pytest.raises(APIError):
            strict.transcribe(b"audio-bytes", filename="voice.mp3")

def test_invalid_input_suppressed_or_raised(monkeypatch: pytest.MonkeyPatch):
    asr, _, calls = _pair(monkeypatch)
    assert asr.transcribe(b"data", filename="voice.silk") is None
    strict, _, _ = _pair(monkeypatch, suppress_error=False)
    with pytest.raises(ValueError, match="不支持的音频格式"):
        strict.transcribe(b"data", filename="voice.silk")
    assert calls == [[], []]

def test_build_asr_from_config_maps_fields(monkeypatch: pytest.MonkeyPatch):
    _clients(monkeypatch)
    cfg = ASRConfig(name="a", model="whisper-1", base_url="https://api.example.com/v1/", api_key="k",
                    language="zh", prompt="p", timeout=30)
    asr = build_asr_from_config(cfg)
    assert isinstance(asr, ASR)
    assert asr.model == "whisper-1" and asr.base_url == "https://api.example.com/v1"
    assert asr.language == "zh" and asr.prompt == "p"
    assert asr.api_key == "api key locked"
    async_asr = build_asr_from_config(cfg, async_=True)
    assert isinstance(async_asr, AsyncASR)

def test_build_asr_from_config_omits_none_defaults(monkeypatch: pytest.MonkeyPatch):
    _clients(monkeypatch)
    asr = build_asr_from_config(ASRConfig(model="m"))
    assert asr.language == "" and asr.prompt == ""

def test_insecure_base_url_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch):
    _clients(monkeypatch)
    with pytest.raises(ValueError, match="https"):
        ASR(api_key="k", base_url="http://8.8.8.8:8080/v1")
    asr = ASR(api_key="k", base_url="http://8.8.8.8:8080/v1", allow_insecure_base_url=True)
    assert asr.base_url == "http://8.8.8.8:8080/v1"

@pytest.mark.asyncio
async def test_suppressed_api_error_log_omits_response_body(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    """抑制模式下的 API 错误日志只含类型/状态/request_id, 不含服务端响应体"""
    import logging
    import httpx
    error = APIError("Error code: 400 - secret-body-echo", request=httpx.Request("POST", "http://x"), body={"echo": "secret-body-echo"})
    sync_client, async_client, _ = _pair(monkeypatch, error=error, suppress_error=True)
    with caplog.at_level(logging.ERROR):
        assert sync_client.transcribe(b"RIFF", filename="a.wav") is None
        assert await async_client.transcribe(b"RIFF", filename="a.wav") is None
    messages = [r.getMessage() for r in caplog.records]
    assert len([m for m in messages if "转录 API 错误" in m]) >= 2
    assert not any("secret-body-echo" in m for m in messages)
    assert any("APIError status=None request_id=None" in m for m in messages)

class _SyncEmbeddingEndpoint:
    """按顺序返回响应或抛出异常的同步 Embedding 端点替身"""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.inputs: list[list[str]] = []

    def create(self, **kwargs: Any) -> Any:
        """记录输入并返回下一项预设结果"""
        self.inputs.append(cast(list[str], kwargs["input"]))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

class _AsyncEmbeddingEndpoint(_SyncEmbeddingEndpoint):
    """按顺序返回响应或抛出异常的异步 Embedding 端点替身"""

    async def create(self, **kwargs: Any) -> Any:
        """记录输入并异步返回下一项预设结果"""
        self.inputs.append(cast(list[str], kwargs["input"]))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

def _response(*items: tuple[int, list[Any]]) -> SimpleNamespace:
    """构造包含指定 index 和向量的响应替身"""
    return SimpleNamespace(
        data=[SimpleNamespace(index=index, embedding=embedding) for index, embedding in items],
    )

def _sync_embedding(
    endpoint: _SyncEmbeddingEndpoint,
    *,
    max_batch_size: int = 2,
    suppress_error: bool = True,
    return_false: bool = False,
) -> Embedding:
    """构造不访问网络的同步 Embedding 实例"""
    embedding = object.__new__(Embedding)
    embedding.model = "test-model"
    embedding.dimensions = None
    embedding.encoding_format = "float"
    embedding.suppress_error = suppress_error
    embedding.return_false = return_false
    embedding.max_batch_size = max_batch_size
    cast(Any, embedding).client = SimpleNamespace(embeddings=endpoint)
    return embedding

def _async_embedding(
    endpoint: _AsyncEmbeddingEndpoint,
    *,
    max_batch_size: int = 2,
    suppress_error: bool = True,
    return_false: bool = False,
) -> AsyncEmbedding:
    """构造不访问网络的异步 Embedding 实例"""
    embedding = object.__new__(AsyncEmbedding)
    embedding.model = "test-model"
    embedding.dimensions = None
    embedding.encoding_format = "float"
    embedding.suppress_error = suppress_error
    embedding.return_false = return_false
    embedding.max_batch_size = max_batch_size
    cast(Any, embedding).client = SimpleNamespace(embeddings=endpoint)
    return embedding

@pytest.mark.parametrize("response, expected", [
    (None, []),
    (SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[0.1, 0.2, 0.3]), SimpleNamespace(index=0, embedding=[0.4, 0.5, 0.6])]),
     [[0.4, 0.5, 0.6], [0.1, 0.2, 0.3]]),
    ({"data": [{"index": 1, "embedding": [1.0]}, {"index": 0, "embedding": [0.0]}]}, [[0.0], [1.0]]),
])
def test_parse_embedding_response_variants(response: Any, expected: list[list[float]]):
    """缺失响应返回空; 对象与映射项均按 index 排序"""
    assert parse_embedding_response(response) == expected

@pytest.mark.parametrize("scenario", ["failed_middle_batch", "missing_index", "duplicate_index", "dimension_change", "return_false"])
def test_sync_embedding_batch_anomalies(scenario: str):
    """同步批处理异常: 失败批次留空位继续, 缺失/重复 index 与维度变化占空, return_false 整体拒绝"""
    if scenario == "failed_middle_batch":
        endpoint = _SyncEmbeddingEndpoint([
            _response((0, [1.0, 0.0]), (1, [0.0, 1.0])),
            RuntimeError("batch failed"),
            _response((0, [0.5, 0.5])),
        ])
        embed = _sync_embedding(endpoint)
        assert embed.embed(["a", "b", "c", "d", "e"]) == [[1.0, 0.0], [0.0, 1.0], [], [], [0.5, 0.5]]
        assert endpoint.inputs == [["a", "b"], ["c", "d"], ["e"]]
    elif scenario == "missing_index":
        endpoint = _SyncEmbeddingEndpoint([_response((2, [0.0, 1.0]), (0, [1.0, 0.0]))])
        assert _sync_embedding(endpoint, max_batch_size=3).embed(["a", "b", "c"]) == [[1.0, 0.0], [], [0.0, 1.0]]
    elif scenario == "duplicate_index":
        endpoint = _SyncEmbeddingEndpoint([_response((0, [1.0, 0.0]), (0, [0.5, 0.5]), (1, [0.0, 1.0]))])
        assert _sync_embedding(endpoint).embed(["a", "b"]) == [[], [0.0, 1.0]]
    elif scenario == "dimension_change":
        endpoint = _SyncEmbeddingEndpoint([_response((0, [1.0, 0.0])), _response((0, [1.0]))])
        assert _sync_embedding(endpoint, max_batch_size=1).embed(["a", "b"]) == [[1.0, 0.0], []]
    else:
        endpoint = _SyncEmbeddingEndpoint([_response((0, [1.0, 0.0]))])
        assert _sync_embedding(endpoint, return_false=True).embed(["a", "b"]) is False

@pytest.mark.asyncio
async def test_async_embedding_preserves_failed_batch_positions_and_continues():
    """异步调用在中间批次失败后保留空位并继续处理"""
    endpoint = _AsyncEmbeddingEndpoint([
        _response((0, [1.0, 0.0]), (1, [0.0, 1.0])),
        RuntimeError("batch failed"),
        _response((0, [0.5, 0.5])),
    ])
    embedding = _async_embedding(endpoint)

    result = await embedding.embed(["a", "b", "c", "d", "e"])

    assert result == [[1.0, 0.0], [0.0, 1.0], [], [], [0.5, 0.5]]
    assert endpoint.inputs == [["a", "b"], ["c", "d"], ["e"]]

def test_sync_embedding_unsuppressed_error_is_raised():
    """关闭异常抑制时向调用方传播批次异常"""
    endpoint = _SyncEmbeddingEndpoint([RuntimeError("batch failed")])
    embedding = _sync_embedding(endpoint, suppress_error=False)

    with pytest.raises(RuntimeError, match="batch failed"):
        embedding.embed(["a"])

def test_embedding_rejects_non_positive_batch_size():
    """同步和异步构造方法均拒绝非正批次大小"""
    with pytest.raises(ValueError, match="max_batch_size 必须大于 0"):
        Embedding(api_key="test", max_batch_size=0)
    with pytest.raises(ValueError, match="max_batch_size 必须大于 0"):
        AsyncEmbedding(api_key="test", max_batch_size=-1)

class _SyncResponse:
    """同步重排响应替身"""

    def __init__(self, status_code: int, payload: object) -> None:
        """初始化同步响应替身"""
        self.status_code = status_code
        self.payload = payload

    def json(self) -> object:
        """返回测试响应体"""
        return self.payload

class _AsyncResponse:
    """异步重排响应替身"""

    def __init__(self, status: int, payload: object) -> None:
        """初始化异步响应替身"""
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _AsyncResponse:
        """进入响应上下文"""
        return self

    async def __aexit__(self, *_: object) -> None:
        """退出响应上下文"""

    async def json(self) -> object:
        """返回测试响应体"""
        return self.payload

class _AsyncSession:
    """异步 HTTP 会话替身"""

    def __init__(self, response: _AsyncResponse, capture: dict[str, object]) -> None:
        """初始化异步 HTTP 会话替身"""
        self.response = response
        self.capture = capture

    async def __aenter__(self) -> _AsyncSession:
        """进入会话上下文"""
        return self

    async def __aexit__(self, *_: object) -> None:
        """退出会话上下文"""

    def post(self, url: str, **kwargs: object) -> _AsyncResponse:
        """记录异步请求参数并返回响应"""
        self.capture.update({"url": url, **kwargs})
        return self.response

def test_rerank_masks_public_key_but_uses_private_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步 ReRank 不公开原始凭据, 请求仍使用真实凭据"""
    capture: dict[str, object] = {}

    def post(url: str, **kwargs: object) -> _SyncResponse:
        """记录同步请求参数并返回成功响应"""
        capture.update({"url": url, **kwargs})
        return _SyncResponse(200, {"results": []})

    monkeypatch.setattr(rerank_module.requests, "post", post)
    rerank = ReRank("secret-key", "https://rerank.example/v1", "model")

    assert rerank.api_key == "api key locked"
    assert rerank.get_api_key() == "api key locked"
    assert rerank.call("query", ["document"]) == []
    headers = cast(dict[str, str], capture["headers"])
    assert headers["Authorization"] == "Bearer secret-key"

def test_rerank_rejects_non_success_before_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步 ReRank 遇到非成功状态时不解析或回传响应正文"""
    class ErrorResponse(_SyncResponse):
        def json(self) -> object:
            """阻止非成功响应被解析"""
            raise AssertionError("非成功响应不应解析 JSON")

    def post(*_args: object, **_kwargs: object) -> ErrorResponse:
        """返回固定限流响应"""
        return ErrorResponse(429, {"secret": "provider-detail"})

    monkeypatch.setattr(
        rerank_module.requests,
        "post",
        post,
    )

    assert ReRank("key", "https://rerank.example/v1", "model").call("query", ["doc"]) == []

@pytest.mark.asyncio
async def test_async_rerank_normalizes_url_masks_key_and_checks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步 ReRank 与同步版保持 URL, 凭据和状态码语义一致"""
    capture: dict[str, object] = {}
    response = _AsyncResponse(401, {"secret": "provider-detail"})
    monkeypatch.setattr(
        rerank_module.aiohttp,
        "ClientSession",
        lambda: _AsyncSession(response, capture),
    )
    rerank = AsyncReRank("secret-key", "https://rerank.example/v1/responses", "model")

    assert rerank.base_url == "https://rerank.example/v1/rerank"
    assert rerank.api_key == "api key locked"
    assert await rerank.call("query", ["doc"]) == []
    headers = cast(dict[str, str], capture["headers"])
    assert headers["Authorization"] == "Bearer secret-key"

def _as_llm(fake: "FakeLLM") -> AsyncLLM:
    """LLM 替身类型边界: 替身实现 structured_output/chat 调用面, cast 集中在此工厂"""
    return cast(AsyncLLM, fake)

def _as_embedding(fake: "FakeEmbedding") -> AsyncEmbedding:
    """Embedding 替身类型边界: 替身实现 embed 调用面, cast 集中在此工厂"""
    return cast(AsyncEmbedding, fake)

class FakeEmbedding:
    """离线 Embedding, 基于文本哈希生成稳定向量"""

    def __init__(self, dim: int = 16):
        self.dim = dim

    async def embed(self, text: str | list[str] | None) -> list[float] | list[list[float]]:
        if text is None:
            return []
        if isinstance(text, list):
            return [self._vec(item) for item in text]
        if isinstance(text, str):
            return [] if not text.strip() else self._vec(text)

    def _vec(self, text: str):
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [(digest[i % len(digest)] / 255.0) + 1e-6 for i in range(self.dim)]

class FakeLLM:
    """离线 LLM, 通过状态机返回结构化结果"""

    def __init__(self):
        self.extract_facts = ["我喜欢咖啡"]
        self.update_action = "ADD"
        self.update_memory_id: str | None = None
        self.update_new_content: str | None = None

    async def structured_output(self, messages: list[dict[str, Any]], format: str):
        if "memories" in format:
            return json.dumps({"memories": self.extract_facts}, ensure_ascii=False)

        action = (self.update_action or "NOOP").upper()
        if action == "ADD":
            return json.dumps({"action": "ADD"}, ensure_ascii=False)
        if action == "UPDATE":
            return json.dumps(
                {
                    "action": "UPDATE",
                    "memory_id": self.update_memory_id,
                    "new_content": self.update_new_content,
                },
                ensure_ascii=False,
            )
        if action == "DELETE":
            return json.dumps(
                {"action": "DELETE", "memory_id": self.update_memory_id},
                ensure_ascii=False,
            )
        return json.dumps({"action": "NOOP"}, ensure_ascii=False)

    async def chat(self, messages: list[dict[str, Any]]):
        return "这是一条测试摘要"

@pytest.mark.asyncio
async def test_mem0_add_search_get_delete_and_clear(tmp_path: Path):
    memory = Mem0Memory(
        llm=_as_llm(FakeLLM()),
        embedding=_as_embedding(FakeEmbedding()),
        persist_path=str(tmp_path / "mem0.db"),
        top_k=5,
        similarity_threshold=0.0,
    )

    ids = await memory.add(
        user_message="我喜欢喝咖啡",
        assistant_message="好的, 我记住了",
        user_id="u1",
    )
    assert len(ids) == 1

    results = await memory.search("咖啡", user_id="u1", k=3)
    assert results
    assert any("咖啡" in item.get("content", "") for item in results)
    assert len(await memory.get_all(user_id="u1")) == 1

    assert await memory.delete(ids[0], user_id="u1")
    assert await memory.get_all(user_id="u1") == []

    await memory.add("我喜欢茶", "收到", user_id="u1")
    assert await memory.clear(user_id="u1")
    assert memory.get_stats(user_id="u1")["memory_count"] == 0

@pytest.mark.asyncio
async def test_mem0_update_preserves_memory_id_and_replaces_content(tmp_path: Path):
    llm = FakeLLM()
    memory = Mem0Memory(
        llm=_as_llm(llm),
        embedding=_as_embedding(FakeEmbedding()),
        persist_path=str(tmp_path / "mem0-update.db"),
        top_k=5,
        similarity_threshold=0.0,
    )

    first_ids = await memory.add("我喜欢咖啡", "收到", user_id="u2")
    assert len(first_ids) == 1

    llm.extract_facts = ["我更喜欢喝茶"]
    llm.update_action = "UPDATE"
    llm.update_memory_id = first_ids[0]
    llm.update_new_content = "我更喜欢喝茶"

    updated_ids = await memory.add("我更喜欢喝茶", "收到", user_id="u2")
    assert updated_ids == first_ids

    memories = await memory.get_all(user_id="u2")
    assert len(memories) == 1
    assert "喝茶" in memories[0]["content"]

@pytest.mark.asyncio
async def test_mem0_summary_tasks_are_coalesced_and_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一用户只保留最新摘要任务, 关闭时取消并回收任务"""
    memory = Mem0Memory(
        llm=_as_llm(FakeLLM()),
        embedding=_as_embedding(FakeEmbedding()),
        persist_path=str(tmp_path / "mem0-tasks.db"),
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_refresh(_user_id: str) -> None:
        """等待取消以模拟仍在执行的摘要请求"""
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(memory, "_refresh_summary", blocked_refresh)
    memory._schedule_summary_refresh("u1")
    first = memory._summary_tasks["u1"]
    await started.wait()

    started.clear()
    memory._schedule_summary_refresh("u1")
    second = memory._summary_tasks["u1"]
    await started.wait()
    await asyncio.sleep(0)

    assert first is not second
    assert first.cancelled() is True
    assert cancelled.is_set() is True

    await memory.close()

    assert second.cancelled() is True
    assert memory._summary_tasks == {}
    with pytest.raises(RuntimeError, match="已关闭"):
        await memory.add("new", "reply", user_id="u1")
