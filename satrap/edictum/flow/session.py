"""
流程会话 (issue #4): 多 workflow 由节点图编排

每个 WF 节点一个 AsyncModelWorkflowFramework, 各自 tools_manager 与 ctx;
插件按 WF 节点的 plugins 列表单独安装, 工具互相看不见
"""
from __future__ import annotations

from satrap.core.call_context import bind_call_origin
from satrap.core.framework.Base import AsyncModelWorkflowFramework, AsyncSession
from satrap.core.log.debug.hooks import record_debug
from satrap.core.platform.event import MessageChain
from satrap.core.platform.receipt import SendReceipt
from satrap.core.utils.TCBuilder import AsyncToolsManager
from satrap.edictum.flow.definition import FlowDefinition, FlowNodeInstance, parse_flow_definition
from satrap.edictum.flow.instances import FlowNodeInstancePool, config_fingerprint
from satrap.edictum.flow.registry import FlowNodeRegistry
from satrap.edictum.flow.store import FlowStore
from satrap.edictum.flow.validate import analyze, validate
from satrap.edictum.flow.budget import FlowCallBudget, BudgetExceeded

from collections import deque
from typing import Any
import asyncio
import contextvars
import inspect
import time
import uuid

from satrap.core.log import logger


current_flow_workflow: contextvars.ContextVar[AsyncModelWorkflowFramework | None] = contextvars.ContextVar(
    "satrap_flow_current_workflow", default=None,
)
"""流程会话正在运行的 WF; 回复绑定/工具归属检查等先读这里, 读不到再回退 session._wf"""

current_flow_run: contextvars.ContextVar[tuple[str, str, str] | None] = contextvars.ContextVar(
    "satrap_flow_current_run", default=None,
)
"""正在运行的 (wf_id, event_id, run_id); 计量归属与前台来源都从这里取"""

current_flow_event: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "satrap_flow_current_event", default=None,
)
"""触发本次运行的平台消息事件 (MessageEvent); 后台触发为 None"""


class _MeteredClient:
    """模型客户端计量代理: 每次 call/stream_call 前经回调扣减预算"""

    _METERED_METHODS = ("call", "stream_call", "chat", "generate", "embed",
                        "rerank", "transcribe", "invoke", "image", "images")

    def __init__(self, inner: Any, on_call: Any) -> None:
        self._inner = inner
        self._on_call = on_call

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in self._METERED_METHODS or not callable(attr):
            return attr
        if inspect.isasyncgenfunction(attr):
            async def gen(*args: Any, **kwargs: Any) -> Any:
                self._on_call()
                async for item in attr(*args, **kwargs):
                    yield item
            return gen
        if inspect.iscoroutinefunction(attr):
            async def acall(*args: Any, **kwargs: Any) -> Any:
                self._on_call()
                return await attr(*args, **kwargs)
            return acall
        return attr

    def __repr__(self) -> str:
        return f"<Metered {self._inner!r}>"


