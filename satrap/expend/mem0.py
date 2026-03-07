import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from satrap.core.APICall.EmbedCall import AsyncEmbedding
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.database import DataBase
from satrap.core.log import logger


# ──────────────────────────────────────────────────────────────
# Prompt 模板
# ──────────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """\
你是一个专业的记忆提取助手，请从以下对话中提取出值得长期记忆的关键事实。

【全局对话摘要】
{summary}

【近期消息记录】
{recent_messages}

【最新消息对】
用户: {user_message}
助手: {assistant_message}

提取规则：
- 每条事实独立、简洁（一句话以内）
- 包含具体实体、偏好、事件或关键信息
- 跳过一次性问候或无意义内容
- 如果没有值得记忆的内容，返回空列表

请严格按照以下 JSON 格式返回，不要包含 Markdown 标记或其他多余文本：
{{"memories": ["事实1", "事实2"]}}
"""

_UPDATE_PROMPT = """\
你是一个记忆管理助手。请判断新事实与已有记忆的关系，并选择操作。

【新事实】
{new_fact}

【已有相似记忆】
{existing_memories}

操作说明：
- ADD    : 新事实是全新信息，与已有记忆无重叠
- UPDATE : 新事实补充或修正了某条已有记忆（需指明 memory_id 和更新后的完整内容）
- DELETE : 新事实与某条已有记忆直接矛盾，应删除旧记忆（需指明 memory_id）
- NOOP   : 新事实已包含在已有记忆中，无需任何操作

请严格按照对应格式返回 JSON，不要包含 Markdown 标记或其他多余文本：
ADD    → {{"action": "ADD"}}
UPDATE → {{"action": "UPDATE", "memory_id": "<id>", "new_content": "<更新后的完整内容>"}}
DELETE → {{"action": "DELETE", "memory_id": "<id>"}}
NOOP   → {{"action": "NOOP"}}
"""

_SUMMARY_PROMPT = """\
请为以下对话生成一段简洁的摘要，保留主要话题、关键事实和重要偏好信息。
摘要控制在 200 字以内，使用中文。

【对话内容】
{conversation}

