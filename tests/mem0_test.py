import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

# 将项目根目录加入导入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from satrap.expend.mem0 import Mem0Memory


class FakeEmbedding:
    """离线 Embedding, 基于文本哈希生成稳定向量"""

    def __init__(self, dim: int = 16):
        self.dim = dim

    async def embed(self, text):
        if text is None:
            return []

        if isinstance(text, list):
            return [self._vec(t) for t in text]

        if isinstance(text, str):
            if not text.strip():
                return []
            return self._vec(text)

        return []

    def _vec(self, text: str):
        h = hashlib.sha256(text.encode("utf-8")).digest()
        vals = []
        for i in range(self.dim):
            vals.append((h[i % len(h)] / 255.0) + 1e-6)
        return vals


class FakeLLM:
    """离线 LLM, 通过状态机返回结构化结果"""

    def __init__(self):
        self.extract_facts = ["我喜欢咖啡"]
        self.update_action = "ADD"
        self.update_memory_id = None
        self.update_new_content = None

    async def structured_output(self, messages, format):
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

    async def chat(self, messages):
        return "这是一条测试摘要"


def _clean_dir(path: str):
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)


async def test_add_search_get_delete_clear():
    persist = "./sandbox/mem0_test_db"
    _clean_dir(persist)

    llm = FakeLLM()
    emb = FakeEmbedding()
    mem = Mem0Memory(llm=llm, embedding=emb, persist_path=persist, top_k=5, similarity_threshold=0.0)

    # add
    ids = await mem.add(
        user_message="我喜欢喝咖啡",
        assistant_message="好的, 我记住了",
        user_id="u1",
    )
    assert len(ids) == 1, f"期望新增 1 条记忆, 实际 {len(ids)}"
    memory_id = ids[0]

    # 等待后台摘要任务
    await asyncio.sleep(0.05)

    # search
    results = await mem.search("咖啡", user_id="u1", k=3)
    assert results, "search 结果为空"
    assert any("咖啡" in r.get("content", "") for r in results), "未检索到咖啡相关记忆"

    # get_all
    all_mem = await mem.get_all(user_id="u1")
    assert len(all_mem) == 1, f"期望 1 条记忆, 实际 {len(all_mem)}"

    # delete
    ok = await mem.delete(memory_id, user_id="u1")
    assert ok, "delete 返回 False"
    all_after_delete = await mem.get_all(user_id="u1")
    assert len(all_after_delete) == 0, f"删除后仍有记忆: {len(all_after_delete)}"

    # clear
    await mem.add("我喜欢茶", "收到", user_id="u1")
    cleared = await mem.clear(user_id="u1")
    assert cleared, "clear 返回 False"
    stats = mem.get_stats(user_id="u1")
    assert stats["memory_count"] == 0, f"clear 后 memory_count 不为 0: {stats}"

    print("PASS: test_add_search_get_delete_clear")


async def test_update_flow():
    persist = "./sandbox/mem0_test_update_db"
    _clean_dir(persist)

    llm = FakeLLM()
    emb = FakeEmbedding()
    mem = Mem0Memory(llm=llm, embedding=emb, persist_path=persist, top_k=5, similarity_threshold=0.0)

    # 第一次 ADD
    llm.extract_facts = ["我喜欢咖啡"]
    llm.update_action = "ADD"
    first_ids = await mem.add("我喜欢咖啡", "收到", user_id="u2")
    assert len(first_ids) == 1, "首次 ADD 失败"

    # 第二次 UPDATE 同一条 memory_id
    llm.extract_facts = ["我更喜欢喝茶"]
    llm.update_action = "UPDATE"
    llm.update_memory_id = first_ids[0]
    llm.update_new_content = "我更喜欢喝茶"

    updated_ids = await mem.add("我更喜欢喝茶", "收到", user_id="u2")
    assert len(updated_ids) == 1 and updated_ids[0] == first_ids[0], "UPDATE 未返回原 memory_id"

    all_mem = await mem.get_all(user_id="u2")
    assert len(all_mem) == 1, f"UPDATE 后记忆条数异常: {len(all_mem)}"
    assert "喝茶" in all_mem[0]["content"], f"UPDATE 后内容异常: {all_mem[0]['content']}"

    print("PASS: test_update_flow")


async def run_all_tests():
    await test_add_search_get_delete_clear()
    await test_update_flow()
    print("\nALL PASS: mem0 tests")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
