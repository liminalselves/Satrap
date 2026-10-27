"""
聊天会话编排: AsyncSimpleSession + DisplayRecorder + 插件 + WebSocket 广播

职责:
- 按 conversation_id 管理 (AsyncSimpleSession, DisplayRecorder) 对
- LLM 实例由 ModelConfigManager 的 LLMConfig 构造 (与平台后端同一份配置)
- content/thinking 回调与工具观察钩子同时: 落库 (DisplayRecorder) + 广播 (WS 订阅者)
- send 立即返回, run 在后台 task 执行, 流式经 WS 推送

回调线程模型:
- 异步版 content/thinking 回调是协程, 在事件循环内 await -> 包装协程内同步落库 + put_nowait 广播
- 工具观察钩子是同步调用 (事件循环线程) -> 直接落库 + put_nowait 广播
- DisplayRecorder 内部 RLock + 独立连接, 多会话并发安全
"""
from __future__ import annotations
from satrap.core.plugin_interop.environment import EnvironmentSnapshot, bind_environment
from satrap.core.file_access.artifacts import FileArtifact, artifact_lease, artifact_from_metadata, bind_file_receiver, copy_artifact
from satrap.core.storage.file_lock import FileLease
from typing import BinaryIO
import mimetypes, base64

from satrap.edictum.plugin_compatibility import PluginEnvironment

from dataclasses import asdict, dataclass, field
from functools import wraps
import asyncio
import hashlib
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, TypeVar, cast, Iterable
import copy
import traceback
import os
import json
import time
import uuid

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.storage.session_fork import fork_session_settings
from satrap.core.utils.context_policy import resolve_context_policy
from satrap.core.memory.store import MemoryStore
from satrap.core.memory.service import MemoryService
from satrap.core.utils.async_worker import RAG_WORKERS
from satrap.core.utils.performance import PhaseTimings
from satrap.core.utils.application_progress import progress_instance, progress_locks, progress_phase, progress_result, progress_source_check
from satrap.edictum.plugin_settings import (
    resolve_runtime_specs,
    validate_plugin_settings,
    PluginSettingsService,
    validate_model_values,
    model_options,
)
from satrap.edictum.plugin_runtime import PluginRuntimeState, reconcile_plugin_states_async
from satrap.edictum.plugin_apply import PluginApplySnapshot, plugin_source_version
from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema, schema_to_payload, validate_config_values
from satrap.core.approval.contracts import ApprovalRequest, ApprovalResult
from satrap.core.APICall.LLMCall import AsyncLLM, build_llm_from_config
from satrap.core.APICall.LLMCall.errors import model_error_info
from satrap.edictum.plugin_spec import plugin_specs_fingerprint
from satrap.core.utils.paths import get_data_dir, get_project_root
from satrap.display.recorder import (
    DisplayRecorder,
    create_project as db_create_project,
    delete_project as db_delete_project,
    get_conversation_meta,
    get_project as db_get_project,
    list_projects as db_list_projects,
    query_conversations,
    set_conversation_project as db_set_conversation_project,
)
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.settings import ChatSettings, ChatSettingsStore
from satrap.edictum.plugin import load_plugin_meta
from satrap.core.storage import (
    CHAT_PLATFORM_ID,
    StorageLayout,
    StorageMaintenanceService,
    StorageScope,
    default_storage_layout,
    delete_session_domain_rows,
)
from satrap.core.type import CommandAction, LLMConfig, validate_thinking_levels
from satrap.core.rag import RagService
from satrap.edictum import AsyncSimpleSession

from satrap.core.log import logger

MSG_THINKING = "thinking_delta"
# WS 广播消息类型
MSG_CONTENT = "content_delta"
MSG_TOOL_START = "tool_start"
MSG_TOOL_END = "tool_end"
MSG_ASK_USER = "ask_user"
MSG_ASK_USER_END = "ask_user_end"
MSG_TURN_DONE = "turn_done"
MSG_ERROR = "error"

from satrap.edictum.simple_session.recovery import store_for_session, prepare_session_recovery
from satrap.core.framework.Base.execution.engine import configuration
from satrap.core.utils.media import visual_enabled, MAX_MEDIA_ITEMS, MAX_MEDIA_BYTES
from satrap.core.config.conversation_data import ConversationDataConflict
from satrap.core.config.conversation_runtime import perform_data_operation


def build_llm(cfg: LLMConfig) -> AsyncLLM:
    """
    由 LLMConfig 构造 AsyncLLM (统一经 build_llm_from_config, 含输出预算)

    参数:
    - cfg: 配置对象

    返回:
    - AsyncLLM: 由 LLMConfig 构造 AsyncLLM (统一经 build_llm_from_config, 含输出预算)
    """
    return cast(AsyncLLM, build_llm_from_config(cfg, async_=True))


@dataclass
class _PendingUserInput:
    """等待 Chat 前端回答的用户输入请求"""

    request_id: str
    question: str
    options: tuple[str, ...]
    future: asyncio.Future[str]
    approval: dict[str, object] | None = None


@dataclass
class _Conversation:
    """单个会话的运行时状态"""

    conversation_id: str
    session: AsyncSimpleSession
    recorder: DisplayRecorder
    model: str
    model_id: str | None = None
    """当前实际客户端对应的模型标识, 逐轮保存时不再读取共享配置"""
    default_think: str = "off"
    """会话默认思考强度 (来自 conversation_meta, send 未显式传 think 时使用)"""
    project_id: str | None = None
    """所属项目 ID (None = 无项目, 工作区回落全局)"""
    system_prompt: str | None = None
    """创建主工作流时使用的系统提示词"""
    temperature: float | None = None
    """Chat 会话级温度覆盖"""
    persisted: bool = True
    """是否已经写入会话元数据; 预加载会话在首次发送前为 False"""
    build_fingerprint: str = ""
    """模型, 项目, 提示词和插件运行配置的构建指纹"""
    preload_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """串行化预加载刷新, 首次发送和超时清理"""
    plugin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """串行化插件更新与会话运行"""
    plugin_states: list[PluginRuntimeState] = field(default_factory=list[PluginRuntimeState])
    """Chat 会话插件的已应用和目标运行状态"""
    preload_expiry_task: asyncio.Task[None] | None = None
    """未发送预加载会话的超时清理任务"""
    task: asyncio.Task[Any] | None = None
    """当前正在执行的 run task (None 表示空闲)"""
    pending_model_config: LLMConfig | None = None
    """当前轮次结束后需要应用的最新模型配置"""
    subscribers: set[_SubscriberQueue] = field(
        default_factory=lambda: set()
    )
    """WS 订阅者队列集合"""
    pending_user_inputs: dict[str, _PendingUserInput] = field(
        default_factory=lambda: dict[str, _PendingUserInput]()
    )
    """等待前端回答的 ask_user 请求"""


@dataclass
class _ConversationOperation:
    """从准备到后台运行持有会话, 取消完成后才释放"""

    task: asyncio.Task[Any]
    state: str = "preparing"
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class _SubscriberQueue(asyncio.Queue[dict[str, Any]]):
    """以消息数和 UTF-8 字节数限制订阅缓冲"""

    def __init__(self, maxsize: int = 1000, max_bytes: int = 1024 * 1024):
        super().__init__(maxsize=maxsize)
        self.max_bytes = max_bytes
        self.buffered_bytes = 0
        self.invalidated = False

    @staticmethod
    def _size(item: dict[str, Any]) -> int:
        return len(json.dumps(item, ensure_ascii=False).encode("utf-8"))

    def put_nowait(self, item: dict[str, Any]) -> None:
        size = self._size(item)
        if self.buffered_bytes + size > self.max_bytes:
            raise asyncio.QueueFull
        super().put_nowait(item)
        self.buffered_bytes += size

    def get_nowait(self) -> dict[str, Any]:
        item = super().get_nowait()
        self.buffered_bytes -= self._size(item)
        return item

    def invalidate(self) -> None:
        self.invalidated = True
        while not self.empty():
            self.get_nowait()
            self.task_done()
        self.put_nowait({"type": "resync_required"})


_OperationMethod = TypeVar("_OperationMethod", bound=Callable[..., Awaitable[dict[str, Any]]])
_CreationMethod = TypeVar("_CreationMethod", bound=Callable[..., Coroutine[Any, Any, _Conversation]])


