"""聊天会话编排: AsyncSimpleSession + DisplayRecorder + 插件 + WebSocket 广播

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
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, cast

from satrap.core.APICall.LLMCall import AsyncLLM, build_llm_from_config
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.log import logger
from satrap.core.type import LLMConfig
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.recorder import DisplayRecorder, get_conversation_meta
from satrap.edictum import AsyncSimpleSession

# WS 广播消息类型
MSG_THINKING = "thinking_delta"
MSG_CONTENT = "content_delta"
MSG_TOOL_START = "tool_start"
MSG_TOOL_END = "tool_end"
MSG_TURN_DONE = "turn_done"
MSG_ERROR = "error"


def build_llm(cfg: LLMConfig) -> AsyncLLM:
    """由 LLMConfig 构造 AsyncLLM (统一经 build_llm_from_config, 含输出预算)"""
    return cast(AsyncLLM, build_llm_from_config(cfg, async_=True))


@dataclass
class _Conversation:
    """单个会话的运行时状态"""

    conversation_id: str
    session: AsyncSimpleSession
    recorder: DisplayRecorder
    model: str
    default_think: str = "off"
    """会话默认思考强度 (来自 conversation_meta, send 未显式传 think 时使用)"""
    task: asyncio.Task[Any] | None = None
    """当前正在执行的 run task (None 表示空闲)"""
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(
        default_factory=lambda: set()
    )
    """WS 订阅者队列集合"""


class ChatService:
    """聊天会话编排服务

    生命周期由 server 持有; 所有方法在事件循环线程调用
    """

    def __init__(
        self,
        model_config: ModelConfigManager,
        plugins: ChatPluginRegistry,
        *,
        chat_db_path: str | None = None,
        display_db_path: str | None = None,
    ) -> None:
        self._model_cfg = model_config
        self._plugins = plugins
        self._chat_db_path = chat_db_path  # None 时用会话默认 (satrapdata/chat_history.db)
        self._display_db_path = display_db_path  # None 时用默认 (satrapdata/display.db)
        self._conversations: dict[str, _Conversation] = {}
        self._orphan_queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        """订阅时会话不在内存的孤儿队列, _resume_conversation 完成后挂入"""

    # ---------------- 广播 ----------------

    def _broadcast(self, conv: _Conversation, msg: dict[str, Any]) -> None:
        """向会话所有 WS 订阅者广播 (put_nowait 非阻塞; 队列满则丢弃最旧由消费者保证)"""
        msg = {**msg, "conversation_id": conv.conversation_id, "ts": time.time()}
        for q in list(conv.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning(f"[聊天] 会话 {conv.conversation_id} 订阅者队列满, 丢弃消息")

    def subscribe(self, conversation_id: str) -> asyncio.Queue[dict[str, Any]]:
        """订阅会话广播, 返回队列 (WS 处理器持有, 断开时 unsubscribe)

        会话不在内存时登记为孤儿队列, _resume_conversation 完成后自动挂入
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            conv.subscribers.add(q)
        else:
            self._orphan_queues.setdefault(conversation_id, set()).add(q)
        return q

    def unsubscribe(self, conversation_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        """取消订阅"""
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            conv.subscribers.discard(q)
        orphans = self._orphan_queues.get(conversation_id)
        if orphans is not None:
            orphans.discard(q)
            if not orphans:
                del self._orphan_queues[conversation_id]

    # ---------------- 会话管理 ----------------

    def list_models(self) -> list[str]:
        """列出可用 LLM 配置名"""
        return list(self._model_cfg.list_llm_configs(mask_api_key=True).keys())

    # ---------------- 模型配置管理 ----------------

    def list_models_detail(self) -> dict[str, Any]:
        """列出全部 LLM 配置 (api_key 脱敏)"""
        return {"ok": True, "models": self._model_cfg.list_llm_configs(mask_api_key=True)}

    def add_model(self, config: dict[str, Any]) -> dict[str, Any]:
        """新增 LLM 配置"""
        from satrap.core.type import LLMConfig

        name = str(config.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "缺少 name"}
        existing = self._model_cfg.list_llm_configs()
        if name in existing:
            return {"ok": False, "error": f"配置已存在: {name}"}
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
            lock_api_key=bool(config.get("lock_api_key")),
            reasoning_body=config.get("reasoning_body"),
            thinking_field_name=config.get("thinking_field_name"),
            thinking_fields=config.get("thinking_fields"),
        )
        self._model_cfg.set_llm_config(cfg, name)
        return {"ok": True}

    def update_model(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """更新 LLM 配置"""
        existing = self._model_cfg.list_llm_configs()
        if name not in existing:
            return {"ok": False, "error": f"配置不存在: {name}"}
        # 过滤合法字段
        allowed = {"model", "base_url", "api_key", "temperature", "top_p", "max_tokens", "context_window", "history_ratio", "lock_api_key", "reasoning_body", "thinking_field_name", "thinking_fields"}
        kwargs = {k: v for k, v in config.items() if k in allowed}
        # 过滤脱敏 api_key (含 * 的值是掩码, 不是真实 key)
        if "api_key" in kwargs and isinstance(kwargs["api_key"], str) and "*" in kwargs["api_key"]:
            del kwargs["api_key"]
        if not kwargs:
            return {"ok": False, "error": "没有可更新的字段"}
        self._model_cfg.update_llm_config(name, **kwargs)
        return {"ok": True}

    def delete_model(self, name: str) -> dict[str, Any]:
        """删除 LLM 配置"""
        if not self._model_cfg.remove_llm_config(name):
            return {"ok": False, "error": f"配置不存在: {name}"}
        return {"ok": True}

    async def create_conversation(self, model: str = "default", *, system_prompt: str | None = None, think: str = "off") -> str:
        """创建会话, 返回 conversation_id

        - think: 会话默认思考强度, 存入 conversation_meta (send 未显式传 think 时使用)
        """
        cfg = self._model_cfg.get_llm_config(model)
        llm = build_llm(cfg)
        conversation_id = uuid.uuid4().hex

        recorder = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)

        # 异步回调包装: 落库 + 广播 (在事件循环内)
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
        if system_prompt:
            session_kwargs["system_prompt"] = system_prompt
        if self._chat_db_path:
            session_kwargs["db_path"] = self._chat_db_path

        session = AsyncSimpleSession(conversation_id, llm, **session_kwargs)

        conv = _Conversation(
            conversation_id=conversation_id,
            session=session,
            recorder=recorder,
            model=model,
            default_think=think,
        )
        self._conversations[conversation_id] = conv

        # 工具观察钩子 (同步调用): 落库 + 广播
        def on_tool_start(event: dict[str, Any]) -> None:
            recorder.on_tool_start(event)
            self._broadcast(conv, {"type": MSG_TOOL_START, **event})

        def on_tool_end(event: dict[str, Any]) -> None:
            recorder.on_tool_end(event)
            self._broadcast(conv, {"type": MSG_TOOL_END, **event})

        # 安装已启用的插件 (扫描到且 json 标记 enabled)
        await self._install_enabled_plugins(session)

        # 确保主工作流已初始化 (无插件时 install_plugin 不会触发 initialize)
        await session.initialize()

        # 工具钩子需在插件安装后挂 (确保挂到主工作流 tools_manager)
        session.tools_manager.tool_call_start = on_tool_start
        session.tools_manager.tool_call_end = on_tool_end

        logger.info(f"[聊天] 会话已创建: {conversation_id} (model={model})")
        recorder.save_meta(model, think)
        return conversation_id

    async def _install_enabled_plugins(self, session: AsyncSimpleSession) -> None:
        """对会话并行安装所有 json 标记 enabled 的插件"""
        async def _install_one(name: str, pdir: str) -> None:
            try:
                await session.install_plugin(pdir)
                logger.info(f"[聊天] 会话已安装插件: {name}")
            except Exception as e:
                logger.error(f"[聊天] 会话安装插件失败 {name}: {e}")

        tasks = []
        for info in self._plugins.scan():
            if not info.get("enabled"):
                continue
            name = str(info.get("name") or "")
            pdir = self._plugins.get_plugin_dir(name)
            if pdir is None:
                continue
            tasks.append(_install_one(name, str(pdir)))
        if tasks:
            await asyncio.gather(*tasks)
        self._coordinate_sandbox(session)

    def _coordinate_sandbox(self, session: AsyncSimpleSession) -> None:
        """沙箱协调: coding 启用时停用 base_take 的 code_sandbox (避免模型困惑)

        两插件共享同一 sandbox 目录 (.satrap/sandbox), coding 的 shell 能力更强,
        因此 coding 在场时 base_take 的 code_sandbox 工具禁用
        """
        coding_on = self._plugins.is_enabled("satrap_coding")
        base_on = self._plugins.is_enabled("base_take")
        if coding_on and base_on:
            if session.tools_manager.disable_tool("code_sandbox"):
                logger.info("[聊天] coding 插件在场, 已停用 base_take 的 code_sandbox")
        elif base_on:
            session.tools_manager.enable_tool("code_sandbox")

    def get_conversation(self, conversation_id: str) -> _Conversation | None:
        """取会话运行时状态"""
        return self._conversations.get(conversation_id)

    def list_turns(self, conversation_id: str) -> list[dict[str, Any]]:
        """列出会话对话轮次 (经 recorder)"""
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            return conv.recorder.list_turns()
        # 会话不在内存 (如重启后): 用只读 recorder 查库
        rec = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)
        try:
            return rec.list_turns()
        finally:
            rec.close()

    # ---------------- 会话恢复 ----------------

    async def _resume_conversation(self, conversation_id: str) -> _Conversation | None:
        """从 display.db 恢复会话运行时状态 (懒加载)

        返回 None 表示会话在 db 中也不存在
        """
        meta = get_conversation_meta(conversation_id, db_path=self._display_db_path)
        if meta is not None:
            model = meta.get("model", "default")
            default_think = str(meta.get("think") or "off")
        else:
            # meta 不存在 (旧会话无 meta 记录): 查 display_turns 确认会话存在, 用 default model
            from satrap.display.recorder import DisplayRecorder as _DR
            probe = _DR(db_path=self._display_db_path, conversation_id=conversation_id)
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
        conv = _Conversation(
            conversation_id=conversation_id,
            session=session,
            recorder=recorder,
            model=model,
            default_think=default_think,
        )
        self._conversations[conversation_id] = conv

        def on_tool_start(event: dict[str, Any]) -> None:
            recorder.on_tool_start(event)
            self._broadcast(conv, {"type": MSG_TOOL_START, **event})

        def on_tool_end(event: dict[str, Any]) -> None:
            recorder.on_tool_end(event)
            self._broadcast(conv, {"type": MSG_TOOL_END, **event})

        await self._install_enabled_plugins(session)
        await session.initialize()
        session.tools_manager.tool_call_start = on_tool_start
        session.tools_manager.tool_call_end = on_tool_end

        # 挂入订阅时会话不在内存的孤儿队列
        orphans = self._orphan_queues.pop(conversation_id, None)
        if orphans:
            conv.subscribers.update(orphans)
            logger.info(f"[聊天] 会话 {conversation_id} 恢复后挂入 {len(orphans)} 个孤儿订阅")
        return conv

    # ---------------- 发送 ----------------

    async def send(
        self,
        conversation_id: str,
        text: str,
        *,
        think: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """发送消息: 立即返回, run 在后台 task 执行, 流式经 WS 推送

        同一会话不支持并发 send (上一个 run 未完成时拒绝)
        think 为 None 时使用会话默认 (conversation_meta)
        """
        conv = self._conversations.get(conversation_id)
        if conv is None:
            conv = await self._resume_conversation(conversation_id)
        if conv is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        if conv.task is not None and not conv.task.done():
            return {"ok": False, "error": "上一轮仍在进行, 请等待完成"}
        if not text.strip() and not attachments:
            return {"ok": False, "error": "消息不能为空"}

        effective_think = think if think is not None else conv.default_think

        # 附件拼入文本前缀 + 图片传 img_urls
        display_text = text
        img_urls: list[str] | None = None
        if attachments:
            att_lines = [f"[附件: {a.get('name', 'file')}]" for a in attachments]
            display_text = "\n".join(att_lines) + "\n" + text if text.strip() else "\n".join(att_lines)
            img_urls = [a["url"] for a in attachments if a.get("type", "").startswith("image")]

        conv.recorder.start_turn(text, attachments=attachments)
        self._broadcast(conv, {"type": "turn_start", "user_input": text, "attachments": attachments})
        conv.task = asyncio.ensure_future(self._run_turn(conv, display_text, effective_think, img_urls=img_urls))
        return {"ok": True, "conversation_id": conversation_id}

    async def _run_turn(
        self,
        conv: _Conversation,
        text: str,
        think: str,
        *,
        img_urls: list[str] | None = None,
    ) -> None:
        """后台执行一轮 run, 结束回填 recorder + 广播完成"""
        try:
            answer = await conv.session.run(text, img_urls=img_urls, thinking=think)
            conv.recorder.end_turn(answer)
            self._broadcast(conv, {"type": MSG_TURN_DONE, "answer": answer})
        except Exception as e:
            logger.error(f"[聊天] 会话 {conv.conversation_id} run 失败: {e}")
            conv.recorder.end_turn("")
            self._broadcast(conv, {"type": MSG_ERROR, "error": str(e)})

    # ---------------- Retry / Fork ----------------

    async def retry(self, conversation_id: str, think: str | None = None) -> dict[str, Any]:
        """重试最后一轮: 删除最后一轮记录, 用相同 input 重新发送

        think 缺省 (None) 时回落 send 的会话默认逻辑
        """
        conv = self._conversations.get(conversation_id)
        if conv is None:
            conv = await self._resume_conversation(conversation_id)
        if conv is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        if conv.task is not None and not conv.task.done():
            return {"ok": False, "error": "上一轮仍在进行, 请等待完成"}
        deleted = conv.recorder.delete_last_turn()
        if deleted is None:
            return {"ok": False, "error": "没有可重试的轮次"}
        # 重新发送 (不重新记录 turn, 由 send 内部 start_turn)
        return await self.send(conversation_id, deleted["user_input"], think=think)

    async def fork(self, conversation_id: str, turn_index: int) -> dict[str, Any]:
        """从指定轮次 fork 新会话 (复制该轮之前的上下文, 继承 model 与默认 think)"""
        src = self._conversations.get(conversation_id)
        if src is None:
            src = await self._resume_conversation(conversation_id)
        if src is None:
            return {"ok": False, "error": f"会话不存在: {conversation_id}"}
        # 新建会话 (同 model, 继承默认 think)
        new_cid = await self.create_conversation(model=src.model, think=src.default_think)
        new_conv = self._conversations[new_cid]
        # 复制轮次
        copied = src.recorder.copy_turns_to(new_conv.recorder, turn_index)
        logger.info(f"[聊天] fork: {conversation_id} -> {new_cid}, 复制 {copied} 轮")
        return {"ok": True, "conversation_id": new_cid, "copied_turns": copied}

    # ---------------- 文件上传 ----------------

    def save_upload(self, conversation_id: str, file_name: str, file_data: bytes) -> dict[str, Any]:
        """保存上传文件到 .satrap/uploads/{conversation_id}/"""
        import uuid as _uuid
        from pathlib import Path

        safe_name = Path(file_name).name  # 防路径穿越
        if not safe_name:
            return {"ok": False, "error": "文件名非法"}
        # 限制 10MB
        if len(file_data) > 10 * 1024 * 1024:
            return {"ok": False, "error": "文件超过 10MB 限制"}
        upload_dir = Path(".satrap") / "uploads" / conversation_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"{_uuid.uuid4().hex[:8]}_{safe_name}"
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

    # ---------------- 插件 ----------------

    def list_plugins(self) -> list[dict[str, Any]]:
        """列出插件清单 (扫描 + 启用状态)"""
        return self._plugins.scan()

    async def set_plugin_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """设置插件启用状态, 并对所有活动会话即时 install/uninstall"""
        pdir = self._plugins.get_plugin_dir(name)
        if pdir is None:
            return {"ok": False, "error": f"插件不存在: {name}"}
        self._plugins.set_enabled(name, enabled)
        # 对活动会话即时生效
        for conv in self._conversations.values():
            try:
                if enabled:
                    await conv.session.install_plugin(str(pdir))
                else:
                    await conv.session.uninstall_plugin(name)
                self._coordinate_sandbox(conv.session)
            except Exception as e:
                logger.warning(f"[聊天] 会话 {conv.conversation_id} 插件 {name} 状态切换失败: {e}")
        return {"ok": True}

    async def set_plugin_capability(self, name: str, kind: str, cap: str, enabled: bool) -> dict[str, Any]:
        """设置能力独立启用状态 (持久化; 对活动会话的能力级切换暂经插件重载生效)"""
        if not self._plugins.set_capability(name, kind, cap, enabled):
            return {"ok": False, "error": f"非法能力类别: {kind}"}
        return {"ok": True}

    # ---------------- 插件配置 ----------------

    def get_plugin_config(self, name: str) -> dict[str, Any]:
        """取插件配置 (schema + 当前全局值)"""
        from satrap.edictum.plugin import load_plugin_meta
        from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema, schema_to_payload

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

    def save_plugin_config(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """保存插件全局配置 (按 schema 校验)"""
        from satrap.edictum.plugin import load_plugin_meta
        from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema

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
        return {"ok": True}

    # ---------------- 记忆管理 ----------------
    # 注意: 管理接口是管理员工具, 恒定 full 模式 (MemoryStore 默认),
    # 不受插件配置 memory_mode 约束; memory_mode 只管模型工具与注入

    def list_memories(self, scope: str = "web_chat") -> dict[str, Any]:
        """列出记忆 (按 scope)"""
        from satrap.expend.tools.memory_store import MemoryStore

        store = MemoryStore(scope=scope)
        return {"ok": True, "memories": store.list_all()}

    def add_memory(self, title: str, content: str, *, tags: str = "", importance: int = 1, scope: str = "web_chat") -> dict[str, Any]:
        """添加记忆 (tags 逗号分隔字符串转 list)"""
        from satrap.expend.tools.memory_store import MemoryStore

        store = MemoryStore(scope=scope)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        record = store.add(title=title, content=content, tags=tag_list, importance=importance)
        if not record.get("ok"):
            return {"ok": False, "error": record.get("error", "添加失败")}
        return {"ok": True, "memory": record}

    def update_memory(self, memory_id: str, *, scope: str = "web_chat", **fields: Any) -> dict[str, Any]:
        """更新记忆"""
        from satrap.expend.tools.memory_store import MemoryStore

        store = MemoryStore(scope=scope)
        record = store.update(memory_id, **fields)
        if not record.get("ok"):
            return {"ok": False, "error": record.get("error", "更新失败")}
        return {"ok": True, "memory": record}

    def delete_memory(self, memory_id: str, *, scope: str = "web_chat") -> dict[str, Any]:
        """删除记忆"""
        from satrap.expend.tools.memory_store import MemoryStore

        store = MemoryStore(scope=scope)
        result = store.delete(memory_id)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "删除失败")}
        return {"ok": True}

    # ---------------- 删除会话 ----------------

    async def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        """删除会话: 清内存运行时状态 + 删 display.db 数据"""
        conv = self._conversations.pop(conversation_id, None)
        if conv is not None:
            if conv.task is not None and not conv.task.done():
                conv.task.cancel()
            conv.recorder.close()
        # 删 db 数据 (用独立 recorder, 不依赖内存状态)
        rec = DisplayRecorder(db_path=self._display_db_path, conversation_id=conversation_id)
        try:
            rec.delete_conversation()
        finally:
            rec.close()
        logger.info(f"[聊天] 会话已删除: {conversation_id}")
        return {"ok": True}

    # ---------------- 取消 ----------------

    async def cancel(self, conversation_id: str) -> dict[str, Any]:
        """取消当前正在执行的 run task"""
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
        conv.recorder.end_turn("")
        self._broadcast(conv, {"type": MSG_TURN_DONE, "answer": ""})
        logger.info(f"[聊天] 会话 {conversation_id} 已取消")
        return {"ok": True}

    # ---------------- 关闭 ----------------

    async def close(self) -> None:
        """关闭所有会话 recorder 连接"""
        for conv in self._conversations.values():
            if conv.task is not None and not conv.task.done():
                conv.task.cancel()
            conv.recorder.close()
        self._conversations.clear()
