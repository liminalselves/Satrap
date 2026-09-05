"""
对话上下文管理组件

提供消息的 SQLite 持久化, 查询, 分支和令牌预算裁剪,
并实现供同步与异步工作流使用的上下文管理器
"""
from __future__ import annotations

from satrap.core.utils.tokenizer import tokenizer_estimate, experience_estimate
from typing import List, Dict, Union, Optional, Any, cast, TYPE_CHECKING, Literal
from satrap.core.utils.vision import (
    DEFAULT_IMAGE_TOKEN_COST,
    build_multimodal_content,
    content_text_projection,
    estimate_content_image_count,
)
import aiosqlite
import asyncio
import sqlite3
import json
import copy
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from satrap.core.log import logger
from satrap.core.utils.paths import get_db_path
from types import TracebackType
from satrap.core.state import StateStore
from satrap.core.state.mutation import state_mutation_context
from satrap.core.type import (
    ContextUsageSnapshot,
    JsonRow,
    RestoreOptions,
    SnapshotDomain,
    StateCheckpoint,
    StateScope,
    TokenUsage,
)

if TYPE_CHECKING:
    from satrap.core.APICall.LLMCall import AsyncLLM, LLM


TokenEstimateMethod = Literal["tokenizer", "experience"]
ContextStrategy = Literal["sliding", "mid_truncate", "summarize"]
_SUMMARY_PROMPT_VERSION = 1
_MIN_SUMMARY_OUTPUT_TOKENS = 512
_MAX_SUMMARY_OUTPUT_TOKENS = 2048
_SUMMARY_RETRY_OUTPUT_TOKENS = 4096


def _summary_output_budget(target_tokens: int) -> int:
    """
    返回不受主回复低额度影响的摘要输出预算

    参数:
    - target_tokens: 期望摘要长度

    返回:
    - int: 摘要模型调用的最大输出 token 数
    """
    return max(
        _MIN_SUMMARY_OUTPUT_TOKENS,
        min(_MAX_SUMMARY_OUTPUT_TOKENS, target_tokens),
    )


class ContextOverflowError(RuntimeError):
    """保留项本身已超过上下文预算时抛出的异常"""


@dataclass
class PreparedModelContext:
    """一次模型请求使用的派生上下文及计数元数据"""
    messages: List[Dict[str, Any]]
    """实际发送给模型的消息副本"""
    estimated_input_tokens: int
    """准备后请求的本地估算输入 token 数"""
    effective_input_tokens: int
    """应用历史 API 校准后的输入 token 数"""
    original_estimated_input_tokens: int
    """压缩前完整请求的本地估算输入 token 数"""
    token_source: str
    """计数来源, tokenizer, experience 或 api_calibrated"""
    strategy: str
    """本次采用的上下文策略"""
    compressed: bool
    """本次是否对模型视图执行了压缩"""
    original_turns: int
    """完整上下文轮数"""
    prepared_turns: int
    """模型视图中的轮数"""
    history_budget: int
    """本次请求使用的历史上下文预算"""
    trigger_tokens: int
    """本次请求触发上下文处理的 token 线"""
    floor_tokens: int
    """本次请求执行截取后的目标 token 线"""
    model: Optional[str] = None
    """本次请求的模型名, 用于隔离不同模型的 usage 校准"""


@dataclass
class _ContextRuntimeState:
    """不属于完整消息历史的可重建运行时状态"""
    summary: str = ""
    covered_turn_count: int = 0
    summary_model: Optional[str] = None
    summary_prompt_version: int = _SUMMARY_PROMPT_VERSION
    usage_model: Optional[str] = None
    api_input_tokens: Optional[int] = None
    estimated_input_tokens: Optional[int] = None
    api_output_tokens: Optional[int] = None
    api_total_tokens: Optional[int] = None
    api_cached_tokens: Optional[int] = None


def _estimate_text(text: str, method: TokenEstimateMethod) -> int:
    """
    使用指定方法估算文本 token 数

    参数:
    - text: 待估算文本
    - method: tokenizer 或 experience

    返回:
    - int: 估算 token 数
    """
    if method == "tokenizer":
        return tokenizer_estimate(text)
    return experience_estimate(text)


def _estimate_request_tokens(
    messages: List[Dict[str, Any]],
    method: TokenEstimateMethod,
    tools: Optional[List[Dict[str, Any]]] = None,
    img_urls: Optional[List[str]] = None,
) -> int:
    """
    估算实际请求中的消息, 工具定义和额外图片成本

    参数:
    - messages: 请求消息
    - method: token 估算方法
    - tools: 工具定义列表
    - img_urls: 尚未写入消息内容的额外图片

    返回:
    - int: 请求输入 token 估算值
    """
    token_count = 0
    for msg in messages:
        content = msg.get("content", "")
        image_count = estimate_content_image_count(content)
        token_count += 4 + image_count * DEFAULT_IMAGE_TOKEN_COST
        token_count += _estimate_text(content_text_projection(content), method)
        metadata = {key: value for key, value in msg.items() if key != "content" and value is not None}
        if metadata:
            token_count += _estimate_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True), method)

    if tools:
        token_count += _estimate_text(json.dumps(tools, ensure_ascii=False, sort_keys=True), method)
    if img_urls:
        token_count += len(img_urls) * DEFAULT_IMAGE_TOKEN_COST
    return token_count


def _llm_model_name(llm: object | None) -> Optional[str]:
    """
    获取 LLM 实例当前模型名

    参数:
    - llm: LLM 实例

    返回:
    - Optional[str]: 模型名
    """
    if llm is None:
        return None
    getter = getattr(llm, "get_model", None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, str) and value:
                return value
        except (AttributeError, TypeError):
            pass
    value = getattr(llm, "model", None)
    return value if isinstance(value, str) and value else None


def _summary_lines(turns: List[List[Dict[str, Any]]]) -> List[str]:
    """
    将对话轮次投影为供总结模型读取的纯文本行

    参数:
    - turns: 待总结轮次

    返回:
    - List[str]: 纯文本消息行
    """
    lines: List[str] = []
    for turn in turns:
        for msg in turn:
            role = str(msg.get("role", "unknown"))
            text = content_text_projection(msg.get("content"))
            metadata = {key: value for key, value in msg.items() if key not in {"role", "content"} and value is not None}
            if metadata:
                text = f"{text}\n附加数据: {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}"
            lines.append(f"{role}: {text or '[空消息]'}")
    return lines


def _split_summary_chunks(lines: List[str], token_budget: int) -> List[str]:
    """
    将超长待总结文本切成可逐段归并的块

    参数:
    - lines: 对话文本行
    - token_budget: 每块的近似 token 预算

    返回:
    - List[str]: 文本块
    """
    budget = max(256, token_budget)
    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0
    for line in lines:
        parts = [line]
        if tokenizer_estimate(line) > budget:
            chars_per_chunk = max(256, budget * 3)
            parts = [line[index:index + chars_per_chunk] for index in range(0, len(line), chars_per_chunk)]
        for part in parts:
            part_tokens = tokenizer_estimate(part)
            if current and current_tokens + part_tokens > budget:
                chunks.append("\n".join(current))
                current = []
                current_tokens = 0
            current.append(part)
            current_tokens += part_tokens
    if current:
        chunks.append("\n".join(current))
    return chunks


def _messages_domain() -> SnapshotDomain:
    """
    消息领域注册: 管理 chat_history 表的对话消息

    返回:
    - SnapshotDomain: 消息领域注册: 管理 chat_history 表的对话消息
    """
    def builder(conn: sqlite3.Connection, scope: StateScope) -> List[JsonRow]:
        """
        读取指定对话的全部消息行

        参数:
        - conn: 数据库连接
        - scope: 作用域

        返回:
        - List[JsonRow]: 读取指定对话的全部消息行
        """
        rows = conn.execute(
            "SELECT id, role, content, content_json, tool_call_id, tool_calls, "
            "reasoning_content FROM chat_history WHERE conversation_id = ? ORDER BY id",
            (scope.scope_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def restorer(
        conn: sqlite3.Connection,
        scope: StateScope,
        rows: List[JsonRow],
        options: RestoreOptions,
    ) -> None:
        """
        清空后按快照重写指定对话的消息 (与 save_context 的列保持一致)

        参数:
        - conn: 数据库连接
        - scope: 作用域
        - rows: 数据行集合
        - options: 选项集合
        """
        cleaner(conn, scope)
        for row in rows:
            conn.execute(
                "INSERT INTO chat_history "
                "(conversation_id, role, content, content_json, tool_call_id, "
                " tool_calls, reasoning_content) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.scope_id,
                    str(row.get("role") or ""),
                    row.get("content"),
                    row.get("content_json"),
                    row.get("tool_call_id"),
                    row.get("tool_calls"),
                    row.get("reasoning_content"),
                ),
            )

    def cleaner(conn: sqlite3.Connection, scope: StateScope) -> None:
        """
        清空指定对话的全部消息

        参数:
        - conn: 数据库连接
        - scope: 作用域
        """
        conn.execute(
            "DELETE FROM chat_history WHERE conversation_id = ?",
            (scope.scope_id,),
        )

    def position_provider(conn: sqlite3.Connection, scope: StateScope) -> int:
        """
        提供消息水位: 当前对话消息条数 (追加式指针检查点的截断基准)

        参数:
        - conn: 数据库连接
        - scope: 作用域

        返回:
        - int: 提供消息水位: 当前对话消息条数 (追加式指针检查点的截断基准)
        """
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM chat_history WHERE conversation_id = ?",
            (scope.scope_id,),
        ).fetchone()
        return int(row["count"])

    return SnapshotDomain(
        name="messages",
        builder=builder,
        restorer=restorer,
        cleaner=cleaner,
        position_provider=position_provider,
    )


def _message_content_json(content: Any) -> str | None:
    """
    序列化多模态 content

    参数:
    - content: 内容

    返回:
    - str | None: 序列化多模态 content
    """
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    return None


def _load_message_content(content: str | None, content_json: str | None) -> Any:
    """
    加载消息 content, 优先使用完整 JSON

    参数:
    - content: 内容
    - content_json: 内容json

    返回:
    - Any: 加载消息 content, 优先使用完整 JSON
    """
    if content_json:
        try:
            parsed = json.loads(content_json)
            if isinstance(parsed, list):
                return cast(list[dict[str, Any]], parsed)
        except Exception:
            pass
    return content


def _load_tool_calls(
    raw_tool_calls: str | None,
    conversation_id: str,
    row_index: int,
) -> list[object] | None:
    """
    逐行加载工具调用, 损坏数据只影响当前字段

    参数:
    - raw_tool_calls: 数据库中的工具调用 JSON
    - conversation_id: 对话 ID
    - row_index: 消息行序号

    返回:
    - list[object] | None: 有效工具调用列表, 无效时返回 None
    """
    if raw_tool_calls is None:
        return None
    try:
        parsed = json.loads(raw_tool_calls)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning(
            f"[上下文管理器] 忽略损坏的工具调用字段, 对话ID: {conversation_id}, 行号: {row_index}"
        )
        return None
    if not isinstance(parsed, list):
        logger.warning(
            f"[上下文管理器] 忽略非列表工具调用字段, 对话ID: {conversation_id}, 行号: {row_index}"
        )
        return None
    return cast(list[object], parsed)


