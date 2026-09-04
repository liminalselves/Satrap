import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.APICall.EmbedCall import AsyncEmbedding
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.expend.tools.mem0 import Mem0Memory


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
    """
    同一用户只保留最新摘要任务, 关闭时取消并回收任务

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
