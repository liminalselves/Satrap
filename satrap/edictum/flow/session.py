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
        self._definition = definition

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
        # P1-5 的触发引擎接管实际的磁贴判定; 这里只保证每条消息都被会话看到
        if self._definition is None:
            return
        # 被 @ 的消息通过现有回复链路唤醒前台 WF 是 P1-5 的事; 当前先把消息交给
        # 触发引擎前的事件总线 (P1-5 实现后替换为真实分发)
        event.set_extra("flow_observed", True)

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
