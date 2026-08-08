from satrap.core.utils.tokenizer import tokenizer_estimate, experience_estimate
from typing import List, Dict, Union, Optional, Any, cast
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
import re
import threading

from satrap.core.log import logger
from types import TracebackType
from satrap.core.state import StateStore
from satrap.core.state.mutation import state_mutation_context
from satrap.core.type import (
    JsonRow,
    RestoreOptions,
    SnapshotDomain,
    StateCheckpoint,
    StateScope,
)


def _messages_domain() -> SnapshotDomain:
    """消息领域注册: 管理 chat_history 表的对话消息"""
    def builder(conn: sqlite3.Connection, scope: StateScope) -> List[JsonRow]:
        """读取指定对话的全部消息行"""
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
        """清空后按快照重写指定对话的消息 (与 save_context 的列保持一致)"""
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
        """清空指定对话的全部消息"""
        conn.execute(
            "DELETE FROM chat_history WHERE conversation_id = ?",
            (scope.scope_id,),
        )

    def position_provider(conn: sqlite3.Connection, scope: StateScope) -> int:
        """提供消息水位: 当前对话消息条数 (追加式指针检查点的截断基准)"""
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
    """序列化多模态 content"""
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    return None


def _load_message_content(content: str | None, content_json: str | None) -> Any:
    """加载消息 content, 优先使用完整 JSON"""
    if content_json:
        try:
            parsed = json.loads(content_json)
            if isinstance(parsed, list):
                return cast(list[dict[str, Any]], parsed)
        except Exception:
            pass
    return content