class _NodeResources:
    """插件节点 ctx.resources: 按节点配置字段惰性构造并包装计量"""

    def __init__(self, session: "AsyncFlowSession", node: FlowNodeInstance,
                 schema: dict[str, Any], resolved: dict[str, Any]) -> None:
        self._session = session
        self._node = node
        self._schema = schema
        self._config = resolved
        self._clients: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        field = self._schema.get(key)
        if field is None or field.type not in ("llm", "embed", "rerank", "asr", "img"):
            return default
        name = self._config.get(key)
        if not name:
            return default
        client = self._clients.get(key)
        if client is None:
            from satrap.edictum.plugin_resources import named_model_config, build_model_client
            config = named_model_config(self._session._model_reader(), field.type, str(name))
            raw = build_model_client(field.type, config, async_=True)
            node_id = self._node.id
            client = _MeteredClient(raw, lambda: self._session._spend("plugin", node_id))
            self._clients[key] = client
        return client

    def __getitem__(self, key: str) -> Any:
        client = self.get(key)
        if client is None:
            raise KeyError(key)
        return client


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
        flow_name: str = "",
        history_start: float | None = None,
        new_from: str | None = None,
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
        - flow_name: 命名流程配置名, 持久化分组的第二维; 缺省退化为 session_id
        - history_start: 档案起点 (秒); 起点之前的消息对桶/阈值不可见 (/new 语义)
        - new_from: /new 切换时的旧会话 id; 提供则首次接管按各节点 on_new 结算
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
        self._named_flow = flow_name or ""
        self._history_start = history_start
        self._new_from = new_from
        self._new_done = new_from is None
        """/new 结算是否已完成; 直接创建 (非 /new) 的会话无需结算"""
        self._new_checked = False
        """首次 observe 是否已吸收过属性补丁 (/new 边界由 SessionManager 构造后注入)"""
        self._raw_flow = flow or {}
        self._definition: FlowDefinition | None = None
        self._issues: list[Any] = []
        self._resolved: dict[str, dict[str, Any]] = {}
        """{节点 id: 四层合并后的生效配置}"""
        self._drives: dict[str, set[str]] = {}
        """{磁贴 id: 它实际驱动的 WF 集合} (经逻辑门传播, 来自静态分析)"""
        self._workflows: dict[str, AsyncModelWorkflowFramework] = {}
        """节点 id -> WF 实例"""
        self._flow_store: FlowStore | None = None
        self._scope_key: str = ""
        self._bound_scope: Any = None
        """当前对话的冻结 MessageScope; 后台发送目标"""
        self._bound_adapter: Any = None
        self._engine: Any = None
        self._transform: Any = None
        self._resumed = False
        """重启恢复是否已跑过: 首个 observe 按已持久化位置补触发未完成批次"""
        self._tile_last_at: dict[str, float] = {}
        """{磁贴 id: 上次有触发发出时刻}; idle_seconds 按磁贴判断"""
        self._inflight: dict[tuple[str, str], int] = {}
        """{(磁贴, WF): 已下发未提交的最大 seq}; 防止 on_busy=queue 重复入队同一批"""
        self._failure_notified: dict[str, bool] = {}
        self._wake_count: dict[str, int] = {}
        self._wake_last: dict[str, float] = {}
        """{唤醒磁贴 id: 上次自定义规则触发的时刻}; 冷却用"""
        self._idle_task: Any = None
        """idle_seconds 阈值的定时检查任务 (P1-4); 消息不来也照样触发"""
        self._budget: FlowCallBudget | None = None
        self._closed = False
        self.error_feedback = True
        """跟随调度器 error_feedback 开关; 调度器每次 observe 前刷新"""
        self._model_reader_cache: Any = None
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
        self._events: deque[dict[str, Any]] = deque(maxlen=50)
        """最近 50 条触发/运行事件, 供运行状态接口; 重启即丢弃 (瞬态)"""

    async def _async_init(self) -> None:
        """解析定义, 静态检查, 按 WF 节点装配 AsyncModelWorkflowFramework"""
        if self._definition is not None:
            return
        from satrap.edictum.flow.definition import migrate_flow_definition
        self._refresh_registry()
        # 迁移: 节点实例/流程默认/连线/插件全局默认各自按版本走 (P0-7)
        # 插件升级后首次加载: 迁移 <插件名>.flow_nodes.json 并写回 (写前备份, P0-7)
        from satrap.edictum.flow.definition import _migrate_config_dict
        migrate_funcs: dict[str, Any] = {}
        plugin_names = {t.plugin for t in self._registry.all_types() if t.plugin}
        for name in plugin_names:
            impls = self._plugin_impls.get(name) or {}
            migrate = impls.get("migrate") or getattr(impls.get("Migrate"), "migrate", None)
            if callable(migrate):
                migrate_funcs[name] = migrate
        plugin_defaults: dict[str, dict[str, dict[str, Any]]] = {}
        for name in plugin_names:
            raw = self._plugin_defaults(name)
            if not raw:
                continue
            plugin_defaults[name] = raw
            if self._plugin_config is not None and name in migrate_funcs:
                def _migrate(saved: dict[str, dict[str, Any]], _name: str = name) -> dict[str, dict[str, Any]]:
                    out: dict[str, dict[str, Any]] = {}
                    for node_name, entry in saved.items():
                        type_name = f"{_name}.{node_name}"
                        node_type = self._registry.get(type_name)
                        if node_type is None:
                            out[node_name] = entry
                            continue
                        version, config = _migrate_config_dict(
                            node_type, int(entry.get("version", 1)),
                            dict(entry.get("config") or {}), migrate_funcs.get(_name), node_name,
                        )
                        out[node_name] = {"version": version, "config": config}
                    return out
                try:
                    plugin_defaults[name] = self._plugin_config.migrate_flow_nodes_global(name, _migrate)
                except Exception as error:
                    logger.warning(f"[流程会话] {name} 节点默认迁移写回失败: {error}")
        definition, migrated_defaults, _ = migrate_flow_definition(
            parse_flow_definition(self._raw_flow), self._registry,
            plugin_defaults=plugin_defaults, migrate_funcs=migrate_funcs,
        )
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
        self._refresh_resolved()
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
            # 计量包装: 每次模型调用前检查预算 (P2-1)
            node_id = node.id
            wf.llm = _MeteredClient(wf.llm, lambda nid=node_id: self._spend("wf", nid, per_run=True))
            self._track_workflow_context(wf_id, wf.ctx)
            self._workflows[node.id] = wf
            self._bind_snapshot_tools(node, wf)
            self._bind_reply_tool(node, wf)
            self._bind_handoff_tools(node, wf)
            await self._install_node_plugins(node, wf)
        await self._refresh_instances()
        self._start_idle_timer()

    def _refresh_resolved(self) -> None:
        """重算全部节点的生效配置 (四层合并) 与磁贴->WF 驱动表"""
        if self._definition is None:
            return
        from satrap.edictum.flow.definition import resolve_node_config
        resolved: dict[str, dict[str, Any]] = {}
        for node in self._definition.nodes.values():
            node_type = self._node_types.get(node.id)
            if node_type is None:
                continue
            plugin_defaults = self._plugin_defaults(node_type.plugin) if node_type.plugin else {}
            resolved[node.id] = resolve_node_config(
                node_type, node, self._definition.defaults, plugin_defaults,
            ).values
        self._resolved = resolved
        analysis = analyze(self._definition, self._registry, resolved)
        self._drives = {tile: set(wfs) for tile, wfs in analysis.drives.items()}

    def _config(self, node_id: str) -> dict[str, Any]:
        """节点生效配置 (四层合并); 未解析到时退回实例 config"""
        if node_id in self._resolved:
            return self._resolved[node_id]
        if self._definition is None:
            return {}
        if not self._resolved:
            self._refresh_resolved()
            if node_id in self._resolved:
                return self._resolved[node_id]
        node = self._definition.nodes.get(node_id)
        return dict(node.config) if node is not None else {}

    def _model_reader(self) -> Any:
        """插件模型配置读取器, 惰性创建"""
        if self._model_reader_cache is None:
            from satrap.core.framework.BackGroundManager import ModelConfigManager
            self._model_reader_cache = ModelConfigManager(auto_create=False)
        return self._model_reader_cache

    def _start_idle_timer(self) -> None:
        """按最小 idle_seconds 起后台定时检查; 消息不来时阈值也照常触发 (P1-4)"""
        if self._definition is None:
            return
        idles = [
            int(self._config(node.id).get("idle_seconds") or 0)
            for node in self._definition.nodes.values() if node.type == "threshold"
        ]
        period = min((x for x in idles if x > 0), default=0)
        if period <= 0:
            return
        interval = max(5.0, period / 2)

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._ensure_engine()
                    await self._check_thresholds()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(f"[流程会话] idle 检查异常: {error}")

        self._idle_task = asyncio.create_task(_loop(), name=f"flow-idle-{self.session_id}")

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
            ctx = self._node_ctx(node, node_type, resolved.values)
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

    def _node_ctx(self, node: FlowNodeInstance, node_type: Any, resolved: dict[str, Any]) -> Any:
        """给插件 setup/条件/transform 用的轻量 ctx; resources 按节点模型字段构造"""
        from types import SimpleNamespace
        return SimpleNamespace(
            config=resolved, node_id=node.id, session_id=self.session_id,
            diagnostic=lambda *a, **kw: None,
            resources=_NodeResources(self, node, node_type.config_schema, resolved),
            service=lambda *a, **kw: None,
        )

    def _bind_snapshot_tools(self, node: FlowNodeInstance, wf: AsyncModelWorkflowFramework) -> None:
        """
        给装了 flow_snapshot 的 WF 注入状态槽工具, 只绑它连着的状态槽

        参数:
        - node: WF 节点实例
        - wf: 已装配的工作流
        """
        plugins = [p.strip() for p in str(self._config(node.id).get("plugins") or "").splitlines() if p.strip()]
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
            max_chars = int(self._config(e.target).get("max_chars") or 6000)
            allowed[e.target] = max_chars
        self._flow_snapshot_binding = (self._flow_store, self._scope_key, allowed)
        from satrap.expend.plugins.flow_snapshot.tools import get_tools
        for tool in get_tools(self):
            wf.tools_manager.register_tool(tool)

    def _bind_reply_tool(self, node: FlowNodeInstance, wf: AsyncModelWorkflowFramework) -> None:
        """
        给连着平台回复节点的 WF 注入 flow_reply 工具 (P1-8)

        参数:
        - node: WF 节点实例
        - wf: 已装配的工作流
        """
        assert self._definition is not None
        quote = False
        connected = False
        for e in self._definition.edges:
            if e.source != node.id or e.source_port != "text":
                continue
            target = self._definition.nodes.get(e.target)
            if target is None or target.type != "output":
                continue
            connected = True
            if str(self._config(e.target).get("send_mode") or "flow_reply") == "flow_reply":
                quote = bool(self._config(e.target).get("quote", False))
        if not connected:
            return
        from satrap.edictum.flow.reply_tool import AsyncFlowReplyTool
        wf.tools_manager.register_tool(AsyncFlowReplyTool(self._reply_send, quote=quote))

    def _bind_handoff_tools(self, node: FlowNodeInstance, wf: AsyncModelWorkflowFramework) -> None:
        """
        给 WF 注入它 caller 边连着的交接工具 (tool 磁贴), 调用时触发下游 WF

        参数:
        - node: WF 节点实例
        - wf: 已装配的工作流
        """
        assert self._definition is not None
        for e in self._definition.edges:
            if e.source != node.id or e.source_port != "tool" or e.target_port != "caller":
                continue
            tile = self._definition.nodes.get(e.target)
            tile_type = self._node_types.get(e.target)
            if tile is None or tile_type is None or tile_type.role != "trigger" or tile.type != "tool":
                continue
            wf.tools_manager.register_tool(self._make_handoff_tool(node.id, e.target))

    def _make_handoff_tool(self, caller_id: str, tile_id: str) -> Any:
        """构造一个交接工具: 调用即向该磁贴发 call 事件"""
        from satrap.core.utils.TCBuilder import AsyncTool

        session = self
        tile_cfg = self._config(tile_id)
        tool_name = str(tile_cfg.get("tool_name") or f"handoff_{tile_id}")
        description = str(tile_cfg.get("tool_description") or "把任务交接给下游工作流")
        max_calls = int(tile_cfg.get("max_calls") or 1)
        tile_node = self._definition.nodes.get(tile_id)
        tile_title = tile_node.title if tile_node is not None else tile_id

        class _HandoffTool(AsyncTool):
            tool_name = tool_name
            description = description
            params_dict = {"payload": ("string", "交给下游的内容")}

            def __init__(self) -> None:
                super().__init__()
                self.config: dict[str, Any] = {}
                self._used: dict[str, int] = {}

            async def execute(self, payload: str = "") -> dict[str, Any]:
                run = current_flow_run.get()
                run_id = run[2] if run else ""
                used = self._used.get(run_id, 0)
                if used >= max_calls:
                    return {"ok": False, "error": f"每次运行最多调用 {max_calls} 次"}
                self._used[run_id] = used + 1
                from satrap.edictum.flow.engine import FlowEvent
                source_event = current_flow_event.get()
                message = None
                if source_event is not None:
                    message = session._event_to_flow_message(source_event)
                event = FlowEvent(
                    event_id=uuid.uuid4().hex, kind="call", tile=tile_id,
                    why=f"{tile_title}: 交接调用", payload=str(payload or ""),
                    source_wf=caller_id, message=message, source_event=source_event,
                )
                await session._ensure_engine()
                delivered = await session._engine.handoff(tile_id, event)
                return {"ok": delivered, "delivered": delivered,
                        "target": tile_title}

        return _HandoffTool()

    async def _install_node_plugins(self, node: FlowNodeInstance, wf: AsyncModelWorkflowFramework) -> None:
        """把 WF 节点 plugins 列表里的目录插件装进该 WF (P1-1)"""
        if self._plugin_installer is None:
            return
        plugins = [p.strip() for p in str(self._config(node.id).get("plugins") or "").splitlines() if p.strip()]
        catalog = self._plugin_catalog
        for name in plugins:
            if name == "flow_snapshot":
                continue   # 内置插件由 _bind_snapshot_tools 注入
            entry = catalog.get(name) if catalog is not None and hasattr(catalog, "get") else None
            if entry is None:
                logger.warning(f"[流程会话] WF {node.id} 配置的插件 {name} 不在目录中, 跳过安装")
                continue
            try:
                await self.install_plugin_to_workflow(node.id, str(entry.path), None)
            except Exception as error:
                logger.error(f"[流程会话] WF {node.id} 安装插件 {name} 失败: {error}")
                raise

    async def _reply_send(self, text: str, quote_trigger: bool) -> bool:
        """flow_reply 的实际发送: 本次运行有来源消息走前台, 否则走后台发送"""
        from satrap.edictum.flow.reply_tool import send_flow_reply
        source_event = current_flow_event.get()
        sender = None
        if source_event is not None:
            async def sender(text: str) -> bool:
                receipt = await source_event.send(
                    MessageChain.from_text(text), explicit_reply=not quote_trigger)
                return getattr(receipt, "status", "") == "success"
        return await send_flow_reply(
            send_scheduled=self._send_scheduled_scope,
            scope=self._bound_scope, text=text,
            foreground_sender=sender, quote_trigger=quote_trigger,
        )

    def _flow_name(self) -> str:
        """流程配置名, 用作持久化分组的第二维; 缺省退化为 session_id"""
        return self._named_flow or self.session_id

    def _resolve_llm(self, node: FlowNodeInstance, registry: FlowNodeRegistry) -> Any:
        """
        按节点 model_name 解析模型实例, 未设置时用会话默认

        参数:
        - node: WF 节点实例
        - registry: 节点类型注册表

        返回:
        - AsyncLLM: 模型实例
        """
        model_name = str(self._config(node.id).get("model_name") or "")
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

    # ---------- 消息入口 ----------

    async def observe(self, event: Any) -> None:
        """
        流程会话的消息入口: 每条入站消息都先到这里, 由内部决定唤醒谁

        参数:
        - event: 平台消息事件 (MessageEvent)
        """
        await self._ensure_initialized()
        if self._definition is None or self._closed:
            return
        event.set_extra("flow_observed", True)
        # 档案来源: 首次 observe 从事件适配器补入消息库与对话键
        self._bind_archive(event)
        # SessionManager 经属性注入的 /new 边界在构造后到达, 首个 observe 才读
        if not self._new_checked:
            self._new_checked = True
            if self._new_from is None:
                self._new_from = getattr(self, "_flow_new_from", None)
            if self._history_start is None:
                self._history_start = getattr(self, "_flow_history_start", None)
            self._new_done = self._new_from is None
        # /new 接管: 先按各节点 on_new 结算 (drain 会跑完积压批次), 再处理新消息
        if not self._new_done:
            self._new_done = True
            await self._settle_new()
        # 配置/插件变化时重建插件节点实例; 频率低, 变化才产生开销
        self._refresh_registry()
        await self._refresh_instances()
        self._refresh_resolved()
        # 重启恢复: 首个事件先补触发未完成批次 (瞬态队列丢弃, 位置未推进的批次重新触发)
        if not self._resumed:
            self._resumed = True
            await self._resume_unfinished()
        await self._handle_observed(event)

    async def observe_outbound(self, scope: Any, snapshot: Any) -> None:
        """
        已确认的出站消息入口: 由适配器档案采集回调, 让阈值按最新消息位置推进

        参数:
        - scope: 对话身份 (MessageScope)
        - snapshot: 已入档的消息 (ArchiveMessage)
        """
        if self._closed or self._definition is None:
            return
        try:
            if self._scope_key != scope.key:
                self._scope_key = scope.key
                self._bound_scope = scope
                if self._flow_store is not None:
                    self._flow_store.scope_key = scope.key
                    self._flow_store.adapter_id = scope.adapter_id
            await self._ensure_initialized()
            await self._ensure_engine()
            await self._check_thresholds()
        except Exception as error:
            logger.warning(f"[流程会话] 出站观察处理失败: {error}")

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

    async def _settle_new(self) -> None:
        """
        /new 结算: 按各节点 on_new 处理持久化状态与位置

        状态槽 on_new=clear 清回版本 0; 阈值磁贴 on_new=drop 直接把每个
        驱动 WF 的已提交位置跳到最新 seq; drain 先按已提交位置把每个
        落后 WF 的积压批次跑完 (受 max_batch 限制, 120 秒上限), 再让
        档案起点生效
        """
        if self._definition is None or self._flow_store is None:
            return
        record_debug("flow_new", "start", metadata={"session": self.session_id, "from": self._new_from})
        latest = await asyncio.to_thread(
            self._message_store.latest_seq_by_scope_key, self._scope_key) if self._message_store else 0
        # Step.1 状态槽: clear 的清回版本 0
        for node in self._definition.nodes.values():
            if node.type != "state":
                continue
            if str(self._config(node.id).get("on_new") or "keep") == "clear":
                await asyncio.to_thread(
                    self._flow_store.clear_state_by_scope_key, self._scope_key, node.id)
        # Step.2 阈值磁贴: drain 先跑完积压 (带 120 秒上限), drop 直接跳最新
        for node in self._definition.nodes.values():
            if node.type != "threshold":
                continue
            mode = str(self._config(node.id).get("on_new") or "drop")
            wf_ids = self._threshold_wfs(node.id)
            positions = await asyncio.to_thread(
                self._flow_store.positions_for_tile_by_scope_key, self._scope_key, node.id)
            if mode == "drain" and self._message_store is not None:
                await self._drain_threshold(node, wf_ids, positions, latest)
            elif mode == "drop":
                for wf_id in wf_ids:
                    await asyncio.to_thread(
                        self._flow_store.drop_position_by_scope_key,
                        self._scope_key, node.id, wf_id, latest)
        record_debug("flow_new", "end", metadata={"session": self.session_id})

    async def _drain_threshold(
        self, node: FlowNodeInstance, wf_ids: list[str], positions: dict[str, int], latest: int,
    ) -> None:
        """
        drain: 落后的 WF 各按 max_batch 把积压批次跑完, 超过 120 秒的按 drop 结算

        参数:
        - node: 阈值磁贴节点
        - wf_ids: 该磁贴驱动的 WF
        - positions: {wf_id: 已提交位置}
        - latest: 档案当前最新 seq
        """
        deadline = time.monotonic() + 120.0
        await self._ensure_engine()
        for wf_id in wf_ids:
            committed = positions.get(wf_id, 0)
            while committed < latest:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 超时按 drop 结算并写诊断
                    await asyncio.to_thread(
                        self._flow_store.drop_position_by_scope_key,
                        self._scope_key, node.id, wf_id, latest)
                    record_debug("flow_new", "drain_timeout", metadata={
                        "tile": node.id, "wf": wf_id, "dropped_to": latest})
                    logger.warning(f"[流程会话] {node.title} 对 {wf_id} 的 /new drain 超过 120 秒, 按 drop 跳到最新")
                    break
                pending = await asyncio.to_thread(
                    self._message_store.after_by_scope_key,
                    self._scope_key, committed,
                    int(self._config(node.id).get("max_batch") or 200),
                    received_from=None,
                )
                if not pending:
                    break
                from satrap.edictum.flow.engine import FlowEvent
                event = FlowEvent(
                    event_id=uuid.uuid4().hex, kind="batch", tile=node.id,
                    why=f"/new drain: {node.title}",
                    batch=tuple(self._to_flow_message(m) for m in pending),
                    batch_from=committed, batch_to=pending[-1]["seq"],
                )
                try:
                    # 单次运行也受 120 秒总上限约束
                    await asyncio.wait_for(self._run_workflow(wf_id, event), timeout=remaining)
                except (asyncio.TimeoutError, Exception) as error:
                    logger.warning(f"[流程会话] drain 运行失败 {wf_id}: {error}")
                committed = await asyncio.to_thread(
                    lambda: self._flow_store.positions_for_tile_by_scope_key(
                        self._scope_key, node.id).get(wf_id, committed))
                if committed <= event.batch_from:
                    # 位置没推进 (运行失败) 按 drop 结算剩余
                    await asyncio.to_thread(
                        self._flow_store.drop_position_by_scope_key,
                        self._scope_key, node.id, wf_id, latest)
                    break

    async def _ensure_engine(self) -> None:
        """触发引擎惰性装配 (恢复与常规 observe 共用)"""
        if self._engine is not None:
            return
        from satrap.edictum.flow.engine import ConditionEvaluator, TriggerEngine
        self._engine = TriggerEngine(
            self._definition,
            self._node_types,
            evaluate_condition=ConditionEvaluator(release_ref=self._instances.release_ref),
            resolve_state=lambda sid: self._state_view(sid),
            calls_used=self._calls_used,
            resolve_condition=lambda node: self._instances.acquire(node.id),
            release_ref=self._instances.release_ref,
            resolve_config=self._config,
        )
        self._engine.on_trigger(self._on_wf_trigger)

    def _calls_used(self, kind: str, scope: str, window_seconds: float, target: str) -> int:
        """条件节点 ctx.calls() 的实际查询: 按参数过滤调用计数"""
        if self._budget is None:
            return 0
        try:
            return self._budget.calls(
                kind="wf" if kind == "wf" else "all",
                scope="target" if scope == "target" else "flow",
                target=target,
                window_seconds=window_seconds or 3600,
            )
        except Exception:
            return 0

    def _spend(self, source: str, node_id: str, *, per_run: bool = False) -> None:
        """一次模型调用前的原子扣减; 从 contextvar 取本次运行的归属"""
        if self._budget is None:
            return
        run = current_flow_run.get()
        event_id, run_id = "", ""
        if run is not None:
            event_id = run[1]
            if source == "wf" and run[0] == node_id:
                run_id = run[2]
        limit = int(self._config(node_id).get("max_calls_per_run") or 0) if per_run else 0
        self._budget.check_and_spend(
            source=source, node_id=node_id, event_id=event_id,
            run_id=run_id, per_run_limit=limit,
        )

    def _bind_archive(self, event: Any) -> None:
        """从事件适配器补入消息档案库与 scope 键; 已注入或已绑定时跳过"""
        adapter = getattr(event, "adapter", None)
        if adapter is not None:
            if adapter is not self._bound_adapter:
                self._bound_adapter = adapter
                # 出站已确认消息经档案采集回调也走 observe (合同 10.1 的发送证据)
                observers = getattr(adapter, "_flow_observers", None)
                if observers is None:
                    observers = set()
                    adapter._flow_observers = observers
                observers.add(self.observe_outbound)
        if self._message_store is not None and self._scope_key != self.session_id:
            return
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
            self._bound_scope = scope
            if self._flow_store is not None:
                self._flow_store.scope_key = scope.key
                self._flow_store.adapter_id = scope.adapter_id

    async def _handle_observed(self, event: Any) -> None:
        """分发一条已观察的消息给触发引擎 (唤醒/阈值判定由引擎做)"""
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
            decision_triggered = decision is None or decision.triggered
        else:
            decision_triggered = decision is not None and decision.triggered
        for node in self._definition.nodes.values():
            if node.type != "wake":
                continue
            cfg = self._config(node.id)
            if cfg.get("use_platform_policy", True):
                if not decision_triggered:
                    continue
            else:
                # 自定义唤醒规则不走平台判定: 自定义规则可以扩大唤醒范围 (P1-15)
                if not self._wake_custom_hit(node, event):
                    continue
            from satrap.edictum.flow.engine import FlowEvent
            msg = self._event_to_flow_message(event)
            await self._engine.fire(
                node.id,
                FlowEvent(event_id=uuid.uuid4().hex, kind="message", tile=node.id,
                          why=f"唤醒: {getattr(decision, 'reason', 'custom')}", message=msg,
                          source_event=event),
            )

    def _event_to_flow_message(self, event: Any) -> Any:
        """把平台消息事件转成 FlowMessage; mentions_bot 用平台解析结果而不是裸扫 @"""
        from satrap.edictum.flow.types import FlowMessage
        text = event.get_message_str() or ""
        mentions = False
        try:
            from satrap.core.pipeline.wake_policy import is_self_mention
            mentions = any(
                is_self_mention(c, event.call_origin.self_id) for c in event.get_messages()
            )
        except Exception:
            mentions = "@" in text
        return FlowMessage(
            message_id=str(event.call_origin.source_message_id or ""),
            position=0,
            sender_id=str(event.call_origin.actor_id or ""),
            nickname=str(getattr(getattr(event, "sender", None), "nickname", "") or ""),
            text=text,
            is_bot=False,
            mentions_bot=mentions,
            time=event.platform_message.received_at,
        )

    async def _on_wf_trigger(self, wf_id: str, event: Any) -> None:
        """WF 被触发: 拉取输入并运行, 结果按 send_mode 发送"""
        await self._run_workflow(wf_id, event)

    async def _run_workflow(self, wf_id: str, event: Any) -> None:
        """执行一次 WF 运行; 状态槽提交/位置推进/失败处理按运行时契约"""
        run_id = uuid.uuid4().hex
        self._events.append({"at": time.time(), "kind": "run_start",
                             "wf": wf_id, "run_id": run_id, "event_id": event.event_id, "why": event.why})
        record_debug("flow_run", "start", metadata={"wf": wf_id, "run_id": run_id, "event_id": event.event_id})
        tile = event.tile if event.kind == "batch" else ""
        if tile:
            self._inflight[(tile, wf_id)] = event.batch_to
        # 运行开始时的状态槽版本快照: CAS 要对比这个起点, 不是运行结束时的版本
        base_versions = self._snapshot_base_versions(wf_id)
        try:
            wf = self._workflows[wf_id]
            # 拉取输入 (P1-7 的共享求值在事件内生效)
            pulled = await self._pull_inputs(wf_id, run_id, event)
            run_token = current_flow_run.set((wf_id, event.event_id, run_id))
            event_token = current_flow_event.set(getattr(event, "source_event", None))
            wf_token = current_flow_workflow.set(wf)
            try:
                source_event = getattr(event, "source_event", None)
                if source_event is not None:
                    # 前台运行: 绑定来源消息的回复轮次与调用身份 (P1-11)
                    from satrap.core.group_chat.reply import bind_reply_turn
                    with bind_reply_turn(source_event), bind_call_origin(source_event.call_origin):
                        output = await self._wf_run(wf, wf_id, pulled, event)
                else:
                    with bind_call_origin(None):
                        output = await self._wf_run(wf, wf_id, pulled, event)
            finally:
                current_flow_workflow.reset(wf_token)
                current_flow_event.reset(event_token)
                current_flow_run.reset(run_token)
            # 状态槽提交与阈值位置推进 (同事务); CAS 用运行开始时的版本
            commits = self._pending_commits(wf_id, event, run_id, base_versions)
            if commits or tile:
                committed_seq = await asyncio.to_thread(
                    self._flow_store.commit_states_by_scope_key,
                    self._scope_key, tile, wf_id, event.batch_to, commits,
                )
                for commit in commits:
                    record_debug("flow_state", "commit", metadata={
                        "state": commit.state_id, "version": commit.base_version + 1,
                        "covers_to": commit.covers_to, "run_id": run_id})
            # 发送: send_mode 决定前台回复或后台发送
            await self._send_output(wf_id, output or "", event)
            # done 事件
            await self._engine.complete(wf_id, event, output or "")
            self._events.append({"at": time.time(), "kind": "run_end", "wf": wf_id,
                                 "run_id": run_id, "status": "ok"})
            record_debug("flow_run", "end", metadata={"wf": wf_id, "run_id": run_id, "status": "ok"})
            self._failure_notified.pop(wf_id, None)
        except Exception as error:
            self._discard_pending(wf_id)
            self._events.append({"at": time.time(), "kind": "run_end", "wf": wf_id,
                                 "run_id": run_id, "status": "failed", "error": str(error)[:200]})
            record_debug("flow_run", "end", metadata={"wf": wf_id, "run_id": run_id, "status": "failed"})
            logger.error(f"[流程会话] 流程 {self._flow_name()} 节点 {wf_id} 运行失败 "
                         f"run={run_id} scope={self._scope_key}: {error}")
            if isinstance(error, BudgetExceeded):
                record_debug("flow_budget", "exceeded", metadata={
                    "wf": wf_id, "run_id": run_id, "error": str(error)[:200]})
            await self._notify_failure(wf_id, str(error))
        finally:
            if tile:
                # 本次批次结束: 位置未推进时 inflight 回落到 committed (允许重试);
                # 推进成功时位置已覆盖, inflight 同步清掉;
                # last_batch_to 同步回落, 失败批次的"在飞上限"不再阻止重试
                committed = await asyncio.to_thread(
                    lambda: self._flow_store.positions_for_tile_by_scope_key(
                        self._scope_key, tile).get(wf_id, 0))
                self._inflight[(tile, wf_id)] = committed
                state = self._engine._wf.get(wf_id) if self._engine is not None else None
                if state is not None:
                    state.last_batch_to = max(committed, state.last_batch_to)
            if self._budget is not None:
                self._budget.end_run(run_id)
            if self._transform is not None:
                self._transform.forget_run(run_id)
            next_event = await self._engine.finish(wf_id, event)
            if next_event is not None:
                await self._on_wf_trigger(wf_id, next_event)

    async def _wf_run(self, wf: Any, wf_id: str, pulled: list[Any], event: Any) -> str:
        """运行一次 full_agent; 包装便于前后台路径共用"""
        return await wf.full_agent(
            user_input=self._compose_input(wf_id, pulled, event),
            callback=False,
            max_iterations=int(self._config(wf_id).get("max_steps") or 8),
        )

    def _snapshot_base_versions(self, wf_id: str) -> dict[str, int]:
        """记录运行开始时各状态槽的版本, CAS 对比这个起点"""
        if self._flow_store is None or self._definition is None:
            return {}
        out: dict[str, int] = {}
        for e in self._definition.edges:
            if e.source != wf_id or e.source_port != "text":
                continue
            target = self._definition.nodes.get(e.target)
            if target is None or target.type != "state" or e.target_port != "write":
                continue
            try:
                row = self._flow_store.state_by_scope_key(self._scope_key, e.target)
                out[e.target] = int(row["version"])
            except Exception:
                out[e.target] = 0
        return out

    def _discard_pending(self, wf_id: str) -> None:
        """失败运行不留待提交内容: 清空 snapshot_write 的 _pending"""
        wf = self._workflows.get(wf_id)
        if wf is None:
            return
        for tool in wf.tools_manager.tools.values():
            pending = getattr(tool, "pending", None)
            clear = getattr(tool, "clear", None)
            if callable(pending) and callable(clear):
                try:
                    clear()
                except Exception:
                    pass

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
                resolve_source=self._source_messages,
                resolve_config=self._config,
                build_ctx=lambda node, resolved: self._node_ctx(
                    node, self._node_types.get(node.id), resolved),
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

    def _source_messages(self) -> tuple[Any, ...]:
        """source 节点供给: 档案视图 (同步, 由 transform 在求值任务里调用)"""
        if self._message_store is None or not self._scope_key:
            return ()
        try:
            rows = self._message_store.after_by_scope_key(
                self._scope_key, 0, limit=500, received_from=self._history_start,
            )
        except Exception as error:
            logger.warning(f"[流程会话] source 读取档案失败: {error}")
            return ()
        return tuple(self._to_flow_message(m) for m in rows)

    def _compose_input(self, wf_id: str, pulled: list[Any], event: Any) -> str:
        """把拉取结果和触发负载拼成 WF 的 user_input; writer_reads_previous 自动带上上一版"""
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
        # 写入方读取上一版: 该 WF 写的每个开了开关的状态槽自动带上一版内容
        if self._definition is not None:
            for e in self._definition.edges:
                if e.source != wf_id or e.source_port != "text":
                    continue
                target = self._definition.nodes.get(e.target)
                if target is None or target.type != "state" or e.target_port != "write":
                    continue
                if not self._config(e.target).get("writer_reads_previous", True):
                    continue
                view = self._state_view(e.target)
                if view.content:
                    parts.append(f"上一版快照 ({e.target}):\n{view.content}")
        return "\n\n".join(parts) or "(空)"

    def _pending_commits(
        self, wf_id: str, event: Any, run_id: str, base_versions: dict[str, int],
    ) -> list[Any]:
        """收集 snapshot_write 的待提交内容, 转成 StateCommit; CAS 起点用运行开始时的版本"""
        from satrap.edictum.flow.store import StateCommit
        commits: list[StateCommit] = []
        for tool in self._workflows[wf_id].tools_manager.tools.values():
            pending = getattr(tool, "pending", None)
            if not callable(pending):
                continue
            for state_id, content in pending().items():
                covers_to = (event.batch_to if event.kind == "batch"
                             else self._flow_store.state_by_scope_key(self._scope_key, state_id)["covers_to"])
                commits.append(StateCommit(
                    state_id=state_id, content=content,
                    base_version=base_versions.get(state_id, 0),
                    covers_to=covers_to, run_id=run_id))
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
            send_mode = str(self._config(e.target).get("send_mode") or "flow_reply")
            if send_mode == "final_text" and output:
                await self._send_scheduled_scope(self._bound_scope, output)
            # flow_reply: 模型已通过工具发过了, 不做兜底
            # group_chat_reply: 由 group_chat 插件自己发, 不干预

    async def _send_scheduled_scope(self, scope: Any, text: str) -> Any:
        """
        后台发送 (无来源消息路径): 经适配器 ScheduledTarget 通道, 证据落 flow_send_log

        参数:
        - scope: 冻结的 MessageScope
        - text: 发送文本

        返回:
        - SendReceipt; 通道不可用或目标非法时返回 failed
        """
        adapter = self._bound_adapter
        if adapter is None or scope is None:
            return SendReceipt("failed", reason="no_send_channel")
        supported = (adapter.supports_scheduled_group_send if scope.conversation_kind == "group"
                     else adapter.supports_scheduled_private_send)
        if not supported:
            return SendReceipt("failed", reason="scheduled_send_unsupported")
        sender = getattr(adapter, "group_chat_send_scheduled", None)
        if not callable(sender):
            return SendReceipt("failed", reason="scheduled_send_unsupported")
        database = getattr(self._message_store, "database", None)
        if database is None:
            return SendReceipt("failed", reason="no_send_log_db")
        from satrap.core.platform.scheduled import ScheduledTarget
        from satrap.edictum.flow.reply_tool import FlowSendRecorder
        recorder = FlowSendRecorder(database, scope.key)
        connection = adapter.group_chat_connection_token()
        session = self

        async def guard() -> bool:
            """每段网络 I/O 前复核适配器身份, 连接代次与会话存活"""
            return (
                self._bound_adapter is adapter
                and adapter.group_chat_connection_token() == connection
                and bool(adapter.config.enable)
                and not session._closed
            )

        target = ScheduledTarget(
            scope=scope, connection_token=connection, policy_revision="",
            reminder_id=f"flow-{self._flow_name()}", attempt_id=recorder.attempt_id,
            guard=guard,
        )
        return await sender(target, MessageChain.from_text(text), recorder)

    def _wake_custom_hit(self, node: FlowNodeInstance, event: Any) -> bool:
        """自定义唤醒规则: @ / 唤醒词 / 新消息数, 任一满足; 冷却独立生效"""
        text = event.get_message_str() or ""
        config = self._config(node.id)
        now = time.monotonic()
        hit = False
        if config.get("mention", True):
            try:
                from satrap.core.pipeline.wake_policy import is_self_mention
                hit = any(is_self_mention(c, event.call_origin.self_id)
                          for c in event.get_messages())
            except Exception:
                hit = "@" in text
        if not hit:
            words = [x.strip() for x in str(config.get("wake_words") or "").splitlines() if x.strip()]
            hit = any(word in text for word in words)
        new_messages = int(config.get("new_messages") or 0)
        if not hit and new_messages > 0:
            count = self._wake_count.setdefault(node.id, 0) + 1
            if count >= new_messages:
                hit = True
        if not hit:
            return False
        # 冷却: 上次自定义触发后 cooldown 秒内不再触发
        cooldown = float(config.get("cooldown") or 0)
        last = self._wake_last.get(node.id, 0.0)
        if cooldown > 0 and now - last < cooldown:
            return False
        self._wake_last[node.id] = now
        self._wake_count[node.id] = 0
        return True

    async def _check_thresholds(self) -> None:
        """按 seq 检查阈值磁贴, 每个 WF 只看自己位置之后的消息, 只触发满足的 WF"""
        if self._message_store is None or not self._scope_key or self._flow_store is None:
            return
        for node in self._definition.nodes.values():
            if node.type != "threshold":
                continue
            wf_ids = self._threshold_wfs(node.id)
            if self._flow_store.scope_key != self._scope_key:
                self._flow_store.scope_key = self._scope_key
            positions = await asyncio.to_thread(
                self._flow_store.ensure_positions_for_tile, node.id, wf_ids)
            config = self._config(node.id)
            # 首个有消息的 seq: 新驱动的 WF 从桶末尾开始, 不回放历史
            # 但会话首次接管时桶里可能已有消息 (重启/首轮), 这些消息属于"积压"
            for wf_id, committed in positions.items():
                # 已下发未提交的批次不再重复入队: inflight 阻止运行中同一批再入队;
                # 失败/结束后 inflight 清掉, 位置未推进的按 committed 重新读批
                floor = max(committed, self._inflight.get((node.id, wf_id), committed))
                pending = await asyncio.to_thread(
                    self._message_store.after_by_scope_key,
                    self._scope_key, floor,
                    int(config.get("max_batch") or 200),
                    received_from=self._history_start,
                )
                hit = self._threshold_hit(node.id, config, committed, pending)
                if not hit:
                    continue
                batch_to = pending[-1]["seq"] if pending else committed
                # committed 位置没推进说明上次运行失败: busy 仍挂说明上次任务没走到
                # finish (测试桩/崩溃路径), 复位 busy 让批次能重试
                state = self._engine._wf.get(wf_id)
                if state is not None and state.busy and committed < batch_to:
                    state.busy = False
                    state.last_batch_to = committed
                from satrap.edictum.flow.engine import FlowEvent
                event = FlowEvent(
                    event_id=uuid.uuid4().hex, kind="batch", tile=node.id,
                    why=f"{node.title}: {hit}",
                    batch=tuple(self._to_flow_message(m) for m in pending),
                    batch_from=committed, batch_to=batch_to,
                )
                # 只触发这一个 WF; targets 限定传播终点 (经逻辑门也一样);
                # 忙碌时按 on_busy 入队/合并/丢弃, 不重复记 inflight
                await self._engine.fire(node.id, event, targets={wf_id})
                self._tile_last_at[node.id] = time.time()

    def _threshold_wfs(self, tile_id: str) -> list[str]:
        """一个阈值磁贴 event 输出实际驱动的 WF 节点 id 列表 (经逻辑门传播, 来自静态分析)"""
        if self._definition is None:
            return []
        if not self._resolved:
            self._refresh_resolved()
        return sorted(self._drives.get(tile_id, ()))

    def _threshold_hit(
        self, tile_id: str, config: dict[str, Any], committed: int, pending: list[Any],
    ) -> str | None:
        """按消息数/字符数/闲置时间判定是否触发"""
        count = len(pending)
        chars = sum(len(str(m.get("text") or "")) for m in pending)
        if count <= 0:
            return None
        if int(config.get("messages") or 0) > 0 and count >= int(config["messages"]):
            return f"{count} 条 ≥ {config['messages']}"
        if int(config.get("chars") or 0) > 0 and chars >= int(config["chars"]):
            return f"{chars} 字 ≥ {config['chars']}"
        if int(config.get("idle_seconds") or 0) > 0 and count >= max(1, int(config.get("min_messages") or 1)):
            last = self._tile_last_at.get(tile_id, 0.0)
            if time.time() - last >= int(config["idle_seconds"]):
                return f"距上次触发 {int((time.time() - last) / 60)} 分钟"
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
        """
        按运行时契约 10.1: 同一 WF 连续失败只告知一次, 成功后复位;
        前台失败回复到来源消息, 后台走发送通道; 跟随 error_feedback 开关
        """
        if self._failure_notified.get(wf_id):
            return
        self._failure_notified[wf_id] = True
        node = self._definition.nodes.get(wf_id)
        title = node.title if node is not None else wf_id
        logger.warning(f"[流程会话] {title} 本次运行失败: {reason[:120]}")
        if not self.error_feedback:
            return
        text = f"{title} 本次运行失败: {reason[:120]}"
        try:
            source_event = current_flow_event.get()
            if source_event is not None:
                await source_event.send(MessageChain.from_text(text), purpose="error_feedback")
            else:
                await self._send_scheduled_scope(self._bound_scope, text)
        except Exception as error:
            logger.warning(f"[流程会话] 失败告知发送失败: {error}")

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
        """会话释放: 停 idle 任务, 取消运行中的 WF, 关闭全部插件节点实例"""
        self._closed = True
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        if self._engine is not None:
            try:
                await self._engine.shutdown()
            except Exception as error:
                logger.warning(f"[流程会话] 引擎关闭异常: {error}")
        await self._instances.close_all()

    def runtime_status(self) -> dict[str, Any]:
        """
        流程会话运行状态: 阈值位置与积压, 状态槽版本, 正在运行的 WF

        返回:
        - dict: {flow_name, scope_key, tiles, states, running_wfs, node_status};
          未绑档案或未初始化时尽力返回已持久化部分
        """
        out: dict[str, Any] = {
            "flow_name": self._flow_name(),
            "session_id": self.session_id,
            "scope_key": self._scope_key,
            "history_start": self._history_start,
            "tiles": {},
            "states": {},
            "running_wfs": [],
            "events": list(self._events),
            "node_status": self._instances.status(),
        }
        if self._engine is not None:
            out["running_wfs"] = self._engine.running_workflows()
        if self._flow_store is None or self._definition is None:
            return out
        # 阈值磁贴: 每个 WF 的已提交位置与积压数
        positions = self._flow_store.positions_for_scope_key(self._scope_key)
        latest = self._message_store.latest_seq_by_scope_key(self._scope_key) if self._message_store else 0
        tiles: dict[str, dict[str, Any]] = {}
        for (tile_id, wf_id), committed in positions.items():
            entry = tiles.setdefault(tile_id, {"title": self._title_of(tile_id), "wfs": {}})
            entry["wfs"][wf_id] = {"committed": committed, "backlog": max(0, latest - committed)}
        out["tiles"] = tiles
        # 状态槽
        out["states"] = self._flow_store.states_for_scope_key(self._scope_key)
        return out

    def _title_of(self, node_id: str) -> str:
        """节点显示名"""
        node = self._definition.nodes.get(node_id) if self._definition else None
        return node.title if node is not None else node_id

    @property
    def issues(self) -> list[Any]:
        """静态检查发现的问题列表"""
        return self._issues