def _exclusive_conversation_operation(method: _OperationMethod) -> _OperationMethod:
    """在首个 await 前占用会话; 所有入口均在同一事件循环中执行"""
    @wraps(method)
    async def wrapped(self: ChatService, conversation_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        conv = self._conversations.get(conversation_id)
        data_editing = any(operation.state == "data_edit" and (identity.startswith(conversation_id + "_") or conversation_id.startswith(identity + "_")) for identity, operation in self._operations.items())
        if conversation_id in self._operations or data_editing or (conv and conv.task is not None and not conv.task.done()):
            return {"ok": False, "error": "上一轮仍在进行, 请等待完成"}
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("会话操作必须运行在 asyncio 任务中")
        operation = _ConversationOperation(current)
        self._operations[conversation_id] = operation
        try:
            result = await method(self, conversation_id, *args, **kwargs)
            conv = self._conversations.get(conversation_id)
            if result.get("ok") and conv and conv.task is not None and not conv.task.done():
                operation.task = conv.task
                operation.state = "running"
                conv.task.add_done_callback(lambda _: self._release_operation(conversation_id, operation))
            return result
        finally:
            if operation.task is current:
                self._release_operation(conversation_id, operation)

    return cast(_OperationMethod, wrapped)


def _shared_runtime_creation(method: _CreationMethod) -> _CreationMethod:
    """按会话 ID 共享创建任务, 防止预加载和发送构造两个运行时"""
    @wraps(method)
    async def wrapped(self: ChatService, conversation_id: str, *args: Any, **kwargs: Any) -> _Conversation:
        if any(operation.state == "data_edit" and (identity == conversation_id or identity.startswith(conversation_id + "_")) for identity, operation in self._operations.items()):
            raise ValueError("对话数据正在编辑, 请稍后再激活")
        existing = self._conversations.get(conversation_id)
        if existing is not None:
            return existing
        restoring = self._resume_tasks.get(conversation_id)
        if restoring is not None:
            restored = await asyncio.shield(restoring)
            if restored is not None:
                return restored
        pending = self._creation_tasks.get(conversation_id)
        if pending is None:
            pending = asyncio.create_task(method(self, conversation_id, *args, **kwargs))
            self._creation_tasks[conversation_id] = pending

            def finished(task: asyncio.Task[_Conversation]) -> None:
                if self._creation_tasks.get(conversation_id) is task:
                    self._creation_tasks.pop(conversation_id)
                if not task.cancelled():
                    task.exception()

            pending.add_done_callback(finished)
        return await asyncio.shield(pending)

    return cast(_CreationMethod, wrapped)


class ChatService:
    """
    聊天会话编排服务

    生命周期由 server 持有; 所有方法在事件循环线程调用
    """

    def __init__(
        self,
        model_config: ModelConfigManager,
        plugins: ChatPluginRegistry,
        *,
        chat_db_path: str | None = None,
        display_db_path: str | None = None,
        storage_layout: StorageLayout | None = None,
        platform_id: str = CHAT_PLATFORM_ID,
        preload_ttl_seconds: float = 300.0,
        ask_user_timeout_seconds: float = 300.0,
        workspace_roots: Iterable[str | Path] | None = None,
        denied_workspace_roots: Iterable[str | Path] | None = None,
        settings: ChatSettingsStore | None = None,
    ) -> None:
        """
        初始化 ChatService

        参数:
        - model_config: 模型配置
        - plugins: 插件列表
        - chat_db_path: chatdb路径
        - display_db_path: displaydb路径
        - storage_layout: v2 数据布局
        - platform_id: Chat 存储平台实例 ID
        - preload_ttl_seconds: 未发送预加载会话的内存保留时间
        - ask_user_timeout_seconds: ask_user 等待前端回答的超时时间
        - workspace_roots: 项目目录允许浏览和绑定的根目录, 缺省或空列表使用本机磁盘, 兼容旧默认值 ["."]
        - denied_workspace_roots: 额外禁止绑定和浏览的目录根
        - settings: 可选的整体默认设置存储, 默认使用集中配置目录
        """
        self._model_cfg = model_config
        self._settings = settings or ChatSettingsStore()
        self._plugins = plugins
        self._storage = storage_layout or default_storage_layout
        self._platform_id = platform_id.strip() or CHAT_PLATFORM_ID
        self._storage.ensure_platform(self._platform_id)
        platform_db = str(self._storage.platform_db(self._platform_id))
        self._chat_db_path = chat_db_path or platform_db
        self._display_db_path = display_db_path or platform_db
        self._preload_ttl_seconds = max(float(preload_ttl_seconds), 1.0)
        self._ask_user_timeout_seconds = max(float(ask_user_timeout_seconds), 1.0)
        configured_roots = list(workspace_roots or [])
        if configured_roots == ["."]:
            configured_roots = []   # 旧版自动生成的默认值改为本机磁盘视图
        if not configured_roots:
            configured_roots = ([Path(f"{drive}:/") for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                 if Path(f"{drive}:/").is_dir()] if os.name == "nt" else [Path("/")])
        resolved_roots: list[Path] = []
        for raw_root in configured_roots:
            root = Path(raw_root).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"工作区根目录不存在或不是目录: {raw_root}")
            if root not in resolved_roots:
                resolved_roots.append(root)
        if not resolved_roots:
            raise ValueError("至少需要配置一个工作区根目录")
        self._workspace_roots = tuple(resolved_roots)
        denied_roots = [get_data_dir().resolve()]
        for raw_root in denied_workspace_roots or []:
            denied_root = Path(raw_root).expanduser().resolve()
            if denied_root not in denied_roots:
                denied_roots.append(denied_root)
        self._denied_workspace_roots = tuple(denied_roots)
        if any(self._is_workspace_denied(root) for root in self._workspace_roots):
            raise ValueError("工作区根目录不能位于受保护的 .satrap 数据目录内")
        self._conversations: dict[str, _Conversation] = {}
        self._operations: dict[str, _ConversationOperation] = {}
        self._resume_tasks: dict[str, asyncio.Task[_Conversation | None]] = {}
        self._creation_tasks: dict[str, asyncio.Task[_Conversation]] = {}
        self._stream_id = uuid.uuid4().hex
        self._event_sequences: dict[str, int] = {}
        self._orphan_queues: dict[str, set[_SubscriberQueue]] = {}
        """订阅时会话不在内存的孤儿队列, _resume_conversation 完成后挂入"""

    # ---------- 广播 ----------

    def _release_operation(self, conversation_id: str, operation: _ConversationOperation) -> None:
        """仅持有者可以释放占用; 取消端完成收尾前保持忙状态"""
        if operation.state != "cancelling" and self._operations.get(conversation_id) is operation:
            self._operations.pop(conversation_id)

    def _broadcast(self, conv: _Conversation, msg: dict[str, Any]) -> None:
        """
        非阻塞广播, 溢出订阅退出实时流并要求重同步

        参数:
        - conv: 会话对象
        - msg: 消息对象
        """
        sequence = self._event_sequences.get(conv.conversation_id, 0) + 1
        self._event_sequences[conv.conversation_id] = sequence
        msg = {**msg, "conversation_id": conv.conversation_id, "ts": time.time(), "seq": sequence, "stream_id": self._stream_id}
        for q in list(conv.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                conv.subscribers.discard(q)
                q.invalidate()
                logger.warning(f"[聊天] 会话 {conv.conversation_id} 慢订阅需要重同步")

    def runtime_snapshot(self, conversation_id: str, history_limit: int | None = None) -> dict[str, Any]:
        """
        同步捕获有界权威快照及事件序号, 避免异步读取越过未包含的事件

        参数:
        - conversation_id: 订阅会话 ID
        - history_limit: 可选历史窗口, 旧客户端不指定时保留全量语义

        返回:
        - 包含生成缓冲, 分页边界和事件序号的快照
        """
        conv = self._conversations.get(conversation_id)
        limit = history_limit + 1 if history_limit is not None else None
        turns = conv.recorder.snapshot_turns(limit=limit) if conv else self.list_turns(conversation_id, limit=limit, latest=limit is not None, all_variants=limit is None)
        pagination = {}
        if history_limit is not None:
            has_more = len(turns) > history_limit
            turns = turns[-history_limit:]
            pagination = {'has_more': has_more, 'next_before': turns[0]['turn_index'] if turns else None}
        active_turn = next((turn["id"] for turn in turns if turn.get("generating")), None)
        operation = self._operations.get(conversation_id)
        state = operation.state if operation and (active_turn is not None or operation.state != "running") else "idle"
        if active_turn is not None and state == "idle":
            state = "running"
        if state == "idle":
            for turn in turns:
                if not turn.get("variant_count"):
                    turn["interrupted"] = True
        pending_inputs: Iterable[_PendingUserInput] = conv.pending_user_inputs.values() if conv else ()
        meta = get_conversation_meta(conversation_id, db_path=self._display_db_path) if conv is None else None
        return {
            "type": "snapshot", "conversation_id": conversation_id, "stream_id": self._stream_id,
            "seq": self._event_sequences.get(conversation_id, 0), "state": state,
            "active_turn_id": active_turn, "turns": turns,
            "model": conv.model if conv else str(meta.get("model") or "default") if meta else None,
            **pagination,
            "pending_user_inputs": [
                {"request_id": item.request_id, "question": item.question, "options": list(item.options), "approval": item.approval}
                for item in pending_inputs if not item.future.done()
            ],
        }

    def subscribe(self, conversation_id: str) -> _SubscriberQueue:
        """
        订阅会话广播, 返回队列 (WS 处理器持有, 断开时 unsubscribe)

        参数:
        - conversation_id: 会话 ID

        会话不在内存时登记为孤儿队列, _resume_conversation 完成后自动挂入

        返回:
        - _SubscriberQueue: 队列 (WS 处理器持有, 断开时 unsubscribe)
        """
        q = _SubscriberQueue()
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            conv.subscribers.add(q)
        else:
            self._orphan_queues.setdefault(conversation_id, set()).add(q)
        return q

    async def get_approval(self, conversation_id: str) -> dict[str, object]:
        """
        获取当前 Chat 实际使用的宿主审批状态, 不从前端缓存推断模式

        参数:
        - conversation_id: 已存在的 Chat 会话 ID

        返回:
        - 安装可用性, 当前模式, 权限版本和计划模式; 会话不存在时抛出 ValueError
        """
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            raise ValueError("会话不存在")
        async with conv.plugin_lock:
            from satrap.core.approval.state import session_approval
            plugin = next((plugin for plugin in conv.session.list_plugins() if plugin.name == "permissions"), None)
            if plugin is None and getattr(conv.session, "_approval_state", None) is None:
                return {"ok": True, "available": False, "mode": "user", "revision": 0, "plan_mode": False}
            engine = session_approval(conv.session)
            return {"ok": True, "available": bool(plugin and plugin.enabled), "mode": engine.mode,
                    "revision": engine.revision, "plan_mode": engine.plan_mode}

    @_exclusive_conversation_operation
    async def set_approval(self, conversation_id: str, mode: object, expected_revision: object) -> dict[str, Any]:
        """
        通过已认证的 Chat 管理入口修改共享审批引擎, 不运行模型或写入对话历史

        参数:
        - conversation_id: 当前 Chat 会话 ID
        - mode: user, auto 或 full-access
        - expected_revision: 前端最近读取的权限版本, 防止覆盖命令或其它页面的新决定

        返回:
        - 保存后的实际状态; 未启用插件, 参数错误或版本冲突时抛出 ValueError
        """
        from satrap.core.approval.contracts import APPROVAL_MODES
        from satrap.core.approval.state import session_approval
        if not isinstance(mode, str) or mode not in APPROVAL_MODES or type(expected_revision) is not int:
            raise ValueError("审批模式或权限版本无效")
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            raise ValueError("会话不存在")
        async with conv.plugin_lock:
            plugin = next((plugin for plugin in conv.session.list_plugins() if plugin.name == "permissions"), None)
            if plugin is None or not plugin.enabled:
                raise ValueError("请先启用 permissions 插件")
            engine = session_approval(conv.session)
            engine.set_mode(mode, expected_revision=expected_revision)
            return {"ok": True, "available": True, "mode": engine.mode,
                    "revision": engine.revision, "plan_mode": engine.plan_mode}

    async def get_default_agent_prompt(self, conversation_id: str) -> dict[str, object]:
        """
        读取当前 Chat 会话的内置 Agent 提示开关

        参数:
        - conversation_id: Chat 会话 ID

        返回:
        - 当前开关; 会话不存在时抛出 ValueError
        """
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            raise ValueError("会话不存在")
        return {"ok": True, "default_agent_prompt_enabled": conv.session.default_agent_prompt_enabled}

    @_exclusive_conversation_operation
    async def set_default_agent_prompt(self, conversation_id: str, enabled: object) -> dict[str, object]:
        """
        持久化本会话提示开关, 与生成中的轮次互斥

        参数:
        - conversation_id: Chat 会话 ID
        - enabled: 布尔开关

        返回:
        - 已保存的开关, 非法参数或不存在的会话抛出 ValueError
        """
        if type(enabled) is not bool:
            raise ValueError("default_agent_prompt_enabled 必须是布尔值")
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            raise ValueError("会话不存在")
        async with conv.plugin_lock:
            self._save_default_agent_prompt(conv, enabled)
        return {"ok": True, "default_agent_prompt_enabled": enabled}

    def _save_default_agent_prompt(self, conv: _Conversation, enabled: bool) -> None:
        """
        在调用方已占用会话时保存提示开关

        参数:
        - conv: 已由设置操作或首次发送独占的会话
        - enabled: 已校验的布尔开关
        """
        store = conv.session.plugin_override_store
        if store is None:
            raise ValueError("会话覆盖存储尚未就绪")
        current = store.read(conv.conversation_id, "session.agent_prompt")
        store.replace(conv.conversation_id, "session.agent_prompt", {"default_agent_prompt_enabled": enabled}, expected_revision=current["revision"])
        conv.session.default_agent_prompt_enabled = enabled

    async def get_command_history(self, conversation_id: str) -> dict[str, object]:
        """
        读取会话命令历史设置

        参数:
        - conversation_id: 当前 Chat 会话 ID

        返回:
        - 命令记录开关; 会话不存在时抛出 ValueError
        """
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            raise ValueError("会话不存在")
        return {"ok": True, "command_history_enabled": conv.session.command_history_enabled}

    @_exclusive_conversation_operation
    async def set_command_history(self, conversation_id: str, enabled: object) -> dict[str, object]:
        """
        保存命令历史设置, 与当前轮次互斥

        参数:
        - conversation_id: 当前 Chat 会话 ID
        - enabled: 是否记录命令及回复, 必须为布尔值

        返回:
        - 已保存的开关状态
        """
        if type(enabled) is not bool:
            raise ValueError("command_history_enabled 必须是布尔值")
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            raise ValueError("会话不存在")
        async with conv.plugin_lock:
            store = conv.session.plugin_override_store
            if store is None:
                raise ValueError("会话覆盖存储尚未就绪")
            current = store.read(conversation_id, "session.commands")
            store.replace(conversation_id, "session.commands", {"command_history_enabled": enabled}, expected_revision=current["revision"])
            conv.session.command_history_enabled = enabled
        return {"ok": True, "command_history_enabled": enabled}

    async def get_max_iterations(self, conversation_id: str) -> dict[str, object]:
        """
        返回当前 Chat 会话的最大工具迭代次数

        参数:
        - conversation_id: 当前 Chat 会话 ID

        返回:
        - 会话当前生效的最大迭代次数
        """
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            raise ValueError("会话不存在")
        return {"ok": True, "max_iterations": conv.session.default_max_iterations}

    @_exclusive_conversation_operation
    async def set_max_iterations(self, conversation_id: str, value: object) -> dict[str, object]:
        """
        保存或清除最大工具迭代次数, 与当前轮次互斥

        参数:
        - conversation_id: 当前 Chat 会话 ID
        - value: 正整数上限, None 清除覆盖还原会话定义默认

        返回:
        - 已保存或还原后的最大迭代次数
        """
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError("max_iterations 必须是正整数或 null")
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            raise ValueError("会话不存在")
        async with conv.plugin_lock:
            store = conv.session.plugin_override_store
            if store is None:
                raise ValueError("会话覆盖存储尚未就绪")
            current = store.read(conversation_id, "session.engine")
            store.replace(conversation_id, "session.engine", {} if value is None else {"max_iterations": value}, expected_revision=current["revision"])
            conv.session.default_max_iterations = conv.session.configured_max_iterations if value is None else value
        return {"ok": True, "max_iterations": conv.session.default_max_iterations}

    async def _request_approval(self, conv: _Conversation, request: ApprovalRequest) -> ApprovalResult:
        """
        展示精确动作并把明确点击结果绑定到原始审批请求

        参数:
        - conv: 宿主 Chat 会话
        - request: 插件冻结的单次审批请求

        返回:
        - 仅批准本次或拒绝的结构化决定; 超时和取消沿用待回答请求的清理路径
        """
        approval = {"tool": request.tool, "operation": request.operation, "description": request.description,
                    "action": json.loads(request.action_json)}
        # 人工界面只展示被冻结的实际动作, 完整对话证据仅用于模型审查
        answer = await self._request_user_input(conv, "是否批准本次操作?", ["批准本次", "拒绝"], approval=approval)
        allowed = answer == "批准本次"
        return ApprovalResult.for_request(request, "allow" if allowed else "deny",
                                          "用户批准本次操作" if allowed else "用户未批准本次操作", "user")

    async def _request_user_input(
        self,
        conv: _Conversation,
        question: str,
        options: list[str] | None = None,
        *,
        approval: dict[str, object] | None = None,
    ) -> str:
        """
        向 Chat 前端发出问题并等待回答

        参数:
        - conv: 会话运行时
        - question: 问题内容
        - options: 推荐回答选项
        - approval: 可信宿主生成的单次操作资料, 默认 None 表示普通问答

        返回:
        - str: 用户回答
        """
        request_id = uuid.uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        normalized_options = tuple(dict.fromkeys(
            option.strip() for option in options or [] if isinstance(option, str) and option.strip()
        ))
        pending = _PendingUserInput(
            request_id=request_id,
            question=question,
            options=normalized_options,
            future=future,
            approval=approval,
        )
        conv.pending_user_inputs[request_id] = pending
        self._broadcast(conv, {
            "type": MSG_ASK_USER,
            "request_id": request_id,
            "question": question,
            "options": list(normalized_options),
            "approval": approval,
        })
        logger.info(f"[聊天] 会话 {conv.conversation_id} 等待用户回答: {request_id}")
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._ask_user_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._broadcast(conv, {
                "type": MSG_ASK_USER_END,
                "request_id": request_id,
                "status": "timeout",
            })
            logger.warning(f"[聊天] 会话 {conv.conversation_id} 等待用户回答超时: {request_id}")
            raise
        except asyncio.CancelledError:
            self._broadcast(conv, {
                "type": MSG_ASK_USER_END,
                "request_id": request_id,
                "status": "cancelled",
            })
            raise
        finally:
            conv.pending_user_inputs.pop(request_id, None)
            if not future.done():
                future.cancel()

    def answer_user_input(self, conversation_id: str, request_id: str, answer: str) -> dict[str, Any]:
        """
        回填 Chat 前端对 ask_user 的回答

        参数:
        - conversation_id: 会话 ID
        - request_id: 用户输入请求 ID
        - answer: 用户回答

        返回:
        - dict[str, Any]: 回填结果
        """
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        pending = conv.pending_user_inputs.get(request_id)
        if pending is None or pending.future.done():
            return {"ok": False, "error": "询问不存在或已结束"}
        if not answer.strip():
            return {"ok": False, "error": "回答不能为空"}
        if pending.approval is not None and answer not in pending.options:
            logger.warning(f"[聊天] 会话 {conversation_id} 审批回答无效: {request_id}")
            return {"ok": False, "error": "审批只能选择批准本次或拒绝"}
        pending.future.set_result(answer)
        self._broadcast(conv, {
            "type": MSG_ASK_USER_END,
            "request_id": request_id,
            "status": "answered",
        })
        logger.info(f"[聊天] 会话 {conversation_id} 已收到用户回答: {request_id}")
        return {"ok": True}

    def unsubscribe(self, conversation_id: str, q: _SubscriberQueue) -> None:
        """
        取消订阅

        参数:
        - conversation_id: 会话 ID
        - q: 输入值
        """
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            conv.subscribers.discard(q)
        orphans = self._orphan_queues.get(conversation_id)
        if orphans is not None:
            orphans.discard(q)
            if not orphans:
                del self._orphan_queues[conversation_id]

    # ---------- 会话管理 ----------

    def list_models(self) -> list[str]:
        """
        列出可用 LLM 配置名

        返回:
        - list[str]: 列出可用 LLM 配置名
        """
        return list(self._model_cfg.list_llm_configs(mask_api_key=True).keys())

    def _require_model(self, model: str) -> LLMConfig:
        """
        取得存在的模型配置, 不使用管理器为未知名称构造的空默认

        参数:
        - model: 用户明确选择或会话已绑定的配置名

        返回:
        - 实际配置; 不存在时抛出 ValueError 并要求手动重选
        """
        if model not in self.list_models():
            logger.warning(f"[聊天模型] 配置不可用: {model}")
            raise ValueError(f"模型配置不可用: {model}, 请手动选择其它模型")
        return self._model_cfg.get_llm_config(model)

    async def get_settings(self) -> ChatSettings:
        """
        读取不影响已有会话的整体默认

        返回:
        - 模型名与配置修订, 文件读取在线程内执行
        """
        return await asyncio.to_thread(self._settings.read)

    async def set_settings(self, model: str, expected_revision: str) -> ChatSettings:
        """
        保存整体默认, 不遍历或修改已有会话

        参数:
        - model: 已存在的目标模型配置名
        - expected_revision: 前端最近读取的修订

        返回:
        - 已保存的模型名与新修订
        """
        self._require_model(model)
        return await asyncio.to_thread(self._settings.save, model, expected_revision)

    async def get_model(self, conversation_id: str) -> dict[str, object]:
        """
        读取会话绑定, 模型已删除时仍允许用户查看并重选

        参数:
        - conversation_id: 已有或已预加载的会话 ID

        返回:
        - 已保存的名称和可用性, 不因查询而换用整体默认
        """
        conv = self._conversations.get(conversation_id)
        meta = get_conversation_meta(conversation_id, db_path=self._display_db_path) if conv is None else None
        if conv is None and meta is None:
            turns = self.list_turns(conversation_id, limit=1, all_variants=False)
            if not turns:
                raise ValueError("会话不存在")
        model = conv.model if conv else str(meta.get("model") or "default") if meta else "default"
        return {"ok": True, "model": model, "available": model in self.list_models()}

    @_exclusive_conversation_operation
    async def set_model(self, conversation_id: str, model: str, expected_model: str | None = None) -> dict[str, Any]:
        """
        在生成互斥下切换会话模型, 保存失败时恢复原客户端与上下文策略

        参数:
        - conversation_id: 当前会话 ID
        - model: 目标模型配置名
        - expected_model: 可选的客户端已确认绑定, 不一致时拒绝覆盖其它页面的选择

        返回:
        - 服务端确认的绑定; 忙碌或绑定冲突返回失败, 配置和持久化失败抛给请求边界
        """
        self._require_model(model)
        binding = await self.get_model(conversation_id)
        if expected_model is not None and binding["model"] != expected_model:
            return {"ok": False, "model": binding["model"], "error": "会话模型已在其它页面变更, 请重新读取后选择"}
        cold = conversation_id not in self._conversations
        conv: _Conversation | None = None
        try:
            conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id, model_override=model, publish=False)
            if conv is None:
                raise ValueError("会话不存在")
            async with conv.plugin_lock:
                config = self._require_model(model)
                llm = build_llm(config)
                if conv.temperature is not None:
                    llm.set_parameters(temperature=conv.temperature)
                fingerprint = self._runtime_fingerprint(model, system_prompt=conv.system_prompt,
                                                        project_id=conv.project_id, temperature=conv.temperature)
                previous_llm, previous_config = conv.session.llm, conv.session._context_config
                try:
                    conv.session.reload_llm(llm)
                    conv.session.apply_context_config(config)
                    if conv.persisted:
                        conv.recorder.save_meta(model, conv.default_think)
                except BaseException:
                    conv.session.reload_llm(previous_llm)
                    if previous_config is not None:
                        conv.session.apply_context_config(previous_config)
                    raise
                conv.model, conv.model_id = model, config.model
                conv.build_fingerprint = fingerprint
                conv.pending_model_config = None
                if self._conversations.get(conversation_id) is not conv:
                    self._conversations[conversation_id] = conv
                    conv.subscribers.update(self._orphan_queues.pop(conversation_id, set()))
                self._broadcast(conv, {"type": "model_changed", "model": model, "available": True})
                return {"ok": True, "model": model, "available": True}
        except BaseException:
            if cold:
                pending = self._resume_tasks.get(conversation_id)
                if pending is not None:
                    results = await asyncio.gather(pending, return_exceptions=True)
                    if conv is None and isinstance(results[0], _Conversation):
                        conv = results[0]
                if conv is not None and self._conversations.get(conversation_id) is not conv:
                    await self._dispose_conversation_runtime(conv)
            raise

    async def _initial_model(self, model: str | None) -> str:
        """
        为新建会话解析一次默认模型

        参数:
        - model: 显式名称, None 时读取整体默认

        返回:
        - 已验证存在的配置名, 不修改任何已有会话
        """
        selected = model if model is not None else (await self.get_settings())["default_model"]
        self._require_model(selected)
        return selected

    # ---------- 模型配置管理 ----------

    def list_models_detail(self) -> dict[str, Any]:
        """
        列出全部 LLM 配置 (api_key 脱敏)

        返回:
        - dict[str, Any]: 列出全部 LLM 配置 (api_key 脱敏)
        """
        return {"ok": True, "models": self._model_cfg.list_llm_configs(mask_api_key=True)}

    def add_model(self, config: dict[str, Any]) -> dict[str, Any]:
        """
        新增 LLM 配置

        参数:
        - config: 配置信息

        返回:
        - dict[str, Any]: 新增 LLM 配置
        """
        name = str(config.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "缺少 name"}
        existing = self._model_cfg.list_llm_configs()
        if name in existing:
            return {"ok": False, "error": f"配置已存在: {name}"}
        try:
            thinking_levels = validate_thinking_levels(config.get("thinking_levels"))
            cfg = LLMConfig(
                name=name,
                model=config.get("model"),
                base_url=config.get("base_url"),
                api_key=config.get("api_key"),
                temperature=config.get("temperature"),
                top_p=config.get("top_p"),
                max_tokens=config.get("max_tokens"),
                network_timeout=config.get("network_timeout", 60.0),
                context_window=config.get("context_window"),
                history_ratio=config.get("history_ratio"),
                context_strategy=config.get("context_strategy") or "sliding",
                context_threshold=config.get("context_threshold", 0.8),
                truncation_floor=config.get("truncation_floor", 0.4),
                summary_keep_recent_turns=config.get("summary_keep_recent_turns", 6),
                lock_api_key=bool(config.get("lock_api_key")),
                allow_insecure_base_url=bool(config.get("allow_insecure_base_url")),
                thinking_field_name=config.get("thinking_field_name"),
                thinking_fields=config.get("thinking_fields"),
                supports_visual_input=config.get("supports_visual_input", False),
                supports_native_video_input=config.get("supports_native_video_input", False),
                video_frame_interval=config.get("video_frame_interval", 2.0),
                thinking_levels=thinking_levels,
                omit_none_thinking_fields=bool(config.get("omit_none_thinking_fields")),
            )
            resolve_context_policy(cfg)
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        self._model_cfg.set_llm_config(cfg, name)
        refreshed = self._refresh_chat_model_runtimes(name, cfg)
        return {"ok": True, **refreshed}

    def update_model(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """
        更新 LLM 配置

        参数:
        - name: 名称
        - config: 配置信息

        返回:
        - dict[str, Any]: 更新 LLM 配置
        """
        existing = self._model_cfg.list_llm_configs()
        if name not in existing:
            return {"ok": False, "error": f"配置不存在: {name}"}
        # 过滤合法字段
        allowed = {
            "model",
            "base_url",
            "api_key",
            "temperature",
            "top_p",
            "max_tokens",
            "network_timeout",
            "context_window",
            "history_ratio",
            "context_strategy",
            "context_threshold",
            "truncation_floor",
            "summary_keep_recent_turns",
            "lock_api_key",
            "allow_insecure_base_url",
            "thinking_field_name",
            "thinking_fields",
            "supports_visual_input", "supports_native_video_input", "video_frame_interval",
            "thinking_levels",
            "omit_none_thinking_fields",
        }
        kwargs = {k: v for k, v in config.items() if k in allowed}
        if "thinking_levels" in kwargs:
            try:
                kwargs["thinking_levels"] = validate_thinking_levels(kwargs["thinking_levels"])
            except ValueError as e:
                return {"ok": False, "error": str(e)}
        # 过滤脱敏 api_key (含 * 的值是掩码, 不是真实 key)
        if "api_key" in kwargs and isinstance(kwargs["api_key"], str) and "*" in kwargs["api_key"]:
            del kwargs["api_key"]
        if not kwargs:
            return {"ok": False, "error": "没有可更新的字段"}
        try:
            self._model_cfg.update_named_config("llm", name, kwargs)
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        refreshed = self._refresh_chat_model_runtimes(name, self._model_cfg.get_llm_config(name))
        return {"ok": True, **refreshed}

    def delete_model(self, name: str) -> dict[str, Any]:
        """
        删除 LLM 配置

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 删除 LLM 配置
        """
        if not self._model_cfg.remove_llm_config(name):
            return {"ok": False, "error": f"配置不存在: {name}"}
        return {"ok": True}

    def _apply_chat_model_runtime(self, conv: _Conversation, config: LLMConfig) -> None:
        """
        刷新一个 Chat 会话的 LLM 和上下文策略

        参数:
        - conv: Chat 会话运行时
        - config: 最新 LLM 配置
        """
        llm = build_llm(config)
        if conv.temperature is not None:
            llm.set_parameters(temperature=conv.temperature)
        conv.session.reload_llm(llm)
        conv.session.apply_context_config(config)
        conv.model_id = config.model
        conv.build_fingerprint = self._runtime_fingerprint(
            conv.model,
            system_prompt=conv.system_prompt,
            project_id=conv.project_id,
            temperature=conv.temperature,
        )

    def _refresh_chat_model_runtimes(self, model: str, config: LLMConfig) -> dict[str, int]:
        """
        热更新使用指定模型的 Chat 会话, 正在生成的会话延迟到本轮结束

        参数:
        - model: 模型配置名
        - config: 最新 LLM 配置

        返回:
        - dict[str, int]: 立即刷新和延迟刷新的会话数量
        """
        refreshed = 0
        deferred = 0
        for conv in self._conversations.values():
            if conv.model != model:
                continue
            if conv.task is not None and not conv.task.done():
                conv.pending_model_config = config
                deferred += 1
                continue
            self._apply_chat_model_runtime(conv, config)
            refreshed += 1
        return {"refreshed_conversations": refreshed, "deferred_conversations": deferred}

    def _apply_pending_model_refresh(self, conv: _Conversation) -> None:
        """
        在当前生成结束后应用等待中的模型配置

        参数:
        - conv: Chat 会话运行时
        """
        config = conv.pending_model_config
        if config is None:
            return
        conv.pending_model_config = None
        self._apply_chat_model_runtime(conv, config)
        logger.info(f"[聊天] 会话模型配置已在本轮结束后刷新: {conv.conversation_id}")

    def _runtime_fingerprint(
        self,
        model: str,
        *,
        system_prompt: str | None,
        project_id: str | None,
        temperature: float | None,
    ) -> str:
        """
        计算会话运行时构建指纹

        参数:
        - model: 模型配置名
        - system_prompt: 系统提示词
        - project_id: 项目 ID
        - temperature: Chat 会话级温度覆盖

        返回:
        - str: 模型完整配置, 插件状态和会话设置的稳定哈希
        """
        cfg = self._require_model(model)
        plugin_specs = [
            spec
            for spec in self._plugins.resolve_specs(PluginConfigManager())
            if spec.enabled
        ]
        payload: dict[str, Any] = {
            "model": model,
            "model_config": asdict(cfg),
            "temperature": temperature,
            "system_prompt": system_prompt or "",
            "project_id": project_id or "",
            "plugins": plugin_specs_fingerprint(plugin_specs),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @_shared_runtime_creation
    async def _create_conversation_runtime(
        self,
        conversation_id: str,
        model: str,
        *,
        system_prompt: str | None,
        think: str,
        project_id: str | None,
        temperature: float | None,
        persisted: bool,
        build_fingerprint: str,
        default_agent_prompt_enabled: bool = False,
        preload_lock: asyncio.Lock | None = None,
        subscribers: set[_SubscriberQueue] | None = None,
    ) -> _Conversation:
        """
        构造并注册一个 Chat 会话运行时

        参数:
        - conversation_id: 预分配的会话 ID
        - model: 模型配置名
        - system_prompt: 系统提示词
        - think: 默认思考等级
        - project_id: 项目 ID
        - temperature: Chat 会话级温度覆盖
        - persisted: 是否立即保存正式会话元数据
        - build_fingerprint: 当前运行时构建指纹
        - default_agent_prompt_enabled: 是否补充内置 Agent 提示, 默认 False
        - preload_lock: 重建时复用的预加载锁
        - subscribers: 重建时继承的 WebSocket 订阅者

        返回:
        - _Conversation: 已完成插件和主工作流初始化的运行时
        """
        project: dict[str, Any] | None = None
        if project_id:
            project = db_get_project(project_id, db_path=self._display_db_path)
            if project is None:
                raise ValueError(f"项目不存在: {project_id}")
        cfg = self._model_cfg.get_llm_config(model)
        llm = build_llm(cfg)
        if temperature is not None:
            llm.set_parameters(temperature=temperature)

        recorder = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)

        async def on_content(delta: str) -> None:
            """
            在事件循环内记录并广播内容增量

            参数:
            - delta: 增量内容
            """
            recorder.on_content(delta)
            self._broadcast(conv, {"type": MSG_CONTENT, "delta": delta})

        async def on_thinking(delta: str) -> None:
            """
            在事件循环内记录并广播思考增量

            参数:
            - delta: 增量内容
            """
            recorder.on_thinking(delta)
            self._broadcast(conv, {"type": MSG_THINKING, "delta": delta})

        session_kwargs: dict[str, Any] = {
            "stream": True,
            "return_thinking": True,
            "content_callback": on_content,
            "thinking_callback": on_thinking,
            "enable_checkpoint": False,
            "recoverable": True,
            "default_agent_prompt_enabled": default_agent_prompt_enabled,
        }
        if system_prompt:
            session_kwargs["system_prompt"] = system_prompt
        if self._chat_db_path:
            session_kwargs["db_path"] = self._chat_db_path

        session = AsyncSimpleSession(conversation_id, llm, plugin_environment=PluginEnvironment("chat"), **session_kwargs)
        from satrap.core.log.debug.contracts import DebugObject, bind_debug_object

        bind_debug_object(session, DebugObject(self._platform_id, "local", "chat", conversation_id))
        session.apply_context_config(cfg)
        self._apply_project(session, project)
        # 会话存储与项目工作区绑定需在插件安装前完成

        conv = _Conversation(
            conversation_id=conversation_id,
            session=session,
            recorder=recorder,
            model=model,
            model_id=cfg.model,
            default_think=think,
            project_id=project_id,
            system_prompt=system_prompt or None,
            temperature=temperature,
            persisted=persisted,
            build_fingerprint=build_fingerprint,
            preload_lock=preload_lock or asyncio.Lock(),
            subscribers=subscribers or set(),
        )
        async def user_input_provider(question: str, options: list[str] | None = None) -> str:
            """把插件用户询问桥接到 Chat 前端"""
            return await self._request_user_input(conv, question, options)

        session.user_input_provider = user_input_provider

        async def approval_provider(request: ApprovalRequest) -> ApprovalResult:
            """
            将会话审批交给当前 Chat 的绑定回填通道

            参数:
            - request: 本轮精确动作

            返回:
            - 只对原请求有效的人工决定
            """
            return await self._request_approval(conv, request)

        session.approval_provider = approval_provider

        def on_tool_start(event: dict[str, Any]) -> None:
            """
            同步记录并广播工具开始事件

            参数:
            - event: 事件
            """
            recorder.on_tool_start(event)
            self._broadcast(conv, {"type": MSG_TOOL_START, **event})

        def on_tool_end(event: dict[str, Any]) -> None:
            """
            同步记录并广播工具结束事件

            参数:
            - event: 事件
            """
            recorder.on_tool_end(event)
            self._broadcast(conv, {"type": MSG_TOOL_END, **event})

        try:
            conv.plugin_states = await self._install_enabled_plugins(session)
            # 安装已启用的插件 (扫描到且 json 标记 enabled)

            await session.initialize()
            # 确保主工作流已初始化 (无插件时 install_plugin 不会触发 initialize)

            session.tools_manager.tool_call_start = on_tool_start
            # 工具钩子需在插件安装后挂 (确保挂到主工作流 tools_manager)
            session.tools_manager.tool_call_end = on_tool_end
        except BaseException:
            if self._conversations.get(conversation_id) is conv:
                self._conversations.pop(conversation_id, None)
            recorder.close()
            self._storage.purge_session(self._platform_id, conversation_id)
            raise

        self._conversations[conversation_id] = conv
        orphans = self._orphan_queues.pop(conversation_id, None)
        if orphans:
            conv.subscribers.update(orphans)
        if persisted:
            recorder.save_meta(model, think, project_id=project_id)
            logger.info(f"[聊天] 会话已创建: {conversation_id} (model={model})")
        else:
            conv.preload_expiry_task = asyncio.create_task(self._expire_preloaded(conversation_id, conv))
            logger.info(f"[聊天] 会话预加载完成: {conversation_id} (model={model})")
        return conv

    async def create_conversation(
        self,
        model: str | None = None,
        *,
        system_prompt: str | None = None,
        think: str = "off",
        project_id: str | None = None,
        default_agent_prompt_enabled: bool = False,
    ) -> str:
        """
        创建并立即持久化正式会话

        参数:
        - model: 模型名称, None 时采用整体默认
        - system_prompt: 系统提示词
        - think: 思考模式
        - project_id: 项目 ID
        - default_agent_prompt_enabled: 是否补充内置 Agent 提示, 默认 False

        返回:
        - str: 会话 ID
        """
        model = await self._initial_model(model)
        conversation_id = uuid.uuid4().hex
        fingerprint = self._runtime_fingerprint(
            model,
            system_prompt=system_prompt,
            project_id=project_id,
            temperature=None,
        )
        await self._create_conversation_runtime(
            conversation_id,
            model,
            system_prompt=system_prompt,
            think=think,
            project_id=project_id,
            temperature=None,
            persisted=True,
            build_fingerprint=fingerprint,
            default_agent_prompt_enabled=default_agent_prompt_enabled,
        )
        if default_agent_prompt_enabled:
            await self.set_default_agent_prompt(conversation_id, default_agent_prompt_enabled)
        return conversation_id

    async def preload_conversation(
        self,
        model: str | None = None,
        *,
        system_prompt: str | None = None,
        think: str = "off",
        project_id: str | None = None,
        temperature: float | None = None,
        conversation_id: str | None = None,
        default_agent_prompt_enabled: bool = False,
    ) -> str:
        """
        预分配 ID 并在内存完成 Session, 插件和工作流初始化

        参数:
        - model: 模型名称, None 时采用整体默认
        - system_prompt: 系统提示词
        - think: 默认思考等级
        - project_id: 项目 ID
        - temperature: Chat 会话级温度覆盖
        - conversation_id: 后端重启或超时后的指定 ID 恢复
        - default_agent_prompt_enabled: 是否补充内置 Agent 提示, 默认 False

        返回:
        - str: 预加载会话 ID
        """
        existing = self._conversations.get(conversation_id or "")
        model = existing.model if model is None and existing else await self._initial_model(model)
        target_id = conversation_id or uuid.uuid4().hex
        fingerprint = self._runtime_fingerprint(
            model,
            system_prompt=system_prompt,
            project_id=project_id,
            temperature=temperature,
        )
        await self._create_conversation_runtime(
            target_id,
            model,
            system_prompt=system_prompt,
            think=think,
            project_id=project_id,
            temperature=temperature,
            persisted=False,
            build_fingerprint=fingerprint,
            default_agent_prompt_enabled=default_agent_prompt_enabled,
        )
        return target_id

    async def _expire_preloaded(self, conversation_id: str, expected: _Conversation) -> None:
        """
        超时释放未发送的预加载会话

        参数:
        - conversation_id: 会话 ID
        - expected: 创建清理任务时对应的运行时对象
        """
        try:
            await asyncio.sleep(self._preload_ttl_seconds)
            async with expected.preload_lock:
                current = self._conversations.get(conversation_id)
                if current is None or current is not expected:
                    return
                if current.persisted:
                    return
                if current.subscribers:
                    current.preload_expiry_task = asyncio.create_task(
                        self._expire_preloaded(conversation_id, current)
                    )
                    return
                self._conversations.pop(conversation_id, None)
                await self._dispose_conversation_runtime(current)
                delete_session_domain_rows(self._chat_db_path, conversation_id)
                self._storage.purge_session(self._platform_id, conversation_id)
                logger.info(f"[聊天] 已释放超时预加载会话: {conversation_id}")
        except asyncio.CancelledError:
            return

    async def _dispose_conversation_runtime(self, conv: _Conversation) -> None:
        """
        释放会话运行时持有的任务, 插件和 recorder

        参数:
        - conv: 会话运行时
        """
        current_task = asyncio.current_task()
        if conv.preload_expiry_task is not None and conv.preload_expiry_task is not current_task:
            conv.preload_expiry_task.cancel()
        if conv.task is not None and not conv.task.done() and conv.task is not current_task:
            conv.task.cancel()
        for pending in list(conv.pending_user_inputs.values()):
            if not pending.future.done():
                pending.future.cancel()
        async with conv.plugin_lock:
            for plugin in list(conv.session.list_plugins()):
                try:
                    await conv.session.uninstall_plugin(plugin.name)
                except Exception as e:
                    logger.warning(f"[聊天] 会话 {conv.conversation_id} 卸载插件 {plugin.name} 失败: {e}")
        conv.recorder.close()

    async def _install_enabled_plugins(
        self,
        session: AsyncSimpleSession,
    ) -> list[PluginRuntimeState]:
        """
        对会话安装当前目标插件规格并建立共享运行状态

        参数:
        - session: 会话

        返回:
        - list[PluginRuntimeState]: 插件运行状态
        """
        states: list[PluginRuntimeState] = []
        result = await reconcile_plugin_states_async(
            states,
            resolve_runtime_specs(session, self._plugins.resolve_specs(PluginConfigManager()), self._plugins.catalog),
            session.install_plugin,
            session.uninstall_plugin,
        )
        for item in result["plugins"]:
            if item["status"] == "applied":
                logger.info(f"[聊天] 会话插件已同步: {item['plugin']}")
            else:
                logger.error(
                    f"[聊天] 会话插件同步失败 {item['plugin']}: {item.get('error', item['status'])}"
                )
        return states

    def get_conversation(self, conversation_id: str) -> _Conversation | None:
        """
        取会话运行时状态

        参数:
        - conversation_id: 会话 ID

        返回:
        - _Conversation | None: 取会话运行时状态
        """
        return self._conversations.get(conversation_id)

    def list_turns(self, conversation_id: str, *, limit: int | None = None, before: int | None = None, latest: bool = False, all_variants: bool = True) -> list[dict[str, Any]]:
        """
        列出会话对话轮次 (经 recorder)

        参数:
        - conversation_id: 会话 ID
        - limit: 可选读取数量上限
        - before: 可选轮次游标
        - latest: 是否优先读取最新轮次
        - all_variants: 是否读取全部回答变体

        返回:
        - list[dict[str, Any]]: 列出会话对话轮次 (经 recorder)
        """
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            return conv.recorder.list_turns(limit=limit, before=before, latest=latest, all_variants=all_variants)
        # 会话不在内存 (如重启后): 用只读 recorder 查库
        rec = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)
        try:
            return rec.list_turns(limit=limit, before=before, latest=latest, all_variants=all_variants)
        finally:
            rec.close()

    def open_output_file(self, conversation_id: str, file_id: str) -> tuple[BinaryIO, FileArtifact, FileLease]:
        """
        为鉴权下载打开当前会话已登记附件, 租约保护下载期间的目录

        参数:
        - conversation_id: 下载目标会话
        - file_id: 本会话回复版本中的不透明文件 ID

        返回:
        - 已校验二进制流, 文件资料和必须释放的维护租约; 未登记时拒绝
        """
        root = self._storage.session_artifacts(self._platform_id, conversation_id)
        lease = artifact_lease(root)
        recorder: DisplayRecorder | None = None
        try:
            recorder = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)
            metadata = recorder.find_output_file(file_id)
            if metadata is None:
                raise FileNotFoundError("文件不存在或不属于当前会话")
            artifact = artifact_from_metadata(root, metadata)
            return artifact.open(), artifact, lease
        except BaseException:
            lease.close()
            raise
        finally:
            if recorder is not None:
                recorder.close()

    def query_history(
        self,
        *,
        search: str = "",
        project_id: str | None = None,
        model: str = "",
        turn_count: str = "all",
        older_than_days: float | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """
        查询历史并合并当前 Chat 运行态

        参数:
        - search: 标题或会话 ID 搜索文本
        - project_id: 项目 ID 或 `__none__`
        - model: 模型配置名称
        - turn_count: 轮数过滤
        - older_than_days: 最后使用时间过滤
        - page: 页码
        - page_size: 每页数量

        返回:
        - dict[str, Any]: 历史分页、统计和热管理状态
        """
        result = query_conversations(
            self._display_db_path,
            search=search,
            project_id=project_id,
            model=model,
            turn_count=turn_count,
            older_than_days=older_than_days,
            page=page,
            page_size=page_size,
        )
        for item in cast(list[dict[str, Any]], result["items"]):
            conversation_id = str(item["conversation_id"])
            runtime = self._conversations.get(conversation_id)
            item["active"] = runtime is not None
            item["generating"] = bool(
                runtime is not None
                and runtime.task is not None
                and not runtime.task.done()
            )
            item["waiting_user"] = bool(
                runtime is not None and runtime.pending_user_inputs
            )
        result.update(StorageMaintenanceService(self._storage).session_size_snapshot(self._platform_id))
        result["mode"] = "hot"
        return result

    def list_history_archives(self) -> dict[str, Any]:
        """返回 Chat 会话回收站内容"""
        items = StorageMaintenanceService(self._storage).list_archives(self._platform_id)
        return {
            "items": items,
            "total": len(items),
            "storage_size_bytes": sum(int(item["size_bytes"]) for item in items),
            "mode": "hot",
        }

    def _all_history_items(self, **filters: Any) -> list[dict[str, Any]]:
        """
        按过滤条件读取全部历史项

        参数:
        - filters: query_conversations 支持的过滤字段

        返回:
        - list[dict[str, Any]]: 全部匹配项
        """
        page = 1
        results: list[dict[str, Any]] = []
        while True:
            batch = query_conversations(
                self._display_db_path,
                **filters,
                page=page,
                page_size=200,
            )
            results.extend(cast(list[dict[str, Any]], batch["items"]))
            if len(results) >= int(batch["total"]):
                return results
            page += 1

    async def delete_conversations(
        self,
        *,
        mode: str = "selected",
        conversation_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        批量回收 Chat 历史会话

        参数:
        - mode: selected、empty、single 或 filtered
        - conversation_ids: selected 模式的会话 ID
        - filters: filtered 模式的查询条件
        - force: 是否取消正在运行的会话后继续回收

        返回:
        - dict[str, Any]: 逐会话结果与汇总
        """
        if mode == "selected":
            targets = list(dict.fromkeys(
                item.strip() for item in conversation_ids or [] if item.strip()
            ))
            if not targets:
                raise ValueError("至少选择一个会话")
        elif mode in {"empty", "single"}:
            targets = [
                str(item["conversation_id"])
                for item in self._all_history_items(turn_count=mode)
            ]
        elif mode == "filtered":
            targets = [
                str(item["conversation_id"])
                for item in self._all_history_items(**dict(filters or {}))
            ]
        else:
            raise ValueError(f"未知批量删除模式: {mode}")
        results: list[dict[str, Any]] = []
        for conversation_id in targets:
            try:
                result = await self.delete_conversation(conversation_id, force=force)
                results.append(result)
            except Exception as error:
                results.append({
                    "ok": False,
                    "conversation_id": conversation_id,
                    "error": str(error),
                })
        return {
            "ok": all(item.get("ok", False) for item in results),
            "deleted_count": sum(1 for item in results if item.get("ok", False)),
            "results": results,
        }

    def restore_history_archive(self, archive_id: str) -> dict[str, Any]:
        """
        恢复一个 Chat 历史回收包

        参数:
        - archive_id: 回收包 ID

        返回:
        - dict[str, Any]: 恢复结果
        """
        archives = StorageMaintenanceService(self._storage).list_archives(self._platform_id)
        target = next((item for item in archives if item["archive_id"] == archive_id), None)
        if target is not None and str(target["session_id"]) in self._conversations:
            raise ValueError("同 ID 会话仍在运行, 无法恢复")
        return StorageMaintenanceService(self._storage).restore_archive(
            self._platform_id,
            archive_id,
            database_path=self._display_db_path,
        )

    def purge_history_archive(self, archive_id: str) -> dict[str, Any]:
        """
        永久删除一个 Chat 历史回收包

        参数:
        - archive_id: 回收包 ID

        返回:
        - dict[str, Any]: 删除结果
        """
        return {
            "ok": StorageMaintenanceService(self._storage).purge_archive(
                self._platform_id,
                archive_id,
            ),
            "archive_id": archive_id,
        }

    # ---------- 会话恢复 ----------

    async def _resume_conversation(self, conversation_id: str, *, model_override: str | None = None, publish: bool = True) -> _Conversation | None:
        """
        共享初始化任务, 只发布已经完成初始化的运行时

        参数:
        - conversation_id: 待恢复的会话 ID
        - model_override: 显式重选时使用的目标模型, 默认沿用持久绑定
        - publish: 是否立即发布新运行时, 模型切换时延后到绑定保存成功

        返回:
        - 已完成初始化的会话, 不存在时为 None; 模型不可用时向调用方抛错
        """
        existing = self._conversations.get(conversation_id)
        if existing is not None:
            return existing
        creating = self._creation_tasks.get(conversation_id)
        if creating is not None:
            return await asyncio.shield(creating)
        pending = self._resume_tasks.get(conversation_id)
        if pending is None:
            pending = asyncio.create_task(self._load_conversation(conversation_id, model_override=model_override, publish=publish))
            self._resume_tasks[conversation_id] = pending

            def finished(task: asyncio.Task[_Conversation | None]) -> None:
                if self._resume_tasks.get(conversation_id) is task:
                    self._resume_tasks.pop(conversation_id)
                if not task.cancelled():
                    task.exception()   # 调用者取消等待后仍收集初始化失败

            pending.add_done_callback(finished)
        return await asyncio.shield(pending)

    async def _load_conversation(self, conversation_id: str, *, model_override: str | None = None, publish: bool = True) -> _Conversation | None:
        """
        从 display.db 恢复会话运行时状态 (懒加载)

        参数:
        - conversation_id: 会话 ID
        - model_override: 仅显式重选模型时使用, 允许修复已删除的旧绑定, 此处不保存元数据
        - publish: 默认完成初始化后发布, False 时交由模型切换完成持久化后发布

        返回 None 表示会话在 db 中也不存在

        返回:
        - _Conversation | None: 从 display.db 恢复会话运行时状态 (懒加载)
        """
        meta = get_conversation_meta(conversation_id, db_path=self._display_db_path)
        project: dict[str, Any] | None = None
        if meta is not None:
            model = meta.get("model", "default")
            default_think = str(meta.get("think") or "off")
            project_id = meta.get("project_id") or None
            if project_id:
                project = db_get_project(str(project_id), db_path=self._display_db_path)
                if project is None:
                    logger.warning(f"[聊天] 会话 {conversation_id} 绑定的项目 {project_id} 已不存在, 按无项目恢复")
        else:
            # meta 不存在 (旧会话无 meta 记录): 查 display_turns 确认会话存在, 用 default model
            probe = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)
            try:
                turns = probe.list_turns(limit=1)
            finally:
                probe.close()
            if not turns:
                return None
            model = "default"
            default_think = "off"
            logger.info(f"[聊天] 会话 {conversation_id} 无 meta 记录, 从 display_turns 恢复 (model=default)")
        logger.info(f"[聊天] 恢复会话: {conversation_id} (model={model})")
        # 用 create_conversation 重建, 但保留原 conversation_id
        model = model_override if model_override is not None else model
        cfg = self._require_model(model)
        llm = build_llm(cfg)
        recorder = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)

        async def on_content(delta: str) -> None:
            recorder.on_content(delta)
            self._broadcast(conv, {"type": MSG_CONTENT, "delta": delta})

        async def on_thinking(delta: str) -> None:
            recorder.on_thinking(delta)
            self._broadcast(conv, {"type": MSG_THINKING, "delta": delta})

        session_kwargs: dict[str, Any] = {
            "stream": True,
            "return_thinking": True,
            "content_callback": on_content,
            "thinking_callback": on_thinking,
            "enable_checkpoint": False,
            "recoverable": True,
        }
        if self._chat_db_path:
            session_kwargs["db_path"] = self._chat_db_path

        session = AsyncSimpleSession(conversation_id, llm, plugin_environment=PluginEnvironment("chat"), **session_kwargs)
        from satrap.core.log.debug.contracts import DebugObject, bind_debug_object

        bind_debug_object(session, DebugObject(self._platform_id, "local", "chat", conversation_id))
        session.apply_context_config(cfg)
        self._apply_project(session, project)
        conv = _Conversation(
            conversation_id=conversation_id,
            session=session,
            recorder=recorder,
            model=model,
            model_id=cfg.model,
            default_think=default_think,
            project_id=str(project["project_id"]) if project is not None else None,
        )
        async def user_input_provider(question: str, options: list[str] | None = None) -> str:
            """把插件用户询问桥接到 Chat 前端"""
            return await self._request_user_input(conv, question, options)

        session.user_input_provider = user_input_provider

        async def approval_provider(request: ApprovalRequest) -> ApprovalResult:
            """
            将会话审批交给当前 Chat 的绑定回填通道

            参数:
            - request: 本轮精确动作

            返回:
            - 只对原请求有效的人工决定
            """
            return await self._request_approval(conv, request)

        session.approval_provider = approval_provider

        def on_tool_start(event: dict[str, Any]) -> None:
            recorder.on_tool_start(event)
            self._broadcast(conv, {"type": MSG_TOOL_START, **event})

        def on_tool_end(event: dict[str, Any]) -> None:
            recorder.on_tool_end(event)
            self._broadcast(conv, {"type": MSG_TOOL_END, **event})

        try:
            conv.plugin_states = await self._install_enabled_plugins(session)
            await session.initialize()
        except BaseException:
            await self._dispose_conversation_runtime(conv)
            raise
        session.tools_manager.tool_call_start = on_tool_start
        session.tools_manager.tool_call_end = on_tool_end

        if publish:
            self._conversations[conversation_id] = conv
            orphans = self._orphan_queues.pop(conversation_id, None)
            if orphans:
                conv.subscribers.update(orphans)
                logger.info(f"[聊天] 会话 {conversation_id} 恢复后挂入 {len(orphans)} 个孤儿订阅")
        return conv

    # ---------- 发送 ----------

    async def _activate_preloaded(
        self,
        conv: _Conversation,
        *,
        think: str | None,
        settings: dict[str, Any],
    ) -> _Conversation:
        """
        校验预加载配置并在首次发送前转为正式会话

        参数:
        - conv: 当前预加载运行时
        - think: 首次发送使用的思考等级
        - settings: 前端首次发送时的最新设置, 可用 approval_mode 显式指定初始审批模式

        返回:
        - 已使用最新配置并持久化的会话; 初始审批模式无效或权限插件不可用时抛出 ValueError
        """
        lock = conv.preload_lock
        async with lock:
            current = self._conversations.get(conv.conversation_id)
            runtime_missing = current is None
            if current is not None:
                conv = current
            if conv.persisted:
                return conv

            approval_mode = settings.get("approval_mode")
            if "approval_mode" in settings:
                from satrap.core.approval.contracts import APPROVAL_MODES
                if not isinstance(approval_mode, str) or approval_mode not in APPROVAL_MODES:
                    raise ValueError("审批模式必须是 user, auto 或 full-access")

            model = str(settings.get("model") or conv.model)
            prompt_enabled = settings.get("default_agent_prompt_enabled", conv.session.default_agent_prompt_enabled)
            if type(prompt_enabled) is not bool:
                raise ValueError("default_agent_prompt_enabled 必须是布尔值")
            system_prompt = (
                str(settings.get("system_prompt") or "") or None
                if "system_prompt" in settings
                else conv.system_prompt
            )
            project_id = (
                str(settings.get("project_id") or "") or None
                if "project_id" in settings
                else conv.project_id
            )
            temperature_raw = settings.get("temperature", conv.temperature)
            temperature = float(temperature_raw) if temperature_raw is not None else None
            effective_think = think if think is not None else conv.default_think
            fingerprint = self._runtime_fingerprint(
                model,
                system_prompt=system_prompt,
                project_id=project_id,
                temperature=temperature,
            )

            if runtime_missing or fingerprint != conv.build_fingerprint:
                logger.info(f"[聊天] 预加载运行时缺失或配置已变化, 重新加载会话: {conv.conversation_id}")
                subscribers = conv.subscribers
                if not runtime_missing:
                    if conv.preload_expiry_task is not None:
                        conv.preload_expiry_task.cancel()
                    self._conversations.pop(conv.conversation_id, None)
                    await self._dispose_conversation_runtime(conv)
                try:
                    conv = await self._create_conversation_runtime(
                        conv.conversation_id,
                        model,
                        system_prompt=system_prompt,
                        think=effective_think,
                        project_id=project_id,
                        temperature=temperature,
                        persisted=False,
                        build_fingerprint=fingerprint,
                        default_agent_prompt_enabled=prompt_enabled,
                        preload_lock=lock,
                        subscribers=subscribers,
                    )
                except Exception:
                    self._storage.purge_session(self._platform_id, conv.conversation_id)
                    raise

            if "default_agent_prompt_enabled" in settings or prompt_enabled:
                self._save_default_agent_prompt(conv, prompt_enabled)

            if "command_history_enabled" in settings:
                enabled = settings["command_history_enabled"]
                if type(enabled) is not bool:
                    raise ValueError("command_history_enabled 必须是布尔值")
                store = conv.session.plugin_override_store
                if store is None:
                    raise ValueError("会话覆盖存储尚未就绪")
                current = store.read(conv.conversation_id, "session.commands")
                store.replace(conv.conversation_id, "session.commands", {"command_history_enabled": enabled}, expected_revision=current["revision"])
                conv.session.command_history_enabled = enabled

            if "max_iterations" in settings:
                value = settings["max_iterations"]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError("max_iterations 必须是正整数")
                store = conv.session.plugin_override_store
                if store is None:
                    raise ValueError("会话覆盖存储尚未就绪")
                current = store.read(conv.conversation_id, "session.engine")
                store.replace(conv.conversation_id, "session.engine", {"max_iterations": value}, expected_revision=current["revision"])
                conv.session.default_max_iterations = value

            if approval_mode is not None:
                from satrap.core.approval.state import session_approval
                async with conv.plugin_lock:
                    plugin = next((item for item in conv.session.list_plugins() if item.name == "permissions"), None)
                    if plugin is None or not plugin.enabled:
                        raise ValueError("请先启用 permissions 插件, 再选择初始审批模式")
                    session_approval(conv.session).set_mode(approval_mode)
                # 在会话持久化和首轮任务启动前应用显式选择, 重建预加载运行时后也必须生效

            conv.default_think = effective_think
            conv.recorder.save_meta(conv.model, effective_think, project_id=conv.project_id)
            conv.persisted = True
            if conv.preload_expiry_task is not None:
                conv.preload_expiry_task.cancel()
                conv.preload_expiry_task = None
            logger.info(f"[聊天] 预加载会话已转为正式会话: {conv.conversation_id}")
            return conv

    @_exclusive_conversation_operation
    async def send(
        self,
        conversation_id: str,
        text: str,
        *,
        think: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        preload_settings: dict[str, Any] | None = None,
        expected_model: str | None = None,
    ) -> dict[str, Any]:
        """
        发送消息: 立即返回, run 在后台 task 执行, 流式经 WS 推送

        参数:
        - conversation_id: 会话 ID
        - text: 待处理文本
        - think: 思考模式
        - attachments: 附件列表
        - preload_settings: 首次发送时用于校验预加载运行时的最新设置
        - expected_model: 前端已确认的会话绑定, 不一致时先要求同步而不生成

        同一会话不支持并发 send (上一个 run 未完成时拒绝)
        think 为 None 时使用会话默认 (conversation_meta)

        返回:
        - dict[str, Any]: , run 在后台 task 执行, 流式经 WS 推送
        """
        if not text.strip() and not attachments:
            return {"ok": False, "error": "消息不能为空"}

        settings = preload_settings or {}
        conv = self._conversations.get(conversation_id)
        if conv is None:
            try:
                conv = await self._resume_conversation(conversation_id)
            except ValueError as error:
                return {"ok": False, "error": str(error)}
        if conv is None and settings:
            try:
                await self.preload_conversation(
                    model=str(settings["model"]) if settings.get("model") is not None else None,
                    system_prompt=str(settings.get("system_prompt") or "") or None,
                    think=think or "off",
                    project_id=str(settings.get("project_id") or "") or None,
                    temperature=(
                        float(settings["temperature"])
                        if settings.get("temperature") is not None
                        else None
                    ),
                    conversation_id=conversation_id,
                )
                conv = self._conversations.get(conversation_id)
            except Exception as e:
                logger.error(f"[聊天] 会话 {conversation_id} 重新预加载失败: {e}")
                return {"ok": False, "error": f"重新预加载会话失败: {e}"}
        if conv is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        if not conv.persisted:
            try:
                conv = await self._activate_preloaded(conv, think=think, settings=settings)
            except Exception as e:
                logger.error(f"[聊天] 会话 {conversation_id} 更新预加载失败: {e}")
                return {"ok": False, "error": f"更新预加载会话失败: {e}"}
        if conv.task is not None and not conv.task.done():
            return {"ok": False, "error": "上一轮仍在进行, 请等待完成"}

        try:
            self._require_model(conv.model)
            if expected_model is not None and expected_model != conv.model:
                return {"ok": False, "model": conv.model, "error": "会话模型已变更, 请同步当前选择后重试"}
            img_urls, video_urls = self._attachment_media(conv, attachments)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        effective_think = think if think is not None else conv.default_think

        display_text = text
        if attachments:
            att_lines = [f"[附件: {a.get('name', 'file')}]" for a in attachments]
            display_text = "\n".join(att_lines) + "\n" + text if text.strip() else "\n".join(att_lines)

        turn = conv.recorder.start_turn(text, attachments=attachments)
        from satrap.core.telemetry.capture import bind_scope, record_message
        from satrap.core.telemetry.contracts import MetricScope

        with bind_scope(service="chat"):
            record_message(MetricScope(platform_id=self._platform_id, self_id="local", conversation_kind="chat", target_id=conversation_id),
                           f"input:{turn['turn_id']}", command=conv.session.command_handler._is_cmd(text))
        context_start = conv.session.ctx.static_message()
        self._broadcast(
            conv,
            {
                "type": "turn_start",
                "user_input": text,
                "attachments": attachments,
                **turn,
            },
        )
        conv.task = asyncio.ensure_future(
            self._run_turn(
                conv,
                display_text,
                effective_think,
                img_urls=img_urls, video_urls=video_urls,
                context_start=context_start,
            )
        )
        return {"ok": True, "conversation_id": conversation_id, **turn}

    async def _run_turn(
        self, conv: _Conversation, text: str, think: str, *, img_urls: list[str] | None = None,
        video_urls: list[str] | None = None, context_start: int, resume_run_id: str | None = None,
        model_metadata: dict[str, object] | None = None,
    ) -> None:
        """
        为实际 Chat 执行创建独立诊断请求

        参数:
        - conv: 实际持久会话
        - text: 本轮输入
        - think: 思考设置
        - img_urls: 输入图片
        - video_urls: 输入视频
        - context_start: 上下文起点
        - resume_run_id: 可选恢复任务
        - model_metadata: 已完成任务回放资料
        """
        from satrap.core.log.debug.capture import debug_request, record_debug, previous_chat_request
        from satrap.core.log.debug.contracts import DebugObject
        from satrap.core.telemetry.capture import bind_scope
        from satrap.core.telemetry.contracts import MetricScope

        obj = DebugObject(self._platform_id, "local", "chat", conv.conversation_id)
        version = conv.recorder.recovery_origin()
        with bind_scope(MetricScope(platform_id=self._platform_id, self_id="local", conversation_kind="chat", target_id=conv.conversation_id,
                                    session_id=conv.conversation_id, context_id=conv.conversation_id, model_config=conv.model), service="chat"), \
             debug_request(obj, "chat-" + uuid.uuid4().hex,
                           context_id=conv.conversation_id,
                           input_data={"text": text, "images": img_urls, "videos": video_urls}) as trace:
            if trace is not None:
                try:
                    trace.parent_request_id = await asyncio.to_thread(previous_chat_request, obj, conv.conversation_id, version, resume_run_id)
                except Exception:
                    trace.incomplete = True
            record_debug("chat", "start", metadata={"resume_run_id": resume_run_id,
                         "reply_version": version})
            await self._run_turn_traced(conv, text, think, img_urls=img_urls, video_urls=video_urls,
                                        context_start=context_start, resume_run_id=resume_run_id, model_metadata=model_metadata)

    async def _run_turn_traced(
        self,
        conv: _Conversation,
        text: str,
        think: str,
        *,
        img_urls: list[str] | None = None,
        video_urls: list[str] | None = None,
        context_start: int,
        resume_run_id: str | None = None,
        model_metadata: dict[str, object] | None = None,
    ) -> None:
        """
        后台执行一轮 run, 结束回填 recorder + 广播完成

        参数:
        - conv: 会话对象
        - text: 待处理文本
        - think: 思考模式
        - img_urls: 图片 URL 列表
        - video_urls: 视频来源列表, 默认 None
        - context_start: 运行前模型上下文消息数
        - resume_run_id: 已校验恢复指纹的任务 ID, 默认创建新执行
        - model_metadata: 已完成任务回放时的旧模型资料, 默认取本轮实际客户端
        """
        async with conv.plugin_lock:
            response_model: dict[str, object] = model_metadata if model_metadata is not None else {
                "model": conv.model, "model_id": getattr(conv.session.llm, "model", conv.model_id)}
            try:
                await self._refresh_session_plugins(conv)
                conv.session.recovery_origin = conv.recorder.recovery_origin()
                file_owner = conv.recorder.recovery_origin()

                async def receive_file(artifact: FileArtifact) -> dict[str, object]:
                    """
                    将受控附件登记到原始回复版本

                    参数:
                    - artifact: 已冻结文件

                    返回:
                    - available 表示可以通过鉴权接口下载
                    """
                    conv.recorder.add_output_file(artifact.public(), file_owner)
                    return {"ok": True, "status": "available", "file": artifact.public()}

                with bind_file_receiver(receive_file):
                    result = (await conv.session.resume_run(resume_run_id) if resume_run_id is not None
                              else await conv.session.run(text, img_urls=img_urls, video_urls=video_urls, thinking=think))
                answer = result.message if isinstance(result, CommandAction) else result
                from satrap.core.log.debug.capture import record_debug

                record_debug("reply", "end", status="completed", bodies={"output": answer})
                context_messages = copy.deepcopy(conv.session.ctx.get_context()[context_start:])
                context_stats = conv.session.get_context_stats()
                completed = conv.recorder.end_turn(
                    answer,
                    context_messages=context_messages,
                    context_stats=context_stats,
                    response_metadata={"status": "completed", **response_model},
                ) or {}
                from satrap.core.telemetry.capture import current_scope, record_message, record_session_completed

                record_message(current_scope(), f"output:{completed.get('turn_id')}:{completed.get('variant_index')}", outbound=True)
                record_session_completed()
                self._broadcast(
                    conv,
                    {"type": MSG_TURN_DONE, "answer": answer, "context_stats": context_stats, **completed},
                )
                record_debug("chat_delivery", "end", status="queued", metadata={"client_acknowledged": False, "context_stats": context_stats})
            except asyncio.CancelledError:
                from satrap.core.log.debug.capture import current_trace, record_debug

                debug = current_trace()
                if debug is not None:
                    debug.status = "cancelled"
                record_debug("reply", "end", status="cancelled")
                raise
            except Exception as e:
                info = model_error_info(e)
                from satrap.core.log.debug.capture import current_trace, record_debug

                debug = current_trace()
                if debug is not None:
                    debug.status = "timeout" if info.code.endswith("timeout") else "failed"
                record_debug("reply", "end", status="failed", metadata={"code": info.code, "reason": info.message})
                log = logger.error if info.code == "internal_error" else logger.warning
                log(f"[聊天] 会话 {conv.conversation_id} run 失败: {info.text}")
                context_messages = copy.deepcopy(conv.session.ctx.get_context()[context_start:])
                context_stats = conv.session.get_context_stats()
                completed = conv.recorder.end_turn(
                    "",
                    context_messages=context_messages,
                    context_stats=context_stats,
                    response_metadata={"status": "failed", **response_model, "error": asdict(info), "error_text": info.text},
                ) or {}
                self._broadcast(
                    conv,
                    {"type": MSG_ERROR, "error": info.text, "error_info": asdict(info), "context_stats": context_stats, **completed},
                )
            finally:
                self._apply_pending_model_refresh(conv)

    # ---------- 重试与分支 ----------

    async def list_runs(self, conversation_id: str, *, limit: int = 20, cursor: str | None = None, unfinished: bool = False) -> dict[str, Any]:
        """查询会话内任务, 不暴露持久化请求正文"""
        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            return {"ok": False, "error": "会话不存在"}

        return {"ok": True, **store_for_session(conv.session).summaries(limit, cursor, unfinished)}

    @_exclusive_conversation_operation
    async def manage_run(self, conversation_id: str, run_id: str, action: str, step_id: str = "") -> dict[str, Any]:
        """恢复和取消均复用现有会话占用及插件互斥"""

        conv = self._conversations.get(conversation_id) or await self._resume_conversation(conversation_id)
        if conv is None:
            return {"ok": False, "error": "会话不存在"}
        if conv.task is not None and not conv.task.done():
            return {"ok": False, "error": "会话正在执行任务"}
        async with conv.plugin_lock:
            try:
                await conv.session.initialize()
                store = store_for_session(conv.session)
                run = store.get(run_id)
                if action == "abort":
                    await conv.session.abort_run(run_id)
                    return {"ok": True}
                if action == "authorize_retry":
                    await conv.session.retry_uncertain_step(run_id, step_id)
                    return {"ok": True}
                if action != "resume":
                    return {"ok": False, "error": "未知任务操作"}
                await self._refresh_session_plugins(conv)
                prepare_session_recovery(conv.session)
                if run["status"] == "cancelled":
                    return {"ok": False, "error": "任务已取消"}
                if run["status"] == "completed" and run["payload"].get("committed_context_fingerprint") != store.history_signature():
                    return {"ok": False, "error": "任务完成后会话历史已改变, 不能回填旧轮次"}
                workflow = conv.session._wf
                if workflow is None:
                    return {"ok": False, "error": "会话工作流未初始化"}
                if run["status"] != "completed" and (
                    run["context_fingerprint"] != store.history_signature()
                    or run["config_fingerprint"] != configuration(workflow)
                ):
                    return {"ok": False, "error": "历史或执行配置已改变, 请使用重新生成或分支"}
                origin = run["payload"].get("origin", {})
                turn = conv.recorder.resume_turn(int(origin["turn_id"]), int(origin["variant_index"]))
                old_metadata = turn.get("response_metadata") or {}
                replay_model = {key: old_metadata[key] for key in ("model", "model_id") if key in old_metadata} if run["status"] == "completed" else None
                context_start = int(run["payload"].get("context_start", conv.session.ctx.static_message()))
                self._broadcast(conv, {"type": "turn_start", "retry": True, "resumed": True, **turn})
                conv.task = asyncio.ensure_future(self._run_turn(conv, "", conv.default_think,
                                                                context_start=context_start, resume_run_id=run_id, model_metadata=replay_model))
                return {"ok": True, **turn}
            except (ValueError, RuntimeError, KeyError) as exc:
                return {"ok": False, "error": str(exc)}

    @_exclusive_conversation_operation
    async def retry(self, conversation_id: str, think: str | None = None, *, expected_model: str | None = None) -> dict[str, Any]:
        """
        重试最后一轮: 保留旧回复版本, 回退模型上下文后重新生成

        参数:
        - conversation_id: 会话 ID
        - think: 思考模式
        - expected_model: 前端已确认的会话模型, 不匹配时不创建回复变体

        think 缺省 (None) 时回落 send 的会话默认逻辑

        返回:
        - dict[str, Any]: 新回复版本的轮次标识
        """
        conv = self._conversations.get(conversation_id)
        if conv is None:
            try:
                conv = await self._resume_conversation(conversation_id)
            except ValueError as error:
                return {"ok": False, "error": str(error)}
        if conv is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        if conv.task is not None and not conv.task.done():
            return {"ok": False, "error": "上一轮仍在进行, 请等待完成"}
        try:
            self._require_model(conv.model)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        if expected_model is not None and expected_model != conv.model:
            return {"ok": False, "model": conv.model, "error": "会话模型已变更, 请同步当前选择后重试"}
        async with conv.plugin_lock:
            return await self._retry_reserved(conv, conversation_id, think)

    async def _retry_reserved(self, conv: _Conversation, conversation_id: str, think: str | None) -> dict[str, Any]:
        """
        在会话占用和插件锁保护下先校验媒体, 再准备重试

        参数:
        - conv: 当前会话运行时
        - conversation_id: 会话 ID
        - think: 显式思考等级, None 沿用会话默认

        返回:
        - 新回复版本标识或失败信息; 预检失败不修改上下文与变体
        """
        latest = conv.recorder.list_turns(limit=1, latest=True, all_variants=False)
        if not latest:
            return {"ok": False, "error": "没有可重试的轮次"}
        attachments = cast(list[dict[str, Any]] | None, latest[-1].get("attachments"))
        try:
            img_urls, video_urls = self._attachment_media(conv, attachments)
        except ValueError as error:
            logger.warning(f"[聊天重试] 媒体校验失败, 会话={conversation_id}, 原因={error}")
            return {"ok": False, "error": str(error)}
        original_context = copy.deepcopy(conv.session.ctx.get_context())
        retry_turn = conv.recorder.start_retry_variant()
        if retry_turn is None:
            return {"ok": False, "error": "没有可重试的轮次"}
        try:
            previous_context = retry_turn.get("previous_context_messages")
            if previous_context is None:
                all_messages = copy.deepcopy(conv.session.ctx.get_context())
                start_index = next(
                    (
                        index
                        for index in range(len(all_messages) - 1, -1, -1)
                        if all_messages[index].get("role") == "user"
                    ),
                    len(all_messages),
                )
                previous_context = all_messages[start_index:]
                conv.recorder.save_variant_context(
                    int(retry_turn["turn_index"]),
                    int(retry_turn["previous_variant"]),
                    previous_context,
                )
            if previous_context is None or previous_context:
                await conv.session.ctx.del_last_chat(1)
            text = str(retry_turn["user_input"])
            display_text = text
            if attachments:
                att_lines = [f"[附件: {item.get('name', 'file')}]" for item in attachments]
                display_text = "\n".join(att_lines) + "\n" + text if text.strip() else "\n".join(att_lines)
            effective_think = think if think is not None else conv.default_think
            context_start = conv.session.ctx.static_message()
            self._broadcast(
                conv,
                {
                    "type": "turn_start",
                    "user_input": text,
                    "attachments": attachments,
                    "retry": True,
                    "turn_id": retry_turn["turn_id"],
                    "turn_index": retry_turn["turn_index"],
                    "variant_index": retry_turn["variant_index"],
                },
            )
            conv.task = asyncio.ensure_future(
                self._run_turn(
                    conv,
                    display_text,
                    effective_think,
                    img_urls=img_urls, video_urls=video_urls,
                    context_start=context_start,
                )
            )
            return {
                "ok": True,
                "conversation_id": conversation_id,
                "turn_id": retry_turn["turn_id"],
                "turn_index": retry_turn["turn_index"],
                "variant_index": retry_turn["variant_index"],
            }
        except BaseException as e:
            try:
                await conv.session.ctx.replace_messages(original_context)
            finally:
                conv.recorder.abort_active_variant()
            if isinstance(e, asyncio.CancelledError):
                raise
            return {"ok": False, "error": f"回退模型上下文失败: {e}"}

    @_exclusive_conversation_operation
    async def select_variant(
        self,
        conversation_id: str,
        turn_index: int,
        variant_index: int,
    ) -> dict[str, Any]:
        """
        记录用户切换回复版本的实际结果, 不复制来源历史

        参数:
        - conversation_id: 当前持久会话 ID
        - turn_index: 目标展示轮次
        - variant_index: 目标回复版本

        返回:
        - 原有切换结果, 诊断开关不影响业务路径
        """
        from satrap.core.log.debug.capture import debug_request, record_debug
        from satrap.core.log.debug.contracts import DebugObject

        with debug_request(DebugObject(self._platform_id, "local", "chat", conversation_id), "variant-" + uuid.uuid4().hex,
                           context_id=conversation_id, input_data={"turn_index": turn_index, "variant_index": variant_index}) as trace:
            result = await self._select_variant(conversation_id, turn_index, variant_index)
            status = "success" if result.get("ok") else "failed"
            record_debug("context_variant", "end", status=status, metadata={"turn_index": turn_index,
                         "variant_index": variant_index, "reason": result.get("error", "")})
            if trace is not None:
                trace.status = status
            return result

    async def _select_variant(
        self,
        conversation_id: str,
        turn_index: int,
        variant_index: int,
    ) -> dict[str, Any]:
        """
        切换最后一轮的已持久化回复版本并同步模型上下文

        参数:
        - conversation_id: 会话 ID
        - turn_index: 轮次索引
        - variant_index: 回复版本索引

        返回:
        - dict[str, Any]: 切换结果和更新后的展示轮次
        """
        conv = self._conversations.get(conversation_id)
        if conv is None:
            conv = await self._resume_conversation(conversation_id)
        if conv is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        if conv.task is not None and not conv.task.done():
            return {"ok": False, "error": "回复仍在生成, 暂时不能切换版本"}
        last_turn = conv.recorder.last_turn()
        if last_turn is None or int(last_turn["turn_index"]) != turn_index:
            return {"ok": False, "error": "只能切换最后一轮的回复版本, 旧轮次请使用 Fork"}
        current_variant = int(last_turn.get("active_variant", 0))
        if current_variant == variant_index:
            return {"ok": True, "turn": last_turn}
        current_context = conv.recorder.variant_context(turn_index, current_variant)
        target_context = conv.recorder.variant_context(turn_index, variant_index)
        if target_context is None:
            return {"ok": False, "error": "目标回复版本缺少模型上下文, 无法安全切换"}

        async with conv.plugin_lock:
            original_context = copy.deepcopy(conv.session.ctx.get_context())
            try:
                if current_context is None or current_context:
                    await conv.session.ctx.del_last_chat(1)
                if target_context:
                    await conv.session.ctx.add_turn_messages(target_context)
                turn = conv.recorder.activate_variant(turn_index, variant_index)
                if turn is None:
                    raise ValueError("回复版本不存在")
            except BaseException as e:
                await conv.session.ctx.replace_messages(original_context)
                if isinstance(e, asyncio.CancelledError):
                    raise
                return {"ok": False, "error": f"切换模型上下文失败: {e}"}
        self._broadcast(
            conv,
            {
                "type": "variant_selected",
                "turn_index": turn_index,
                "variant_index": variant_index,
            },
        )
        return {"ok": True, "turn": turn}

    @staticmethod
    def _context_text(message: dict[str, Any]) -> str:
        """
        提取模型上下文消息中的文本内容

        参数:
        - message: 模型上下文消息

        返回:
        - str: 可用于匹配展示层用户输入的文本
        """
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in cast(list[Any], content):
                if not isinstance(item, dict):
                    continue
                typed_item = cast(dict[str, Any], item)
                if typed_item.get("type") == "text":
                    parts.append(str(typed_item.get("text") or ""))
            return "\n".join(part for part in parts if part)
        return ""

    def _legacy_fork_context(
        self,
        src: _Conversation,
        turn_index: int,
    ) -> list[dict[str, Any]]:
        """
        从旧会话运行时推断指定轮次之前的模型上下文

        参数:
        - src: 源会话运行时
        - turn_index: 不包含的结束轮次索引

        返回:
        - list[dict[str, Any]]: 去除系统消息后的上下文前缀
        """
        if turn_index <= 0:
            return []
        target = src.recorder.get_turn(turn_index)
        messages = copy.deepcopy(src.session.ctx.get_context())
        target_input = str(target.get("user_input") or "") if target else ""
        user_positions = [
            index for index, message in enumerate(messages) if message.get("role") == "user"
        ]
        boundary: int | None = None
        for index in user_positions:
            text = self._context_text(messages[index])
            if target_input and (text == target_input or text.endswith(target_input)):
                boundary = index
                break
        if boundary is None and turn_index < len(user_positions):
            boundary = user_positions[turn_index]
        if boundary is None:
            return []
        return [
            message for message in messages[:boundary] if message.get("role") != "system"
        ]

    async def fork(self, conversation_id: str, turn_index: int) -> dict[str, Any]:
        """
        关联实际 Fork 操作, 新对象继续按自己的设置决定是否采集

        参数:
        - conversation_id: 已校验的来源会话
        - turn_index: 来源轮次

        返回:
        - 原 Fork 结果, 诊断不复制原链路或对象开关
        """
        from satrap.core.log.debug.capture import debug_request, record_debug
        from satrap.core.log.debug.contracts import DebugObject

        with debug_request(DebugObject(self._platform_id, "local", "chat", conversation_id), "fork-" + uuid.uuid4().hex,
                           context_id=conversation_id, input_data={"action": "fork", "turn_index": turn_index}) as trace:
            result = await self._fork(conversation_id, turn_index)
            if trace is not None:
                trace.status = "success" if result.get("ok") else "failed"
            record_debug("context_fork", "end", status="completed" if result.get("ok") else "failed",
                         metadata={"source_context": conversation_id, "source_turn_index": turn_index, "target_context": result.get("conversation_id")})
            return result

    async def _fork(self, conversation_id: str, turn_index: int) -> dict[str, Any]:
        """
        从指定轮次 fork 新会话 (复制该轮之前的上下文, 继承 model 与默认 think)

        参数:
        - conversation_id: 会话 ID
        - turn_index: turn索引

        返回:
        - dict[str, Any]: 从指定轮次 fork 新会话 (复制该轮之前的上下文, 继承 model 与默认 think)
        """
        src = self._conversations.get(conversation_id)
        if src is None:
            src = await self._resume_conversation(conversation_id)
        if src is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        async with src.plugin_lock:
            new_cid = await self.create_conversation(
                model=src.model,
                think=src.default_think,
                system_prompt=src.system_prompt,
                project_id=src.project_id,
            )
            try:
                new_conv = self._conversations[new_cid]
                await RAG_WORKERS.run(fork_session_settings, self._storage, self._platform_id, conversation_id, new_cid)
                new_conv.session.command_history_enabled = src.session.command_history_enabled
                new_conv.session.default_agent_prompt_enabled = src.session.default_agent_prompt_enabled
                new_conv.session.default_max_iterations = src.session.default_max_iterations
                async with new_conv.plugin_lock:
                    await self._refresh_session_plugins(new_conv)
                context_messages = src.recorder.active_context_before(turn_index)
                copied = src.recorder.copy_turns_to(new_conv.recorder, turn_index)
                source_root = self._storage.session_artifacts(self._platform_id, conversation_id)
                target_root = self._storage.session_artifacts(self._platform_id, new_cid)

                def copy_files() -> None:
                    """在两个会话维护租约内复制实际选中版本的附件"""
                    with artifact_lease(source_root), artifact_lease(target_root):
                        for turn in new_conv.recorder.list_turns():
                            for metadata in turn.get("output_files", []):
                                copy_artifact(artifact_from_metadata(source_root, metadata), target_root)
                await asyncio.to_thread(copy_files)
                if copied > 0 and not context_messages and not src.recorder.has_context_deltas_before(turn_index):
                    context_messages = self._legacy_fork_context(src, turn_index)
                if context_messages:
                    from satrap.core.utils.media_store import referenced_media, store_for
                    store_for(src.session.ctx.db_path, src.session.ctx.conversation_id).copy_to(store_for(new_conv.session.ctx.db_path, new_conv.session.ctx.conversation_id), referenced_media(context_messages))
                    await new_conv.session.ctx.add_turn_messages(context_messages)
            except BaseException:
                await self.delete_conversation(new_cid)
                raise
        logger.info(f"[聊天] fork: {conversation_id} -> {new_cid}, 复制 {copied} 轮")
        return {"ok": True, "conversation_id": new_cid, "copied_turns": copied}

    # ---------- 项目 (工作区文件夹绑定) ----------

    def _apply_project(self, session: AsyncSimpleSession, project: dict[str, Any] | None) -> None:
        """
        通过统一宿主环境绑定项目工作区, 插件在实际操作时读取冻结快照

        参数:
        - session: 会话
        - project: 项目

        无项目时工作区与 sandbox 都限定在会话独占目录;
        有项目时只共享外部工作区, sandbox 与 uploads 仍保持会话隔离
        """
        session.plugin_override_store = SessionOverrideStore(self._storage.platform_db(self._platform_id))
        session.command_history_enabled = session.plugin_override_store.read(session.session_id, "session.commands")["overrides"].get("command_history_enabled", False)
        session.default_agent_prompt_enabled = session.plugin_override_store.read(session.session_id, "session.agent_prompt")["overrides"].get("default_agent_prompt_enabled", session.default_agent_prompt_enabled)
        engine_overrides = session.plugin_override_store.read(session.session_id, "session.engine")["overrides"]
        if type(engine_overrides.get("max_iterations")) is int and engine_overrides["max_iterations"] >= 1:
            session.default_max_iterations = engine_overrides["max_iterations"]
        session.plugin_model_manager = self._model_cfg
        session.storage_layout = self._storage
        session.storage_platform_id = self._platform_id
        project_id = str(project.get("project_id") or "") if project else ""
        scope = StorageScope(
            platform_id=self._platform_id,
            user_id="local",
            session_id=session.session_id,
            project_id=project_id,
        )
        session_root = self._storage.bind_session(scope)
        if project is not None:
            try:
                workspace_root = self._resolve_workspace_directory(str(project["root_path"]))
            except ValueError as error:
                logger.warning(f"[聊天] 项目工作区越出允许根目录, 回落会话沙箱: {error}")
                workspace_root = session_root / "sandbox"
        else:
            workspace_root = session_root / "sandbox"
        bind_environment(session, EnvironmentSnapshot(
            workspace_root=workspace_root,
            sandbox_root=session_root / "sandbox",
            upload_root=session_root / "uploads",
            session_root=session_root,
            artifacts_root=session_root / "artifacts",
            scope=f"{self._platform_id}:{session.session_id}",
            project=project_id,
        ))   # 项目改绑统一递增环境版本, 不改变会话私有目录
        setattr(session, "memory_db", str(self._storage.platform_db(self._platform_id)))
        setattr(session, "memory_scope", f"session:{session.session_id}")

    def create_project(self, name: str, root_path: str) -> dict[str, object]:
        """
        创建项目并把工作区限制在服务端配置的允许根目录

        参数:
        - name: 名称
        - root_path: 根目录路径

        返回:
        - dict[str, object]: 创建结果, 路径越出允许根目录时返回错误
        """
        name = name.strip()
        if not name:
            return {"ok": False, "error": "项目名称不能为空"}
        try:
            p = self._resolve_workspace_directory(root_path)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        project = db_create_project(name, str(p), db_path=self._display_db_path)
        logger.info(f"[聊天] 项目已创建: {name} -> {p}")
        return {"ok": True, "project": project}

    def list_projects(self) -> list[dict[str, Any]]:
        """
        列出全部项目

        返回:
        - list[dict[str, Any]]: 列出全部项目
        """
        return db_list_projects(db_path=self._display_db_path)

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """
        删除项目: 仅解绑其下会话 (归入"最近"), 不动会话数据与磁盘文件

        参数:
        - project_id: 项目 ID

        返回:
        - dict[str, Any]: 删除项目: 仅解绑其下会话 (归入"最近"), 不动会话数据与磁盘文件
        """
        if not db_delete_project(project_id, db_path=self._display_db_path):
            return {"ok": False, "error": f"项目不存在: {project_id}"}
        for conv in list(self._conversations.values()):
            if conv.project_id == project_id:
                conv.project_id = None
                self._apply_project(conv.session, None)
        logger.info(f"[聊天] 项目已删除 (仅解绑会话): {project_id}")
        return {"ok": True}

    def set_conversation_project(self, conversation_id: str, project_id: str | None) -> dict[str, Any]:
        """
        改绑会话所属项目 (None = 移出项目); 仅影响之后的工具调用

        参数:
        - conversation_id: 会话 ID
        - project_id: 项目 ID

        返回:
        - dict[str, Any]: 改绑会话所属项目 (None = 移出项目); 仅影响之后的工具调用
        """
        meta = get_conversation_meta(conversation_id, db_path=self._display_db_path)
        if meta is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        project: dict[str, Any] | None = None
        if project_id:
            project = db_get_project(project_id, db_path=self._display_db_path)
            if project is None:
                return {"ok": False, "error": f"项目不存在: {project_id}"}
        if not db_set_conversation_project(conversation_id, project_id, db_path=self._display_db_path):
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            conv.project_id = project_id or None
            self._apply_project(conv.session, project)
        return {"ok": True}

    def _conversation_project(self, conversation_id: str) -> dict[str, Any] | None:
        """
        查会话所属项目 (内存优先, 未命中查 db)

        参数:
        - conversation_id: 会话 ID

        返回:
        - dict[str, Any] | None: 查会话所属项目 (内存优先, 未命中查 db)
        """
        conv = self._conversations.get(conversation_id)
        pid = conv.project_id if conv is not None else None
        if pid is None:
            meta = get_conversation_meta(conversation_id, db_path=self._display_db_path)
            pid = (meta or {}).get("project_id") or None
        if pid is None:
            return None
        return db_get_project(str(pid), db_path=self._display_db_path)

    def _resolve_workspace_directory(self, path: str) -> Path:
        """
        解析项目目录并限制在配置的工作区根内

        参数:
        - path: 待校验目录路径

        返回:
        - Path: 位于允许根目录内的真实目录
        """
        candidate = Path(path.strip()).expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError(f"路径不存在或不是目录: {path}")
        if self._is_workspace_denied(candidate):
            raise ValueError("路径位于受保护的 .satrap 数据目录内")
        if not any(
            candidate == root or candidate.is_relative_to(root)
            for root in self._workspace_roots
        ):
            raise ValueError(f"路径越出允许的工作区根目录: {path}")
        return candidate

    def _is_workspace_denied(self, path: Path) -> bool:
        """
        判断路径是否位于系统或额外配置的拒绝根内

        参数:
        - path: 已解析的候选路径

        返回:
        - 路径等于拒绝根或位于其下时返回 True
        """
        resolved = path.resolve()
        return any(
            resolved == denied or resolved.is_relative_to(denied)
            for denied in self._denied_workspace_roots
        )

    def browse_directories(self, path: str = "") -> dict[str, object]:
        """
        浏览服务器目录 (只列子目录, 供前端新建项目时选择工作区文件夹)

        参数:
        - path: 路径

        - path 为空: 返回服务端显式配置的工作区根目录
        - 根层级: parent 为空字符串, 返回根目录选择视图
        - 权限不足/异常子目录跳过; 只列目录不列文件, 按名排序 (忽略大小写), 上限 500 条

        返回:
        - dict[str, object]: 目录浏览结果
        """
        if not path.strip():
            roots = [
                {"name": root.name or str(root), "path": str(root)}
                for root in self._workspace_roots
            ]
            return {"ok": True, "path": "", "parent": None, "dirs": roots}
        try:
            p = self._resolve_workspace_directory(path)
        except ValueError as error:
            return {"ok": False, "error": str(error)}

        dirs: list[dict[str, str]] = []
        try:
            children = sorted(p.iterdir(), key=lambda c: c.name.lower())
        except OSError as error:
            logger.warning(f"[项目目录] 浏览失败, 路径={p}, 原因={error}")
            return {"ok": False, "error": f"目录无法读取: {error}"}
        for child in children:
            if len(dirs) >= 500:
                break
            try:
                resolved_child = child.resolve()
                if (
                    resolved_child.is_dir()
                    and not self._is_workspace_denied(resolved_child)
                    and any(
                        resolved_child == root or resolved_child.is_relative_to(root)
                        for root in self._workspace_roots
                    )
                ):
                    dirs.append({"name": child.name, "path": str(resolved_child)})
            except OSError:
                continue

        parent: str | None
        if p in self._workspace_roots:
            parent = ""
        else:
            parent = str(p.parent)
        return {"ok": True, "path": str(p), "parent": parent, "dirs": dirs}

    # ---------- 文件上传 ----------

    def _attachment_media(
        self, conv: _Conversation, attachments: list[dict[str, Any]] | None,
    ) -> tuple[list[str], list[str]]:
        """
        校验 Chat 媒体附件并按类型分组

        参数:
        - conv: 当前会话, 使用实际运行模型检查视觉能力
        - attachments: 已上传附件列表, None 表示无附件

        返回:
        - 图片和视频本地路径列表, 无效类型, 越界路径或未启用视觉时抛出 ValueError
        """

        images: list[str] = []
        videos: list[str] = []
        if attachments is not None and not isinstance(attachments, list):
            raise ValueError("附件必须是列表")
        for attachment in attachments or []:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("url"), str):
                raise ValueError("附件缺少有效文件路径")
            source = attachment["url"]
            declared = str(attachment.get("type", ""))
            detected = mimetypes.guess_type(source)[0] or ""
            kind = detected.split("/", 1)[0]
            if kind not in {"image", "video"}:
                if declared.startswith(("image/", "video/")):
                    raise ValueError("附件媒体类型与文件格式不符")
                continue
            root = self._storage.session_uploads(self._platform_id, conv.conversation_id).resolve()
            path = Path(source).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("媒体附件必须是当前会话已上传的文件")
            from satrap.core.utils.media import sniff_media_mime
            with path.open("rb") as stream:
                actual = sniff_media_mime(stream.read(32), detected)
            kind = actual.split("/", 1)[0]
            if not visual_enabled(conv.session.llm) and not (kind == "video" and getattr(conv.session.llm, "supports_native_video_input", False) is True):
                raise ValueError("当前模型未启用相应的图片或视频输入能力")
            (images if kind == "image" else videos).append(str(path))
            if len(images) + len(videos) > MAX_MEDIA_ITEMS:
                raise ValueError("单次媒体输入不能超过 16 项")
        return images, videos

    def preview_media(self, conversation_id: str, source: str) -> dict[str, str]:
        """
        读取当前会话私有媒体供已鉴权的 Chat 客户端预览

        参数:
        - conversation_id: 媒体所属会话 ID
        - source: 上传接口返回的本地文件路径

        返回:
        - 包含 Data URL 的字典, 越界路径, 非媒体和超限文件抛出 ValueError
        """

        root = self._storage.session_uploads(self._platform_id, conversation_id).resolve()
        path = Path(source).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("媒体附件必须是当前会话已上传的文件")
        mime = mimetypes.guess_type(path.name)[0] or ""
        if not mime.startswith(("image/", "video/")):
            raise ValueError("仅支持预览图像和视频")
        with path.open("rb") as file:
            data = file.read(MAX_MEDIA_BYTES + 1)
        if len(data) > MAX_MEDIA_BYTES:
            raise ValueError("预览文件超过 32 MiB 限制")
        from satrap.core.utils.media import sniff_media_mime
        mime = sniff_media_mime(data[:32], mime)
        return {"data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}

    def save_upload(self, conversation_id: str, file_name: str, file_data: bytes) -> dict[str, Any]:
        """
        保存上传文件到当前会话的私有 uploads 目录

        参数:
        - conversation_id: 会话 ID
        - file_name: 文件名称
        - file_data: 文件数据

        项目会话落在项目工作区内 (read_document 白名单内, 项目内跨会话可读);
        项目绑定不改变上传隔离边界

        返回:
        - dict[str, Any]: 上传结果和私有文件路径
        """
        safe_name = Path(file_name).name   # 防路径穿越
        if not safe_name:
            return {"ok": False, "error": "文件名非法"}
        from satrap.core.utils.media import inspect_media, sniff_media_mime
        from satrap.core.utils.media_store import store_for
        file_type = sniff_media_mime(file_data[:32], mimetypes.guess_type(safe_name)[0] or "application/octet-stream")
        is_media = file_type.startswith(("image/", "video/"))
        limit = MAX_MEDIA_BYTES if is_media else 10 * 1024 * 1024
        if len(file_data) > limit:
            return {"ok": False, "error": f"文件超过 {limit // (1024 * 1024)} MiB 限制"}
        upload_dir = self._storage.session_uploads(self._platform_id, conversation_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        fpath = upload_dir / unique_name
        animated = False
        kind = ""
        if is_media:
            file_type, kind, animated = inspect_media(file_data, file_type)
        if animated or kind == "video":
            conv = self._conversations.get(conversation_id)
            scope = conv.session.ctx.conversation_id if conv is not None else conversation_id + "_main"
            store_for(self._chat_db_path, scope).write_upload(file_data, file_type, fpath)
        else:
            fpath.write_bytes(file_data)

        return {
            "ok": True,
            "file_name": safe_name,
            "file_url": str(fpath),
            "file_type": file_type,
        }

    # ---------- 插件 ----------

    def list_plugins(self) -> list[dict[str, Any]]:
        """
        列出插件清单 (扫描 + 启用状态)

        返回:
        - list[dict[str, Any]]: 列出插件清单 (扫描 + 启用状态)
        """
        return self._plugins.scan()

    async def _reconcile_chat_plugins(self, *, plugin_name: str | None = None, concurrency: int = 4) -> list[dict[str, Any]]:
        """
        使用共享生命周期协调器有界并发同步正式 Chat 会话

        参数:
        - plugin_name: 可选插件引用过滤, 已移除的旧引用仍需卸载
        - concurrency: 最大并发会话数, 默认 4

        返回:
        - list[dict[str, Any]]: 逐会话同步结果
        """
        targets = [(conv, conv.session, conv.persisted,
                    plugin_name is None or any(state.name == plugin_name and (state.enabled or state.handle is not None) for state in conv.plugin_states))
                   for conv in list(self._conversations.values())]
        read_started = time.perf_counter()

        def prepare() -> tuple[PluginApplySnapshot, list[Any]]:
            """
            在线程中一次捕获来源并解析 Chat 规格

            返回:
            - 应用快照与全部目标规格
            """
            manager = PluginConfigManager()
            try:
                for _attempt in range(3):
                    state_before = plugin_source_version(self._plugins.state_path)
                    self._plugins.refresh(strict=True)
                    model_managers = tuple(cast(ModelConfigManager, session.plugin_model_manager) for _conv, session, persisted, _old_reference in targets
                                           if persisted and session.plugin_model_manager is not None)
                    snapshot = PluginApplySnapshot.capture(
                        self._plugins.catalog, manager, additional_sources=(self._plugins.state_path,), model_managers=model_managers,
                        model_plugin_filter=lambda entry: self._plugins.is_enabled(entry.name) and entry.check_environment(PluginEnvironment("chat")).allowed,
                    )
                    specs = self._plugins.resolve_specs(manager, snapshot=snapshot)
                    if state_before == plugin_source_version(self._plugins.state_path) and snapshot.is_current():
                        return snapshot, specs
                raise RuntimeError("Chat 插件状态在捕获期间持续变化, 请重试应用")
            except Exception as error:
                logger.error(f"[聊天] 准备插件应用失败: {error}\n{traceback.format_exc()}")
                raise

        with progress_phase("config_read_ms"):
            snapshot, desired_specs = await asyncio.to_thread(prepare)
        config_read_ms = (time.perf_counter() - read_started) * 1000
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def reconcile_one(conv: _Conversation, planned_session: AsyncSimpleSession) -> dict[str, Any]:
            """
            在原会话操作锁内协调一个 Chat 实例

            参数:
            - conv: 已选中的正式会话
            - planned_session: 来源准备前固定的运行实例

            返回:
            - 成功或失败的独立应用结果
            """
            timings = PhaseTimings()
            lock_started = time.perf_counter()
            lock_wait_ms = 0.0
            try:
                async with progress_locks(semaphore, conv.plugin_lock):
                    lock_wait_ms = (time.perf_counter() - lock_started) * 1000
                    if self._conversations.get(conv.conversation_id) is not conv or conv.session is not planned_session or not conv.persisted:
                        return {"conversation_id": conv.conversation_id, "ok": False, "status": "error", "error": "会话运行时已变化"}
                    if not await progress_source_check(snapshot.is_current):
                        logger.warning(f"[聊天] 会话 {conv.conversation_id} 插件应用快照已过期, 请重试应用")
                        return {"conversation_id": conv.conversation_id, "ok": False, "status": "error",
                                "error": "插件配置在等待期间已变化, 请重试应用",
                                "timings_ms": {**timings.to_payload(), "lock_wait_ms": round(lock_wait_ms, 3), "config_read_ms": round(config_read_ms, 3)}}
                    if self._conversations.get(conv.conversation_id) is not conv or conv.session is not planned_session or not conv.persisted:
                        return {"conversation_id": conv.conversation_id, "ok": False, "status": "error", "error": "会话运行时已变化"}
                    original_models = conv.session.plugin_model_manager
                    if snapshot.has_model_sources:
                        conv.session.plugin_model_manager = snapshot.models_for(cast(ModelConfigManager | None, original_models))
                    try:
                        result = await reconcile_plugin_states_async(
                            conv.plugin_states,
                            resolve_runtime_specs(conv.session, desired_specs, snapshot.catalog, snapshot=snapshot),
                            conv.session.install_plugin,
                            conv.session.uninstall_plugin,
                        )
                    finally:
                        conv.session.plugin_model_manager = original_models
                result['timings_ms'] = {**result.get('timings_ms', {}), **timings.to_payload(),
                                        'lock_wait_ms': round(lock_wait_ms, 3), 'config_read_ms': round(config_read_ms, 3)}
                logger.debug(f"[聊天] 插件协调完成, 会话={conv.conversation_id}, timings_ms={result['timings_ms']}")
                return {
                    "conversation_id": conv.conversation_id,
                    "status": "applied" if result.get("ok", False) else "error",
                    **result,
                }
            except Exception as error:
                logger.error(f"[聊天] 会话 {conv.conversation_id} 插件同步失败: {error}\n{traceback.format_exc()}")
                return {
                    "conversation_id": conv.conversation_id,
                    "ok": False,
                    "status": "error",
                    "error": str(error),
                    "timings_ms": {**timings.to_payload(), 'lock_wait_ms': round(lock_wait_ms, 3),
                                   'config_read_ms': round(config_read_ms, 3)},
                }

        async def observed_chat(conv: _Conversation, planned_session: AsyncSimpleSession) -> dict[str, Any]:
            """Chat 实例的观测范围独立, 原会话操作和失败结果保持不变"""
            with progress_instance(self._platform_id, conv.conversation_id):
                result = await reconcile_one(conv, planned_session)
                progress_result(result)
                return result

        pending = []
        for conv, planned_session, persisted, old_reference in targets:
            if plugin_name is not None and not (
                any(spec.name == plugin_name and spec.enabled for spec in desired_specs)
                or old_reference
            ):
                continue
            if not persisted:
                if self._conversations.get(conv.conversation_id) is conv and conv.session is planned_session and not conv.persisted:
                    conv.build_fingerprint = ""
                continue
            pending.append(observed_chat(conv, planned_session))
        return list(await asyncio.gather(*pending)) if pending else []

    async def reconcile_plugins(self, *, plugin_name: str | None = None) -> dict[str, Any]:
        """
        读取最新冷配置并协调全部或指定插件引用的 Chat 实例

        参数:
        - plugin_name: 可选插件引用过滤

        返回:
        - 配置应用状态及逐会话协调结果
        """
        return self._plugin_update_result(await self._reconcile_chat_plugins(plugin_name=plugin_name))

    async def manage_conversation_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        查找上下文所属对话, 保留现有 Chat 操作互斥规则

        参数:
        - payload: 对话数据读取或编辑参数

        返回:
        - 数据快照, 不激活尚未加载的历史对话
        """

        conversation = payload.get("conversation_id", "")
        layer = str(payload.get("layer", "context"))
        if not isinstance(conversation, str) or not conversation:
            raise ValueError("缺少对话 ID")

        async def read_saved() -> dict[str, Any]:
            """
            只读展示已保存快照, 不占用或绕过正在运行的会话写操作

            返回:
            - 带已保存数据来源提示的快照
            """
            database = self._chat_db_path if layer == "context" else self._display_db_path
            result = await RAG_WORKERS.run(perform_data_operation, database, conversation, layer, payload)
            return {**result, "read_notice": "此对话正在处理消息或激活实例, 当前显示已保存的数据, 可稍后重新读取"}

        if any(conversation == identity or conversation.startswith(identity + "_") for identity in {*self._creation_tasks, *self._resume_tasks}):
            if not payload.get("action"):
                return await read_saved()
            raise ConversationDataConflict("此对话正在激活, 请等待完成后重新读取")
        owner = next((conv.conversation_id for conv in self._conversations.values()
                      if conv.conversation_id == conversation or conversation.startswith(conv.conversation_id + "_") or any(context.conversation_id == conversation for context in conv.session._all_contexts().values())), None)
        result = await self._manage_conversation_data_reserved(owner or conversation, conversation, layer, payload)
        if not payload.get("action") and not result.get("ok"):
            return await read_saved()
        return result

    @_exclusive_conversation_operation
    async def _manage_conversation_data_reserved(self, owner: str, conversation: str, layer: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        在 Chat 对话预留和插件锁内编辑, 提交后通知订阅页面重新同步

        参数:
        - owner: 活动 Chat 对话 ID
        - conversation: 上下文或展示历史 ID
        - layer: 数据层
        - payload: 操作参数

        返回:
        - 最新快照和保存结果
        """

        conv = self._conversations.get(owner)
        self._operations[owner].state = "data_edit"
        if conv is None:
            database = self._chat_db_path if layer == "context" else self._display_db_path
            return await RAG_WORKERS.run(perform_data_operation, database, conversation, layer, payload)
        async with conv.plugin_lock:
            context = next((context for context in conv.session._all_contexts().values() if context.conversation_id == conversation), None)
            database = self._chat_db_path if layer == "context" else self._display_db_path
            result = await RAG_WORKERS.run(perform_data_operation, database, conversation, layer, payload, context)
            if result.get("saved"):
                conv.recorder._next_turn_index = conv.recorder._load_max_turn_index() + 1
                self._broadcast(conv, {"type": "resync_required"})
            return result

    def plugin_runtime_snapshot(self) -> dict[str, Any]:
        """
        读取正式 Chat 会话的插件实际状态, 不触发安装或模型请求

        返回:
        - 逐会话运行快照, 预加载临时会话不计入使用位置
        """
        return {"ok": True, "sessions": [{"conversation_id": conv.conversation_id, "platform_id": "chat", "config_name": "chat",
                                          "plugins": [state.to_payload() for state in conv.plugin_states]}
                                         for conv in self._conversations.values() if conv.persisted]}

    @staticmethod
    def _plugin_update_result(results: list[dict[str, Any]]) -> dict[str, Any]:
        """
        汇总 Chat 插件多会话同步结果

        参数:
        - results: 逐会话同步结果

        返回:
        - dict[str, Any]: 前端可展示的汇总结果
        """
        failures = [item for item in results if not item.get("ok", False)]
        return {
            "ok": True,
            "applied": not failures,
            "sessions": results,
            "failed": len(failures),
        }

    async def set_plugin_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """
        设置插件启用状态, 并对所有活动会话即时 install/uninstall

        参数:
        - name: 名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 设置插件启用状态, 并对所有活动会话即时 install/uninstall
        """
        pdir = self._plugins.get_plugin_dir(name)
        if pdir is None:
            return {"ok": False, "error": f"插件不存在: {name}"}
        self._plugins.set_enabled(name, enabled)
        return self._plugin_update_result(await self._reconcile_chat_plugins())

    async def set_plugin_capability(self, name: str, kind: str, cap: str, enabled: bool) -> dict[str, Any]:
        """
        设置能力独立启用状态并即时同步到活动会话

        参数:
        - name: 名称
        - kind: 类型
        - cap: 能力名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 持久化和逐会话应用结果
        """
        entry = self._plugins.catalog.get(name)
        if entry is None:
            return {"ok": False, "error": f"插件不存在: {name}"}
        if kind not in entry.capabilities:
            return {"ok": False, "error": f"非法能力类别: {kind}"}
        if cap not in entry.capabilities[kind]:
            return {"ok": False, "error": f"插件未声明能力: {name}.{kind}.{cap}"}
        if not self._plugins.set_capability(name, kind, cap, enabled):
            return {"ok": False, "error": f"非法能力类别: {kind}"}
        return self._plugin_update_result(await self._reconcile_chat_plugins())

    # ---------- 插件配置 ----------

    def get_plugin_config(self, name: str) -> dict[str, Any]:
        """
        取插件配置 (schema + 当前全局值)

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 取插件配置 (schema + 当前全局值)
        """
        pdir = self._plugins.get_plugin_dir(name)
        if pdir is None:
            return {"ok": False, "error": f"插件不存在: {name}"}
        meta = load_plugin_meta(pdir)
        schema = parse_config_schema(meta)
        mgr = PluginConfigManager()
        global_cfg = mgr.load_global(name, schema)
        return {
            "ok": True,
            "schema": schema_to_payload(schema),
            "config": global_cfg,
        }

    def session_plugin_config(self, conversation_id: str, name: str) -> dict[str, Any]:
        """读取当前会话的插件覆盖和来源, 不激活未运行的会话"""
        if conversation_id not in self._conversations and not get_conversation_meta(conversation_id, db_path=self._display_db_path):
            raise ValueError("会话不存在")
        entry = self._plugins.catalog.get(name)
        if entry is None:
            raise ValueError("插件不存在")
        service = PluginSettingsService(self._storage.platform_db(self._platform_id))
        tool_settings = {}
        if name == "friend_manager":
            spec = next(item for item in self._plugins.resolve_specs() if item.name == name)
            tool_settings = service.tool_settings(conversation_id, spec, entry)
        return {"ok": True, **service.get(conversation_id, name, entry.config_schema), **tool_settings, "model_options": model_options(self._model_cfg)}

    async def save_session_plugin_config(
        self, conversation_id: str, name: str, values: dict[str, Any], expected_revision: int,
        *, tool_overrides: dict[str, bool] | None = None, expected_tool_revision: int | None = None,
    ) -> dict[str, Any]:
        """保存会话显式参数, 本轮执行中时留到下一轮安全应用"""
        self.session_plugin_config(conversation_id, name)
        entry = self._plugins.catalog.get(name)
        if entry is None:
            raise ValueError("插件不存在")
        validate_model_values(self._model_cfg, entry.config_schema, values)
        service = PluginSettingsService(self._storage.platform_db(self._platform_id), models=self._model_cfg, rag=RagService(self._storage, self._model_cfg, self._platform_id, conversation_id))
        await RAG_WORKERS.run(service.save, conversation_id, name, entry.config_schema, values, expected_revision=expected_revision,
                              tool_overrides=tool_overrides, expected_tool_revision=expected_tool_revision)
        conv = self._conversations.get(conversation_id)
        result = {"status": "next_activation"}
        if conv is not None:
            if conv.task is not None and not conv.task.done():
                result = {"status": "next_turn"}
            else:
                async with conv.plugin_lock:
                    result = await self._refresh_session_plugins(conv)
        return {**self.session_plugin_config(conversation_id, name), "runtime": result}

    async def _refresh_session_plugins(self, conv: _Conversation) -> dict[str, Any]:
        """在会话锁内按最新模型引用和覆盖协调插件"""
        reload_models = getattr(self._model_cfg, "reload", None)
        if callable(reload_models):
            reload_models()
        desired = resolve_runtime_specs(conv.session, self._plugins.resolve_specs(PluginConfigManager()), self._plugins.catalog)
        result = await reconcile_plugin_states_async(conv.plugin_states, desired, conv.session.install_plugin, conv.session.uninstall_plugin)
        return result

    async def save_plugin_config(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """
        保存插件全局配置 (按 schema 校验)

        参数:
        - name: 名称
        - config: 配置信息

        返回:
        - dict[str, Any]: 保存插件全局配置 (按 schema 校验)
        """
        pdir = self._plugins.get_plugin_dir(name)
        if pdir is None:
            return {"ok": False, "error": f"插件不存在: {name}"}
        meta = load_plugin_meta(pdir)
        schema = parse_config_schema(meta)
        mgr = PluginConfigManager()
        try:
            config = validate_config_values(schema, config)
            validate_plugin_settings(name, schema, {**{key: field.default for key, field in schema.items()}, **config}, models=self._model_cfg, rag=RagService(self._storage, self._model_cfg, self._platform_id))
            mgr.save_global(name, schema, config)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return self._plugin_update_result(await self._reconcile_chat_plugins())

    # ---------- 记忆管理 ----------

    def list_memories(self, scope: str) -> dict[str, Any]:
        """
        列出记忆 (按 scope)

        参数:
        - scope: 作用域

        返回:
        - dict[str, Any]: 列出记忆 (按 scope)
        """
        if not scope.startswith("session:") or not scope.removeprefix("session:").strip():
            return {"ok": False, "error": "记忆 scope 必须绑定到具体会话"}
        store = MemoryService(MemoryStore(db_path=self._storage.platform_db(self._platform_id), scope=scope))
        result = store.execute("list_all")
        return result if isinstance(result, dict) else {"ok": True, "memories": result}

    def add_memory(self, title: str, content: str, *, tags: str = "", importance: int = 1, scope: str) -> dict[str, Any]:
        """
        添加记忆 (tags 逗号分隔字符串转 list)

        参数:
        - title: 标题
        - content: 内容
        - tags: 标签集合
        - importance: 重要度
        - scope: 作用域

        返回:
        - dict[str, Any]: 添加记忆 (tags 逗号分隔字符串转 list)
        """
        if not scope.startswith("session:") or not scope.removeprefix("session:").strip():
            return {"ok": False, "error": "记忆 scope 必须绑定到具体会话"}
        store = MemoryService(MemoryStore(db_path=self._storage.platform_db(self._platform_id), scope=scope))
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        record = cast(dict[str, Any], store.execute("add", title=title, content=content, tags=tag_list, importance=importance))
        if not record.get("ok"):
            return {"ok": False, "error": record.get("error", "添加失败")}
        return {"ok": True, "memory": record}

    def update_memory(self, memory_id: str, *, scope: str, **fields: Any) -> dict[str, Any]:
        """
        更新记忆

        参数:
        - memory_id: 记忆 ID
        - scope: 作用域
        - fields: 字段集合

        返回:
        - dict[str, Any]: 更新记忆
        """
        if not scope.startswith("session:") or not scope.removeprefix("session:").strip():
            return {"ok": False, "error": "记忆 scope 必须绑定到具体会话"}
        store = MemoryService(MemoryStore(db_path=self._storage.platform_db(self._platform_id), scope=scope))
        record = cast(dict[str, Any], store.execute("update", memory_id, **fields))
        if not record.get("ok"):
            return {"ok": False, "error": record.get("error", "更新失败")}
        return {"ok": True, "memory": record}

    def delete_memory(self, memory_id: str, *, scope: str) -> dict[str, Any]:
        """
        删除记忆

        参数:
        - memory_id: 记忆 ID
        - scope: 作用域

        返回:
        - dict[str, Any]: 删除记忆
        """
        if not scope.startswith("session:") or not scope.removeprefix("session:").strip():
            return {"ok": False, "error": "记忆 scope 必须绑定到具体会话"}
        store = MemoryService(MemoryStore(db_path=self._storage.platform_db(self._platform_id), scope=scope))
        result = cast(dict[str, Any], store.execute("delete", memory_id))
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "删除失败")}
        return {"ok": True}
    # 管理接口是管理员工具, 恒定使用 MemoryStore 默认的 full 模式
    # 不受插件配置 memory_mode 约束; memory_mode 只控制模型工具与注入

    # ---------- 删除会话 ----------

    async def delete_conversation(
        self,
        conversation_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        删除会话: 清运行时状态, 级联删除平台库记录并回收私有目录

        参数:
        - conversation_id: 会话 ID
        - force: 是否取消正在执行的任务后继续回收

        返回:
        - dict[str, Any]: 删除结果
        """
        conv = self._conversations.get(conversation_id)
        if (
            conversation_id in self._operations
            or (conv is not None and conv.task is not None and not conv.task.done())
        ):
            if not force:
                raise ValueError("会话正在生成, 请停止生成或确认强制删除")
            await self.cancel(conversation_id)
        conv = self._conversations.pop(conversation_id, None)
        was_preloaded = conv is not None and not conv.persisted
        if conv is not None:
            await self._dispose_conversation_runtime(conv)
        if was_preloaded:
            self._storage.purge_session(self._platform_id, conversation_id)
            delete_session_domain_rows(self._chat_db_path, conversation_id)
            return {
                "ok": True,
                "conversation_id": conversation_id,
                "preloaded": True,
            }
        archived = await RAG_WORKERS.run(
            StorageMaintenanceService(self._storage).archive_session,
            self._platform_id,
            conversation_id,
            database_path=self._display_db_path,
        )
        if Path(self._chat_db_path).resolve() != Path(self._display_db_path).resolve():
            delete_session_domain_rows(self._chat_db_path, conversation_id)
        logger.info(f"[聊天] 会话已删除: {conversation_id}")
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "archive_id": archived["archive_id"],
        }


    # ---------- 取消 ----------

    async def cancel(self, conversation_id: str) -> dict[str, Any]:
        """
        取消当前正在执行的 run task

        参数:
        - conversation_id: 会话 ID

        返回:
        - dict[str, Any]: 取消当前正在执行的 run task
        """
        conv = self._conversations.get(conversation_id)
        operation = self._operations.get(conversation_id)
        task = operation.task if operation else (conv.task if conv else None)
        if task is None or task.done():
            return {"ok": False, "error": "没有正在进行的任务"}
        if operation and operation.state == "cancelling":
            return {"ok": False, "error": "任务正在取消"}
        was_running = operation is None or operation.state == "running"
        if operation:
            operation.state = "cancelling"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if operation and self._operations.get(conversation_id) is operation:
                self._operations.pop(conversation_id)
        if conv and was_running:
            completed = conv.recorder.end_turn("", response_metadata={"model": conv.model, "model_id": conv.model_id}) or {}
            self._broadcast(conv, {"type": MSG_TURN_DONE, "answer": "", **completed})
        logger.info(f"[聊天] 会话 {conversation_id} 已取消")
        return {"ok": True}

    # ---------- 关闭 ----------

    async def close(self) -> None:
        """关闭所有会话运行时并清理未持久化目录"""
        pending: list[asyncio.Task[Any]] = [*self._resume_tasks.values(), *self._creation_tasks.values()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        conversations = list(self._conversations.values())
        self._conversations.clear()
        for conv in conversations:
            await self._dispose_conversation_runtime(conv)
            if not conv.persisted:
                delete_session_domain_rows(self._chat_db_path, conv.conversation_id)
                self._storage.purge_session(self._platform_id, conv.conversation_id)
