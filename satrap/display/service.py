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

import asyncio
import copy
import hashlib
import json
import os
import string
import time
import uuid
from dataclasses import asdict, dataclass, field, replace as dataclass_replace
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from satrap.core.APICall.LLMCall import AsyncLLM, build_llm_from_config
from satrap.core.utils.context_policy import resolve_context_policy
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.log import logger
from satrap.core.storage import (
    CHAT_PLATFORM_ID,
    StorageLayout,
    StorageMaintenanceService,
    StorageScope,
    default_storage_layout,
    delete_session_domain_rows,
)
from satrap.core.type import CommandAction, LLMConfig, validate_thinking_levels
from satrap.display.plugins import ChatPluginRegistry
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
from satrap.edictum import AsyncSimpleSession
from satrap.edictum.plugin import load_plugin_meta
from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema, schema_to_payload
from satrap.edictum.plugin_runtime import (
    PluginRuntimeState,
    reconcile_plugin_states_async,
)
from satrap.edictum.plugin_spec import plugin_specs_fingerprint
from satrap.expend.tools.memory_store import MemoryStore

MSG_THINKING = "thinking_delta"
# WS 广播消息类型
MSG_CONTENT = "content_delta"
MSG_TOOL_START = "tool_start"
MSG_TOOL_END = "tool_end"
MSG_ASK_USER = "ask_user"
MSG_ASK_USER_END = "ask_user_end"
MSG_TURN_DONE = "turn_done"
MSG_ERROR = "error"


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
    future: asyncio.Future[str]