class ContextManager:
    """对话上下文管理器"""
    def __init__(
        self,
        conversation_id: Union[int, str],
        keep_in_memory: bool = False,
        db_path: str = ".satrap/chat_history.db",
        max_context: int = 128000,
        context_threshold: float = 0.9,
        exceed_process: str = "sliding",
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        auto_checkpoint: bool = True,
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
        - context_threshold: 上下文阈值, 超过阈值时删除旧消息, 默认 0.9 (即 90% 上下文长度)
        - exceed_process: 超过阈值时的处理方式, 默认 "sliding" (滑动窗口)
            - "sliding": 滑动窗口策略, 删除旧消息, 保持上下文长度在阈值以下
            - "mid_truncate": 中间截断策略, 从中间截断上下文, 不删除旧消息
        - state_store: 状态检查点存储实例, 传入后启用检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向当前库的 StateStore, 与显式传入 state_store 二选一
        - auto_checkpoint: 启用检查点后, 每次写入用户/机器人消息自动保存稳定检查点 (同水位去重), 默认 True

        返回:
        - None
        """
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
        self._messages: List[Dict[str, Any]] = []   # 内存中的消息缓存
        self._saved_count = 0   # 已持久化到库的消息条数 (增量保存水位, -1 表示需全量重写)
        self._conn: Optional[sqlite3.Connection] = None   # 复用数据库连接 (惰性创建)
        self._conn_lock = threading.Lock()   # 连接创建/释放互斥 (读写路径假定单线程使用)
        self._init_db_table()   # 初始化数据库表结构
        self.load_context()     # 加载数据

        self.max_context = max_context
        self.context_threshold = context_threshold
        self.exceed_process = exceed_process

        logger.info(f"[上下文管理器] 初始化完成, 对话ID: {self.conversation_id}")

    def _get_conn(self):
        """获取数据库连接 (进程内复用, 避免每次操作建连开销)

        注意: 复用连接假定 ContextManager 单线程使用 (Session 内串行调用),
        连接创建与释放由 _conn_lock 保护。
        """
        with self._conn_lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self.db_path)
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
            for row in rows:
                msg = {"role": row[0], "content": _load_message_content(row[1], row[2])}
                if row[3] is not None:
                    msg["tool_call_id"] = row[3]
                if row[4] is not None:
                    msg["tool_calls"] = json.loads(row[4])
                if row[5] is not None:
                    msg["reasoning_content"] = row[5]
                self._messages.append(msg)
            self._saved_count = len(rows)
        except Exception as e:
            logger.error(f"[上下文管理器] 加载上下文失败: {self.conversation_id}: {e}, ID: {self.conversation_id}")
            self._messages: List[Dict[str, Any]] = []
            self._saved_count = 0

    def save_context(self):
        """保存当前上下文到数据库

        增量策略: 纯追加时只 INSERT 尾部新消息 (O(1));
        编辑/外部修改 (标记 dirty) 或状态未知时全量重写
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        prev_saved = self._saved_count   # 失败时恢复水位, 保证重试幂等
        try:
            if self._saved_count < 0 or self._saved_count > len(self._messages):
                # 全量重写: 消息列表被编辑过, 行 id 将重新分配
                cursor.execute(
                    "DELETE FROM chat_history WHERE conversation_id = ?",
                    (self.conversation_id,),
                )
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
            conn.commit()
            # 提交成功后才推进已保存水位
            self._saved_count = len(self._messages)

        except Exception as e:
            logger.error(f"[上下文管理器] 保存上下文失败: {self.conversation_id}: {e}, ID: {self.conversation_id}")
            conn.rollback()
            self._saved_count = prev_saved   # 恢复水位, 下次保存重试 (全量重写路径幂等)

    def _mark_dirty(self) -> None:
        """标记消息列表被外部编辑 (非纯追加), 下次保存走全量重写"""
        self._saved_count = -1

    # ================= 检查点支持 =================

    def _scope(self) -> StateScope:
        """当前对话对应的状态作用域"""
        return StateScope(namespace="conversation", scope_id=self.conversation_id)

    def _require_state_store(self) -> StateStore:
        """获取状态存储, 未启用时抛出 ValueError"""
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
        """编辑类操作前: 物化当前状态为保护检查点, 并清理失效的指针检查点

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
        """为当前对话创建状态检查点

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
        """列出当前对话的全部检查点 (按创建时间升序)

        返回:
        - List[StateCheckpoint]: 检查点列表
        """
        return self._require_state_store().list_checkpoints(self._scope())

    def rollback(self, checkpoint_id: str) -> None:
        """回滚当前对话到指定检查点并重载上下文

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_rollback", reason=f"回滚到检查点 {checkpoint_id}"
        ):
            store.rollback(checkpoint_id)
        self.load_context()

    def retry(self, checkpoint_id: str) -> None:
        """从指定检查点重试并重载上下文 (保留未来检查点)

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_retry", reason=f"重试到检查点 {checkpoint_id}"
        ):
            store.retry(checkpoint_id)
        self.load_context()

    def fork(
        self,
        branch_name: str,
        checkpoint_id: Optional[str] = None,
    ) -> "ContextManager":
        """从指定检查点 (默认最近一个) fork 一条新剧情线, 返回新的上下文管理器

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
            context_threshold=self.context_threshold,
            exceed_process=self.exceed_process,
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

    def get_model_context(self, method: str = "tokenizer") -> List[Dict[str, Any]]:
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
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,   # type: ignore
                    "tool_calls": tools_calls,
                }
            )
        else:
            self._messages.append(
                {
                    "role": "assistant", 
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,   # type: ignore
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
        - separator: 拼接时插入的分隔符，默认为空字符串
        """
        self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                # 在开头拼接新内容
                msg["content"] = message + separator + msg["content"]   # type: ignore
                break
        else:
            # 没有系统消息, 则新建一条并插入到开头
            self._messages.insert(0, {"role": "system", "content": message})
        self._mark_dirty()
        self._sync()

    def add_at_system_end(self, message: str, separator: str = ""):
        """
        在原系统提示词的结尾添加消息 (修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符，默认为空字符串
        """
        self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                # 在结尾拼接新内容
                msg["content"] = msg["content"] + separator + message   # type: ignore
                break
        else:
            # 没有系统消息, 则新建一条并插入到开头
            self._messages.insert(0, {"role": "system", "content": message})
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

        # 1. 倒序遍历消息列表
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
        """将分组后的轮次列表还原为扁平消息列表"""
        return [msg for turn in turns for msg in turn]

    def estimate_token(self, messages: List[Dict[str, Any]] | None = None, method: str = "tokenizer") -> int:
        """
        估计当前上下文中的 token 数量

        参数:
        - messages: 待估算 token 数的消息列表, 默认当前上下文中的所有消息
        - method: 估计方法, 可选值为 "tokenizer" 或 "experience"

        返回:
        - int: token 数量
        """
        token_count = 0
        if messages is None:
            messages = self._messages
        for msg in messages:
            content = msg.get("content", "")
            token_count += 4   # 每个消息有 4 个固定 token
            token_count += estimate_content_image_count(content) * DEFAULT_IMAGE_TOKEN_COST
            text_content = content_text_projection(content)
            if method == "tokenizer":
                token_count += tokenizer_estimate(text_content)
            elif method == "experience":
                token_count += experience_estimate(text_content)
            else:
                token_count += experience_estimate(text_content)   # 默认使用经验法则
        return token_count

    def _apply_sliding_truncation(self, messages: List[Dict[str, Any]], threshold: int, method: str) -> List[Dict[str, Any]]:
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

        # 提取并保留纯系统轮次
        system_turn = None
        if all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        # 逐轮删除最早的非系统轮次
        truncated_turns = turns[:]
        while truncated_turns and self.estimate_token(
            self._flatten_turns(([system_turn] if system_turn else []) + truncated_turns),
            method=method
        ) > threshold:
            truncated_turns.pop(0)

        result_turns = ([system_turn] if system_turn else []) + truncated_turns
        return self._flatten_turns(result_turns)

    def _apply_truncation(self, messages: List[Dict[str, Any]], method: str = "tokenizer") -> List[Dict[str, Any]]:
        """
        对消息列表应用截断策略, 返回截断后的新列表 (不修改原列表)

        参数:
        - messages: 待截断的消息列表
        - method: token 估算方法, 同 estimate_token() 参数

        返回:
        - List[Dict]: 截断后的新列表
        """
        threshold = int(self.max_context * self.context_threshold)

        current_tokens = self.estimate_token(messages, method=method)
        if current_tokens <= threshold:
            return messages.copy()   # 无需截断, 返回副本

        logger.debug(f"模型上下文超限 ({current_tokens} > {threshold})，应用 {self.exceed_process} 截断")

        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        if self.exceed_process == "sliding":   # 滑动窗口截断
            return self._apply_sliding_truncation(messages, threshold, method)

        elif self.exceed_process == "mid_truncate":   # 中间截断: 保留头部和尾部, 删除中间轮次

            if len(turns) <= 4:
                logger.debug(f"[上下文管理] 轮次过少，中间截断退化为滑动窗口, ID: {self.conversation_id}")
                return self._apply_sliding_truncation(messages, threshold, method)

            def token_of_turn_list(turn_list: list[list[dict[str, Any]]]):   # 计算需要删除多少 token
                return self.estimate_token(self._flatten_turns(
                    ([system_turn] if system_turn else []) + turn_list
                ), method=method)

            kept_turns = turns[:]   # 初始保留所有轮次  
            while token_of_turn_list(kept_turns) > threshold and len(kept_turns) > 2:   # 从中间开始逐轮删除, 直到满足条件
                mid = len(kept_turns) // 2   # 找到中间索引
                del kept_turns[mid]   # 删除

            result = ([system_turn] if system_turn else []) + kept_turns   # 合并保留的轮次
            return self._flatten_turns(result)

        else:
            logger.error(f"[上下文管理] 未知截断策略 {self.exceed_process}, 返回原列表, ID: {self.conversation_id}")
            return messages.copy()

