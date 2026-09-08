from __future__ import annotations
import aiosqlite
from typing import List, Dict, Any, TYPE_CHECKING
from types import TracebackType
import json
import time
from satrap.core.utils.vision import content_text_projection
from satrap.core.log import logger
from .utils import (
    _SUMMARY_PROMPT_VERSION,
    _ContextRuntimeState,
    _messages_domain,
    _message_content_json,
    _load_message_content,
    _load_tool_calls,
)

if TYPE_CHECKING:
    from satrap.core.APICall.LLMCall import LLM, AsyncLLM
    from .sync import ContextManager
    from .async_ import AsyncContextManager
from .base import _ContextCore
from .base import _ContextCore


class _AsyncStorage(_ContextCore):

    async def initialize(self):
        """
        异步初始化数据库表并加载上下文
        需在实例化后手动调用, 或使用 async with 语句
        """
        await self._init_db_table()
        if self.state_store is not None:
            self.state_store.register_domain(_messages_domain())
        await self.load_context()
        logger.info(f"[异步上下文管理] 初始化完成，对话 ID: {self.conversation_id}")

    async def __aenter__(self):
        """
        支持 async with 语句

        返回:
        - 支持 async with 语句
        """
        await self.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ):
        """
        退出时确保数据保存

        参数:
        - exc_type: exc类型
        - exc_val: 异常值
        - exc_tb: 异常回溯
        """
        if not self.keep_in_memory:
            await self.save_context()

    async def _init_db_table(self):
        """初始化数据库表结构"""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT,
                        content_json TEXT,
                        tool_call_id TEXT,
                        tool_calls TEXT,
                        reasoning_content TEXT
                    )
                """
                )

                await conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_conv_id ON chat_history (conversation_id)"""
                )

                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS context_runtime_state (
                        conversation_id TEXT PRIMARY KEY,
                        summary TEXT NOT NULL DEFAULT '',
                        covered_turn_count INTEGER NOT NULL DEFAULT 0,
                        summary_model TEXT,
                        summary_prompt_version INTEGER NOT NULL DEFAULT 1,
                        usage_model TEXT,
                        api_input_tokens INTEGER,
                        estimated_input_tokens INTEGER,
                        api_output_tokens INTEGER,
                        api_total_tokens INTEGER,
                        api_cached_tokens INTEGER,
                        updated_at REAL NOT NULL
                    )
                """
                )

                try:
                    await conn.execute(
                        "ALTER TABLE context_runtime_state ADD COLUMN api_cached_tokens INTEGER"
                    )
                except aiosqlite.OperationalError:
                    pass

                for col in (
                    "content_json",
                    "tool_call_id",
                    "tool_calls",
                    "reasoning_content",
                ):
                    try:
                        await conn.execute(
                            f"ALTER TABLE chat_history ADD COLUMN {col} TEXT"
                        )
                    except aiosqlite.OperationalError:
                        pass

                await conn.commit()
        except Exception as e:
            logger.error(
                f"[异步上下文管理] 初始化数据库表失败：{e}, ID: {self.conversation_id}"
            )

    async def _sync(self):
        """根据 keep_in_memory 策略决定是否立即写入数据库"""
        if not self.keep_in_memory:
            await self.save_context()

    async def load_context(self):
        """从数据库加载当前 ID 的上下文信息到内存中"""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(
                    "SELECT role, content, content_json, tool_call_id, tool_calls, reasoning_content FROM chat_history WHERE conversation_id = ? ORDER BY id ASC",
                    (self.conversation_id,),
                )
                rows = list(await cursor.fetchall())

            self._messages: List[Dict[str, Any]] = []
            for row_index, row in enumerate(rows, start=1):
                msg = {"role": row[0], "content": _load_message_content(row[1], row[2])}
                if row[3] is not None:
                    msg["tool_call_id"] = row[3]
                tool_calls = _load_tool_calls(row[4], self.conversation_id, row_index)
                if tool_calls is not None:
                    msg["tool_calls"] = tool_calls
                if row[5] is not None:
                    msg["reasoning_content"] = row[5]
                self._messages.append(msg)
            self._saved_count = len(rows)
            await self._load_runtime_state()
        except Exception as e:
            logger.error(
                f"[异步上下文管理] 加载上下文失败：{self.conversation_id}: {e}, ID: {self.conversation_id}"
            )
            self._messages = []
            self._saved_count = 0

    async def _load_runtime_state(self) -> None:
        """加载当前对话的总结缓存和 usage 校准状态"""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT summary, covered_turn_count, summary_model, summary_prompt_version, "
                "usage_model, api_input_tokens, estimated_input_tokens, api_output_tokens, api_total_tokens, "
                "api_cached_tokens "
                "FROM context_runtime_state WHERE conversation_id = ?",
                (self.conversation_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            self._runtime_state = _ContextRuntimeState()
            self._runtime_state_dirty = False
            return
        self._runtime_state = _ContextRuntimeState(
            summary=str(row[0] or ""),
            covered_turn_count=int(row[1] or 0),
            summary_model=str(row[2]) if row[2] else None,
            summary_prompt_version=int(row[3] or _SUMMARY_PROMPT_VERSION),
            usage_model=str(row[4]) if row[4] else None,
            api_input_tokens=int(row[5]) if row[5] is not None else None,
            estimated_input_tokens=int(row[6]) if row[6] is not None else None,
            api_output_tokens=int(row[7]) if row[7] is not None else None,
            api_total_tokens=int(row[8]) if row[8] is not None else None,
            api_cached_tokens=int(row[9]) if row[9] is not None else None,
        )
        self._runtime_state_dirty = False
        conversation_turns = self._conversation_turns(self._messages)
        if (
            self._runtime_state.summary_prompt_version != _SUMMARY_PROMPT_VERSION
            or self._runtime_state.covered_turn_count > len(conversation_turns)
        ):
            await self._invalidate_summary(persist=True)

    async def _write_runtime_state(self, conn: aiosqlite.Connection) -> None:
        """
        将运行时状态写入当前异步事务

        参数:
        - conn: aiosqlite 连接
        """
        state = self._runtime_state
        await conn.execute(
            "INSERT INTO context_runtime_state "
            "(conversation_id, summary, covered_turn_count, summary_model, summary_prompt_version, "
            "usage_model, api_input_tokens, estimated_input_tokens, api_output_tokens, api_total_tokens, "
            "api_cached_tokens, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET "
            "summary = excluded.summary, covered_turn_count = excluded.covered_turn_count, "
            "summary_model = excluded.summary_model, summary_prompt_version = excluded.summary_prompt_version, "
            "usage_model = excluded.usage_model, api_input_tokens = excluded.api_input_tokens, "
            "estimated_input_tokens = excluded.estimated_input_tokens, api_output_tokens = excluded.api_output_tokens, "
            "api_total_tokens = excluded.api_total_tokens, api_cached_tokens = excluded.api_cached_tokens, "
            "updated_at = excluded.updated_at",
            (
                self.conversation_id,
                state.summary,
                state.covered_turn_count,
                state.summary_model,
                state.summary_prompt_version,
                state.usage_model,
                state.api_input_tokens,
                state.estimated_input_tokens,
                state.api_output_tokens,
                state.api_total_tokens,
                state.api_cached_tokens,
                time.time(),
            ),
        )

    async def _save_runtime_state(self) -> None:
        """持久化当前对话的总结缓存和 usage 校准状态"""
        async with aiosqlite.connect(self.db_path) as conn:
            await self._write_runtime_state(conn)
            await conn.commit()
        self._runtime_state_dirty = False

    async def _invalidate_summary(self, persist: bool = False) -> None:
        """
        清除因历史编辑而失效的总结缓存

        参数:
        - persist: 是否立即同步到数据库
        """
        self._runtime_state.summary = ""
        self._runtime_state.covered_turn_count = 0
        self._runtime_state.summary_model = None
        self._runtime_state.summary_prompt_version = _SUMMARY_PROMPT_VERSION
        self._runtime_state_dirty = True
        if persist:
            await self._save_runtime_state()

    async def save_context(self):
        """
        保存当前上下文到数据库

        增量策略: 纯追加时只 INSERT 尾部新消息 (O(1));
        编辑/外部修改 (标记 dirty) 或状态未知时全量重写
        """
        prev_saved = self._saved_count  # 失败时恢复水位, 保证重试幂等
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                if self._saved_count < 0 or self._saved_count > len(self._messages):
                    await conn.execute(
                        "DELETE FROM chat_history WHERE conversation_id = ?",
                        (self.conversation_id,),
                    )
                    # 全量重写: 消息列表被编辑过, 行 id 将重新分配
                    self._saved_count = 0

                new_messages = self._messages[self._saved_count :]
                if new_messages:
                    data_to_insert = [
                        (
                            self.conversation_id,
                            msg["role"],
                            content_text_projection(msg.get("content")),
                            _message_content_json(msg.get("content")),
                            msg.get("tool_call_id"),
                            (
                                json.dumps(msg["tool_calls"], ensure_ascii=False)
                                if msg.get("tool_calls")
                                else None
                            ),
                            msg.get("reasoning_content"),
                        )
                        for msg in new_messages
                    ]
                    await conn.executemany(
                        "INSERT INTO chat_history (conversation_id, role, content, content_json, tool_call_id, tool_calls, reasoning_content) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        data_to_insert,
                    )
                if self._runtime_state_dirty:
                    await self._write_runtime_state(conn)
                await conn.commit()
                # 提交成功后才推进已保存水位
                self._saved_count = len(self._messages)
                self._runtime_state_dirty = False

        except Exception as e:
            logger.error(
                f"[异步上下文管理] 保存上下文失败：{self.conversation_id}: {e}, ID: {self.conversation_id}"
            )
            self._saved_count = prev_saved  # 恢复水位, 下次保存重试 (全量重写路径幂等)

    def _mark_dirty(self) -> None:
        """标记消息列表被外部编辑 (非纯追加), 下次保存走全量重写"""
        self._saved_count = -1
        self._runtime_state.summary = ""
        self._runtime_state.covered_turn_count = 0
        self._runtime_state.summary_model = None
        self._runtime_state.summary_prompt_version = _SUMMARY_PROMPT_VERSION
        self._runtime_state_dirty = True
