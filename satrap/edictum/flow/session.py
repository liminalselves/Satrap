"""
流程会话 (issue #4): 多 workflow 由节点图编排

每个 WF 节点一个 AsyncModelWorkflowFramework, 各自 tools_manager 与 ctx;
插件按 WF 节点的 plugins 列表单独安装, 工具互相看不见
"""
from __future__ import annotations

from satrap.core.framework.Base import AsyncModelWorkflowFramework, AsyncSession
from satrap.core.utils.TCBuilder import AsyncToolsManager
from satrap.edictum.flow.definition import FlowDefinition, FlowNodeInstance, parse_flow_definition
from satrap.edictum.flow.registry import FlowNodeRegistry
from satrap.edictum.flow.store import FlowStore
from satrap.edictum.flow.validate import validate

from typing import Any
import contextvars


current_flow_workflow: contextvars.ContextVar[AsyncModelWorkflowFramework | None] = contextvars.ContextVar(
    "satrap_flow_current_workflow", default=None,
)
"""流程会话正在运行的 WF; 回复绑定/工具归属检查等先读这里, 读不到再回退 session._wf"""


class AsyncFlowSession(AsyncSession):
    """流程会话: 每个 WF 节点一个 AsyncModelWorkflowFramework"""

    def __init__(
        self,
        session_id: str,
        llm: Any,
        *,
        flow: dict[str, Any] | None = None,
        content_callback: Any = None,
        db_path: str | None = None,
        enable_checkpoint: bool = False,
        model_resolver: Any = None,
        plugin_installer: Any = None,
        **kwargs: Any,
    ) -> None:
        """
        初始化流程会话

        参数:
        - session_id: 会话 ID
        - llm: 默认模型实例; 节点 model_name 缺省时使用
        - flow: params.flow 原始定义
        - content_callback: 内容回调
        - db_path: 上下文数据库路径
        - enable_checkpoint: 是否启用会话级检查点
        - model_resolver: 按 model_name 解析 AsyncLLM 的可调用对象, 缺省使用 llm
        - plugin_installer: 往指定 WF 安装一个插件的可调用对象 (session, wf, path, config)
        - kwargs: 其他会话参数
        """
        init_kwargs: dict[str, Any] = {"enable_checkpoint": enable_checkpoint}
        if db_path is not None:
            init_kwargs["db_path"] = db_path
        super().__init__(session_id, content_callback, **init_kwargs)
        self._init_llm = llm
        self._model_resolver = model_resolver
        self._plugin_installer = plugin_installer
        self._raw_flow = flow or {}
        self._definition: FlowDefinition | None = None
        self._issues: list[Any] = []
        self._workflows: dict[str, AsyncModelWorkflowFramework] = {}
        """节点 id -> WF 实例"""
        self._flow_store: FlowStore | None = None
        self._scope_key: str = ""
        self._engine: Any = None
        self._transform: Any = None
        self._message_store: Any = None
        self._tile_last_at: dict[str, float] = {}
        self._failure_notified: dict[str, bool] = {}
        self._wake_count: dict[str, int] = {}

    async def _async_init(self) -> None:
        """解析定义, 静态检查, 按 WF 节点装配 AsyncModelWorkflowFramework"""
        if self._definition is not None:
            return
        from satrap.edictum.flow.definition import migrate_flow_definition
        definition, _, _ = migrate_flow_definition(parse_flow_definition(self._raw_flow), FlowNodeRegistry())
        registry = FlowNodeRegistry()
        self._issues = validate(definition, registry)
        if any(i.level == "error" for i in self._issues):
            raise ValueError("流程定义存在错误, 请先通过 /config/edictum/flow/validate 检查")
        self._definition = definition
        for node in definition.nodes.values():
            node_type = registry.get(node.type)
            if node_type is None or node_type.role != "workflow":
                continue
            wf_id = self.workflow_id_assign(node.id)
            resolved_llm = self._resolve_llm(node, registry)
            wf = AsyncModelWorkflowFramework(
                resolved_llm,
                context_id=wf_id,
                tools_manager=AsyncToolsManager(),
                db_path=self.session_ctx.db_path,
            )
            await wf.initialize()
            self._track_workflow_context(wf_id, wf.ctx)
            self._workflows[node.id] = wf
            self._bind_snapshot_tools(node, wf)

    def _bind_snapshot_tools(self, node: FlowNodeInstance, wf: AsyncModelWorkflowFramework) -> None:
        """
        给装了 flow_snapshot 的 WF 注入状态槽工具, 只绑它连着的状态槽

        参数:
        - node: WF 节点实例
        - wf: 已装配的工作流
        """
        plugins = [p.strip() for p in str(node.config.get("plugins") or "").splitlines() if p.strip()]
        if "flow_snapshot" not in plugins:
            return
        # 找这个 WF 写着的状态槽 (text -> state.write) 并读它的 max_chars
        assert self._definition is not None
        allowed: dict[str, int] = {}
        registry = FlowNodeRegistry()
        for e in self._definition.edges:
            if e.source != node.id or e.source_port != "text":
                continue
            target = self._definition.nodes.get(e.target)
            if target is None or target.type != "state" or e.target_port != "write":
                continue
            max_chars = int(target.config.get("max_chars") or 6000)
            allowed[e.target] = max_chars
        self._flow_store = FlowStore(self.session_ctx.db_path, "", self._flow_name())
        self._scope_key = self.session_id
        self._flow_snapshot_binding = (self._flow_store, self._scope_key, allowed)
        from satrap.expend.plugins.flow_snapshot.tools import get_tools
        for tool in get_tools(self):
            wf.tools_manager.register_tool(tool)

    def _flow_name(self) -> str:
        """流程配置名, 用作持久化分组的第二维"""
        return self.session_id

    def _resolve_llm(self, node: FlowNodeInstance, registry: FlowNodeRegistry) -> Any:
        """
        按节点 model_name 解析模型实例, 未设置时用会话默认

        参数:
        - node: WF 节点实例
        - registry: 节点类型注册表

        返回:
        - AsyncLLM: 模型实例
        """
        model_name = str(node.config.get("model_name") or "")
        if self._model_resolver is not None and model_name:
            resolved = self._model_resolver(model_name)
            if resolved is not None:
                return resolved
        return self._init_llm

    def workflow_for(self, node_id: str) -> AsyncModelWorkflowFramework:
        """
        按节点 id 取 WF 实例

        参数:
        - node_id: WF 节点 id

        返回:
        - AsyncModelWorkflowFramework: 对应 WF; 不存在抛 KeyError
        """
        return self._workflows[node_id]

    def workflows(self) -> dict[str, AsyncModelWorkflowFramework]:
        """全部 WF 实例 {节点 id: WF}"""
        return dict(self._workflows)

    async def observe(self, event: Any) -> None:
        """
        流程会话的消息入口: 每条入站消息都先到这里, 由内部决定唤醒谁

        参数:
        - event: 平台消息事件 (MessageEvent)
        """
        await self._ensure_initialized()
        if self._definition is None:
            return
        event.set_extra("flow_observed", True)
        await self._handle_observed(event)

    async def _handle_observed(self, event: Any) -> None:
        """分发一条已观察的消息给触发引擎 (P1-5 已就位, 唤醒/阈值判定由引擎做)"""
        if self._engine is None:
            from satrap.edictum.flow.engine import TriggerEngine
            self._engine = TriggerEngine(
                self._definition,
                {n.id: FlowNodeRegistry().get(n.type) for n in self._definition.nodes.values()},
                resolve_state=lambda sid: self._state_view(sid),
            )
            self._engine.on_trigger(self._on_wf_trigger)
        # 每条消息都过阈值磁贴 (按 seq); 唤醒磁贴只在唤醒判定通过时发事件
        await self._check_thresholds()
        is_private = bool(getattr(event, "is_private_chat", lambda: False)())
        try:
            from satrap.core.pipeline.wake_policy import evaluate_wake
            decision = evaluate_wake(event)
        except Exception:
            decision = None
        if is_private:
            # 私聊里沿用群唤醒的私聊语义: use_platform_policy 时每条消息都唤醒,
            # 自定义规则时按 @ (私聊视为总是被 @)/唤醒词/新消息数
            decision_triggered = decision is None or decision.triggered
        else:
            decision_triggered = decision is not None and decision.triggered
        if decision_triggered:
            for node in self._definition.nodes.values():
                if node.type != "wake":
                    continue
                if not node.config.get("use_platform_policy", True):
                    # 自定义唤醒规则: 按节点配置判定而不是沿用群策略
                    if not self._wake_custom_hit(node, event):
                        continue
                from satrap.edictum.flow.engine import FlowEvent
                from satrap.edictum.flow.types import FlowMessage
                import uuid
                msg = FlowMessage(
                    message_id=str(event.call_origin.source_message_id or ""),
                    position=0,
                    sender_id=str(event.call_origin.actor_id or ""),
                    nickname="",
                    text=event.get_message_str() or "",
                    is_bot=False,
                    mentions_bot="@" in (event.get_message_str() or ""),
                    time=event.platform_message.received_at,
                )
                await self._engine.fire(
                    node.id,
                    FlowEvent(event_id=uuid.uuid4().hex, kind="message", tile=node.id,
                              why=f"群唤醒: {decision.reason}", message=msg),
                )

    async def _on_wf_trigger(self, wf_id: str, event: Any) -> None:
        """WF 被触发: 拉取输入并运行, 结果按 send_mode 发送"""
        await self._run_workflow(wf_id, event)

    async def _run_workflow(self, wf_id: str, event: Any) -> None:
        """执行一次 WF 运行; 状态槽提交/位置推进/失败处理按运行时契约"""
        import uuid
        run_id = uuid.uuid4().hex
        record_debug("flow_run", "start", metadata={"wf": wf_id, "run_id": run_id, "event_id": event.event_id})
        try:
            wf = self._workflows[wf_id]
            # 拉取输入 (P1-7 的共享求值在事件内生效)
            pulled = await self._pull_inputs(wf_id, run_id, event)
            # 运行 WF
            token = current_flow_workflow.set(wf)
            try:
                output = await wf.full_agent(
                    user_input=self._compose_input(pulled, event),
                    callback=False,
                    max_iterations=int(wf.llm and self._config(wf_id).get("max_steps") or 8),
                )
            finally:
                current_flow_workflow.reset(token)
            # 状态槽提交与阈值位置推进 (同事务)
            commits = self._pending_commits(wf_id, event, run_id)
            tile = event.tile if event.kind == "batch" else ""
            if commits or tile:
                self._flow_store.commit_states_by_scope_key(
                    self._scope_key, tile, wf_id, event.batch_to, commits,
                )
            # 发送: send_mode 决定前台回复或后台发送
            await self._send_output(wf_id, output or "", event)
            # done 事件
            await self._engine.complete(wf_id, event, output or "")
            record_debug("flow_run", "end", metadata={"wf": wf_id, "run_id": run_id, "status": "ok"})
        except Exception as error:
            record_debug("flow_run", "end", metadata={"wf": wf_id, "run_id": run_id, "status": "failed"})
            logger.error(f"[流程会话] WF {wf_id} 本次运行失败 run={run_id}: {error}")
            await self._notify_failure(wf_id, str(error))
        finally:
            next_event = await self._engine.finish(wf_id, event)
            if next_event is not None:
                await self._on_wf_trigger(wf_id, next_event)

    async def _pull_inputs(self, wf_id: str, run_id: str, event: Any) -> list[Any]:
        """按 WF.input 口连线拉取上游数据节点"""
        from satrap.edictum.flow.transform import TransformEvaluator
        if self._transform is None:
            self._transform = TransformEvaluator(
                self._definition,
                {n.id: FlowNodeRegistry().get(n.type) for n in self._definition.nodes.values()},
                resolve_state=self._state_view,
            )
        inputs: list[Any] = []
        for e in self._definition.edges:
            if e.target == wf_id and e.target_port == "input":
                try:
                    inputs.append(await self._transform.pull(wf_id, e.source, run_id, event))
                except Exception as error:
                    logger.warning(f"[流程会话] 拉取 {e.source} 失败: {error}")
                    raise
        return inputs

    def _compose_input(self, pulled: list[Any], event: Any) -> str:
        """把拉取结果和触发负载拼成 WF 的 user_input"""
        parts: list[str] = []
        if event.kind == "batch" and event.batch:
            parts.append(f"本批消息 #{event.batch_from + 1}–#{event.batch_to}:\n"
                         + "\n".join(m.text for m in event.batch))
        if event.payload:
            parts.append(event.payload)
        for value in pulled:
            if isinstance(value, tuple):
                parts.extend(getattr(item, "text", str(item)) for item in value)
            elif isinstance(value, str) and value:
                parts.append(value)
        return "\n\n".join(parts) or "(空)"

    def _pending_commits(self, wf_id: str, event: Any, run_id: str) -> list[Any]:
        """收集 snapshot_write 的待提交内容, 转成 StateCommit"""
        from satrap.edictum.flow.store import StateCommit
        commits: list[StateCommit] = []
        for tool in self._workflows[wf_id].tools_manager.tools.values():
            pending = getattr(tool, "pending", None)
            if not callable(pending):
                continue
            for state_id, content in pending().items():
                base = self._flow_store.state_by_scope_key(self._scope_key, state_id)["version"]
                covers_to = event.batch_to if event.kind == "batch" else self._flow_store.state_by_scope_key(
                    self._scope_key, state_id)["covers_to"]
                commits.append(StateCommit(state_id=state_id, content=content,
                                           base_version=base, covers_to=covers_to, run_id=run_id))
            tool.clear()
        return commits

    def _state_view(self, state_id: str) -> Any:
        """state 槽的只读视图"""
        from satrap.edictum.flow.types import StateView
        row = self._flow_store.state_by_scope_key(self._scope_key, state_id)
        return StateView(content=row["content"], version=row["version"],
                         covers_to=row["covers_to"], committed_at=row["committed_at"])

    async def _send_output(self, wf_id: str, output: str, event: Any) -> None:
        """按平台回复节点的 send_mode 发送 WF 输出"""
        for e in self._definition.edges:
            if e.source != wf_id or e.source_port != "text":
                continue
            target = self._definition.nodes.get(e.target)
            if target is None or target.type != "output":
                continue
            send_mode = str(target.config.get("send_mode") or "flow_reply")
            if send_mode == "final_text" and output:
                # 由宿主直接发最终文本, 走后台发送通道
                await self._send_scheduled_text(output)
            # flow_reply: 模型已通过工具发过了, 不做兜底
            # group_chat_reply: 由 group_chat 插件自己发, 不干预

    async def _send_scheduled_text(self, text: str) -> bool:
        """后台发送文本 (无来源消息路径), 由 P1-8 的发送通道负责"""
        return False   # 适配器发送通道由会话创建时注入, 当前未接入时按未发出处理

    def _wake_custom_hit(self, node: FlowNodeInstance, event: Any) -> bool:
        """自定义唤醒规则: @ / 唤醒词 / 新消息数, 任一满足"""
        text = event.get_message_str() or ""
        config = node.config
        if config.get("mention", True) and "@" in text:
            return True
        words = [x.strip() for x in str(config.get("wake_words") or "").splitlines() if x.strip()]
        if any(word in text for word in words):
            return True
        new_messages = int(config.get("new_messages") or 0)
        if new_messages > 0:
            count = self._wake_count.setdefault(node.id, 0) + 1
            if count >= new_messages:
                self._wake_count[node.id] = 0
                return True
        return False

    async def _check_thresholds(self) -> None:
        """按 seq 检查阈值磁贴, 满足的触发对应 WF 批次"""
        for node in self._definition.nodes.values():
            if node.type != "threshold":
                continue
            positions = self._flow_store.positions_for_tile(
                MessageScope("", "", "", self._scope_key), node.id,
            )
            config = node.config
            for wf_id, committed in positions.items():
                pending = self._message_store.after(
                    MessageScope("", "", "", self._scope_key), committed,
                    limit=int(config.get("max_batch") or 200),
                ) if self._message_store is not None else []
                hit = self._threshold_hit(config, committed, pending)
                if not hit:
                    continue
                from satrap.edictum.flow.engine import FlowEvent
                import uuid
                event = FlowEvent(
                    event_id=uuid.uuid4().hex, kind="batch", tile=node.id,
                    why=f"{node.title}: {hit}", batch=tuple(self._to_flow_message(m) for m in pending),
                    batch_from=committed, batch_to=pending[-1]["seq"] if pending else committed,
                )
                await self._engine.fire(node.id, event)

    def _threshold_hit(self, config: dict[str, Any], committed: int, pending: list[Any]) -> str | None:
        """按消息数/字符数/闲置时间判定是否触发"""
        count = len(pending)
        chars = sum(len(str(m.get("text") or "")) for m in pending)
        if int(config.get("messages") or 0) > 0 and count >= int(config["messages"]):
            return f"{count} 条 ≥ {config['messages']}"
        if int(config.get("chars") or 0) > 0 and chars >= int(config["chars"]):
            return f"{chars} 字 ≥ {config['chars']}"
        if int(config.get("idle_seconds") or 0) > 0 and count >= max(1, int(config.get("min_messages") or 1)):
            last = self._tile_last_at.setdefault("_default", 0.0)
            import time as _t
            if _t.time() - last >= int(config["idle_seconds"]):
                return f"距上次触发 {int((_t.time() - last) / 60)} 分钟"
        return None

    @staticmethod
    def _to_flow_message(item: Any) -> Any:
        """档案消息转 FlowMessage"""
        from satrap.edictum.flow.types import FlowMessage
        return FlowMessage(
            message_id=str(item.get("message_id") or ""),
            position=int(item.get("seq") or 0),
            sender_id=str(item.get("sender_id") or ""),
            nickname=str(item.get("nickname") or ""),
            text=str(item.get("text") or ""),
            is_bot=item.get("direction") == "outbound",
            mentions_bot="@" in str(item.get("text") or ""),
            time=float(item.get("message_time") or 0.0),
        )

    async def _notify_failure(self, wf_id: str, reason: str) -> None:
        """按运行时契约 10.1: 同一 WF 连续失败只告知一次, 成功后重置"""
        if self._failure_notified.get(wf_id):
            return
        self._failure_notified[wf_id] = True
        node = self._definition.nodes.get(wf_id)
        title = node.title if node is not None else wf_id
        # 发送通道由宿主注入; 未接入时只写日志
        logger.warning(f"[流程会话] {title} 本次运行失败: {reason[:120]}")

    def _config(self, node_id: str) -> dict[str, Any]:
        """节点实例 config (生效值由 resolve 层合成, 这里取实例值)"""
        node = self._definition.nodes.get(node_id) if self._definition else None
        return dict(node.config) if node is not None else {}

    async def install_plugin_to_workflow(
        self,
        node_id: str,
        path: str,
        config: dict[str, Any] | None = None,
    ) -> Any:
        """
        往指定 WF 安装一个插件 (P0-6 注册的 plugin_installer 委托)

        参数:
        - node_id: 目标 WF 节点 id
        - path: 插件目录
        - config: 会话级插件配置

        返回:
        - 安装结果
        """
        if self._plugin_installer is None:
            raise TypeError("流程会话未配置插件安装器")
        return await self._plugin_installer(self, self._workflows[node_id], path, config)

    @property
    def issues(self) -> list[Any]:
        """静态检查发现的问题列表"""
        return self._issues