class AsyncContextManager:
    """异步对话上下文管理器"""
    def __init__(
        self,
        conversation_id: Union[int, str],
        keep_in_memory: bool = False,
        db_path: str = ".satrap/chat_history.db",
        max_context: int = 128000,
        context_threshold: float = 0.9,
        exceed_process: str = "sliding",
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        auto_checkpoint: bool = True,
    ):
        """
        初始化异步上下文管理器

        注意: 初始化后请 await manager.initialize() 或使用 async with 语句自动初始化

        参数:
        - conversation_id: 当前对话的唯一 ID
        - keep_in_memory:
            True: 加载数据后在内存操作，需手动调用 save_context() 写入数据库 <br>
            False: (推荐) 每次修改操作自动同步到数据库, 保证数据不丢失
        - db_path: SQLite 数据库路径, 默认 ".satrap/chat_history.db"
        - max_context: 最大上下文长度, 默认 128k
        - context_threshold: 上下文阈值, 超过阈值时删除旧消息, 默认 0.9 (即 90% 上下文长度)
        - exceed_process: 超过阈值时的处理方式, 默认 "sliding" (滑动窗口)
            - "sliding": 滑动窗口策略, 删除旧消息, 保持上下文长度在阈值以下
            - "mid_truncate": 中间截断策略, 从中间截断上下文, 不删除旧消息
        - state_store: 状态检查点存储实例, 传入后启用检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向当前库的 StateStore, 与显式传入 state_store 二选一
        - auto_checkpoint: 启用检查点后, 每次写入用户/机器人消息自动保存稳定检查点 (同水位去重), 默认 True

        """
        self.db_path = db_path
        self.conversation_id = str(conversation_id)
        self.keep_in_memory = keep_in_memory
        self.auto_checkpoint = auto_checkpoint
        # 检查点存储: 显式传入优先, 否则按开关自动创建 (与消息同库, 保证事务原子性)
        self.state_store = state_store
        if self.state_store is None and enable_checkpoint:
            self.state_store = StateStore(db_path=self.db_path)
        self._messages: List[Dict[str, Any]] = []   # 内存中的消息缓存
        self._saved_count = 0   # 已持久化到库的消息条数 (增量保存水位, -1 表示需全量重写)
        self.max_context = max_context                     # 最大上下文长度
        self.context_threshold = context_threshold         # 上下文阈值
        self.exceed_process = exceed_process               # 超过阈值时的处理方式
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
        """支持 async with 语句"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None):
        """退出时确保数据保存"""
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
            for row in rows:
                msg = {"role": row[0], "content": _load_message_content(row[1], row[2])}
                if row[3] is not None:
                    msg["tool_call_id"] = row[3]
                if row[4] is not None:
                    msg["tool_calls"] = json.loads(row[4])
                if row[5] is not None:
                    msg["reasoning_content"] = row[5]
                self._messages.append(msg)
            self._saved_count = len(rows)
        except Exception as e:
            logger.error(f"[异步上下文管理] 加载上下文失败：{self.conversation_id}: {e}, ID: {self.conversation_id}")
            self._messages = []
            self._saved_count = 0

    async def save_context(self):
        """保存当前上下文到数据库

        增量策略: 纯追加时只 INSERT 尾部新消息 (O(1));
        编辑/外部修改 (标记 dirty) 或状态未知时全量重写
        """
        prev_saved = self._saved_count   # 失败时恢复水位, 保证重试幂等
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                if self._saved_count < 0 or self._saved_count > len(self._messages):
                    # 全量重写: 消息列表被编辑过, 行 id 将重新分配
                    await conn.execute(
                        "DELETE FROM chat_history WHERE conversation_id = ?",
                        (self.conversation_id,),
                    )
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
                await conn.commit()
                # 提交成功后才推进已保存水位
                self._saved_count = len(self._messages)

        except Exception as e:
            logger.error(f"[异步上下文管理] 保存上下文失败：{self.conversation_id}: {e}, ID: {self.conversation_id}")
            self._saved_count = prev_saved   # 恢复水位, 下次保存重试 (全量重写路径幂等)

    def _mark_dirty(self) -> None:
        """标记消息列表被外部编辑 (非纯追加), 下次保存走全量重写"""
        self._saved_count = -1

    # ================= 检查点支持 =================

    def _scope(self) -> StateScope:
        """当前对话对应的状态作用域"""
        return StateScope(namespace="conversation", scope_id=self.conversation_id)

    def _require_state_store(self) -> StateStore:
        """获取状态存储, 未启用时抛出 ValueError"""
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
        """编辑类操作前: 物化当前状态为保护检查点, 并清理失效的指针检查点

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
        """为当前对话创建状态检查点

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
        """列出当前对话的全部检查点 (按创建时间升序)

        返回:
        - List[StateCheckpoint]: 检查点列表
        """
        store = self._require_state_store()
        return await asyncio.to_thread(store.list_checkpoints, self._scope())

    async def rollback(self, checkpoint_id: str) -> None:
        """回滚当前对话到指定检查点并重载上下文

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_rollback", reason=f"回滚到检查点 {checkpoint_id}"
        ):
            await asyncio.to_thread(store.rollback, checkpoint_id)
        await self.load_context()

    async def retry(self, checkpoint_id: str) -> None:
        """从指定检查点重试并重载上下文 (保留未来检查点)

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_retry", reason=f"重试到检查点 {checkpoint_id}"
        ):
            await asyncio.to_thread(store.retry, checkpoint_id)
        await self.load_context()

    async def fork(
        self,
        branch_name: str,
        checkpoint_id: Optional[str] = None,
    ) -> "AsyncContextManager":
        """从指定检查点 (默认最近一个) fork 一条新剧情线, 返回已初始化的新上下文管理器

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
            context_threshold=self.context_threshold,
            exceed_process=self.exceed_process,
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
    
    def get_model_context(self, method: str = "tokenizer") -> List[Dict[str, Any]]:
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
        - tools_calls: 工具调用信息列表，可选，每个元素格式为 {"id": "工具 ID", "type": "function", "function": {"name": "工具名", "arguments": "参数 JSON 字符串"}}
        - ignore_think: 是否忽略消息中的思考过程, 默认True
        - reasoning: 思考过程, 可选
        """
        if tools_calls:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,   # type: ignore
                    "tool_calls": tools_calls,
                }
            )
        else:
            self._messages.append(
                {
                    "role": "assistant", 
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,   # type: ignore
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
        - tool_results: 工具调用的返回结果列表，与 tool_messages 一一对应
        """
        await self.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            await self.add_tool_message(tool_msg["id"], tool_res)
        await self._sync()

    async def add_at_system_start(self, message: str, separator: str = ""):
        """
        在原系统提示词的开头添加消息（修改第一条系统消息的内容）

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符，默认为空字符串
        """
        await self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                # 在开头拼接新内容
                msg["content"] = message + separator + msg["content"]   # type: ignore
                break
        else:
            # 没有系统消息，则新建一条并插入到开头
            self._messages.insert(0, {"role": "system", "content": message})
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
                # 在结尾拼接新内容
                msg["content"] = msg["content"] + separator + message   # type: ignore
                break
        else:
            # 没有系统消息, 则新建一条并插入到开头
            self._messages.insert(0, {"role": "system", "content": message})
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

    async def del_last_chat(self, n: int = 1):
        """
        删除上下文中的最后 n 组聊天消息

        参数:
        - n: 删除的组数
        """
        await self._protect_before_edit()
        indices_to_remove: list[int] = []
        groups_removed = 0

        # 1. 倒序遍历消息列表
        for i in range(len(self._messages) - 1, -1, -1):
            msg = self._messages[i]
            role = msg.get("role")

            if role == "system":   # 系统消息在开头，说明已经没对话了
                break

            indices_to_remove.append(i)   # 将当前索引加入待删除列表

            if role == "user":   # 遇到 user 消息，说明完成了一整组对话的定位
                groups_removed += 1
                if groups_removed >= n:
                    break

        # 需要排序索引以确保 pop 顺序正确 (从大到小 pop 避免索引偏移)
        for index in sorted(indices_to_remove, reverse=True):
            self._messages.pop(index)

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
            # 使用 asyncio.to_thread 避免阻塞事件循环
            def _write_file():
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
        """将分组后的轮次列表还原为扁平消息列表"""
        return [msg for turn in turns for msg in turn]

    def estimate_token(self, messages: List[Dict[str, Any]] | None = None, method: str = "tokenizer") -> int:
        """
        估计当前上下文中的 token 数量

        参数:
        - messages: 待估算 token 数的消息列表, 默认当前上下文中的所有消息
        - method: 估计方法, 可选值为 "tokenizer" 或 "experience"

        返回:
        - int: token 数量
        """
        token_count = 0
        if messages is None:
            messages = self._messages
        for msg in messages:
            content = msg.get("content", "")
            token_count += 4   # 每个消息有 4 个固定 token
            token_count += estimate_content_image_count(content) * DEFAULT_IMAGE_TOKEN_COST
            text_content = content_text_projection(content)
            if method == "tokenizer":
                token_count += tokenizer_estimate(text_content)
            elif method == "experience":
                token_count += experience_estimate(text_content)
            else:
                token_count += experience_estimate(text_content)   # 默认使用经验法则
        return token_count

    def _apply_sliding_truncation(self, messages: List[Dict[str, Any]], threshold: int, method: str) -> List[Dict[str, Any]]:
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

        # 提取并保留纯系统轮次
        system_turn = None
        if all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        # 逐轮删除最早的非系统轮次
        truncated_turns = turns[:]
        while truncated_turns and self.estimate_token(
            self._flatten_turns(([system_turn] if system_turn else []) + truncated_turns),
            method=method
        ) > threshold:
            truncated_turns.pop(0)

        result_turns = ([system_turn] if system_turn else []) + truncated_turns
        return self._flatten_turns(result_turns)

    def _apply_truncation(self, messages: List[Dict[str, Any]], method: str = "tokenizer") -> List[Dict[str, Any]]:
        """
        对消息列表应用截断策略, 返回截断后的新列表 (不修改原列表)

        参数:
        - messages: 待截断的消息列表
        - method: token 估算方法, 同 estimate_token() 参数

        返回:
        - List[Dict]: 截断后的新列表
        """
        threshold = int(self.max_context * self.context_threshold)

        current_tokens = self.estimate_token(messages, method=method)
        if current_tokens <= threshold:
            return messages.copy()   # 无需截断, 返回副本

        logger.debug(f"[异步上下文管理] 模型上下文超限 ({current_tokens} > {threshold})，应用 {self.exceed_process} 截断, ID: {self.conversation_id}")

        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        if self.exceed_process == "sliding":   # 滑动窗口截断
            return self._apply_sliding_truncation(messages, threshold, method)

        elif self.exceed_process == "mid_truncate":   # 中间截断: 保留头部和尾部, 删除中间轮次

            if len(turns) <= 4:
                logger.debug(f"[异步上下文管理] 轮次过少，中间截断退化为滑动窗口, ID: {self.conversation_id}")
                return self._apply_sliding_truncation(messages, threshold, method)

            def token_of_turn_list(turn_list: list[list[dict[str, Any]]]):   # 计算需要删除多少 token
                return self.estimate_token(self._flatten_turns(
                    ([system_turn] if system_turn else []) + turn_list
                ), method=method)

            kept_turns = turns[:]   # 初始保留所有轮次  
            while token_of_turn_list(kept_turns) > threshold and len(kept_turns) > 2:   # 从中间开始逐轮删除, 直到满足条件
                mid = len(kept_turns) // 2   # 找到中间索引
                del kept_turns[mid]   # 删除

            result = ([system_turn] if system_turn else []) + kept_turns   # 合并保留的轮次
            return self._flatten_turns(result)

        else:
            logger.error(f"[异步上下文管理] 未知截断策略 {self.exceed_process}, 返回原列表, ID: {self.conversation_id}")
            return messages.copy()