@dataclass
class _Conversation:
    """单个会话的运行时状态"""

    conversation_id: str
    session: AsyncSimpleSession
    recorder: DisplayRecorder
    model: str
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
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(
        default_factory=lambda: set()
    )
    """WS 订阅者队列集合"""
    pending_user_inputs: dict[str, _PendingUserInput] = field(
        default_factory=lambda: dict[str, _PendingUserInput]()
    )
    """等待前端回答的 ask_user 请求"""


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
        """
        self._model_cfg = model_config
        self._plugins = plugins
        self._storage = storage_layout or default_storage_layout
        self._platform_id = platform_id.strip() or CHAT_PLATFORM_ID
        self._storage.ensure_platform(self._platform_id)
        platform_db = str(self._storage.platform_db(self._platform_id))
        self._chat_db_path = chat_db_path or platform_db
        self._display_db_path = display_db_path or platform_db
        self._preload_ttl_seconds = max(float(preload_ttl_seconds), 1.0)
        self._ask_user_timeout_seconds = max(float(ask_user_timeout_seconds), 1.0)
        self._conversations: dict[str, _Conversation] = {}
        self._orphan_queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        """订阅时会话不在内存的孤儿队列, _resume_conversation 完成后挂入"""

    # ---------- 广播 ----------

    def _broadcast(self, conv: _Conversation, msg: dict[str, Any]) -> None:
        """
        向会话所有 WS 订阅者广播 (put_nowait 非阻塞; 队列满则丢弃最旧由消费者保证)

        参数:
        - conv: 会话对象
        - msg: 消息对象
        """
        msg = {**msg, "conversation_id": conv.conversation_id, "ts": time.time()}
        for q in list(conv.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning(f"[聊天] 会话 {conv.conversation_id} 订阅者队列满, 丢弃消息")

    def subscribe(self, conversation_id: str) -> asyncio.Queue[dict[str, Any]]:
        """
        订阅会话广播, 返回队列 (WS 处理器持有, 断开时 unsubscribe)

        参数:
        - conversation_id: 会话 ID

        会话不在内存时登记为孤儿队列, _resume_conversation 完成后自动挂入

        返回:
        - asyncio.Queue[dict[str, Any]]: 队列 (WS 处理器持有, 断开时 unsubscribe)
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            conv.subscribers.add(q)
            for pending in conv.pending_user_inputs.values():
                q.put_nowait({
                    "type": MSG_ASK_USER,
                    "conversation_id": conversation_id,
                    "request_id": pending.request_id,
                    "question": pending.question,
                    "ts": time.time(),
                })
        else:
            self._orphan_queues.setdefault(conversation_id, set()).add(q)
        return q

    async def _request_user_input(self, conv: _Conversation, question: str) -> str:
        """
        向 Chat 前端发出问题并等待回答

        参数:
        - conv: 会话运行时
        - question: 问题内容

        返回:
        - str: 用户回答
        """
        request_id = uuid.uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = _PendingUserInput(request_id=request_id, question=question, future=future)
        conv.pending_user_inputs[request_id] = pending
        self._broadcast(conv, {
            "type": MSG_ASK_USER,
            "request_id": request_id,
            "question": question,
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
        pending.future.set_result(answer)
        self._broadcast(conv, {
            "type": MSG_ASK_USER_END,
            "request_id": request_id,
            "status": "answered",
        })
        logger.info(f"[聊天] 会话 {conversation_id} 已收到用户回答: {request_id}")
        return {"ok": True}

    def unsubscribe(self, conversation_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
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
                context_window=config.get("context_window"),
                history_ratio=config.get("history_ratio"),
                context_strategy=config.get("context_strategy") or "sliding",
                context_threshold=config.get("context_threshold", 0.8),
                truncation_floor=config.get("truncation_floor", 0.4),
                summary_keep_recent_turns=config.get("summary_keep_recent_turns", 6),
                lock_api_key=bool(config.get("lock_api_key")),
                thinking_field_name=config.get("thinking_field_name"),
                thinking_fields=config.get("thinking_fields"),
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
            "context_window",
            "history_ratio",
            "context_strategy",
            "context_threshold",
            "truncation_floor",
            "summary_keep_recent_turns",
            "lock_api_key",
            "thinking_field_name",
            "thinking_fields",
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
            current = self._model_cfg.get_llm_config(name)
            candidate = dataclass_replace(current, **kwargs)
            resolve_context_policy(candidate)
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        self._model_cfg.update_llm_config(name, **kwargs)
        refreshed = self._refresh_chat_model_runtimes(name, candidate)
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
        cfg = self._model_cfg.get_llm_config(model)
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
        preload_lock: asyncio.Lock | None = None,
        subscribers: set[asyncio.Queue[dict[str, Any]]] | None = None,
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
        }
        if system_prompt:
            session_kwargs["system_prompt"] = system_prompt
        if self._chat_db_path:
            session_kwargs["db_path"] = self._chat_db_path

        session = AsyncSimpleSession(conversation_id, llm, **session_kwargs)
        session.apply_context_config(cfg)
        self._apply_project(session, project)
        # 会话存储与项目工作区绑定需在插件安装前完成

        conv = _Conversation(
            conversation_id=conversation_id,
            session=session,
            recorder=recorder,
            model=model,
            default_think=think,
            project_id=project_id,
            system_prompt=system_prompt or None,
            temperature=temperature,
            persisted=persisted,
            build_fingerprint=build_fingerprint,
            preload_lock=preload_lock or asyncio.Lock(),
            subscribers=subscribers or set(),
        )
        self._conversations[conversation_id] = conv

        async def user_input_provider(question: str) -> str:
            """把插件用户询问桥接到 Chat 前端"""
            return await self._request_user_input(conv, question)

        session.user_input_provider = user_input_provider

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
        except Exception:
            if self._conversations.get(conversation_id) is conv:
                self._conversations.pop(conversation_id, None)
            recorder.close()
            self._storage.purge_session(self._platform_id, conversation_id)
            raise

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
        model: str = "default",
        *,
        system_prompt: str | None = None,
        think: str = "off",
        project_id: str | None = None,
    ) -> str:
        """
        创建并立即持久化正式会话

        参数:
        - model: 模型名称
        - system_prompt: 系统提示词
        - think: 思考模式
        - project_id: 项目 ID

        返回:
        - str: 会话 ID
        """
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
        )
        return conversation_id

    async def preload_conversation(
        self,
        model: str = "default",
        *,
        system_prompt: str | None = None,
        think: str = "off",
        project_id: str | None = None,
        temperature: float | None = None,
        conversation_id: str | None = None,
    ) -> str:
        """
        预分配 ID 并在内存完成 Session, 插件和工作流初始化

        参数:
        - model: 模型名称
        - system_prompt: 系统提示词
        - think: 默认思考等级
        - project_id: 项目 ID
        - temperature: Chat 会话级温度覆盖
        - conversation_id: 后端重启或超时后的指定 ID 恢复

        返回:
        - str: 预加载会话 ID
        """
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
            self._plugins.resolve_specs(PluginConfigManager()),
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
        self._coordinate_sandbox(session)
        return states

    def _coordinate_sandbox(self, session: AsyncSimpleSession) -> None:
        """
        沙箱协调: coding 启用时停用 base_take 的 code_sandbox (避免模型困惑)

        参数:
        - session: 会话

        两插件共享当前会话的私有 sandbox, coding 的 shell 能力更强,
        因此 coding 在场时 base_take 的 code_sandbox 工具禁用
        """
        coding_on = self._plugins.is_enabled("satrap_coding")
        base_on = self._plugins.is_enabled("base_take")
        if coding_on and base_on:
            if session.tools_manager.disable_tool("code_sandbox"):
                logger.info("[聊天] coding 插件在场, 已停用 base_take 的 code_sandbox")
        elif base_on and self._plugins.capability_enabled("base_take", "tools", "code_sandbox"):
            session.tools_manager.enable_tool("code_sandbox")

    def get_conversation(self, conversation_id: str) -> _Conversation | None:
        """
        取会话运行时状态

        参数:
        - conversation_id: 会话 ID

        返回:
        - _Conversation | None: 取会话运行时状态
        """
        return self._conversations.get(conversation_id)

    def list_turns(self, conversation_id: str) -> list[dict[str, Any]]:
        """
        列出会话对话轮次 (经 recorder)

        参数:
        - conversation_id: 会话 ID

        返回:
        - list[dict[str, Any]]: 列出会话对话轮次 (经 recorder)
        """
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            return conv.recorder.list_turns()
        # 会话不在内存 (如重启后): 用只读 recorder 查库
        rec = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)
        try:
            return rec.list_turns()
        finally:
            rec.close()

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
        sessions_root = self._storage.platform_root(self._platform_id) / "sessions"
        result["storage_size_bytes"] = StorageMaintenanceService._directory_size(sessions_root)
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

    async def _resume_conversation(self, conversation_id: str) -> _Conversation | None:
        """
        从 display.db 恢复会话运行时状态 (懒加载)

        参数:
        - conversation_id: 会话 ID

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
        cfg = self._model_cfg.get_llm_config(model)
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
        }
        if self._chat_db_path:
            session_kwargs["db_path"] = self._chat_db_path

        session = AsyncSimpleSession(conversation_id, llm, **session_kwargs)
        session.apply_context_config(cfg)
        self._apply_project(session, project)
        conv = _Conversation(
            conversation_id=conversation_id,
            session=session,
            recorder=recorder,
            model=model,
            default_think=default_think,
            project_id=str(project["project_id"]) if project is not None else None,
        )
        self._conversations[conversation_id] = conv

        async def user_input_provider(question: str) -> str:
            """把插件用户询问桥接到 Chat 前端"""
            return await self._request_user_input(conv, question)

        session.user_input_provider = user_input_provider

        def on_tool_start(event: dict[str, Any]) -> None:
            recorder.on_tool_start(event)
            self._broadcast(conv, {"type": MSG_TOOL_START, **event})

        def on_tool_end(event: dict[str, Any]) -> None:
            recorder.on_tool_end(event)
            self._broadcast(conv, {"type": MSG_TOOL_END, **event})

        conv.plugin_states = await self._install_enabled_plugins(session)
        await session.initialize()
        session.tools_manager.tool_call_start = on_tool_start
        session.tools_manager.tool_call_end = on_tool_end

        orphans = self._orphan_queues.pop(conversation_id, None)
        # 挂入订阅时会话不在内存的孤儿队列
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
        - settings: 前端首次发送时的最新会话设置

        返回:
        - _Conversation: 已使用最新配置并完成元数据持久化的会话
        """
        lock = conv.preload_lock
        async with lock:
            current = self._conversations.get(conv.conversation_id)
            runtime_missing = current is None
            if current is not None:
                conv = current
            if conv.persisted:
                return conv

            model = str(settings.get("model") or conv.model)
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
                        preload_lock=lock,
                        subscribers=subscribers,
                    )
                except Exception:
                    self._storage.purge_session(self._platform_id, conv.conversation_id)
                    raise

            conv.default_think = effective_think
            conv.recorder.save_meta(conv.model, effective_think, project_id=conv.project_id)
            conv.persisted = True
            if conv.preload_expiry_task is not None:
                conv.preload_expiry_task.cancel()
                conv.preload_expiry_task = None
            logger.info(f"[聊天] 预加载会话已转为正式会话: {conv.conversation_id}")
            return conv

    async def send(
        self,
        conversation_id: str,
        text: str,
        *,
        think: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        preload_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        发送消息: 立即返回, run 在后台 task 执行, 流式经 WS 推送

        参数:
        - conversation_id: 会话 ID
        - text: 待处理文本
        - think: 思考模式
        - attachments: 附件列表
        - preload_settings: 首次发送时用于校验预加载运行时的最新设置

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
            conv = await self._resume_conversation(conversation_id)
        if conv is None and settings:
            try:
                await self.preload_conversation(
                    model=str(settings.get("model") or "default"),
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

        effective_think = think if think is not None else conv.default_think

        display_text = text
        # 附件拼入文本前缀 + 图片传 img_urls
        img_urls: list[str] | None = None
        if attachments:
            att_lines = [f"[附件: {a.get('name', 'file')}]" for a in attachments]
            display_text = "\n".join(att_lines) + "\n" + text if text.strip() else "\n".join(att_lines)
            img_urls = [a["url"] for a in attachments if a.get("type", "").startswith("image")]

        turn = conv.recorder.start_turn(text, attachments=attachments)
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
                img_urls=img_urls,
                context_start=context_start,
            )
        )
        return {"ok": True, "conversation_id": conversation_id, **turn}

    async def _run_turn(
        self,
        conv: _Conversation,
        text: str,
        think: str,
        *,
        img_urls: list[str] | None = None,
        context_start: int,
    ) -> None:
        """
        后台执行一轮 run, 结束回填 recorder + 广播完成

        参数:
        - conv: 会话对象
        - text: 待处理文本
        - think: 思考模式
        - img_urls: 图片 URL 列表
        - context_start: 运行前模型上下文消息数
        """
        async with conv.plugin_lock:
            try:
                result = await conv.session.run(text, img_urls=img_urls, thinking=think)
                answer = result.message if isinstance(result, CommandAction) else result
                context_messages = copy.deepcopy(conv.session.ctx.get_context()[context_start:])
                context_stats = conv.session.get_context_stats()
                completed = conv.recorder.end_turn(
                    answer,
                    context_messages=context_messages,
                    context_stats=context_stats,
                ) or {}
                self._broadcast(
                    conv,
                    {"type": MSG_TURN_DONE, "answer": answer, "context_stats": context_stats, **completed},
                )
            except Exception as e:
                logger.error(f"[聊天] 会话 {conv.conversation_id} run 失败: {e}")
                context_messages = copy.deepcopy(conv.session.ctx.get_context()[context_start:])
                context_stats = conv.session.get_context_stats()
                completed = conv.recorder.end_turn(
                    "",
                    context_messages=context_messages,
                    context_stats=context_stats,
                ) or {}
                self._broadcast(
                    conv,
                    {"type": MSG_ERROR, "error": str(e), "context_stats": context_stats, **completed},
                )
            finally:
                self._apply_pending_model_refresh(conv)

    # ---------- 重试与分支 ----------

    async def retry(self, conversation_id: str, think: str | None = None) -> dict[str, Any]:
        """
        重试最后一轮: 保留旧回复版本, 回退模型上下文后重新生成

        参数:
        - conversation_id: 会话 ID
        - think: 思考模式

        think 缺省 (None) 时回落 send 的会话默认逻辑

        返回:
        - dict[str, Any]: 新回复版本的轮次标识
        """
        conv = self._conversations.get(conversation_id)
        if conv is None:
            conv = await self._resume_conversation(conversation_id)
        if conv is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        if conv.task is not None and not conv.task.done():
            return {"ok": False, "error": "上一轮仍在进行, 请等待完成"}
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
        except Exception as e:
            conv.recorder.abort_active_variant()
            return {"ok": False, "error": f"回退模型上下文失败: {e}"}

        text = str(retry_turn["user_input"])
        attachments = cast(list[dict[str, Any]] | None, retry_turn.get("attachments"))
        display_text = text
        img_urls: list[str] | None = None
        if attachments:
            att_lines = [f"[附件: {item.get('name', 'file')}]" for item in attachments]
            display_text = "\n".join(att_lines) + "\n" + text if text.strip() else "\n".join(att_lines)
            img_urls = [
                str(item["url"])
                for item in attachments
                if str(item.get("type", "")).startswith("image") and item.get("url")
            ]
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
                img_urls=img_urls,
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

    async def select_variant(
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
            try:
                if current_context is None or current_context:
                    await conv.session.ctx.del_last_chat(1)
                if target_context:
                    await conv.session.ctx.add_turn_messages(target_context)
            except Exception as e:
                if current_context:
                    try:
                        await conv.session.ctx.add_turn_messages(current_context)
                    except Exception as restore_error:
                        logger.error(f"[聊天] 回复版本切换回滚失败: {restore_error}")
                return {"ok": False, "error": f"切换模型上下文失败: {e}"}
            turn = conv.recorder.activate_variant(turn_index, variant_index)
        if turn is None:
            return {"ok": False, "error": "回复版本不存在"}
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
        # 新建会话并继承影响运行时构建的会话设置
        new_cid = await self.create_conversation(
            model=src.model,
            think=src.default_think,
            system_prompt=src.system_prompt,
            project_id=src.project_id,
        )
        new_conv = self._conversations[new_cid]
        context_messages = src.recorder.active_context_before(turn_index)
        copied = src.recorder.copy_turns_to(new_conv.recorder, turn_index)
        if copied > 0 and not context_messages:
            context_messages = self._legacy_fork_context(src, turn_index)
        if context_messages:
            await new_conv.session.ctx.add_turn_messages(context_messages)
        logger.info(f"[聊天] fork: {conversation_id} -> {new_cid}, 复制 {copied} 轮")
        return {"ok": True, "conversation_id": new_cid, "copied_turns": copied}

    # ---------- 项目 (工作区文件夹绑定) ----------

    def _apply_project(self, session: AsyncSimpleSession, project: dict[str, Any] | None) -> None:
        """
        把项目工作区绑定到会话 (鸭子属性, satrap_coding/base_take 工具按会话解析)

        参数:
        - session: 会话
        - project: 项目

        无项目时工作区与 sandbox 都限定在会话独占目录;
        有项目时只共享外部工作区, sandbox 与 uploads 仍保持会话隔离
        """
        project_id = str(project.get("project_id") or "") if project else ""
        scope = StorageScope(
            platform_id=self._platform_id,
            user_id="local",
            session_id=session.session_id,
            project_id=project_id,
        )
        session_root = self._storage.ensure_session(scope)
        workspace_root = (
            Path(str(project["root_path"])).resolve()
            if project is not None
            else session_root / "sandbox"
        )
        # 鸭子属性注入, 插件在安装和调用时解析当前会话作用域
        setattr(session, "coding_workspace_root", str(workspace_root))
        setattr(session, "coding_session_root", str(session_root))
        setattr(session, "coding_sandbox_root", str(session_root / "sandbox"))
        setattr(session, "coding_upload_root", str(session_root / "uploads"))
        setattr(session, "coding_artifacts_root", str(session_root / "artifacts"))
        setattr(session, "coding_indexes_root", str(session_root / "indexes"))
        setattr(session, "coding_cache_root", str(session_root / "cache"))
        setattr(session, "coding_memory_db", str(self._storage.platform_db(self._platform_id)))
        setattr(session, "coding_memory_scope", f"session:{session.session_id}")

    def create_project(self, name: str, root_path: str) -> dict[str, Any]:
        """
        创建项目 (绑定工作区文件夹, 任意绝对路径; 校验存在且是目录)

        参数:
        - name: 名称
        - root_path: 根目录路径

        返回:
        - dict[str, Any]: 创建项目 (绑定工作区文件夹, 任意绝对路径; 校验存在且是目录)
        """
        name = name.strip()
        if not name:
            return {"ok": False, "error": "项目名称不能为空"}
        p = Path(root_path.strip()).expanduser().resolve()
        if not p.is_dir():
            return {"ok": False, "error": f"路径不存在或不是目录: {root_path}"}
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

    @staticmethod
    def browse_directories(path: str = "") -> dict[str, Any]:
        """
        浏览服务器目录 (只列子目录, 供前端新建项目时选择工作区文件夹)

        参数:
        - path: 路径

        - path 为空: Windows 返回盘符视图 (dirs = 各盘符, parent=None), POSIX 落到用户主目录
        - 根层级: Windows 盘符根的 parent 为 "" (回盘符视图), POSIX 根的 parent 为 None
        - 权限不足/异常子目录跳过; 只列目录不列文件, 按名排序 (忽略大小写), 上限 500 条

        返回:
        - dict[str, Any]: 浏览服务器目录 (只列子目录, 供前端新建项目时选择工作区文件夹)
        """
        if not path.strip():
            if os.name == "nt":
                drives = [
                    {"name": f"{d}:\\", "path": f"{d}:\\"}
                    for d in string.ascii_uppercase
                    if Path(f"{d}:\\").is_dir()
                ]
                return {"ok": True, "path": "", "parent": None, "dirs": drives}
            p = Path.home()
        else:
            p = Path(path.strip()).expanduser()
            if not p.is_dir():
                return {"ok": False, "error": f"路径不存在或不是目录: {path}"}
            p = p.resolve()

        dirs: list[dict[str, str]] = []
        try:
            children = sorted(p.iterdir(), key=lambda c: c.name.lower())
        except OSError:
            children = []
        for child in children:
            if len(dirs) >= 500:
                break
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue

        parent: str | None
        if p.parent == p:
            parent = "" if os.name == "nt" else None
            # 根层级: Windows 盘符根回到盘符视图 (空 path), POSIX 根无上一级
        else:
            parent = str(p.parent)
        return {"ok": True, "path": str(p), "parent": parent, "dirs": dirs}

    # ---------- 文件上传 ----------

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
        # 限制 10MB
        if len(file_data) > 10 * 1024 * 1024:
            return {"ok": False, "error": "文件超过 10MB 限制"}
        upload_dir = self._storage.session_uploads(self._platform_id, conversation_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        fpath = upload_dir / unique_name
        fpath.write_bytes(file_data)
        # 推断类型
        ext = Path(safe_name).suffix.lower()
        img_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        file_type = f"image/{ext[1:]}" if ext in img_exts else "application/octet-stream"
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

    async def _reconcile_chat_plugins(self) -> list[dict[str, Any]]:
        """
        使用共享生命周期协调器同步全部正式 Chat 会话

        返回:
        - list[dict[str, Any]]: 逐会话同步结果
        """
        desired_specs = self._plugins.resolve_specs(PluginConfigManager())
        results: list[dict[str, Any]] = []
        for conv in list(self._conversations.values()):
            if not conv.persisted:
                conv.build_fingerprint = ""
                continue
            try:
                async with conv.plugin_lock:
                    result = await reconcile_plugin_states_async(
                        conv.plugin_states,
                        desired_specs,
                        conv.session.install_plugin,
                        conv.session.uninstall_plugin,
                    )
                    self._coordinate_sandbox(conv.session)
                results.append({
                    "conversation_id": conv.conversation_id,
                    "status": "applied" if result.get("ok", False) else "error",
                    **result,
                })
            except Exception as error:
                logger.warning(f"[聊天] 会话 {conv.conversation_id} 插件同步失败: {error}")
                results.append({
                    "conversation_id": conv.conversation_id,
                    "ok": False,
                    "status": "error",
                    "error": str(error),
                })
        return results

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
        store = MemoryStore(db_path=self._storage.platform_db(self._platform_id), scope=scope)
        return {"ok": True, "memories": store.list_all()}

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
        store = MemoryStore(db_path=self._storage.platform_db(self._platform_id), scope=scope)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        record = store.add(title=title, content=content, tags=tag_list, importance=importance)
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
        store = MemoryStore(db_path=self._storage.platform_db(self._platform_id), scope=scope)
        record = store.update(memory_id, **fields)
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
        store = MemoryStore(db_path=self._storage.platform_db(self._platform_id), scope=scope)
        result = store.delete(memory_id)
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
            conv is not None
            and conv.task is not None
            and not conv.task.done()
            and not force
        ):
            raise ValueError("会话正在生成, 请停止生成或确认强制删除")
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
        archived = StorageMaintenanceService(self._storage).archive_session(
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
        if conv is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        if conv.task is None or conv.task.done():
            return {"ok": False, "error": "没有正在进行的任务"}
        conv.task.cancel()
        try:
            await conv.task
        except asyncio.CancelledError:
            pass
        completed = conv.recorder.end_turn("") or {}
        self._broadcast(conv, {"type": MSG_TURN_DONE, "answer": "", **completed})
        logger.info(f"[聊天] 会话 {conversation_id} 已取消")
        return {"ok": True}

    # ---------- 关闭 ----------

    async def close(self) -> None:
        """关闭所有会话运行时并清理未持久化目录"""
        conversations = list(self._conversations.values())
        self._conversations.clear()
        for conv in conversations:
            await self._dispose_conversation_runtime(conv)
            if not conv.persisted:
                delete_session_domain_rows(self._chat_db_path, conv.conversation_id)
                self._storage.purge_session(self._platform_id, conv.conversation_id)