class ContextManager:
    """对话上下文管理器"""
    def __init__(
        self,
        conversation_id: Union[int, str],
        keep_in_memory: bool = False,
        db_path: str = get_db_path(),
        max_context: int = 128000,
        history_ratio: float = 0.7,
        context_threshold: float = 0.8,
        truncation_floor: float = 0.4,
        exceed_process: str = "sliding",
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        auto_checkpoint: bool = True,
        summary_keep_recent_turns: int = 6,
    ):
        """
        初始化上下文管理器

        参数:
        - conversation_id: 当前对话的唯一ID
        - keep_in_memory:
            - True: 加载数据后在内存操作, 需手动调用 save_context() 写入数据库 <br>
            - False: (推荐) 每次修改操作自动同步到数据库, 保证数据不丢失
        - db_path: SQLite 数据库路径
        - max_context: 最大上下文长度, 默认 128k
        - history_ratio: 历史上下文比例, 历史预算 = max_context x history_ratio, 默认 0.7
        - context_threshold: 上下文阈值(占历史预算比例), 触发线 = 历史预算 x context_threshold, 默认 0.8
        - truncation_floor: 上下文截断底线(占历史预算比例), 截断目标 = 历史预算 x truncation_floor, 默认 0.4
        - exceed_process: 超过触发线时的处理方式, 默认 "sliding" (滑动窗口)
            - "sliding": 滑动窗口策略, 删除旧消息, 保持上下文长度在截断底线以下
            - "mid_truncate": 中间截断策略, 从中间截断上下文, 不删除旧消息
            - "summarize": 总结旧轮次并保留最近 summary_keep_recent_turns 轮原文
        - summary_keep_recent_turns: 总结压缩时必须原样保留的最近轮数, 默认 6
        - state_store: 状态检查点存储实例, 传入后启用检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向当前库的 StateStore, 与显式传入 state_store 二选一
        - auto_checkpoint: 启用检查点后, 每次写入用户/机器人消息自动保存稳定检查点 (同水位去重), 默认 True

        返回:
        - None
        """
        if not (0 < truncation_floor < context_threshold <= 1):
            truncation_floor = 0.4
            logger.warning(
                f"截断底线必须小于上下文阈值: truncation_floor({truncation_floor}) < context_threshold({context_threshold})，退回默认值"
            )

        if not (0 < history_ratio <= 1):
            history_ratio = 0.7
            logger.warning(f"历史上下文比例必须在 (0, 1] 区间: history_ratio={history_ratio}，退回默认值")


        self.db_path = db_path
        self.conversation_id = str(conversation_id)
        self.keep_in_memory = keep_in_memory
        self.auto_checkpoint = auto_checkpoint
        # 检查点存储: 显式传入优先, 否则按开关自动创建 (与消息同库, 保证事务原子性)

        self.state_store = state_store

        if self.state_store is None and enable_checkpoint:
            self.state_store = StateStore(db_path=self.db_path)
        if self.state_store is not None:
            self.state_store.register_domain(_messages_domain())

        self._messages: List[Dict[str, Any]] = []         # 内存中的消息缓存
        self._runtime_state = _ContextRuntimeState()      # 可重建的总结与 usage 校准状态
        self._runtime_state_dirty = False                 # 运行时状态待持久化标记
        self._conn_lock = threading.Lock()                # 连接创建/释放互斥 (读写路径假定单线程使用)
        self._conn: Optional[sqlite3.Connection] = None   # 复用数据库连接 (惰性创建)
        self._saved_count = 0   # 已持久化到库的消息条数 (增量保存水位, -1 表示需全量重写)
        self._init_db_table()   # 初始化数据库表结构
        self.load_context()     # 加载数据

        self.max_context = max_context
        self.history_ratio = history_ratio
        self.context_threshold = context_threshold
        self.truncation_floor = truncation_floor
        self.exceed_process = exceed_process
        self.summary_keep_recent_turns = max(0, summary_keep_recent_turns)

        self.history_budget = int(max_context * history_ratio)
        self.trigger_tokens = int(self.history_budget * context_threshold)
        self.floor_tokens = int(self.history_budget * truncation_floor)
        self.output_budget = max_context - self.history_budget
        # 派生值: 滞回截断的触发线/底线/输出预算

        logger.info(f"[上下文管理器] 初始化完成, 对话ID: {self.conversation_id}")

    def _get_conn(self):
        """
        获取数据库连接 (进程内复用, 避免每次操作建连开销)

        注意: 复用连接假定 ContextManager 单线程使用 (Session 内串行调用),
        连接创建与释放由 _conn_lock 保护

        返回:
        - 数据库连接 (进程内复用, 避免每次操作建连开销)
        """
        with self._conn_lock:
            if self._conn is None:
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._conn

    def close(self):
        """关闭复用连接 (进程退出或不再使用时调用, 未调用时由 GC 兜底)"""
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _init_db_table(self):
        """初始化数据库表结构"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute('''
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
            ''')

            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_conv_id ON chat_history (conversation_id)''')

            cursor.execute('''
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
            ''')

            try:
                cursor.execute("ALTER TABLE context_runtime_state ADD COLUMN api_cached_tokens INTEGER")
            except sqlite3.OperationalError:
                pass

            for col in ('content_json', 'tool_call_id', 'tool_calls', 'reasoning_content'):
                try:
                    cursor.execute(f"ALTER TABLE chat_history ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass

            conn.commit()
        except Exception as e:
            logger.error(f"[上下文管理器] 初始化数据库表失败: {e}, ID: {self.conversation_id}")

    def _sync(self):
        """根据 keep_in_memory 策略决定是否立即写入数据库"""
        if not self.keep_in_memory:
            self.save_context()

    # ================= 核心功能实现 =================

    def load_context(self):
        """从数据库加载当前ID的上下文信息到内存中"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT role, content, content_json, tool_call_id, tool_calls, reasoning_content FROM chat_history WHERE conversation_id = ? ORDER BY id ASC", 
                (self.conversation_id,)
            )
            rows = cursor.fetchall()

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
            self._load_runtime_state(conn)
        except Exception as e:
            logger.error(f"[上下文管理器] 加载上下文失败: {self.conversation_id}: {e}, ID: {self.conversation_id}")
            self._messages: List[Dict[str, Any]] = []
            self._saved_count = 0

    def _load_runtime_state(self, conn: sqlite3.Connection) -> None:
        """
        加载当前对话的总结缓存和 usage 校准状态

        参数:
        - conn: SQLite 连接
        """
        row = conn.execute(
            "SELECT summary, covered_turn_count, summary_model, summary_prompt_version, "
            "usage_model, api_input_tokens, estimated_input_tokens, api_output_tokens, api_total_tokens, "
            "api_cached_tokens "
            "FROM context_runtime_state WHERE conversation_id = ?",
            (self.conversation_id,),
        ).fetchone()
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
            self._invalidate_summary(persist=True)

    def _write_runtime_state(self, conn: sqlite3.Connection) -> None:
        """
        将运行时状态写入当前事务

        参数:
        - conn: SQLite 连接
        """
        state = self._runtime_state
        conn.execute(
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

    def _save_runtime_state(self) -> None:
        """持久化当前对话的总结缓存和 usage 校准状态"""
        conn = self._get_conn()
        self._write_runtime_state(conn)
        conn.commit()
        self._runtime_state_dirty = False

    def _invalidate_summary(self, persist: bool = False) -> None:
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
            self._save_runtime_state()

    def save_context(self):
        """
        保存当前上下文到数据库

        增量策略: 纯追加时只 INSERT 尾部新消息 (O(1));
        编辑/外部修改 (标记 dirty) 或状态未知时全量重写
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        prev_saved = self._saved_count   # 失败时恢复水位, 保证重试幂等
        try:
            if self._saved_count < 0 or self._saved_count > len(self._messages):
                cursor.execute(
                    "DELETE FROM chat_history WHERE conversation_id = ?",
                    (self.conversation_id,),
                )
                # 全量重写: 消息列表被编辑过, 行 id 将重新分配
                self._saved_count = 0

            new_messages = self._messages[self._saved_count:]
            if new_messages:
                data_to_insert = [
                    (
                        self.conversation_id,
                        msg["role"],
                        content_text_projection(msg.get("content")),
                        _message_content_json(msg.get("content")),
                        msg.get("tool_call_id"),
                        json.dumps(msg["tool_calls"], ensure_ascii=False) if msg.get("tool_calls") else None,
                        msg.get("reasoning_content"),
                    )
                    for msg in new_messages
                ]
                cursor.executemany(
                    "INSERT INTO chat_history (conversation_id, role, content, content_json, tool_call_id, tool_calls, reasoning_content) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                    data_to_insert
                )
            if self._runtime_state_dirty:
                self._write_runtime_state(conn)
            conn.commit()
            # 提交成功后才推进已保存水位
            self._saved_count = len(self._messages)
            self._runtime_state_dirty = False

        except Exception as e:
            logger.error(f"[上下文管理器] 保存上下文失败: {self.conversation_id}: {e}, ID: {self.conversation_id}")
            conn.rollback()
            self._saved_count = prev_saved   # 恢复水位, 下次保存重试 (全量重写路径幂等)

    def _mark_dirty(self) -> None:
        """标记消息列表被外部编辑 (非纯追加), 下次保存走全量重写"""
        self._saved_count = -1
        self._invalidate_summary()

    # ================= 检查点支持 =================

    def _scope(self) -> StateScope:
        """
        当前对话对应的状态作用域

        返回:
        - StateScope: 当前对话对应的状态作用域
        """
        return StateScope(namespace="conversation", scope_id=self.conversation_id)

    def _require_state_store(self) -> StateStore:
        """
        获取状态存储, 未启用时抛出 ValueError

        返回:
        - StateStore: 状态存储, 未启用时抛出 ValueError
        """
        if self.state_store is None:
            raise ValueError("未启用状态检查点, 请传入 state_store 或设置 enable_checkpoint=True")
        return self.state_store

    def _maybe_auto_checkpoint(self) -> None:
        """消息写入后自动保存稳定检查点 (同水位去重, 指针式零快照, 失败不阻断主流程)"""
        if not self.auto_checkpoint or self.state_store is None:
            return
        try:
            self.state_store.ensure_stable_checkpoint(self._scope())
        except Exception as e:
            logger.warning(f"[上下文管理器] 自动检查点保存失败: {e}")

    def _protect_before_edit(self) -> None:
        """
        编辑类操作前: 物化当前状态为保护检查点, 并清理失效的指针检查点

        追加式消息被改写/删除后, 旧指针检查点的水位截断语义失效,
        因此物化保护后删除该作用域全部指针检查点 (保护检查点保留, 可撤销编辑)
        失败不阻断编辑主流程
        """
        if self.state_store is None:
            return
        try:
            with state_mutation_context(
                source="edit_protect", reason="编辑前状态保护"
            ):
                self.state_store.materialize_edit_protection(self._scope())
        except Exception as e:
            logger.warning(f"[上下文管理器] 编辑前状态保护失败: {e}")

    def create_checkpoint(self, name: str = "", description: str = "", batch_id: str = "") -> StateCheckpoint:
        """
        为当前对话创建状态检查点

        参数:
        - name: 检查点显示名称
        - description: 检查点说明
        - batch_id: 会话级聚合检查点的批次 ID, 同批检查点共享; 非聚合时留空

        返回:
        - StateCheckpoint: 创建的检查点
        """
        return self._require_state_store().create_checkpoint(
            self._scope(), name=name, description=description, batch_id=batch_id
        )

    def list_checkpoints(self) -> List[StateCheckpoint]:
        """
        列出当前对话的全部检查点 (按创建时间升序)

        返回:
        - List[StateCheckpoint]: 检查点列表
        """
        return self._require_state_store().list_checkpoints(self._scope())

    def rollback(self, checkpoint_id: str) -> None:
        """
        回滚当前对话到指定检查点并重载上下文

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_rollback", reason=f"回滚到检查点 {checkpoint_id}"
        ):
            store.rollback(checkpoint_id)
        self.load_context()
        self._invalidate_summary(persist=True)

    def retry(self, checkpoint_id: str) -> None:
        """
        从指定检查点重试并重载上下文 (保留未来检查点)

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_retry", reason=f"重试到检查点 {checkpoint_id}"
        ):
            store.retry(checkpoint_id)
        self.load_context()
        self._invalidate_summary(persist=True)

    def fork(
        self,
        branch_name: str,
        checkpoint_id: Optional[str] = None,
    ) -> "ContextManager":
        """
        从指定检查点 (默认最近一个) fork 一条新剧情线, 返回新的上下文管理器

        参数:
        - branch_name: 分支名称, 新对话 ID 形如 "{原ID}:fork:{分支名}"
        - checkpoint_id: 源检查点 ID, 默认最近一个检查点

        返回:
        - ContextManager: 新分支的上下文管理器

        异常:
        - ValueError: 当前对话没有检查点
        """
        store = self._require_state_store()
        checkpoints = store.list_checkpoints(self._scope())
        if not checkpoints:
            raise ValueError("当前对话没有检查点, 请先创建检查点")
        source_id = checkpoint_id or checkpoints[-1].checkpoint_id
        new_conversation_id = f"{self.conversation_id}:fork:{branch_name}"
        with state_mutation_context(
            source="checkpoint_fork", reason=f"从检查点 {source_id} 分支"
        ):
            store.fork(source_id, new_conversation_id)
        return ContextManager(
            new_conversation_id,
            keep_in_memory=self.keep_in_memory,
            db_path=self.db_path,
            max_context=self.max_context,
            history_ratio=self.history_ratio,
            context_threshold=self.context_threshold,
            truncation_floor=self.truncation_floor,
            exceed_process=self.exceed_process,
            summary_keep_recent_turns=self.summary_keep_recent_turns,
            state_store=store,
            auto_checkpoint=self.auto_checkpoint,
        )

    def get_context(self) -> List[Dict[str, Any]]:
        """
        获取当前上下文中的所有消息

        返回:
        - List[Dict[str, str]]: 消息列表
        """
        return self._messages

    def get_model_context(self, method: TokenEstimateMethod = "tokenizer") -> List[Dict[str, Any]]:
        """
        获取发送给模型的上下文 (保证在最大上下文长度内)

        参数:
        - method: token 估算方法, 同 estimate_token()

        返回:
        - 截断后的消息列表副本, 适合直接用于 API 调用
        """
        return self._apply_truncation(self._messages, method)

    def add_user_message(self, message: str, img_urls: list[str] | None = None):
        """
        添加用户消息到上下文中

        参数:
        - message: 消息内容
        - img_urls: 图片 URL 或本地路径列表, 可选
        """
        self._messages.append({"role": "user", "content": build_multimodal_content(message, img_urls)})
        self._sync()
        self._maybe_auto_checkpoint()

    def reset_system_prompt(self, message: str):
        """
        重置系统提示词

        参数:
        - message: 新的系统提示词
        """
        self._protect_before_edit()
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        # 在开头插入新的系统消息
        self._messages.insert(0, {"role": "system", "content": message})
        self._mark_dirty()
        self._sync()

    def add_bot_message(self, message: str, tools_calls: list[dict[str, Any]] | None = None, ignore_think: bool = True, reasoning: str | None = None):
        """
        添加机器人消息到上下文中

        参数:
        - message: 消息内容
        - tools_calls: 工具调用信息列表, 可选, 每个元素格式为 {"id": "工具ID", "type": "function", "function": {"name": "工具名", "arguments": "参数JSON字符串"}}
        - ignore_think: 是否忽略消息中的思考过程, 默认True
        - reasoning: 思考过程, 可选
        """
        if tools_calls:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,
                    "tool_calls": tools_calls,
                }
            )
        else:
            self._messages.append(
                {
                    "role": "assistant", 
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,
                }
            )

        self._sync()
        self._maybe_auto_checkpoint()

    def add_chat(self, user_message: str, bot_message: str):
        """
        添加一条用户消息和对应的机器人消息到上下文中

        参数:
        - user_message: 用户消息
        - bot_message: 机器人消息
        """
        self._messages.append({"role": "user", "content": user_message})
        self._messages.append({"role": "assistant", "content": bot_message})
        self._sync()
        self._maybe_auto_checkpoint()

    def add_tool_message(self, tool_call_id: str, tool_result: dict[str, Any] | str):
        """
        添加工具调用的返回消息到上下文中

        参数:
        - tool_call_id: 工具调用ID
        - tool_result: 工具调用的返回结果
        """
        self._messages.append({"role": "tool", "tool_call_id": tool_call_id,
            "content": json.dumps(tool_result, ensure_ascii=False) if isinstance(tool_result, dict) else tool_result})
        self._sync()
        self._maybe_auto_checkpoint()

    def add_tool_call_flow(self, message: str, tool_messages: list[dict[str, Any]], tool_results: list[dict[str, Any]]):
        """
        添加一个完整的工具调用消息流到上下文中

        相当于:
        ``` python
        ctx.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            ctx.add_tool_message(tool_msg["id"], tool_res)
        ```

        参数:
        - message: 模型消息
        - tool_messages: 工具调用消息列表
        - tool_results: 工具调用的返回结果列表, 与 tool_messages 一一对应
        """
        self.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            self.add_tool_message(tool_msg["id"], tool_res)
        self._sync()

    def add_at_system_start(self, message: str, separator: str = ""):
        """
        在原系统提示词的开头添加消息 (修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符, 默认为空字符串
        """
        self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                msg["content"] = message + separator + msg["content"]
                # 在开头拼接新内容
                break
        else:
            self._messages.insert(0, {"role": "system", "content": message})
            # 没有系统消息, 则新建一条并插入到开头
        self._mark_dirty()
        self._sync()

    def add_at_system_end(self, message: str, separator: str = ""):
        """
        在原系统提示词的结尾添加消息 (修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符, 默认为空字符串
        """
        self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                msg["content"] = msg["content"] + separator + message
                # 在结尾拼接新内容
                break
        else:
            self._messages.insert(0, {"role": "system", "content": message})
            # 没有系统消息, 则新建一条并插入到开头
        self._mark_dirty()
        self._sync()

    def add_turn_messages(self, turn_messages: list[dict[str, Any]]):
        """
        添加多条消息到上下文中

        参数:
        - turn_messages: 要添加的消息列表
        """
        serialized: list[dict[str, Any]] = []
        for msg in turn_messages:
            new_msg = copy.deepcopy(msg)
            serialized.append(new_msg)
        
        self._messages.extend(serialized)
        self._sync()
        self._maybe_auto_checkpoint()

    def static_message(self) -> int:
        """
        统计上下文中的消息数量

        返回:
        - int: 消息总数
        """
        return len(self._messages)

    def del_context(self):
        """删除当前上下文中的所有消息, 保留系统消息"""
        self._protect_before_edit()
        self._messages = [copy.deepcopy(msg) for msg in self._messages if msg.get("role") == "system"]
        self._mark_dirty()
        self._sync()
        logger.info(f"上下文已清空, ID: {self.conversation_id}")

    def del_system_message(self):
        """删除上下文中的系统消息"""
        self._protect_before_edit()
        self._messages[:] = [msg for msg in self._messages if msg.get("role") != "system"]
        self._mark_dirty()
        self._sync()

    def del_message(self, index: int):
        """
        删除上下文中指定索引的消息

        参数:
        - index: 消息的索引
        """
        if 0 <= index < len(self._messages) or -len(self._messages) <= index < 0:
            self._protect_before_edit()
            self._messages.pop(index)
        else:
            logger.error(f"[上下文管理] 删除消息失败: 索引 {index} 超出范围, ID: {self.conversation_id}")
        self._mark_dirty()
        self._sync()

    def del_last_message(self, n: int = 1):
        """
        删除上下文中的最后n条消息

        参数:
        - n: 删除的数量
        """
        self._protect_before_edit()
        for _ in range(n):
            if self._messages:
                self._messages.pop()
        self._mark_dirty()
        self._sync()

    def del_last_chat(self, n: int = 1):
        """
        删除上下文中的最后 n 组聊天消息

        参数:
        - n: 删除的组数
        """
        self._protect_before_edit()
        indices_to_remove: list[int] = []
        groups_removed = 0

        # Step.1 倒序遍历消息列表
        for i in range(len(self._messages) - 1, -1, -1):
            msg = self._messages[i]
            role = msg.get("role")

            if role == "system":   # 系统消息在开头, 说明已经没对话了
                break

            indices_to_remove.append(i)   # 将当前索引加入待删除列表

            if role == "user":   # 遇到 user 消息, 说明完成了一整组对话的定位
                groups_removed += 1
                if groups_removed >= n:
                    break

        for index in indices_to_remove:
            self._messages.pop(index)
        # 执行删除

        self._mark_dirty()
        self._sync()

    def export_json(self, file_path: str):
        """
        导出当前上下文到json文件

        参数:
        - file_path: 导出路径
        """
        data: dict[str, Any] = {"id": self.conversation_id, "messages": self._messages}
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            logger.info(f"[上下文管理] 成功导出 json 文件: {file_path}, ID: {self.conversation_id}")

        except Exception as e:
            logger.error(f"[上下文管理] 导出 json 文件失败: {e}, ID: {self.conversation_id}")

    def _group_messages_by_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        将消息列表按对话轮次分组
        每组以 user 消息开头, 包含随后的 assistant 和 tool 消息
        第一组可能以 system 消息开头

        参数:
        - messages: 待分组的消息列表

        返回:
        - List[List[Dict]]: 分组后的轮次列表
        """
        turns: List[List[Dict[str, Any]]] = []
        current_turn: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system" and not current_turn:
                current_turn.append(msg)
            elif role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            else:
                current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)
        return turns

    def _flatten_turns(self, turns: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """
        将分组后的轮次列表还原为扁平消息列表

        参数:
        - turns: 对话轮次

        返回:
        - List[Dict[str, Any]]: 将分组后的轮次列表还原为扁平消息列表
        """
        return [msg for turn in turns for msg in turn]

    _SUMMARY_PROMPT = (
        "请将以下对话历史浓缩为一段简洁的摘要, 保留关键事实、用户意图、已做的决策和待办事项。"
        "摘要将注入 system prompt 作为后续对话的上下文, 请用第三人称客观描述, 不要遗漏影响后续交互的信息。"
    )

    def _conversation_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        返回不含开头 system 消息的完整对话轮次

        参数:
        - messages: 消息列表

        返回:
        - List[List[Dict[str, Any]]]: 对话轮次
        """
        turns = self._group_messages_by_turns(messages)
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            turns.pop(0)
        return turns

    def _system_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        提取开头连续的 system 消息副本

        参数:
        - messages: 消息列表

        返回:
        - List[Dict[str, Any]]: system 消息副本
        """
        result: List[Dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "system":
                break
            result.append(copy.deepcopy(message))
        return result

    def _summary_context(self, messages: List[Dict[str, Any]], keep_recent_turns: int) -> List[Dict[str, Any]]:
        """
        使用缓存摘要构建非破坏性的模型上下文

        参数:
        - messages: 完整消息及本轮临时消息
        - keep_recent_turns: 原样保留的最近轮数

        返回:
        - List[Dict[str, Any]]: 摘要与最近轮次组成的模型视图
        """
        turns = self._conversation_turns(messages)
        recent = turns[-keep_recent_turns:] if keep_recent_turns > 0 else []
        result = self._system_messages(messages)
        if self._runtime_state.summary:
            result.append({
                "role": "system",
                "content": f"<对话历史摘要>\n{self._runtime_state.summary}\n</对话历史摘要>",
            })
        return result + self._flatten_turns(recent)

    def _call_summary_llm(self, llm: LLM, text_chunks: List[str], prior_summary: str) -> str:
        """
        逐块调用同步 LLM 并滚动归并摘要

        参数:
        - llm: 用于总结的同步 LLM
        - text_chunks: 待总结文本块
        - prior_summary: 已有摘要

        返回:
        - str: 新摘要
        """
        summary = prior_summary
        for chunk in text_chunks:
            previous = f"\n\n已有摘要:\n{summary}" if summary else ""
            prompt = f"{self._SUMMARY_PROMPT}{previous}\n\n新增对话:\n{chunk}"
            result = llm.chat(
                [{"role": "user", "content": prompt}],
                thinking="off",
                max_tokens=_summary_output_budget(self.floor_tokens // 2),
            )
            if not isinstance(result, str) or not result.strip():
                logger.warning("[上下文管理] 摘要正文为空, 使用扩展输出预算重试")
                result = llm.chat(
                    [{"role": "user", "content": prompt}],
                    thinking="off",
                    max_tokens=_SUMMARY_RETRY_OUTPUT_TOKENS,
                )
            if not isinstance(result, str) or not result.strip():
                raise RuntimeError("上下文总结模型未返回有效文本")
            summary = result.strip()
        return summary

    def _tighten_summary(self, llm: LLM, target_tokens: int) -> None:
        """
        将缓存摘要进一步压缩到指定目标附近

        参数:
        - llm: 用于压缩的同步 LLM
        - target_tokens: 摘要目标 token 数
        """
        prompt = (
            f"请把以下对话摘要进一步压缩到约 {target_tokens} tokens 以内, "
            "保留关键事实、决定和待办事项:\n\n"
            f"{self._runtime_state.summary}"
        )
        result = llm.chat(
            [{"role": "user", "content": prompt}],
            thinking="off",
            max_tokens=_summary_output_budget(target_tokens),
        )
        if not isinstance(result, str) or not result.strip():
            logger.warning("[上下文管理] 二次压缩正文为空, 使用扩展输出预算重试")
            result = llm.chat(
                [{"role": "user", "content": prompt}],
                thinking="off",
                max_tokens=_SUMMARY_RETRY_OUTPUT_TOKENS,
            )
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError("上下文摘要二次压缩未返回有效文本")
        self._runtime_state.summary = result.strip()
        self._save_runtime_state()

    def summarize_and_compress(self, llm: LLM, keep_recent_turns: int) -> str:
        """
        总结最近 keep_recent_turns 轮之前的对话并缓存, 不修改完整消息历史

        参数:
        - llm: 用于总结的 LLM 实例(调 chat 得字符串)
        - keep_recent_turns: 原样保留的最近对话轮数

        返回:
        - str: 总结文本; 对话轮次不足 keep_recent_turns 时返回空字符串(无可压缩)
        """
        if keep_recent_turns < 0:
            raise ValueError("keep_recent_turns 不能小于 0")
        turns = self._conversation_turns(self._messages)
        if len(turns) <= keep_recent_turns:
            return ""   # 对话轮次不足, 无可压缩

        target_covered = len(turns) - keep_recent_turns
        state = self._runtime_state
        if state.summary_prompt_version != _SUMMARY_PROMPT_VERSION or state.covered_turn_count > target_covered:
            self._invalidate_summary()
            state = self._runtime_state

        new_turns = turns[state.covered_turn_count:target_covered]
        if not new_turns:
            return state.summary
        lines = _summary_lines(new_turns)
        chunk_budget = max(1024, min(self.floor_tokens, self.history_budget // 2))
        chunks = _split_summary_chunks(lines, chunk_budget)
        summary = self._call_summary_llm(llm, chunks, state.summary)

        state.summary = summary
        state.covered_turn_count = target_covered
        state.summary_model = _llm_model_name(llm)
        state.summary_prompt_version = _SUMMARY_PROMPT_VERSION
        self._save_runtime_state()
        logger.info(
            f"[上下文管理] 总结缓存更新完成: 覆盖 {target_covered} 轮, "
            f"保留 {keep_recent_turns} 轮完整历史, ID: {self.conversation_id}"
        )
        return summary

    def estimate_token(self, messages: List[Dict[str, Any]] | None = None, method: TokenEstimateMethod = "tokenizer") -> int:
        """
        估计当前上下文中的 token 数量

        参数:
        - messages: 待估算 token 数的消息列表, 默认当前上下文中的所有消息
        - method: 估计方法, 可选值为 "tokenizer" 或 "experience"

        返回:
        - int: token 数量
        """
        if messages is None:
            messages = self._messages
        return _estimate_request_tokens(messages, method)

    def get_context_usage(
        self,
        method: TokenEstimateMethod = "tokenizer",
    ) -> ContextUsageSnapshot:
        """
        返回当前历史预算和最近一次模型 usage 快照

        参数:
        - method: 当前完整历史的 token 估算方法

        返回:
        - ContextUsageSnapshot: 上下文预算与最近一次模型 usage
        """
        return ContextUsageSnapshot(
            history_tokens=self.estimate_token(method=method),
            context_window_tokens=self.max_context,
            reserved_output_tokens=self.output_budget,
            history_upper_tokens=self.history_budget,
            history_lower_tokens=self.floor_tokens,
            last_output_tokens=self._runtime_state.api_output_tokens,
            cache_hit_tokens=self._runtime_state.api_cached_tokens,
            history_token_source=method,
        )

    def estimate_request_tokens(
        self,
        messages: List[Dict[str, Any]],
        method: TokenEstimateMethod = "tokenizer",
        tools: Optional[List[Dict[str, Any]]] = None,
        img_urls: Optional[List[str]] = None,
    ) -> int:
        """
        估算包含工具定义和图片的完整模型请求

        参数:
        - messages: 请求消息
        - method: token 估算方法
        - tools: 工具定义列表
        - img_urls: 额外图片 URL

        返回:
        - int: 请求输入 token 估算值
        """
        return _estimate_request_tokens(messages, method, tools, img_urls)

    def _calibration_factor(self, model: Optional[str]) -> float:
        """
        计算本地估算到同模型真实 API input usage 的校准系数

        参数:
        - model: 当前模型名

        返回:
        - float: 校准系数, 无可信 usage 时为 1
        """
        state = self._runtime_state
        if (
            state.api_input_tokens is None
            or state.estimated_input_tokens is None
            or state.estimated_input_tokens <= 0
            or state.usage_model != model
        ):
            return 1.0
        return max(0.1, min(10.0, state.api_input_tokens / state.estimated_input_tokens))

    def prepare_model_context(
        self,
        *,
        llm: Optional[LLM] = None,
        pending_messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        img_urls: Optional[List[str]] = None,
        method: TokenEstimateMethod = "tokenizer",
        strategy: Optional[ContextStrategy] = None,
        keep_recent_turns: Optional[int] = None,
    ) -> PreparedModelContext:
        """
        为一次真实模型请求准备非破坏性上下文

        参数:
        - llm: 总结策略使用的 LLM, 同时用于识别校准模型
        - pending_messages: 工具循环中尚未写入历史的临时消息
        - tools: 本次请求携带的工具定义
        - img_urls: 本次请求携带的额外图片
        - method: 本地 token 估算方法
        - strategy: 覆盖当前超限处理策略
        - keep_recent_turns: 总结策略必须原样保留的最近轮数, 默认使用构造参数

        返回:
        - PreparedModelContext: 模型消息副本及 token 元数据
        """
        selected_strategy = strategy or cast(ContextStrategy, self.exceed_process)
        if selected_strategy == "summary":
            selected_strategy = "summarize"
        recent_turns = self.summary_keep_recent_turns if keep_recent_turns is None else keep_recent_turns
        if recent_turns < 0:
            raise ValueError("keep_recent_turns 不能小于 0")
        model = _llm_model_name(llm)
        messages = copy.deepcopy(self._messages)
        if pending_messages:
            messages.extend(copy.deepcopy(pending_messages))
        original_turns = len(self._conversation_turns(messages))
        original_estimate = self.estimate_request_tokens(messages, method, tools, img_urls)
        factor = self._calibration_factor(model)
        original_effective = max(0, round(original_estimate * factor))
        prepared_messages = messages

        if original_effective > self.trigger_tokens:
            local_floor = max(1, int(self.floor_tokens / factor))
            if selected_strategy == "summarize":
                if llm is None:
                    raise ValueError("总结压缩策略需要传入 llm")
                self.summarize_and_compress(llm, recent_turns)
                prepared_messages = self._summary_context(messages, recent_turns)
            elif selected_strategy == "sliding":
                prepared_messages = self._apply_sliding_truncation(messages, local_floor, method)
            elif selected_strategy == "mid_truncate":
                prepared_messages = self._apply_mid_truncation(messages, local_floor, method)
            else:
                raise ValueError(f"未知上下文处理策略: {selected_strategy}")

        prepared_estimate = self.estimate_request_tokens(prepared_messages, method, tools, img_urls)
        prepared_effective = max(0, round(prepared_estimate * factor))
        compressed = prepared_messages != messages
        if (
            prepared_effective > self.history_budget
            and selected_strategy == "summarize"
            and self._runtime_state.summary
            and llm is not None
        ):
            turns = self._conversation_turns(messages)
            recent = turns[-recent_turns:] if recent_turns > 0 else []
            without_summary = self._system_messages(messages) + self._flatten_turns(recent)
            fixed_estimate = self.estimate_request_tokens(without_summary, method, tools, img_urls)
            fixed_effective = max(0, round(fixed_estimate * factor))
            if fixed_effective <= self.history_budget:
                target_tokens = max(64, int((self.history_budget - fixed_effective) / factor * 0.8))
                self._tighten_summary(llm, target_tokens)
                prepared_messages = self._summary_context(messages, recent_turns)
                prepared_estimate = self.estimate_request_tokens(prepared_messages, method, tools, img_urls)
                prepared_effective = max(0, round(prepared_estimate * factor))
        if prepared_effective > self.history_budget:
            raise ContextOverflowError(
                f"保留内容仍超过历史上下文预算: {prepared_effective} > {self.history_budget}"
            )
        return PreparedModelContext(
            messages=prepared_messages,
            estimated_input_tokens=prepared_estimate,
            effective_input_tokens=prepared_effective,
            original_estimated_input_tokens=original_estimate,
            token_source="api_calibrated" if factor != 1.0 else method,
            strategy=selected_strategy,
            compressed=compressed,
            original_turns=original_turns,
            prepared_turns=len(self._conversation_turns(prepared_messages)),
            history_budget=self.history_budget,
            trigger_tokens=self.trigger_tokens,
            floor_tokens=self.floor_tokens,
            model=model,
        )

    def record_model_usage(self, prepared: PreparedModelContext, usage: Optional[TokenUsage]) -> None:
        """
        保存已完成请求的真实 API usage, 供后续请求校准

        参数:
        - prepared: 本次请求的准备结果
        - usage: API 返回的 token 使用量
        """
        if usage is None:
            self._runtime_state.api_output_tokens = None
            self._runtime_state.api_total_tokens = None
            self._runtime_state.api_cached_tokens = None
            self._save_runtime_state()
            return
        state = self._runtime_state
        state.usage_model = prepared.model
        state.api_input_tokens = usage.input_tokens
        state.estimated_input_tokens = prepared.estimated_input_tokens
        state.api_output_tokens = usage.output_tokens
        state.api_total_tokens = usage.total_tokens
        state.api_cached_tokens = usage.cached_tokens
        self._save_runtime_state()

    def _apply_sliding_truncation(self, messages: List[Dict[str, Any]], threshold: int, method: TokenEstimateMethod) -> List[Dict[str, Any]]:
        """
        滑动窗口截断: 保留系统消息, 从最早的对话轮次开始整轮删除, 直到 token 数不超过阈值

        参数:
        - messages: 原始消息列表
        - threshold: token 上限阈值
        - method: 估算方法

        返回:
        - 截断后的新消息列表
        """
        turns = self._group_messages_by_turns(messages)
        if not turns:
            return messages.copy()

        system_turn = None
        # 提取并保留纯系统轮次
        if all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        truncated_turns = turns[:]
        # 逐轮删除最早的非系统轮次
        while truncated_turns and self.estimate_token(
            self._flatten_turns(([system_turn] if system_turn else []) + truncated_turns),
            method=method
        ) > threshold:
            truncated_turns.pop(0)

        result_turns = ([system_turn] if system_turn else []) + truncated_turns
        return self._flatten_turns(result_turns)

    def _apply_mid_truncation(self, messages: List[Dict[str, Any]], threshold: int, method: TokenEstimateMethod) -> List[Dict[str, Any]]:
        """
        保留头尾并按整轮移除中间消息直到满足阈值

        参数:
        - messages: 原始消息列表
        - threshold: 本地估算 token 上限
        - method: token 估算方法

        返回:
        - List[Dict[str, Any]]: 截断后的消息副本
        """
        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)
        if len(turns) <= 4:
            logger.debug(f"[上下文管理] 轮次过少, 中间截断退化为滑动窗口, ID: {self.conversation_id}")
            return self._apply_sliding_truncation(messages, threshold, method)

        def token_of_turn_list(turn_list: List[List[Dict[str, Any]]]) -> int:
            return self.estimate_token(
                self._flatten_turns(([system_turn] if system_turn else []) + turn_list),
                method=method,
            )

        kept_turns = turns[:]
        while token_of_turn_list(kept_turns) > threshold and len(kept_turns) > 2:
            del kept_turns[len(kept_turns) // 2]
        return self._flatten_turns(([system_turn] if system_turn else []) + kept_turns)

    def _apply_truncation(self, messages: List[Dict[str, Any]], method: TokenEstimateMethod = "tokenizer") -> List[Dict[str, Any]]:
        """
        对消息列表应用截断策略, 返回截断后的新列表 (不修改原列表)

        滞回截断: 未达触发线(trigger_tokens)不处理, 超过则截断到截断底线(floor_tokens),
        触发线与底线之间的缓冲带内前缀稳定, 避免贴线抖动导致服务器 prefix cache 失效

        参数:
        - messages: 待截断的消息列表
        - method: token 估算方法, 同 estimate_token() 参数

        返回:
        - List[Dict]: 截断后的新列表
        """
        current_tokens = self.estimate_token(messages, method=method)
        if current_tokens <= self.trigger_tokens:
            return messages.copy()   # 未达触发线, 无需截断

        logger.debug(f"模型上下文达触发线 ({current_tokens} > {self.trigger_tokens})，应用 {self.exceed_process} 截断到 {self.floor_tokens}")

        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        if self.exceed_process == "sliding":   # 滑动窗口截断
            return self._apply_sliding_truncation(messages, self.floor_tokens, method)

        elif self.exceed_process == "mid_truncate":   # 中间截断: 保留头部和尾部, 删除中间轮次
            return self._apply_mid_truncation(messages, self.floor_tokens, method)

        else:
            logger.error(f"[上下文管理] 未知截断策略 {self.exceed_process}, 返回原列表, ID: {self.conversation_id}")
            return messages.copy()

class AsyncContextManager:
    """异步对话上下文管理器"""
    def __init__(
        self,
        conversation_id: Union[int, str],
        keep_in_memory: bool = False,
        db_path: str = get_db_path(),
        max_context: int = 128000,
        history_ratio: float = 0.7,
        context_threshold: float = 0.8,
        truncation_floor: float = 0.4,
        exceed_process: str = "sliding",
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        auto_checkpoint: bool = True,
        summary_keep_recent_turns: int = 6,
    ):
        """
        初始化异步上下文管理器

        注意: 初始化后请 await manager.initialize() 或使用 async with 语句自动初始化

        参数:
        - conversation_id: 当前对话的唯一 ID
        - keep_in_memory:
            True: 加载数据后在内存操作, 需手动调用 save_context() 写入数据库 <br>
            False: (推荐) 每次修改操作自动同步到数据库, 保证数据不丢失
        - db_path: SQLite 数据库路径, 默认 local 平台的 platform.db
        - max_context: 最大上下文长度, 默认 128k
        - history_ratio: 历史上下文比例, 历史预算 = max_context x history_ratio, 默认 0.7
        - context_threshold: 上下文阈值(占历史预算比例), 触发线 = 历史预算 x context_threshold, 默认 0.8
        - truncation_floor: 上下文截断底线(占历史预算比例), 截断目标 = 历史预算 x truncation_floor, 默认 0.4
        - exceed_process: 超过触发线时的处理方式, 默认 "sliding" (滑动窗口)
            - "sliding": 滑动窗口策略, 删除旧消息, 保持上下文长度在截断底线以下
            - "mid_truncate": 中间截断策略, 从中间截断上下文, 不删除旧消息
            - "summarize": 总结旧轮次并保留最近 summary_keep_recent_turns 轮原文
        - summary_keep_recent_turns: 总结压缩时必须原样保留的最近轮数, 默认 6
        - state_store: 状态检查点存储实例, 传入后启用检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向当前库的 StateStore, 与显式传入 state_store 二选一
        - auto_checkpoint: 启用检查点后, 每次写入用户/机器人消息自动保存稳定检查点 (同水位去重), 默认 True
        """
        if not (0 < truncation_floor < context_threshold <= 1):
            truncation_floor = 0.4
            logger.warning(
                f"截断底线必须小于上下文阈值: truncation_floor({truncation_floor}) < context_threshold({context_threshold})，退回默认值"
            )
        if not (0 < history_ratio <= 1):
            history_ratio = 0.7
            logger.warning(f"历史上下文比例必须在 (0, 1] 区间: history_ratio={history_ratio}，退回默认值")

        self.db_path = db_path
        self.conversation_id = str(conversation_id)
        self.keep_in_memory = keep_in_memory
        self.auto_checkpoint = auto_checkpoint

        self.state_store = state_store   # 检查点存储: 显式传入优先, 否则按开关自动创建 (与消息同库, 保证事务原子性)

        if self.state_store is None and enable_checkpoint:
            self.state_store = StateStore(db_path=self.db_path)

        self._messages: List[Dict[str, Any]] = []   # 内存中的消息缓存
        self._runtime_state = _ContextRuntimeState()   # 可重建的总结与 usage 校准状态
        self._runtime_state_dirty = False              # 运行时状态待持久化标记
        self._saved_count = 0   # 已持久化到库的消息条数 (增量保存水位, -1 表示需全量重写)
        self.max_context = max_context   # 最大上下文长度
        self.history_ratio = history_ratio   # 历史上下文比例
        self.context_threshold = context_threshold   # 上下文阈值(占历史预算比例)
        self.truncation_floor = truncation_floor   # 上下文截断底线(占历史预算比例)
        self.exceed_process = exceed_process   # 超过触发线时的处理方式
        self.summary_keep_recent_turns = max(0, summary_keep_recent_turns)   # 总结策略保留轮数

        self.history_budget = int(max_context * history_ratio)
        # 派生值: 滞回截断的触发线/底线/输出预算
        self.trigger_tokens = int(self.history_budget * context_threshold)
        self.floor_tokens = int(self.history_budget * truncation_floor)
        self.output_budget = max_context - self.history_budget
        logger.info(f"[异步上下文管理] 实例已创建，对话 ID: {self.conversation_id}")

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

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None):
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
                await conn.execute('''
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
                ''')

                await conn.execute('''CREATE INDEX IF NOT EXISTS idx_conv_id ON chat_history (conversation_id)''')

                await conn.execute('''
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
                ''')

                try:
                    await conn.execute(
                        "ALTER TABLE context_runtime_state ADD COLUMN api_cached_tokens INTEGER"
                    )
                except aiosqlite.OperationalError:
                    pass

                for col in ('content_json', 'tool_call_id', 'tool_calls', 'reasoning_content'):
                    try:
                        await conn.execute(f"ALTER TABLE chat_history ADD COLUMN {col} TEXT")
                    except aiosqlite.OperationalError:
                        pass

                await conn.commit()
        except Exception as e:
            logger.error(f"[异步上下文管理] 初始化数据库表失败：{e}, ID: {self.conversation_id}")

    async def _sync(self):
        """根据 keep_in_memory 策略决定是否立即写入数据库"""
        if not self.keep_in_memory:
            await self.save_context()

    # ================= 核心功能实现 =================

    async def load_context(self):
        """从数据库加载当前 ID 的上下文信息到内存中"""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(
                    "SELECT role, content, content_json, tool_call_id, tool_calls, reasoning_content FROM chat_history WHERE conversation_id = ? ORDER BY id ASC", 
                    (self.conversation_id,)
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
            logger.error(f"[异步上下文管理] 加载上下文失败：{self.conversation_id}: {e}, ID: {self.conversation_id}")
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
        prev_saved = self._saved_count   # 失败时恢复水位, 保证重试幂等
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                if self._saved_count < 0 or self._saved_count > len(self._messages):
                    await conn.execute(
                        "DELETE FROM chat_history WHERE conversation_id = ?",
                        (self.conversation_id,),
                    )
                    # 全量重写: 消息列表被编辑过, 行 id 将重新分配
                    self._saved_count = 0

                new_messages = self._messages[self._saved_count:]
                if new_messages:
                    data_to_insert = [
                        (
                            self.conversation_id,
                            msg["role"],
                            content_text_projection(msg.get("content")),
                            _message_content_json(msg.get("content")),
                            msg.get("tool_call_id"),
                            json.dumps(msg["tool_calls"], ensure_ascii=False) if msg.get("tool_calls") else None,
                            msg.get("reasoning_content"),
                        )
                        for msg in new_messages
                    ]
                    await conn.executemany(
                        "INSERT INTO chat_history (conversation_id, role, content, content_json, tool_call_id, tool_calls, reasoning_content) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                        data_to_insert
                    )
                if self._runtime_state_dirty:
                    await self._write_runtime_state(conn)
                await conn.commit()
                # 提交成功后才推进已保存水位
                self._saved_count = len(self._messages)
                self._runtime_state_dirty = False

        except Exception as e:
            logger.error(f"[异步上下文管理] 保存上下文失败：{self.conversation_id}: {e}, ID: {self.conversation_id}")
            self._saved_count = prev_saved   # 恢复水位, 下次保存重试 (全量重写路径幂等)

    def _mark_dirty(self) -> None:
        """标记消息列表被外部编辑 (非纯追加), 下次保存走全量重写"""
        self._saved_count = -1
        self._runtime_state.summary = ""
        self._runtime_state.covered_turn_count = 0
        self._runtime_state.summary_model = None
        self._runtime_state.summary_prompt_version = _SUMMARY_PROMPT_VERSION
        self._runtime_state_dirty = True

    # ================= 检查点支持 =================

    def _scope(self) -> StateScope:
        """
        当前对话对应的状态作用域

        返回:
        - StateScope: 当前对话对应的状态作用域
        """
        return StateScope(namespace="conversation", scope_id=self.conversation_id)

    def _require_state_store(self) -> StateStore:
        """
        获取状态存储, 未启用时抛出 ValueError

        返回:
        - StateStore: 状态存储, 未启用时抛出 ValueError
        """
        if self.state_store is None:
            raise ValueError("未启用状态检查点, 请传入 state_store 或设置 enable_checkpoint=True")
        return self.state_store

    async def _maybe_auto_checkpoint(self) -> None:
        """消息写入后自动保存稳定检查点 (同水位去重, 失败不阻断主流程)"""
        if not self.auto_checkpoint or self.state_store is None:
            return
        try:
            await asyncio.to_thread(self.state_store.ensure_stable_checkpoint, self._scope())
        except Exception as e:
            logger.warning(f"[异步上下文管理器] 自动检查点保存失败: {e}")

    async def _protect_before_edit(self) -> None:
        """
        编辑类操作前: 物化当前状态为保护检查点, 并清理失效的指针检查点

        失败不阻断编辑主流程
        """
        if self.state_store is None:
            return
        try:
            with state_mutation_context(
                source="edit_protect", reason="编辑前状态保护"
            ):
                await asyncio.to_thread(
                    self.state_store.materialize_edit_protection, self._scope()
                )
        except Exception as e:
            logger.warning(f"[异步上下文管理器] 编辑前状态保护失败: {e}")

    async def create_checkpoint(self, name: str = "", description: str = "", batch_id: str = "") -> StateCheckpoint:
        """
        为当前对话创建状态检查点

        参数:
        - name: 检查点显示名称
        - description: 检查点说明
        - batch_id: 会话级聚合检查点的批次 ID, 同批检查点共享; 非聚合时留空

        返回:
        - StateCheckpoint: 创建的检查点
        """
        store = self._require_state_store()
        return await asyncio.to_thread(
            store.create_checkpoint, self._scope(), name, description, "manual", None, batch_id
        )

    async def list_checkpoints(self) -> List[StateCheckpoint]:
        """
        列出当前对话的全部检查点 (按创建时间升序)

        返回:
        - List[StateCheckpoint]: 检查点列表
        """
        store = self._require_state_store()
        return await asyncio.to_thread(store.list_checkpoints, self._scope())

    async def rollback(self, checkpoint_id: str) -> None:
        """
        回滚当前对话到指定检查点并重载上下文

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_rollback", reason=f"回滚到检查点 {checkpoint_id}"
        ):
            await asyncio.to_thread(store.rollback, checkpoint_id)
        await self.load_context()
        await self._invalidate_summary(persist=True)

    async def retry(self, checkpoint_id: str) -> None:
        """
        从指定检查点重试并重载上下文 (保留未来检查点)

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_retry", reason=f"重试到检查点 {checkpoint_id}"
        ):
            await asyncio.to_thread(store.retry, checkpoint_id)
        await self.load_context()
        await self._invalidate_summary(persist=True)

    async def fork(
        self,
        branch_name: str,
        checkpoint_id: Optional[str] = None,
    ) -> "AsyncContextManager":
        """
        从指定检查点 (默认最近一个) fork 一条新剧情线, 返回已初始化的新上下文管理器

        参数:
        - branch_name: 分支名称, 新对话 ID 形如 "{原ID}:fork:{分支名}"
        - checkpoint_id: 源检查点 ID, 默认最近一个检查点

        返回:
        - AsyncContextManager: 新分支的上下文管理器 (已初始化)

        异常:
        - ValueError: 当前对话没有检查点
        """
        store = self._require_state_store()
        checkpoints = await asyncio.to_thread(store.list_checkpoints, self._scope())
        if not checkpoints:
            raise ValueError("当前对话没有检查点, 请先创建检查点")
        source_id = checkpoint_id or checkpoints[-1].checkpoint_id
        new_conversation_id = f"{self.conversation_id}:fork:{branch_name}"
        with state_mutation_context(
            source="checkpoint_fork", reason=f"从检查点 {source_id} 分支"
        ):
            await asyncio.to_thread(store.fork, source_id, new_conversation_id)
        new_ctx = AsyncContextManager(
            new_conversation_id,
            keep_in_memory=self.keep_in_memory,
            db_path=self.db_path,
            max_context=self.max_context,
            history_ratio=self.history_ratio,
            context_threshold=self.context_threshold,
            truncation_floor=self.truncation_floor,
            exceed_process=self.exceed_process,
            summary_keep_recent_turns=self.summary_keep_recent_turns,
            state_store=store,
            auto_checkpoint=self.auto_checkpoint,
        )
        await new_ctx.initialize()
        return new_ctx

    def get_context(self) -> List[Dict[str, Any]]:
        """
        获取当前上下文中的所有消息 (内存操作, 同步)

        返回:
        - List[Dict[str, str]]: 消息列表
        """
        return self._messages
    
    def get_model_context(self, method: TokenEstimateMethod = "tokenizer") -> List[Dict[str, Any]]:
        """
        获取发送给模型的上下文 (保证在最大上下文长度内)

        参数:
        - method: token 估算方法, 同 estimate_token()

        返回:
        - 截断后的消息列表副本, 适合直接用于 API 调用
        """
        return self._apply_truncation(self._messages, method)

    async def add_user_message(self, message: str, img_urls: list[str] | None = None):
        """
        添加用户消息到上下文中

        参数:
        - message: 消息内容
        - img_urls: 图片 URL 或本地路径列表, 可选
        """
        self._messages.append({"role": "user", "content": build_multimodal_content(message, img_urls)})
        await self._sync()
        await self._maybe_auto_checkpoint()

    async def reset_system_prompt(self, message: str):
        """
        重置系统提示词

        参数:
        - message: 新的系统提示词
        """
        await self._protect_before_edit()
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        # 在开头插入新的系统消息
        self._messages.insert(0, {"role": "system", "content": message})
        self._mark_dirty()
        await self._sync()

    async def add_bot_message(self, message: str, tools_calls: list[dict[str, Any]] | None = None, ignore_think: bool = True, reasoning: str | None = None):
        """
        添加机器人消息到上下文中

        参数:
        - message: 消息内容
        - tools_calls: 工具调用信息列表, 可选, 每个元素格式为 {"id": "工具 ID", "type": "function", "function": {"name": "工具名", "arguments": "参数 JSON 字符串"}}
        - ignore_think: 是否忽略消息中的思考过程, 默认True
        - reasoning: 思考过程, 可选
        """
        if tools_calls:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,
                    "tool_calls": tools_calls,
                }
            )
        else:
            self._messages.append(
                {
                    "role": "assistant", 
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,
                }
            )

        await self._sync()
        await self._maybe_auto_checkpoint()

    async def add_chat(self, user_message: str, bot_message: str):
        """
        添加一条用户消息和对应的机器人消息到上下文中

        参数:
        - user_message: 用户消息
        - bot_message: 机器人消息
        """
        self._messages.append({"role": "user", "content": user_message})
        self._messages.append({"role": "assistant", "content": bot_message})
        await self._sync()
        await self._maybe_auto_checkpoint()

    async def add_tool_message(self, tool_call_id: str, tool_result: dict[str, Any]):
        """
        添加工具调用的返回消息到上下文中

        参数:
        - tool_call_id: 工具调用 ID
        - tool_result: 工具调用的返回结果
        """
        self._messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(tool_result, ensure_ascii=False)})
        await self._sync()
        await self._maybe_auto_checkpoint()

    async def add_tool_call_flow(self, message: str, tool_messages: list[dict[str, Any]], tool_results: list[dict[str, Any]]):
        """
        添加一个完整的工具调用消息流到上下文中

        相当于:
        ``` python
        await ctx.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            await ctx.add_tool_message(tool_msg["id"], tool_res)
        ```

        参数:
        - message: 模型消息
        - tool_messages: 工具调用消息列表
        - tool_results: 工具调用的返回结果列表, 与 tool_messages 一一对应
        """
        await self.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            await self.add_tool_message(tool_msg["id"], tool_res)
        await self._sync()

    async def add_at_system_start(self, message: str, separator: str = ""):
        """
        在原系统提示词的开头添加消息(修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符, 默认为空字符串
        """
        await self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                msg["content"] = message + separator + msg["content"]
                # 在开头拼接新内容
                break
        else:
            self._messages.insert(0, {"role": "system", "content": message})
            # 没有系统消息, 则新建一条并插入到开头
        self._mark_dirty()
        await self._sync()


    async def add_at_system_end(self, message: str, separator: str = ""):
        """
        在原系统提示词的结尾添加消息 (修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符, 默认为空字符串
        """
        await self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                msg["content"] = msg["content"] + separator + message
                # 在结尾拼接新内容
                break
        else:
            self._messages.insert(0, {"role": "system", "content": message})
            # 没有系统消息, 则新建一条并插入到开头
        self._mark_dirty()
        await self._sync()

    async def add_turn_messages(self, turn_messages: list[dict[str, Any]]):
        """
        添加多条消息到上下文中

        参数:
        - turn_messages: 要添加的消息列表
        """
        serialized: list[dict[str, Any]] = []
        for msg in turn_messages:
            new_msg = copy.deepcopy(msg)
            serialized.append(new_msg)

        self._messages.extend(serialized)
        await self._sync()
        await self._maybe_auto_checkpoint()

    def static_message(self) -> int:
        """
        统计上下文中的消息数量 (内存操作, 同步)

        返回:
        - int: 消息总数
        """
        return len(self._messages)

    async def del_context(self):
        """删除当前上下文中的所有消息, 保留系统消息"""
        await self._protect_before_edit()
        self._messages = [copy.deepcopy(msg) for msg in self._messages if msg.get("role") == "system"]
        self._mark_dirty()
        await self._sync()
        logger.info(f"上下文已清空：{self.conversation_id}, ID: {self.conversation_id}")

    async def del_system_message(self):
        """删除上下文中的系统消息"""
        await self._protect_before_edit()
        self._messages[:] = [msg for msg in self._messages if msg.get("role") != "system"]
        self._mark_dirty()
        await self._sync()

    async def del_message(self, index: int):
        """
        删除上下文中指定索引的消息

        参数:
        - index: 消息的索引
        """
        if 0 <= index < len(self._messages) or -len(self._messages) <= index < 0:
            await self._protect_before_edit()
            self._messages.pop(index)
        else:
            logger.error(f"[异步上下文管理] 删除消息失败: 索引 {index} 超出范围, ID: {self.conversation_id}")
        self._mark_dirty()
        await self._sync()

    async def del_last_message(self, n: int = 1):
        """
        删除上下文中的最后 n 条消息

        参数:
        - n: 删除的数量
        """
        await self._protect_before_edit()
        for _ in range(n):
            if self._messages:
                self._messages.pop()
        self._mark_dirty()
        await self._sync()

    async def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """用完整快照恢复上下文, 供会话准备失败时回滚"""
        self._messages = copy.deepcopy(messages)
        self._mark_dirty()
        await self._sync()

    async def del_last_chat(self, n: int = 1):
        """
        删除上下文中的最后 n 组聊天消息

        参数:
        - n: 删除的组数
        """
        await self._protect_before_edit()
        indices_to_remove: list[int] = []
        groups_removed = 0

        # Step.1 倒序遍历消息列表
        for i in range(len(self._messages) - 1, -1, -1):
            msg = self._messages[i]
            role = msg.get("role")

            if role == "system":   # 系统消息在开头, 说明已经没对话了
                break

            indices_to_remove.append(i)   # 将当前索引加入待删除列表

            if role == "user":   # 遇到 user 消息, 说明完成了一整组对话的定位
                groups_removed += 1
                if groups_removed >= n:
                    break

        for index in sorted(indices_to_remove, reverse=True):
            self._messages.pop(index)
        # 需要排序索引以确保 pop 顺序正确 (从大到小 pop 避免索引偏移)

        self._mark_dirty()
        await self._sync()

    async def export_json(self, file_path: str):
        """
        导出当前上下文到 json 文件 (异步文件 IO)

        参数:
        - file_path: 导出路径
        """
        data: dict[str, Any] = {"id": self.conversation_id, "messages": self._messages}
        try:
            def _write_file():
                """在线程中写入上下文文件, 避免阻塞事件循环"""
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
            
            await asyncio.to_thread(_write_file)
            logger.info(f"[异步上下文管理] 成功导出 json 文件：{file_path}, ID: {self.conversation_id}")

        except Exception as e:
            logger.error(f"[异步上下文管理] 导出 json 文件失败：{e}, ID: {self.conversation_id}")

    def _group_messages_by_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        将消息列表按对话轮次分组
        每组以 user 消息开头, 包含随后的 assistant 和 tool 消息
        第一组可能以 system 消息开头

        参数:
        - messages: 待分组的消息列表

        返回:
        - List[List[Dict]]: 分组后的轮次列表
        """
        turns: List[List[Dict[str, Any]]] = []
        current_turn: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system" and not current_turn:
                current_turn.append(msg)
            elif role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            else:
                current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)
        return turns

    def _flatten_turns(self, turns: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """
        将分组后的轮次列表还原为扁平消息列表

        参数:
        - turns: 对话轮次

        返回:
        - List[Dict[str, Any]]: 将分组后的轮次列表还原为扁平消息列表
        """
        return [msg for turn in turns for msg in turn]

    _SUMMARY_PROMPT = (
        "请将以下对话历史浓缩为一段简洁的摘要, 保留关键事实、用户意图、已做的决策和待办事项。"
        "摘要将注入 system prompt 作为后续对话的上下文, 请用第三人称客观描述, 不要遗漏影响后续交互的信息。"
    )

    def _conversation_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """返回不含开头 system 消息的完整对话轮次"""
        turns = self._group_messages_by_turns(messages)
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            turns.pop(0)
        return turns

    def _system_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """提取开头连续的 system 消息副本"""
        result: List[Dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "system":
                break
            result.append(copy.deepcopy(message))
        return result

    def _summary_context(self, messages: List[Dict[str, Any]], keep_recent_turns: int) -> List[Dict[str, Any]]:
        """使用缓存摘要构建非破坏性的模型上下文"""
        turns = self._conversation_turns(messages)
        recent = turns[-keep_recent_turns:] if keep_recent_turns > 0 else []
        result = self._system_messages(messages)
        if self._runtime_state.summary:
            result.append({
                "role": "system",
                "content": f"<对话历史摘要>\n{self._runtime_state.summary}\n</对话历史摘要>",
            })
        return result + self._flatten_turns(recent)

    async def _call_summary_llm(self, llm: AsyncLLM, text_chunks: List[str], prior_summary: str) -> str:
        """逐块调用异步 LLM 并滚动归并摘要"""
        summary = prior_summary
        for chunk in text_chunks:
            previous = f"\n\n已有摘要:\n{summary}" if summary else ""
            prompt = f"{self._SUMMARY_PROMPT}{previous}\n\n新增对话:\n{chunk}"
            result = await llm.chat(
                [{"role": "user", "content": prompt}],
                thinking="off",
                max_tokens=_summary_output_budget(self.floor_tokens // 2),
            )
            if not isinstance(result, str) or not result.strip():
                logger.warning("[异步上下文管理] 摘要正文为空, 使用扩展输出预算重试")
                result = await llm.chat(
                    [{"role": "user", "content": prompt}],
                    thinking="off",
                    max_tokens=_SUMMARY_RETRY_OUTPUT_TOKENS,
                )
            if not isinstance(result, str) or not result.strip():
                raise RuntimeError("上下文总结模型未返回有效文本")
            summary = result.strip()
        return summary

    async def _tighten_summary(self, llm: AsyncLLM, target_tokens: int) -> None:
        """
        将缓存摘要进一步压缩到指定目标附近

        参数:
        - llm: 用于压缩的异步 LLM
        - target_tokens: 摘要目标 token 数
        """
        prompt = (
            f"请把以下对话摘要进一步压缩到约 {target_tokens} tokens 以内, "
            "保留关键事实、决定和待办事项:\n\n"
            f"{self._runtime_state.summary}"
        )
        result = await llm.chat(
            [{"role": "user", "content": prompt}],
            thinking="off",
            max_tokens=_summary_output_budget(target_tokens),
        )
        if not isinstance(result, str) or not result.strip():
            logger.warning("[异步上下文管理] 二次压缩正文为空, 使用扩展输出预算重试")
            result = await llm.chat(
                [{"role": "user", "content": prompt}],
                thinking="off",
                max_tokens=_SUMMARY_RETRY_OUTPUT_TOKENS,
            )
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError("上下文摘要二次压缩未返回有效文本")
        self._runtime_state.summary = result.strip()
        await self._save_runtime_state()

    async def summarize_and_compress(self, llm: AsyncLLM, keep_recent_turns: int) -> str:
        """
        总结最近 keep_recent_turns 轮之前的对话并缓存, 不修改完整消息历史

        参数:
        - llm: 用于总结的异步 LLM 实例
        - keep_recent_turns: 原样保留的最近对话轮数

        返回:
        - str: 总结文本; 对话轮次不足 keep_recent_turns 时返回空字符串(无可压缩)
        """
        if keep_recent_turns < 0:
            raise ValueError("keep_recent_turns 不能小于 0")
        turns = self._conversation_turns(self._messages)
        if len(turns) <= keep_recent_turns:
            return ""   # 对话轮次不足, 无可压缩

        target_covered = len(turns) - keep_recent_turns
        state = self._runtime_state
        if state.summary_prompt_version != _SUMMARY_PROMPT_VERSION or state.covered_turn_count > target_covered:
            await self._invalidate_summary()
            state = self._runtime_state
        new_turns = turns[state.covered_turn_count:target_covered]
        if not new_turns:
            return state.summary

        chunks = _split_summary_chunks(
            _summary_lines(new_turns),
            max(1024, min(self.floor_tokens, self.history_budget // 2)),
        )
        summary = await self._call_summary_llm(llm, chunks, state.summary)
        state.summary = summary
        state.covered_turn_count = target_covered
        state.summary_model = _llm_model_name(llm)
        state.summary_prompt_version = _SUMMARY_PROMPT_VERSION
        await self._save_runtime_state()
        logger.info(
            f"[异步上下文管理] 总结缓存更新完成: 覆盖 {target_covered} 轮, "
            f"保留 {keep_recent_turns} 轮完整历史, ID: {self.conversation_id}"
        )
        return summary

    def estimate_token(self, messages: List[Dict[str, Any]] | None = None, method: TokenEstimateMethod = "tokenizer") -> int:
        """
        估计当前上下文中的 token 数量

        参数:
        - messages: 待估算 token 数的消息列表, 默认当前上下文中的所有消息
        - method: 估计方法, 可选值为 "tokenizer" 或 "experience"

        返回:
        - int: token 数量
        """
        if messages is None:
            messages = self._messages
        return _estimate_request_tokens(messages, method)

    def get_context_usage(
        self,
        method: TokenEstimateMethod = "tokenizer",
    ) -> ContextUsageSnapshot:
        """
        返回当前历史预算和最近一次模型 usage 快照

        参数:
        - method: 当前完整历史的 token 估算方法

        返回:
        - ContextUsageSnapshot: 上下文预算与最近一次模型 usage
        """
        return ContextUsageSnapshot(
            history_tokens=self.estimate_token(method=method),
            context_window_tokens=self.max_context,
            reserved_output_tokens=self.output_budget,
            history_upper_tokens=self.history_budget,
            history_lower_tokens=self.floor_tokens,
            last_output_tokens=self._runtime_state.api_output_tokens,
            cache_hit_tokens=self._runtime_state.api_cached_tokens,
            history_token_source=method,
        )

    def estimate_request_tokens(
        self,
        messages: List[Dict[str, Any]],
        method: TokenEstimateMethod = "tokenizer",
        tools: Optional[List[Dict[str, Any]]] = None,
        img_urls: Optional[List[str]] = None,
    ) -> int:
        """估算包含工具定义和图片的完整模型请求"""
        return _estimate_request_tokens(messages, method, tools, img_urls)

    def _calibration_factor(self, model: Optional[str]) -> float:
        """计算本地估算到同模型真实 API input usage 的校准系数"""
        state = self._runtime_state
        if (
            state.api_input_tokens is None
            or state.estimated_input_tokens is None
            or state.estimated_input_tokens <= 0
            or state.usage_model != model
        ):
            return 1.0
        return max(0.1, min(10.0, state.api_input_tokens / state.estimated_input_tokens))

    def _prepare_mid_truncation(self, messages: List[Dict[str, Any]], threshold: int, method: TokenEstimateMethod) -> List[Dict[str, Any]]:
        """为异步准备流程执行非破坏性的中间截断"""
        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)
        if len(turns) <= 4:
            return self._apply_sliding_truncation(messages, threshold, method)

        def token_of_turn_list(turn_list: List[List[Dict[str, Any]]]) -> int:
            return self.estimate_token(
                self._flatten_turns(([system_turn] if system_turn else []) + turn_list),
                method=method,
            )

        kept_turns = turns[:]
        while token_of_turn_list(kept_turns) > threshold and len(kept_turns) > 2:
            del kept_turns[len(kept_turns) // 2]
        return self._flatten_turns(([system_turn] if system_turn else []) + kept_turns)

    async def prepare_model_context(
        self,
        *,
        llm: Optional[AsyncLLM] = None,
        pending_messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        img_urls: Optional[List[str]] = None,
        method: TokenEstimateMethod = "tokenizer",
        strategy: Optional[ContextStrategy] = None,
        keep_recent_turns: Optional[int] = None,
    ) -> PreparedModelContext:
        """为一次真实模型请求准备非破坏性上下文"""
        selected_strategy = strategy or cast(ContextStrategy, self.exceed_process)
        if selected_strategy == "summary":
            selected_strategy = "summarize"
        recent_turns = self.summary_keep_recent_turns if keep_recent_turns is None else keep_recent_turns
        if recent_turns < 0:
            raise ValueError("keep_recent_turns 不能小于 0")
        model = _llm_model_name(llm)
        messages = copy.deepcopy(self._messages)
        if pending_messages:
            messages.extend(copy.deepcopy(pending_messages))
        original_turns = len(self._conversation_turns(messages))
        original_estimate = self.estimate_request_tokens(messages, method, tools, img_urls)
        factor = self._calibration_factor(model)
        original_effective = max(0, round(original_estimate * factor))
        prepared_messages = messages

        if original_effective > self.trigger_tokens:
            local_floor = max(1, int(self.floor_tokens / factor))
            if selected_strategy == "summarize":
                if llm is None:
                    raise ValueError("总结压缩策略需要传入 llm")
                await self.summarize_and_compress(llm, recent_turns)
                prepared_messages = self._summary_context(messages, recent_turns)
            elif selected_strategy == "sliding":
                prepared_messages = self._apply_sliding_truncation(messages, local_floor, method)
            elif selected_strategy == "mid_truncate":
                prepared_messages = self._prepare_mid_truncation(messages, local_floor, method)
            else:
                raise ValueError(f"未知上下文处理策略: {selected_strategy}")

        prepared_estimate = self.estimate_request_tokens(prepared_messages, method, tools, img_urls)
        prepared_effective = max(0, round(prepared_estimate * factor))
        if (
            prepared_effective > self.history_budget
            and selected_strategy == "summarize"
            and self._runtime_state.summary
            and llm is not None
        ):
            turns = self._conversation_turns(messages)
            recent = turns[-recent_turns:] if recent_turns > 0 else []
            without_summary = self._system_messages(messages) + self._flatten_turns(recent)
            fixed_estimate = self.estimate_request_tokens(without_summary, method, tools, img_urls)
            fixed_effective = max(0, round(fixed_estimate * factor))
            if fixed_effective <= self.history_budget:
                target_tokens = max(64, int((self.history_budget - fixed_effective) / factor * 0.8))
                await self._tighten_summary(llm, target_tokens)
                prepared_messages = self._summary_context(messages, recent_turns)
                prepared_estimate = self.estimate_request_tokens(prepared_messages, method, tools, img_urls)
                prepared_effective = max(0, round(prepared_estimate * factor))
        if prepared_effective > self.history_budget:
            raise ContextOverflowError(
                f"保留内容仍超过历史上下文预算: {prepared_effective} > {self.history_budget}"
            )
        return PreparedModelContext(
            messages=prepared_messages,
            estimated_input_tokens=prepared_estimate,
            effective_input_tokens=prepared_effective,
            original_estimated_input_tokens=original_estimate,
            token_source="api_calibrated" if factor != 1.0 else method,
            strategy=selected_strategy,
            compressed=prepared_messages != messages,
            original_turns=original_turns,
            prepared_turns=len(self._conversation_turns(prepared_messages)),
            history_budget=self.history_budget,
            trigger_tokens=self.trigger_tokens,
            floor_tokens=self.floor_tokens,
            model=model,
        )

    async def record_model_usage(self, prepared: PreparedModelContext, usage: Optional[TokenUsage]) -> None:
        """保存已完成请求的真实 API usage, 供后续请求校准"""
        if usage is None:
            self._runtime_state.api_output_tokens = None
            self._runtime_state.api_total_tokens = None
            self._runtime_state.api_cached_tokens = None
            await self._save_runtime_state()
            return
        state = self._runtime_state
        state.usage_model = prepared.model
        state.api_input_tokens = usage.input_tokens
        state.estimated_input_tokens = prepared.estimated_input_tokens
        state.api_output_tokens = usage.output_tokens
        state.api_total_tokens = usage.total_tokens
        state.api_cached_tokens = usage.cached_tokens
        await self._save_runtime_state()

    def _apply_sliding_truncation(self, messages: List[Dict[str, Any]], threshold: int, method: TokenEstimateMethod) -> List[Dict[str, Any]]:
        """
        滑动窗口截断: 保留系统消息, 从最早的对话轮次开始整轮删除, 直到 token 数不超过阈值

        参数:
        - messages: 原始消息列表
        - threshold: token 上限阈值
        - method: 估算方法

        返回:
        - 截断后的新消息列表
        """
        turns = self._group_messages_by_turns(messages)
        if not turns:
            return messages.copy()

        system_turn = None
        # 提取并保留纯系统轮次
        if all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        truncated_turns = turns[:]
        # 逐轮删除最早的非系统轮次
        while truncated_turns and self.estimate_token(
            self._flatten_turns(([system_turn] if system_turn else []) + truncated_turns),
            method=method
        ) > threshold:
            truncated_turns.pop(0)

        result_turns = ([system_turn] if system_turn else []) + truncated_turns
        return self._flatten_turns(result_turns)

    def _apply_truncation(self, messages: List[Dict[str, Any]], method: TokenEstimateMethod = "tokenizer") -> List[Dict[str, Any]]:
        """
        对消息列表应用截断策略, 返回截断后的新列表 (不修改原列表)

        滞回截断: 未达触发线(trigger_tokens)不处理, 超过则截断到截断底线(floor_tokens),
        触发线与底线之间的缓冲带内前缀稳定, 避免贴线抖动导致服务器 prefix cache 失效

        参数:
        - messages: 待截断的消息列表
        - method: token 估算方法, 同 estimate_token() 参数

        返回:
        - List[Dict]: 截断后的新列表
        """
        current_tokens = self.estimate_token(messages, method=method)
        if current_tokens <= self.trigger_tokens:
            return messages.copy()   # 未达触发线, 无需截断

        logger.debug(f"[异步上下文管理] 模型上下文达触发线 ({current_tokens} > {self.trigger_tokens})，应用 {self.exceed_process} 截断到 {self.floor_tokens}, ID: {self.conversation_id}")

        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        if self.exceed_process == "sliding":   # 滑动窗口截断
            return self._apply_sliding_truncation(messages, self.floor_tokens, method)

        elif self.exceed_process == "mid_truncate":   # 中间截断: 保留头部和尾部, 删除中间轮次
            if len(turns) <= 4:
                logger.debug(f"[异步上下文管理] 轮次过少, 中间截断退化为滑动窗口, ID: {self.conversation_id}")
                return self._apply_sliding_truncation(messages, self.floor_tokens, method)

            def token_of_turn_list(turn_list: List[List[Dict[str, Any]]]) -> int:
                return self.estimate_token(
                    self._flatten_turns(([system_turn] if system_turn else []) + turn_list),
                    method=method,
                )

            kept_turns = turns[:]
            while token_of_turn_list(kept_turns) > self.floor_tokens and len(kept_turns) > 2:
                del kept_turns[len(kept_turns) // 2]
            return self._flatten_turns(([system_turn] if system_turn else []) + kept_turns)

        else:
            logger.error(f"[异步上下文管理] 未知截断策略 {self.exceed_process}, 返回原列表, ID: {self.conversation_id}")
            return messages.copy()

def add_user_message(context: list[dict[str, Any]], message: str, img_urls: list[str] | None = None):
    """
    向上下文中添加一条用户消息

    参数:
    - context: 上下文列表
    - message: 用户消息内容
    - img_urls: 图片 URL 或本地路径列表, 可选
    """
    context.append({"role": "user", "content": build_multimodal_content(message, img_urls)})

def add_bot_message(context: list[dict[str, Any]], message: str, tools_calls: list[dict[str, Any]] | None = None, reasoning: str | None = None):
    """
    向上下文中添加一条助手消息

    参数:
    - context: 上下文列表
    - message: 助手消息内容
    - tools_calls: 工具调用列表, 默认 None
    - reasoning: 思考内容, 默认 None
    """
    if tools_calls:
        context.append(
            {
                "role": "assistant",
                "content": message,
                "reasoning_content": reasoning if reasoning is not None and reasoning != "" else None,
                "tool_calls": tools_calls,
            }
        )
    else:
        context.append(
            {
                "role": "assistant", 
                "content": message,
                "reasoning_content": reasoning if reasoning is not None and reasoning != "" else None,
            }
        )

def add_tool_message(context: list[dict[str, Any]], tool_call_id: str, tool_result: dict[str, Any] | str):
    """
    向上下文中添加一条工具调用结果消息

    参数:
    - context: 上下文列表
    - tool_call_id: 工具调用 ID
    - tool_result: 工具调用结果
    """
    context.append({"role": "tool", "tool_call_id": tool_call_id,
        "content": json.dumps(tool_result, ensure_ascii=False) if isinstance(tool_result, dict) else tool_result})

def add_tools_call_flow(context: list[dict[str, Any]], message: str, tool_messages: list[dict[str, Any]], tool_results: list[dict[str, Any]], reasoning: str | None = None):
    """
    添加一个完整的工具调用消息流到上下文中

    相当于:
    ``` python
    ctx.add_bot_message(message, tool_messages, reasoning)
    for tool_msg, tool_res in zip(tool_messages, tool_results):
        ctx.add_tool_message(tool_msg["id"], tool_res)
    ```

    参数:
    - context: 上下文列表
    - message: 助手消息内容
    - tool_messages: 工具调用消息列表
    - tool_results: 工具调用结果列表
    - reasoning: 思考内容, 默认 None
    """
    add_bot_message(context, message, tools_calls=tool_messages, reasoning=reasoning)
    for tool_msg, tool_res in zip(tool_messages, tool_results):
        add_tool_message(context, tool_msg["id"], tool_res)

def clear_reasoning_content(messages: list[dict[str, Any]]):
    """
    清除上下文中所有消息的思考内容 (reasoning_content 字段)

    参数:
    - messages: 消息列表
    """
    for message in messages:
        if 'reasoning_content' in message:
            message["reasoning_content"] = None