def add_user_message(context: list[dict[str, Any]], message: str, img_urls: list[str] | None = None):
    """向上下文中添加一条用户消息
    
    参数:
    - context: 上下文列表
    - message: 用户消息内容
    - img_urls: 图片 URL 或本地路径列表, 可选
    """
    context.append({"role": "user", "content": build_multimodal_content(message, img_urls)})

def add_bot_message(context: list[dict[str, Any]], message: str, tools_calls: list[dict[str, Any]] | None = None, reasoning: str | None = None):
    """向上下文中添加一条助手消息
    
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
                "reasoning_content": reasoning if reasoning is not None and reasoning != "" else None,   # type: ignore
                "tool_calls": tools_calls,
            }
        )
    else:
        context.append(
            {
                "role": "assistant", 
                "content": message,
                "reasoning_content": reasoning if reasoning is not None and reasoning != "" else None,   # type: ignore
            }
        )

def add_tool_message(context: list[dict[str, Any]], tool_call_id: str, tool_result: dict[str, Any] | str):
    """向上下文中添加一条工具调用结果消息
    
    参数:
    - context: 上下文列表
    - tool_call_id: 工具调用 ID
    - tool_result: 工具调用结果
    """
    context.append({"role": "tool", "tool_call_id": tool_call_id,
        "content": json.dumps(tool_result, ensure_ascii=False) if isinstance(tool_result, dict) else tool_result})

def add_tools_call_flow(context: list[dict[str, Any]], message: str, tool_messages: list[dict[str, Any]], tool_results: list[dict[str, Any]], reasoning: str | None = None):
    """添加一个完整的工具调用消息流到上下文中

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
    """清除上下文中所有消息的思考内容 (reasoning_content 字段)"""
    for message in messages:
        if 'reasoning_content' in message:
            message["reasoning_content"] = None