摘要："""


# ──────────────────────────────────────────────────────────────
# Mem0 主类
# ──────────────────────────────────────────────────────────────

class Mem0Memory:
    """基于 DataBase(faiss + sqlite) 的长期记忆系统"""

    def __init__(
        self,
        llm: AsyncLLM,
        embedding: AsyncEmbedding,
        persist_path: str = "./satrapdata/mem0",
        top_k: int = 10,
        similarity_threshold: float = 0.5,
        recent_messages_window: int = 10,
    ):
        """初始化记忆系统

        参数:
        - llm: LLM 调用实例, 用于提取, 决策, 摘要
        - embedding: 向量模型实例, 用于文本向量化
        - persist_path: 向量库与元数据持久化目录
        - top_k: 更新阶段检索相似记忆数量
        - similarity_threshold: 相似度阈值
        - recent_messages_window: 保留近期对话轮次窗口
        """
        self.llm = llm
        self.embedding = embedding
        self.vector_db = DataBase(persist_path)
        self.top_k = top_k
        self.threshold = similarity_threshold
        self.window = recent_messages_window

        # 运行时内存: 会话摘要与近期历史
        self._summaries: Dict[str, str] = {}
        self._histories: Dict[str, List[Dict[str, str]]] = {}

        logger.info(f"[Mem0] 初始化完成, persist_path={persist_path}")

    # ──────────────────────────────────────────────────────────────
    # 公开 API
    # ──────────────────────────────────────────────────────────────

    async def add(
        self,
        user_message: str,
        assistant_message: str,
        user_id: str = "default",
    ) -> List[str]:
        """处理一轮对话, 提取候选记忆并执行更新"""
        collection = self._col(user_id)
        await asyncio.to_thread(self.vector_db.create_collection, collection)
        self._push_history(user_id, user_message, assistant_message)

        # 1) 提取候选记忆
        candidates = await self._extract(user_id, user_message, assistant_message)
        logger.info(f"[Mem0] user={user_id}, 提取候选数={len(candidates)}")

        # 2) 对每条候选执行 ADD, UPDATE, DELETE, NOOP
        affected_ids: List[str] = []
        for fact in candidates:
            memory_id = await self._update(user_id, fact)
            if memory_id:
                affected_ids.append(memory_id)

        # 3) 异步刷新会话摘要
        asyncio.create_task(self._refresh_summary(user_id))
        return affected_ids

    async def search(
        self,
        query: str,
        user_id: str = "default",
        k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """按语义检索用户记忆"""
        k = k or self.top_k
        collection = self._col(user_id)

        query_vec = await self.embedding.embed(query)
        if not query_vec:
            logger.error("[Mem0] 查询向量为空")
            return []

        results = await asyncio.to_thread(
            self.vector_db.search,
            collection,
            query_vec,   # type: ignore
            k,
            self.threshold,
        )

        return [
            {"content": r["document"], "score": r["score"], **r.get("metadata", {})}
            for r in results
        ]

    async def get_all(self, user_id: str = "default") -> List[Dict[str, Any]]:
        """获取用户全部记忆"""
        collection = self._col(user_id)
        return await asyncio.to_thread(self._get_all_sync, collection)

    async def delete(self, memory_id: str, user_id: str = "default") -> bool:
        """按 memory_id 删除一条记忆"""
        return await asyncio.to_thread(self._delete_by_id, user_id, memory_id)

    async def clear(self, user_id: str = "default") -> bool:
        """清空用户全部记忆"""
        collection = self._col(user_id)
        self._summaries.pop(user_id, None)
        self._histories.pop(user_id, None)
        return await asyncio.to_thread(self.vector_db.delete_collection, collection)

    async def get_summary(self, user_id: str = "default") -> str:
        """获取用户当前摘要"""
        return self._summaries.get(user_id, "")

    def get_stats(self, user_id: str = "default") -> Dict[str, Any]:
        """获取用户记忆统计信息"""
        collection = self._col(user_id)
        stats = self.vector_db.get_collection_stats(collection)
        return {
            "user_id": user_id,
            "memory_count": stats.get("document_count", 0),
            "has_summary": bool(self._summaries.get(user_id)),
            "history_turns": len(self._histories.get(user_id, [])) // 2,
        }

    # ──────────────────────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────────────────────
    def _col(self, user_id: str) -> str:
        """按用户生成集合名"""
        return f"mem0_{user_id}"

    def _push_history(self, user_id: str, user_msg: str, asst_msg: str):
        """追加历史并维持滑动窗口"""
        history = self._histories.setdefault(user_id, [])
        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": asst_msg})
        if len(history) > self.window * 2:
            self._histories[user_id] = history[-(self.window * 2):]

    async def _extract(
        self,
        user_id: str,
        user_message: str,
        assistant_message: str,
    ) -> List[str]:
        """提取阶段: 从最新对话中提取候选记忆"""
        summary = self._summaries.get(user_id, "(no summary)")
        history = self._histories.get(user_id, [])
        recent_pairs = history[:-2] if len(history) >= 2 else []
        recent_str = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in recent_pairs
        ) or "(none)"

        prompt = _EXTRACT_PROMPT.format(
            summary=summary,
            recent_messages=recent_str,
            user_message=user_message,
            assistant_message=assistant_message,
        )

        response = await self.llm.structured_output(
            messages=[{"role": "user", "content": prompt}],
            format={"memories": ["string"]},
        )
        if not response:
            return []

        try:
            data = json.loads(response) if isinstance(response, str) else {}
            memories = data.get("memories", [])
            return [m for m in memories if isinstance(m, str) and m.strip()]
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"[Mem0] 提取阶段 JSON 解析失败: {str(response)[:100]}")
            return []

    async def _update(self, user_id: str, fact: str) -> Optional[str]:
        """更新阶段: 对单条候选记忆执行操作并落库"""
        collection = self._col(user_id)
        now = datetime.now().isoformat()

        # 1) 向量化候选事实
        fact_vec = await self.embedding.embed(fact)
        if not fact_vec:
            logger.warning(f"[Mem0] 候选向量化失败, 跳过: {fact[:50]}")
            return None

        # 2) 检索相似记忆
        similar = await asyncio.to_thread(
            self.vector_db.search,
            collection,
            fact_vec,   # type: ignore
            self.top_k,
            self.threshold,
        )

        existing_str = "\n".join(
            f'- [id={r["metadata"].get("id", "?")}] {r["document"]} (score {r["score"]:.2f})'
            for r in similar
        ) or "(no similar memories)"

        # 3) LLM 决策
        prompt = _UPDATE_PROMPT.format(new_fact=fact, existing_memories=existing_str)
        response = await self.llm.structured_output(
            messages=[{"role": "user", "content": prompt}],
            format={"action": "string"},
        )
        if not response:
            return None

        try:
            data = json.loads(response) if isinstance(response, str) else {}
            action = str(data.get("action", "NOOP")).upper()
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"[Mem0] 更新阶段 JSON 解析失败: {str(response)[:100]}")
            return None

        # 4) 执行动作
        if action == "ADD":
            new_id = str(uuid.uuid4())
            meta = {
                "id": new_id,
                "user_id": user_id,
                "created_at": now,
                "updated_at": now,
            }
            await asyncio.to_thread(
                self.vector_db.add_to_collection,
                collection,
                [fact],
                [fact_vec],   # type: ignore
                [meta],
            )
            logger.info(f"[Mem0] ADD | {fact[:50]}")
            return new_id

        if action == "UPDATE":
            memory_id = data.get("memory_id")
            new_content = str(data.get("new_content", fact)).strip() or fact
            if not memory_id:
                logger.warning("[Mem0] UPDATE 缺少 memory_id, 回退 ADD")
                return await self._add_raw(collection, fact, fact_vec, user_id, now)   # type: ignore

            deleted = await asyncio.to_thread(self._delete_by_id, user_id, memory_id)
            if not deleted:
                return None

            new_vec = await self.embedding.embed(new_content)
            if not new_vec:
                return None

            meta = {
                "id": memory_id,
                "user_id": user_id,
                "created_at": now,
                "updated_at": now,
            }
            await asyncio.to_thread(
                self.vector_db.add_to_collection,
                collection,
                [new_content],
                [new_vec],   # type: ignore
                [meta],
            )
            logger.info(f"[Mem0] UPDATE | {memory_id[:8]} -> {new_content[:50]}")
            return memory_id

        if action == "DELETE":
            memory_id = data.get("memory_id")
            if memory_id:
                await asyncio.to_thread(self._delete_by_id, user_id, memory_id)
                logger.info(f"[Mem0] DELETE | {str(memory_id)[:8]}")
            return None

        logger.debug(f"[Mem0] NOOP | {fact[:50]}")
        return None

    async def _add_raw(
        self,
        collection: str,
        content: str,
        vec: list,
        user_id: str,
        now: str,
    ) -> str:
        """内部兜底: 直接新增一条记忆"""
        new_id = str(uuid.uuid4())
        meta = {
            "id": new_id,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
        }
        await asyncio.to_thread(
            self.vector_db.add_to_collection,
            collection,
            [content],
            [vec],
            [meta],
        )
        return new_id

    def _delete_by_id(self, user_id: str, memory_id: str) -> bool:
        """按 metadata.id 删除文档并同步删除 faiss 索引 id"""
        collection = self._col(user_id)
        with self.vector_db._connect() as conn:
            rows = conn.execute(
                "SELECT id, metadata FROM documents WHERE collection_name=?",
                (collection,),
            ).fetchall()

            target_row_id = None
            for row in rows:
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                except Exception:
                    meta = {}
                if meta.get("id") == memory_id:
                    target_row_id = int(row["id"])
                    break

            if target_row_id is None:
                logger.warning(f"[Mem0] 未找到 memory_id: {memory_id}")
                return False

            conn.execute(
                "DELETE FROM documents WHERE id=? AND collection_name=?",
                (target_row_id, collection),
            )
            conn.commit()

        index = self.vector_db.indices.get(collection)
        if index is not None:
            index.remove_ids(np.array([target_row_id], dtype=np.int64))
            self.vector_db._save_index(collection)

        return True

    def _get_all_sync(self, collection: str) -> List[Dict[str, Any]]:
        """从 SQLite 同步读取集合内全部记忆"""
        with self.vector_db._connect() as conn:
            rows = conn.execute(
                "SELECT document, metadata FROM documents WHERE collection_name=? ORDER BY id ASC",
                (collection,),
            ).fetchall()

        memories: List[Dict[str, Any]] = []
        for row in rows:
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except Exception:
                meta = {}
            memories.append({"content": row["document"], **meta})
        return memories

    async def _refresh_summary(self, user_id: str):
        """后台任务: 根据近期会话刷新摘要"""
        history = self._histories.get(user_id, [])
        if not history:
            return

        conversation = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content']}"
            for m in history
        )

        response = await self.llm.chat(
            messages=[
                {
                    "role": "user",
                    "content": _SUMMARY_PROMPT.format(conversation=conversation),
                }
            ],
        )

        if response and isinstance(response, str):
            self._summaries[user_id] = response.strip()
            logger.debug(f"[Mem0] 摘要已刷新, user_id={user_id}")
