"""
流程会话 (issue #4): 多 workflow 由节点图编排

每个 WF 节点一个 AsyncModelWorkflowFramework, 各自 tools_manager 与 ctx;
插件按 WF 节点的 plugins 列表单独安装, 工具互相看不见
"""
from __future__ import annotations

from satrap.core.framework.Base import AsyncModelWorkflowFramework, AsyncSession
from satrap.core.log.debug.hooks import record_debug
from satrap.core.utils.TCBuilder import AsyncToolsManager
from satrap.edictum.flow.definition import FlowDefinition, FlowNodeInstance, parse_flow_definition
from satrap.edictum.flow.instances import FlowNodeInstancePool, config_fingerprint
from satrap.edictum.flow.registry import FlowNodeRegistry
from satrap.edictum.flow.store import FlowStore
from satrap.edictum.flow.validate import validate
from satrap.edictum.flow.budget import FlowCallBudget, BudgetExceeded

from typing import Any
import contextvars

from satrap.core.log import logger


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
        plugin_catalog: Any = None,
        plugin_config: Any = None,
        message_store: Any = None,
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
        - plugin_catalog: 插件目录 (PluginCatalog); 插件节点实现经它定位
        - plugin_config: 插件配置管理器 (PluginConfigManager); 插件节点全局默认经它读取
        - message_store: 消息档案 (PlatformMessageStore); 阈值磁贴与桶的数据来源,
          缺省则由首次 observe 从事件适配器补入
        - kwargs: 其他会话参数
        """
        init_kwargs: dict[str, Any] = {"enable_checkpoint": enable_checkpoint}
        if db_path is not None:
            init_kwargs["db_path"] = db_path
        super().__init__(session_id, content_callback, **init_kwargs)
        self._init_llm = llm
        self._model_resolver = model_resolver
        self._plugin_installer = plugin_installer
        self._plugin_catalog = plugin_catalog
        self._plugin_config = plugin_config
        self._message_store = message_store
        self._raw_flow = flow or {}
        self._definition: FlowDefinition | None = None
        self._issues: list[Any] = []
        self._workflows: dict[str, AsyncModelWorkflowFramework] = {}
        """节点 id -> WF 实例"""
        self._flow_store: FlowStore | None = None
        self._scope_key: str = ""
        self._engine: Any = None
        self._transform: Any = None
        self._resumed = False
        """重启恢复是否已跑过: 首个 observe 按已持久化位置补触发未完成批次"""
        self._tile_last_at: dict[str, float] = {}
        self._failure_notified: dict[str, bool] = {}
        self._wake_count: dict[str, int] = {}
        self._budget: FlowCallBudget | None = None
        self._instances = FlowNodeInstancePool()
        """插件节点实例池 (P2-2); 类写法按生效配置指纹重建"""
        self._plugin_impls: dict[str, dict[str, Any]] = {}
        """{插件名: {节点名: 实现}}; 插件重载时重新加载"""
        self._plugin_impl_stamp: dict[str, int] = {}
        """{插件名: flow_nodes.py mtime 纳秒}; 变化时重载实现"""
        self._catalog_stamp: tuple[int, ...] = ()
        """插件目录快照标识 (各 meta.yaml mtime), 变化时重建注册表"""
        self._registry = FlowNodeRegistry()
        self._node_types: dict[str, Any] = {}

    async def _async_init(self) -> None:
        """解析定义, 静态检查, 按 WF 节点装配 AsyncModelWorkflowFramework"""
        if self._definition is not None:
            return
        from satrap.edictum.flow.definition import migrate_flow_definition
        self._refresh_registry()
        definition, _, _ = migrate_flow_definition(parse_flow_definition(self._raw_flow), self._registry)
        self._issues = validate(definition, self._registry)
        if any(i.level == "error" for i in self._issues):
            raise ValueError("流程定义存在错误, 请先通过 /config/edictum/flow/validate 检查")
        self._definition = definition
        self._node_types = {n.id: self._registry.get(n.type) for n in definition.nodes.values()}
        if definition.budget is not None:
            self._budget = FlowCallBudget(
                max_calls=definition.budget.max_calls,
                window_minutes=definition.budget.window_minutes,
                count=definition.budget.count,
            )
        else:
            self._budget = FlowCallBudget(max_calls=0)
        # 阈值位置与状态槽的持久化存取, 任何流程都需要 (不只装 flow_snapshot 的)
        self._flow_store = FlowStore(self.session_ctx.db_path, "", self._flow_name())
        self._scope_key = self.session_id
        for node in definition.nodes.values():
            node_type = self._registry.get(node.type)
            if node_type is None or node_type.role != "workflow":
                continue
            wf_id = self.workflow_id_assign(node.id)
            resolved_llm = self._resolve_llm(node, self._registry)
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
        await self._refresh_instances()

    # ---------- 插件节点实现与实例 (P2-2) ----------

    def _refresh_registry(self) -> None:
        """扫插件目录刷新节点类型注册表与 flow_nodes.py 实现; 目录或文件变化时重载"""
        entries = [] if self._plugin_catalog is None else list(self._plugin_catalog.scan())
        stamp = tuple(sorted(
            (entry.path / "flow_nodes.py").stat().st_mtime_ns
            if (entry.path / "flow_nodes.py").is_file() else 0
            for entry in entries if entry.flow_nodes
        ))
        if stamp == self._catalog_stamp and self._catalog_stamp:
            return
        self._catalog_stamp = stamp
        self._registry = FlowNodeRegistry()
        self._plugin_impls = {}
        self._plugin_impl_stamp = {}
        for entry in entries:
            if not entry.flow_nodes:
                continue
            self._registry.register_plugin_nodes(entry.name, list(entry.flow_nodes))
            module = (entry.path / "flow_nodes.py")
            self._plugin_impl_stamp[entry.name] = module.stat().st_mtime_ns if module.is_file() else 0
            from satrap.edictum.flow.plugin_nodes import load_flow_node_functions
            try:
                impls = load_flow_node_functions(entry.path, f"satrap_flow_plugin_{entry.name}")
            except Exception as error:
                logger.warning(f"[流程会话] 插件 {entry.name} flow_nodes.py 加载失败: {error}")
                impls = {}
            self._plugin_impls[entry.name] = impls

    def _node_impl(self, node_id: str) -> Any:
        """取插件节点的实现 (函数或 FlowNode 子类); 内置/未知返回 None"""
        node = self._definition.nodes.get(node_id) if self._definition else None
        node_type = self._node_types.get(node_id) if node is not None else None
        if node_type is None or not node_type.plugin:
            return None
        return self._plugin_impls.get(node_type.plugin, {}).get(node_type.type.rsplit(".", 1)[-1])

    async def _refresh_instances(self) -> None:
        """重算各插件节点的生效配置指纹, 变化时经池子重建实例"""
        if self._definition is None:
            return
        from satrap.edictum.flow.definition import resolve_node_config
        for node_id, node in self._definition.nodes.items():
            node_type = self._node_types.get(node_id)
            if node_type is None or not node_type.plugin:
                continue
            impl = self._node_impl(node_id)
            if impl is None:
                continue
            plugin_defaults = self._plugin_defaults(node_type.plugin)
            resolved = resolve_node_config(node_type, node, self._definition.defaults, plugin_defaults)
            fingerprint = config_fingerprint(resolved.values)
            ctx = self._node_ctx(node, resolved.values)
            await self._instances.ensure(node_id, impl, fingerprint, ctx)

    def _plugin_defaults(self, plugin: str) -> dict[str, dict[str, Any]]:
        """读插件节点全局默认; 未注入管理器时按空表"""
        if self._plugin_config is None or not plugin:
            return {}
        try:
            return self._plugin_config.load_flow_nodes_global(plugin)
        except Exception as error:
            logger.warning(f"[流程会话] 插件 {plugin} 节点默认读取失败, 按空处理: {error}")
            return {}

    def _node_ctx(self, node: FlowNodeInstance, resolved: dict[str, Any]) -> Any:
        """给插件 setup/条件/transform 用的轻量 ctx"""
        from types import SimpleNamespace
        return SimpleNamespace(
            config=resolved, node_id=node.id, session_id=self.session_id,
            diagnostic=lambda *a, **kw: None, resources={}, service=lambda *a, **kw: None,
        )

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
        for e in self._definition.edges:
            if e.source != node.id or e.source_port != "text":
                continue
            target = self._definition.nodes.get(e.target)
            if target is None or target.type != "state" or e.target_port != "write":
                continue
            max_chars = int(target.config.get("max_chars") or 6000)
            allowed[e.target] = max_chars
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
        # 档案来源: 首次 observe 从事件适配器补入消息库与对话键
        self._bind_archive(event)
        # 配置/插件变化时重建插件节点实例; 频率低, 变化才产生开销
        self._refresh_registry()
        await self._refresh_instances()
        # 重启恢复: 首个事件先补触发未完成批次 (瞬态队列丢弃, 位置未推进的批次重新触发)
        if not self._resumed:
            self._resumed = True
            await self._resume_unfinished()
        await self._handle_observed(event)

    async def _resume_unfinished(self) -> None:
        """
        重启恢复: 会话重建后瞬态 (排队/合并/AND 窗/唤醒计数/调用计数) 已丢弃,
        阈值位置与状态槽在库中保留; 已提交位置落后的 WF 按阈值批次重新触发
        """
        record_debug("flow_resume", "start", metadata={"session": self.session_id})
        # _check_thresholds 对每个磁贴的每个 WF 按已提交位置取积压并重触发;
        # 引擎是新建的, on_busy 队列空, 触发直接走
        try:
            await self._ensure_engine()
            await self._check_thresholds()
        except Exception as error:
            logger.warning(f"[流程会话] 重启恢复检查失败: {error}")
        record_debug("flow_resume", "end", metadata={"session": self.session_id})

    async def _ensure_engine(self) -> None:
        """触发引擎惰性装配 (恢复与常规 observe 共用)"""
        if self._engine is not None:
            return
        from satrap.edictum.flow.engine import ConditionEvaluator, TriggerEngine
        self._engine = TriggerEngine(
            self._definition,
            self._node_types,
            evaluate_condition=ConditionEvaluator(),
            resolve_state=lambda sid: self._state_view(sid),
            calls_used=lambda nid: (self._budget.calls(kind="all") if self._budget else 0),
            resolve_condition=lambda node: self._instances.acquire(node.id),
            release_ref=self._instances.release_ref,
        )
        self._engine.on_trigger(self._on_wf_trigger)

    def _bind_archive(self, event: Any) -> None:
        """从事件适配器补入消息档案库与 scope 键; 已注入或已绑定时跳过"""
        if self._message_store is not None and self._scope_key != self.session_id:
            return
        adapter = getattr(event, "adapter", None)
        if adapter is None:
            return
        if self._message_store is None:
            store = getattr(adapter, "message_archive", None)
            if store is not None:
                self._message_store = store
        try:
            scope = adapter.message_archive_scope(event.platform_message)
        except Exception:
            scope = None
        if scope is not None:
            self._scope_key = scope.key
            if self._flow_store is not None:
                self._flow_store.scope_key = scope.key
                self._flow_store.adapter_id = scope.adapter_id

    async def _handle_observed(self, event: Any) -> None:
        """分发一条已观察的消息给触发引擎 (P1-5 已就位, 唤醒/阈值判定由引擎做)"""
        await self._ensure_engine()
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
            per_run = int(self._config(wf_id).get("max_calls_per_run") or 0)
            if self._budget is not None:
                self._budget.check_and_spend(
                    source="wf", node_id=wf_id, event_id=event.event_id,
                    run_id=run_id, per_run_limit=per_run,
                )
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
            if isinstance(error, BudgetExceeded):
                record_debug("flow_budget", "exceeded", metadata={"wf": wf_id, "run_id": run_id})
            await self._notify_failure(wf_id, str(error))
        finally:
            if self._budget is not None:
                self._budget.end_run(run_id)
            next_event = await self._engine.finish(wf_id, event)
            if next_event is not None:
                await self._on_wf_trigger(wf_id, next_event)

    async def _pull_inputs(self, wf_id: str, run_id: str, event: Any) -> list[Any]:
        """按 WF.input 口连线拉取上游数据节点"""
        from satrap.edictum.flow.transform import TransformEvaluator
        if self._transform is None:
            self._transform = TransformEvaluator(
                self._definition,
                self._node_types,
                resolve_transform=lambda node, run_id: self._instances.acquire(node.id),
                resolve_state=self._state_view,
                release_ref=self._instances.release_ref,
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
        if self._message_store is None or not self._scope_key:
            return
        for node in self._definition.nodes.values():
            if node.type != "threshold":
                continue
            wf_ids = self._threshold_wfs(node.id)
            positions = self._flow_store.ensure_positions_for_tile(node.id, wf_ids)
            config = node.config
            for wf_id, committed in positions.items():
                pending = self._message_store.after_by_scope_key(
                    self._scope_key, committed,
                    limit=int(config.get("max_batch") or 200),
                )
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

    def _threshold_wfs(self, tile_id: str) -> list[str]:
        """一个阈值磁贴 event 输出实际驱动的 WF 节点 id 列表 (经逻辑门/直通传播)"""
        # 首版只取直连 WF.trigger; 经 AND/OR 门传播的驱动关系由 analyze 提供,
        # 这里保守地只登记直连, 经门驱动的 WF 在该门放行时也会走到 trigger
        out: list[str] = []
        for e in self._definition.edges:
            if e.source == tile_id and e.source_port == "event":
                target_type = self._node_types.get(e.target)
                if target_type is not None and target_type.role == "workflow" and e.target_port == "trigger":
                    out.append(e.target)
        return out

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

    async def close_flow_nodes(self) -> None:
        """会话释放时关闭全部插件节点实例 (edictum provider 释放路径调用)"""
        await self._instances.close_all()

    @property
    def issues(self) -> list[Any]:
        """静态检查发现的问题列表"""
        return self._issues
