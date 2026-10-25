"""
插件专用注册表与受控调用

宿主接口绑定插件安装代次, 调用作用域绑定当前实际入口;
服务不进入模型工具列表, 所有子调用保留原始用户与领域审批来源
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast
from functools import wraps
import inspect
import weakref
import traceback

from satrap.core.plugin_interop.contracts import (
    CapabilityKind, HandlerInfo, InteropSpec, ServiceValue, PluginInfo, PluginInteractionError, SkillInfo, copy_data,
)
from satrap.core.plugin_interop.environment import EnvironmentSnapshot, environment_snapshot
from satrap.core.plugin_interop.service import AsyncService, Service

from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession
    from satrap.edictum.plugin import Plugin
    from satrap.core.framework.command.async_ import AsyncCommandHandler
    from satrap.core.framework.command.sync import CommandHandler
    from satrap.core.plugin_interop.skills import SkillHostAccess
    from satrap.core.plugin_interop.artifacts import ArtifactAccess
    from satrap.core.plugin_interop.approvals import ApprovalAccess
    from satrap.core.plugin_interop.debug_logging import DebugLoggingAccess

    SessionType = SimpleSession | AsyncSimpleSession


@dataclass
class _Invocation:
    """当前真实插件调用步骤, 结束后撤销复制出去的作用域"""

    runtime: PluginInterop
    plugin: str
    kind: CapabilityKind
    name: str
    parent: _Invocation | None
    token: object
    revision: int
    active: bool = True

    def is_current(self) -> bool:
        """
        检查入口安装代次与独立状态, 不要求当前步骤尚未返回

        返回:
        - 原入口仍属于同一有效安装时为 True
        """
        binding = self.runtime.bindings.get(self.plugin)
        return (binding is not None and binding.token is self.token
                and binding.reference_revision == self.revision
                and self.runtime.capability_available(self.plugin, self.kind, self.name))


_INVOCATION: ContextVar[_Invocation | None] = ContextVar("satrap_plugin_invocation", default=None)


_CapabilityKey = tuple[str, CapabilityKind, str]
_RelationKey = tuple[str, CapabilityKind | None, str]
_ReplacementEdge = tuple[_CapabilityKey, _CapabilityKey]


@dataclass(frozen=True)
class _RelationNode:
    """目标配置中的结构状态, 不含发话者或审批权限"""

    enabled: bool
    spec: InteropSpec
    capabilities: Mapping[CapabilityKind, Mapping[str, bool]]


class _RelationView:
    """只读计算依赖和替代, 不修改运行实例或用户开关"""

    def __init__(self, nodes: Mapping[str, _RelationNode], allowed: set[_ReplacementEdge] | None = None) -> None:
        """
        构建一次目标状态的关系视图

        参数:
        - nodes: 同一目标配置的插件与独立状态
        - allowed: 增量保护基线允许保留的旧替代边, None 表示全部声明
        """
        self.nodes = nodes
        self.replacements: dict[_CapabilityKey, list[_CapabilityKey]] = {}
        self._cache: dict[_RelationKey, bool] = {}
        self._evaluating: set[_RelationKey] = set()
        for owner, node in nodes.items():
            for provider, groups in node.spec.replaces.items():
                for kind, pairs in groups.items():
                    own = node.capabilities.get(kind, {})
                    for target, implementation in pairs.items():
                        if implementation not in own:
                            raise ValueError(f"替代者未实际注册: {owner}.{kind}.{implementation}")
                        if provider not in nodes:
                            continue
                        if target not in nodes[provider].capabilities.get(kind, {}):
                            raise ValueError(f"替代目标不存在: {provider}.{kind}.{target}")
                        source_key: _CapabilityKey = (owner, kind, implementation)
                        target_key: _CapabilityKey = (provider, kind, target)
                        if allowed is None or (target_key, source_key) in allowed:
                            self.replacements.setdefault(target_key, []).append(source_key)

    def _neighbors(self, key: _RelationKey) -> list[_RelationKey]:
        """
        枚举完整依赖和替代图的前置节点

        参数:
        - key: 插件节点或具体能力节点

        返回:
        - 前置节点, 不根据当前开关隐藏声明环路
        """
        plugin, kind, name = key
        node = self.nodes.get(plugin)
        if node is None:
            return []
        if kind is not None:
            return [(plugin, None, ""), *self.replacements.get((plugin, kind, name), [])]
        result: list[_RelationKey] = []
        for provider, dependency in node.spec.requires.items():
            if dependency.optional:
                continue
            result.append((provider, None, ""))
            for category, names in dependency.capabilities.items():
                result.extend((provider, category, capability) for capability in names)
        return result

    def validate(self) -> None:
        """拒绝合并图环路, 多个生效替代者及排斥冲突"""
        visited: set[_RelationKey] = set()
        visiting: set[_RelationKey] = set()

        def visit(key: _RelationKey) -> None:
            """
            检查一个关系节点的完整前置链

            参数:
            - key: 待检查节点
            """
            if key in visiting:
                raise PluginInteractionError("interaction_cycle", f"插件依赖或替代形成循环: {key}")
            if key in visited:
                return
            visiting.add(key)
            for neighbor in self._neighbors(key):
                visit(neighbor)
            visiting.remove(key)
            visited.add(key)

        for name, node in self.nodes.items():
            visit((name, None, ""))
            for kind, states in node.capabilities.items():
                for capability in states:
                    visit((name, kind, capability))
        for target, implementations in self.replacements.items():
            if sum(self.value(source) for source in implementations) > 1:
                raise PluginInteractionError("replacement_conflict", f"同一能力存在多个生效替代者: {target}")
        for owner, node in self.nodes.items():
            for provider, groups in node.spec.conflicts.items():
                other = self.nodes.get(provider)
                if other is None:
                    continue
                if groups is None:
                    if node.enabled and other.enabled:
                        raise PluginInteractionError("plugin_conflict", f"插件互斥: {owner}, {provider}")
                else:
                    for kind, names in groups.items():
                        for name in names:
                            if name not in other.capabilities.get(kind, {}):
                                raise ValueError(f"排斥目标不存在: {provider}.{kind}.{name}")
                            if node.enabled and self.value((provider, kind, name)):
                                raise PluginInteractionError("plugin_conflict", f"插件能力互斥: {owner}, {provider}.{kind}.{name}")

    def dependency_value(self, provider: str, kind: CapabilityKind | None = None, name: str = "") -> bool:
        """
        计算一项依赖的可访问状态

        参数:
        - provider: 提供者名称
        - kind: None 表示插件整体依赖
        - name: 具体能力名称

        返回:
        - 插件或已导出能力实际可用时为 True
        """
        node = self.nodes.get(provider)
        return (node is not None and (kind is None or name in node.spec.exports.get(kind, ()))
                and self.value((provider, kind, name)))

    def value(self, key: _RelationKey) -> bool:
        """
        计算单个结构状态并在当前视图内复用

        参数:
        - key: 插件节点或能力节点

        返回:
        - 满足开关, 必需依赖及替代规则时为 True
        """
        if key in self._cache:
            return self._cache[key]
        if key in self._evaluating:
            raise PluginInteractionError("interaction_cycle", f"插件关系形成循环: {key}")
        self._evaluating.add(key)
        try:
            plugin, kind, name = key
            node = self.nodes.get(plugin)
            result = node is not None and node.enabled
            if result and node is not None:
                if kind is None:
                    for provider, dependency in node.spec.requires.items():
                        if dependency.optional:
                            continue
                        result = result and self.dependency_value(provider)
                        for category, names in dependency.capabilities.items():
                            result = result and all(self.dependency_value(provider, category, capability) for capability in names)
                else:
                    result = bool(node.capabilities.get(kind, {}).get(name, False)) and self.value((plugin, None, ""))
                    if result:
                        result = not any(self.value(source) for source in self.replacements.get((plugin, kind, name), []))
            self._cache[key] = result
            return result
        finally:
            self._evaluating.remove(key)

    def effective_edges(self) -> set[_ReplacementEdge]:
        """
        取得旧状态中真正压制已具备基础条件目标的替代边

        返回:
        - 增量保护允许保留的旧影响, 已关闭目标不计入旧影响
        """
        result: set[_ReplacementEdge] = set()
        for target, implementations in self.replacements.items():
            plugin, kind, name = target
            node = self.nodes[plugin]
            if not node.capabilities.get(kind, {}).get(name, False) or not self.value((plugin, None, "")):
                continue
            for source in implementations:
                if self.value(source):
                    result.add((target, source))
        return result


def _validate_relation_target(
    nodes: Mapping[str, _RelationNode], before_nodes: Mapping[str, _RelationNode],
    activating: set[str] | None = None,
) -> _RelationView:
    """
    统一校验只读预览与实际配置提交的关系

    参数:
    - nodes: 最终目标配置
    - before_nodes: 同代变更前结构状态
    - activating: 本次新增或重新启用的插件, 默认空集合

    返回:
    - 已通过关系与依赖保护检查的视图
    """
    before = _RelationView(before_nodes)
    after = _RelationView(nodes)
    after.validate()
    baseline = _RelationView(nodes, before.effective_edges())
    without_replacements = _RelationView(nodes, set())
    for owner, node in nodes.items():
        if not node.enabled:
            continue
        for provider, dependency in node.spec.requires.items():
            if dependency.optional:
                continue
            targets: list[tuple[CapabilityKind | None, str]] = [(None, "")]
            targets.extend((kind, name) for kind, names in dependency.capabilities.items() for name in names)
            for kind, name in targets:
                available = after.dependency_value(provider, kind, name)
                if not available and baseline.dependency_value(provider, kind, name):
                    raise PluginInteractionError("replacement_breaks_dependency",
                        f"替代破坏必需依赖: {owner} -> {provider}.{kind or ''}.{name}")
                if not available and owner in (activating or set()):
                    code = "replacement_breaks_dependency" if without_replacements.dependency_value(provider, kind, name) else "dependency_unavailable"
                    raise PluginInteractionError(code, f"必需依赖不可用: {owner} -> {provider}.{kind or ''}.{name}")
    return after


@dataclass
class _ConfigurationUpdate:
    """已预检的完整目标配置, 只在宿主协调步骤内有效"""

    runtime: PluginInterop
    nodes: dict[str, _RelationNode]
    before: Mapping[str, _RelationNode]
    activating: set[str]
    active: bool = True


_CONFIGURATION_UPDATE: ContextVar[_ConfigurationUpdate | None] = ContextVar("satrap_plugin_configuration_update", default=None)


@dataclass
class _Binding:
    """安装期宿主接口与最终注册能力的绑定"""

    token: object
    spec: InteropSpec
    plugin: Plugin | None = None
    services: Mapping[str, Service | AsyncService] | None = None
    reference_revision: int = 0
    active: bool = False


@dataclass(frozen=True)
class PluginHost:
    """只读宿主接口, 不公开会话对象或内部身份资料"""

    _runtime_ref: weakref.ReferenceType[PluginInterop]
    _owner: str
    _token: object

    def _runtime(self) -> PluginInterop:
        """
        核验安装代次并取得私有注册表

        返回:
        - 当前注册表; 已失效时抛出 capability_stale
        """
        runtime = self._runtime_ref()
        if runtime is None or not runtime.host_valid(self):
            raise PluginInteractionError("capability_stale", "插件宿主接口已失效")
        return runtime

    def environment_snapshot(self) -> EnvironmentSnapshot:
        """
        查询当前环境冻结快照

        返回:
        - 不包含发话者和审批授权的环境资料
        """
        return environment_snapshot(self._runtime().session())

    @property
    def skills(self) -> SkillHostAccess:
        """
        取得技能登记与本轮加载接口

        返回:
        - 当前插件身份绑定的技能宿主接口
        """
        from .skills import SkillHostAccess
        self._runtime()
        return SkillHostAccess(self)

    @property
    def plugins(self) -> PluginAccess:
        """
        取得受声明和来源约束的插件访问入口

        返回:
        - 绑定当前消费者的能力访问入口
        """
        self._runtime()
        return PluginAccess(self)

    @property
    def artifacts(self) -> ArtifactAccess:
        """
        取得当前执行范围的宿主产物接口

        返回:
        - 产物读取和本轮文件交付接口, 不授予跨对话发送权限
        """
        from satrap.core.plugin_interop.artifacts import ArtifactAccess

        self._runtime()
        return ArtifactAccess(self)

    @property
    def approvals(self) -> ApprovalAccess:
        """
        取得受真实入口约束的宿主审批接口

        返回:
        - 当前插件的审批入口, 不公开内部引擎
        """
        from satrap.core.plugin_interop.approvals import ApprovalAccess

        self._runtime()
        return ApprovalAccess(self)

    @property
    def debug_logging(self) -> DebugLoggingAccess:
        """
        取得当前真实命令可用的诊断设置入口

        返回:
        - 绑定插件代次和当前对象的宿主管理接口
        """
        from satrap.core.plugin_interop.debug_logging import DebugLoggingAccess

        self._runtime()
        return DebugLoggingAccess(self)


@dataclass(frozen=True)
class CapabilityReference:
    """绑定安装代次的工具或服务引用, 不公开原始实例"""

    _host: PluginHost
    _plugin: str
    _kind: CapabilityKind
    _name: str
    _token: object
    _revision: int
    _adapter: str = ""

    def call_sync(self, arguments: Mapping[str, object]) -> ServiceValue:
        """
        同步调用仍有效的能力

        参数:
        - arguments: 业务具名参数

        返回:
        - 普通业务数据; 失效, 无权或业务失败时抛出异常
        """
        return self._host._runtime().call_sync(self, arguments)

    async def call_async(self, arguments: Mapping[str, object]) -> ServiceValue:
        """
        异步调用仍有效的能力

        参数:
        - arguments: 业务具名参数

        返回:
        - 普通业务数据; 失效, 无权或业务失败时抛出异常
        """
        return await self._host._runtime().call_async(self, arguments)


@dataclass(frozen=True)
class CallableAccess:
    """工具或服务的受控查找与调用入口"""

    _host: PluginHost
    _kind: CapabilityKind

    def find(self, plugin_name: str, name: str) -> CapabilityReference | None:
        """
        查找已声明且当前可用的能力

        参数:
        - plugin_name: 提供者实际名称
        - name: 能力实际名称

        返回:
        - 有效引用, 正常缺失返回 None; 未声明依赖时抛出异常
        """
        return self._host._runtime().find(self._host, plugin_name, self._kind, name)

    def call_sync(self, plugin_name: str, name: str, arguments: Mapping[str, object]) -> ServiceValue:
        """
        同步直接调用目标, 不将目标缺失伪装为成功

        参数:
        - plugin_name: 提供者实际名称
        - name: 能力实际名称
        - arguments: 业务具名参数

        返回:
        - 业务数据; 不可用时抛出明确交互错误
        """
        return self._host._runtime().reference(self._host, plugin_name, self._kind, name).call_sync(arguments)

    async def call_async(self, plugin_name: str, name: str, arguments: Mapping[str, object]) -> ServiceValue:
        """
        异步直接调用目标, 不将目标缺失伪装为成功

        参数:
        - plugin_name: 提供者实际名称
        - name: 能力实际名称
        - arguments: 业务具名参数

        返回:
        - 业务数据; 不可用时抛出明确交互错误
        """
        return await self._host._runtime().reference(self._host, plugin_name, self._kind, name).call_async(arguments)


@dataclass(frozen=True)
class PluginAccess:
    """当前消费者可访问的插件公开能力"""

    _host: PluginHost

    @property
    def tools(self) -> CallableAccess:
        """
        取得模型工具复用入口

        返回:
        - 仍经过既有工具分派器的访问入口
        """
        return CallableAccess(self._host, "tools")

    @property
    def services(self) -> CallableAccess:
        """
        取得插件专用服务入口

        返回:
        - 不进入模型工具管理器的访问入口
        """
        return CallableAccess(self._host, "services")

    @property
    def handlers(self) -> HandlerAccess:
        """
        查询处理器公开资料

        返回:
        - 不公开处理回调的只读查询入口
        """
        return HandlerAccess(self._host)

    @property
    def skills(self) -> SkillAccess:
        """
        查询技能公开资料

        返回:
        - 不修改技能启用或指令顺序的查询入口
        """
        return SkillAccess(self._host)

    @property
    def commands(self) -> CommandAccess:
        """
        取得命令受控访问入口

        返回:
        - 保留原命令权限的访问入口
        """
        return CommandAccess(self._host)

    @property
    def mcp(self) -> MCPAccess:
        """
        取得命名 MCP 连接访问入口

        返回:
        - 仅能访问已注册适配器的入口
        """
        return MCPAccess(self._host)

    def find(self, plugin_name: str) -> PluginInfo | None:
        """
        查询已声明依赖插件的公开资料

        参数:
        - plugin_name: 提供者实际名称

        返回:
        - 已启用插件资料, 不可用时返回 None
        """
        runtime = self._host._runtime()
        runtime.dependency(self._host, plugin_name)
        binding = runtime.bindings.get(plugin_name)
        plugin = binding.plugin if binding is not None else None
        if plugin is None or not runtime.plugin_available(plugin_name):
            return None
        return PluginInfo(plugin.name, plugin.version, plugin.description, plugin.enabled)


@dataclass(frozen=True)
class HandlerAccess:
    """只读处理器访问入口"""

    _host: PluginHost

    def find(self, plugin_name: str, name: str) -> HandlerInfo | None:
        """
        查询已声明依赖的可用处理器

        参数:
        - plugin_name: 提供者实际名称
        - name: 处理器名

        返回:
        - 不包含原始回调的资料, 不可用时为 None
        """
        runtime = self._host._runtime()
        runtime.dependency(self._host, plugin_name, "handlers", name)
        if not runtime.available(plugin_name, "handlers", name):
            return None
        handler = runtime.session()._handlers[name]
        stages = tuple(stage for stage in ("before_user_send", "after_user_send", "before_model_reply", "after_model_reply")
                       if getattr(handler, stage) is not None)
        plugin = runtime.bindings[plugin_name].plugin
        description = plugin.capability_descriptions.get("handlers", {}).get(name, "") if plugin else ""
        return HandlerInfo(name, handler.priority, stages, description)


@dataclass(frozen=True)
class SkillAccess:
    """只读技能访问入口"""

    _host: PluginHost

    def find(self, plugin_name: str, name: str) -> SkillInfo | None:
        """
        查询已声明依赖的可用技能

        参数:
        - plugin_name: 提供者实际名称
        - name: 技能注册名

        返回:
        - 公开说明与指令文本, 不可用时为 None
        """
        runtime = self._host._runtime()
        runtime.dependency(self._host, plugin_name, "skills", name)
        if not runtime.available(plugin_name, "skills", name):
            return None
        manager = runtime.session()._skills_manager
        skill = manager.skills.get(name) if manager is not None else None
        return SkillInfo(name, skill.description, skill.instructions) if skill is not None else None


@dataclass(frozen=True)
class CommandReference:
    """不提升原生命令信任级别的受控命令引用"""

    _reference: CapabilityReference

    def run_sync(self, args: Sequence[str]) -> ServiceValue:
        """
        同步运行公开命令

        参数:
        - args: 原命令字符串参数序列

        返回:
        - 普通命令结果, 不授予原生命令专属管理权限
        """
        runtime = self._reference._host._runtime()
        callback = runtime.command_callback(self._reference, args, async_=False)
        result = copy_data(callback(*args))
        runtime.command_callback(self._reference, args, async_=False)
        return result

    async def run_async(self, args: Sequence[str]) -> ServiceValue:
        """
        异步运行公开命令

        参数:
        - args: 原命令字符串参数序列

        返回:
        - 普通命令结果, 不授予原生命令专属管理权限
        """
        runtime = self._reference._host._runtime()
        callback = runtime.command_callback(self._reference, args, async_=True)
        result = callback(*args)
        if not inspect.isawaitable(result):
            raise PluginInteractionError("dependency_unavailable", "命令没有异步实现")
        data = copy_data(await result)
        runtime.command_callback(self._reference, args, async_=True)
        return data


@dataclass(frozen=True)
class CommandAccess:
    """命令依赖查找入口"""

    _host: PluginHost

    def find(self, plugin_name: str, name: str) -> CommandReference | None:
        """
        查找已声明的命令

        参数:
        - plugin_name: 提供者名称
        - name: 原命令名

        返回:
        - 可用命令引用, 正常缺失为 None
        """
        reference = self._host._runtime().find(self._host, plugin_name, "commands", name)
        return CommandReference(reference) if reference is not None else None


@dataclass(frozen=True)
class MCPReference:
    """命名 MCP 连接的受控适配器查询, 不包含原始客户端"""

    _reference: CapabilityReference

    def find(self, tool_name: str) -> CapabilityReference | None:
        """
        查找该连接实际注册且启用的工具适配器

        参数:
        - tool_name: 已注册的完整适配器名称

        返回:
        - 受控适配器引用, 正常缺失为 None
        """
        reference = self._reference
        runtime = reference._host._runtime()
        binding = runtime.validate_reference(reference)
        plugin = binding.plugin
        if plugin is None:
            return None
        adapters = plugin._mcp_clients.get(reference._name, (None, []))[1]
        if not any(adapter.get_tool_name() == tool_name and adapter.is_enabled() for adapter in adapters):
            return None
        return CapabilityReference(reference._host, reference._plugin, "mcp", reference._name,
                                   reference._token, reference._revision, tool_name)


@dataclass(frozen=True)
class MCPAccess:
    """仅开放已声明命名连接的查询入口"""

    _host: PluginHost

    def find(self, plugin_name: str, connection_name: str) -> MCPReference | None:
        """
        查找公开且已启用的 MCP 连接

        参数:
        - plugin_name: 提供者名称
        - connection_name: 连接注册名

        返回:
        - 不包含凭据的连接引用, 正常缺失为 None
        """
        reference = self._host._runtime().find(self._host, plugin_name, "mcp", connection_name)
        return MCPReference(reference) if reference is not None else None


class PluginInterop:
    """一个实际会话的插件注册与分派状态"""

    def __init__(self, session: SessionType, *, async_: bool) -> None:
        """
        绑定实际会话与执行形态

        参数:
        - session: 宿主会话
        - async_: 是否使用异步执行形态
        """
        self._session_ref = weakref.ref(session)
        self.async_ = async_
        self.bindings: dict[str, _Binding] = {}

    def session(self) -> SessionType:
        """
        取得内部会话, 不作为公共 SDK 暴露

        返回:
        - 实际宿主会话; 已回收时抛出失效错误
        """
        session = self._session_ref()
        if session is None:
            raise PluginInteractionError("capability_stale", "宿主会话已结束")
        return session

    def prepare(self, name: str, spec: InteropSpec) -> PluginHost:
        """
        为正在安装的插件创建宿主接口

        参数:
        - name: 当前插件名
        - spec: 已严格解析的交互声明

        返回:
        - 可查询环境但尚不能执行业务的宿主接口
        """
        if name in self.bindings:
            raise ValueError(f"插件宿主接口已注册: {name}")
        binding = _Binding(object(), spec)
        self.bindings[name] = binding
        return PluginHost(weakref.ref(self), name, binding.token)

    def host_valid(self, host: PluginHost) -> bool:
        """
        检查宿主接口是否属于当前安装代次

        参数:
        - host: 消费方保存的宿主接口

        返回:
        - 当前安装或构造阶段仍有效时为 True
        """
        binding = self.bindings.get(host._owner)
        return binding is not None and binding.token is host._token

    def activate(self, plugin: Plugin, services: Mapping[str, Service | AsyncService]) -> None:
        """
        对照真实注册能力核验声明并激活

        参数:
        - plugin: 已安装插件句柄
        - services: 当前会话形态的服务实例
        """
        binding = self.bindings[plugin.name]
        for kind, names in binding.spec.exports.items():
            registered = cast(Mapping[str, bool], getattr(plugin, kind))
            if set(names) - set(registered):
                raise ValueError(f"插件导出未匹配真实能力: {plugin.name}.{kind}")
        binding.plugin, binding.services = plugin, dict(services)
        update = self._configuration_update()
        if update is None:
            self._validate_target(self._relation_nodes(include=plugin.name), activating={plugin.name})
        else:
            actual = self._relation_nodes(include=plugin.name)[plugin.name]
            desired = update.nodes.get(plugin.name)
            if desired is None:
                raise PluginInteractionError("dependency_unavailable", "安装插件不在当前完整目标配置中")
            capabilities = {kind: {name: desired.capabilities.get(kind, {}).get(name, state) for name, state in states.items()}
                            for kind, states in actual.capabilities.items()}
            candidate = {**update.nodes, plugin.name: replace(actual, enabled=desired.enabled, capabilities=capabilities)}
            self._validate_target(candidate, activating=update.activating, before_nodes=update.before)
            update.nodes = candidate
        binding.active = True

    def remove(self, name: str) -> None:
        """
        撤销当前安装的宿主接口和所有能力引用

        参数:
        - name: 被卸载或安装失败的插件名
        """
        self.bindings.pop(name, None)
        manager = self.session()._skills_manager
        if manager is not None:
            manager.sources.pop(name, None)

    def check_remove(self, name: str) -> None:
        """
        卸载前预检替代恢复是否引入排斥等新关系冲突

        参数:
        - name: 拟卸载的插件名, 安装失败的未激活对象无需预检
        """
        if self._configuration_update() is not None:
            return
        nodes = self._relation_nodes()
        if name in nodes:
            nodes.pop(name)
            self._validate_target(nodes)

    def revoke_references(self, name: str) -> None:
        """
        插件停用时撤销已有能力引用, 保留安装期宿主绑定

        参数:
        - name: 正在停用的插件名
        """
        binding = self.bindings.get(name)
        if binding is not None:
            binding.reference_revision += 1

    def _relation_nodes(self, *, include: str = "") -> dict[str, _RelationNode]:
        """
        从真实注册表捕获一次结构状态, 不执行插件业务

        参数:
        - include: 正在预检的未激活插件名, 默认不包含构造阶段对象

        返回:
        - 同一会话内的插件目标节点
        """
        from satrap.core.plugin_interop.contracts import CAPABILITY_KINDS

        result: dict[str, _RelationNode] = {}
        for owner, binding in tuple(self.bindings.items()):
            plugin = binding.plugin
            if plugin is None or not binding.active and owner != include:
                continue
            capabilities: dict[CapabilityKind, dict[str, bool]] = {}
            for kind in CAPABILITY_KINDS:
                states = cast(Mapping[str, bool], getattr(plugin, kind))
                capabilities[kind] = {name: self._independent(owner, kind, name) for name in states}
            result[owner] = _RelationNode(plugin.enabled, binding.spec, capabilities)
        return result

    def _validate_target(self, nodes: Mapping[str, _RelationNode], *, activating: set[str] | None = None,
                         before_nodes: Mapping[str, _RelationNode] | None = None) -> _RelationView:
        """
        校验目标配置的关系及增量依赖保护

        参数:
        - nodes: 本次变更后的完整结构状态
        - activating: 新安装或重新启用的插件, 默认空集合
        - before_nodes: 批量更新前的固定基线, 默认读取当前实例

        返回:
        - 已通过预检的只读关系视图, 不改变当前实例
        """
        return _validate_relation_target(
            nodes, self._relation_nodes() if before_nodes is None else before_nodes, activating,
        )

    def check_change(self, owner: str, *, enabled: bool, kind: CapabilityKind | None = None, name: str = "") -> None:
        """
        在提交插件或能力开关之前预检完整目标状态

        参数:
        - owner: 要变更的插件
        - enabled: 目标独立开关或插件聚合开关
        - kind: None 表示插件聚合开关
        - name: 能力名, 插件整体变更时为空
        """
        if self._configuration_update() is not None:
            return
        nodes = self._relation_nodes()
        node = nodes.get(owner)
        if node is None:
            return
        if kind is None:
            nodes[owner] = replace(node, enabled=enabled)
        else:
            capabilities = {category: dict(states) for category, states in node.capabilities.items()}
            if name not in capabilities.get(kind, {}):
                return
            capabilities[kind][name] = enabled
            nodes[owner] = replace(node, capabilities=capabilities)
        activating = {owner} if kind is None and enabled and not node.enabled else set()
        self._validate_target(nodes, activating=activating)

    def _configuration_update(self) -> _ConfigurationUpdate | None:
        """
        读取仍有效且属于当前会话的完整配置更新

        返回:
        - 当前协调作用域, 未进入或已退出时为 None
        """
        scope = _CONFIGURATION_UPDATE.get()
        return scope if scope is not None and scope.active and scope.runtime is self else None

    @contextmanager
    def configuration_update(self, nodes: Mapping[str, _RelationNode], *, activating: set[str],
                             baseline: Mapping[str, _RelationNode] | None = None) -> Iterator[None]:
        """
        以同一目标配置校验整个批次, 避免中间安装顺序改变裁定

        参数:
        - nodes: 完整最终配置的结构节点
        - activating: 需要完整激活校验的新增或重新启用插件
        - baseline: 回滚使用的原始基线, 默认读取当前实例

        返回:
        - 仅供宿主协调器使用的更新作用域, 不开放给插件 SDK
        """
        before = self._relation_nodes() if baseline is None else baseline
        self._validate_target(nodes, activating=activating, before_nodes=before)
        scope = _ConfigurationUpdate(self, dict(nodes), before, activating)
        token = _CONFIGURATION_UPDATE.set(scope)
        try:
            yield
        finally:
            scope.active = False
            _CONFIGURATION_UPDATE.reset(token)

    def plugin_available(self, name: str, trail: frozenset[str] = frozenset()) -> bool:
        """
        合成插件及其必需依赖的可用状态

        参数:
        - name: 实际插件名
        - trail: 当前依赖链, 防止未完成安装的环路递归

        返回:
        - 已激活且所有必需依赖可用时为 True
        """
        if any(binding.active and binding.spec.replaces for binding in self.bindings.values()):
            return _RelationView(self._relation_nodes()).value((name, None, ""))
        binding = self.bindings.get(name)
        if binding is None or not binding.active or binding.plugin is None or not binding.plugin.enabled or name in trail:
            return False
        next_trail = trail | {name}
        for provider, dependency in binding.spec.requires.items():
            if dependency.optional:
                continue
            if not self.plugin_available(provider, next_trail):
                return False
            target = self.bindings[provider]
            for kind, names in dependency.capabilities.items():
                if any(capability not in target.spec.exports.get(kind, ()) or not self._independent(provider, kind, capability)
                       for capability in names):
                    return False
        return True

    def _independent(self, provider: str, kind: CapabilityKind, name: str) -> bool:
        """
        读取实际注册能力的独立状态, 不检查导出或递归依赖

        参数:
        - provider: 提供者名称
        - kind: 能力类别
        - name: 实际能力名

        返回:
        - 真实能力仍存在且独立启用时为 True
        """
        binding = self.bindings.get(provider)
        plugin = binding.plugin if binding else None
        if binding is None or plugin is None:
            return False
        states = cast(Mapping[str, bool], getattr(plugin, kind))
        if not states.get(name, False):
            return False
        if kind == "services":
            service = (binding.services or {}).get(name)
            return service is not None and service.is_enabled()
        if kind == "tools":
            workflow = self.session()._wf
            tool = workflow.tools_manager.tools.get(name) if workflow is not None else None
            return tool is not None and tool.owner_plugin == provider and tool.is_enabled()
        if kind == "handlers":
            handler = self.session()._handlers.get(name)
            return handler is not None and handler.owner_plugin == provider and handler.enabled
        if kind == "skills":
            session = self.session()
            manager = session._skills_manager
            return manager is not None and name in manager.skills
        if kind == "commands":
            registry = self.command_registry()
            return name in registry.commands and (not plugin.enabled or registry.is_command_enabled(name))
        if kind == "mcp":
            return name in plugin._mcp_clients
        return True

    def _skill_changes(self) -> list[tuple[str, bool]]:
        """
        比较完整关系状态与实际技能装配, 批次中间阶段不恢复临时能力

        返回:
        - 先停用后激活的技能变更, 不修改用户独立开关
        """
        if self._configuration_update() is not None:
            return []
        session = self.session()
        manager, workflow = session._skills_manager, session._wf
        if manager is None or workflow is None:
            return []
        view = _RelationView(self._relation_nodes())
        active = manager._active.get(id(workflow), {})
        changes: list[tuple[str, bool]] = []
        for owner, binding in tuple(self.bindings.items()):
            plugin = binding.plugin
            if plugin is None or not binding.active:
                continue
            for name in plugin.skills:
                desired = view.value((owner, "skills", name))
                if desired != (name in active):
                    changes.append((name, desired))
        return sorted(changes, key=lambda item: item[1])

    def synchronize_skills(self) -> None:
        """同步装配关系变更后的有效技能, 保留原用户开关"""
        session = self.session()
        manager, workflow = session._skills_manager, session._wf
        if manager is None or workflow is None:
            return
        for name, enabled in self._skill_changes():
            success = manager.activate(name, workflow) if enabled else manager.deactivate(name, workflow)
            if not success:
                raise RuntimeError(f"技能关系状态未能应用: {name}, enabled={enabled}")

    async def synchronize_skills_async(self) -> None:
        """异步装配关系变更后的有效技能, 保留原用户开关"""
        session = self.session()
        manager, workflow = session._skills_manager, session._wf
        if manager is None or workflow is None:
            return
        for name, enabled in self._skill_changes():
            success = await manager.activate_async(name, workflow) if enabled else await manager.deactivate_async(name, workflow)
            if not success:
                raise RuntimeError(f"技能关系状态未能应用: {name}, enabled={enabled}")

    def command_registry(self) -> CommandHandler | AsyncCommandHandler:
        """
        取得当前形态的内部命令注册表

        返回:
        - 同步或异步命令注册表, 不向消费者暴露
        """
        from satrap.edictum.simple_session.sync import SimpleSession

        session = self.session()
        return session.cmd_handler if isinstance(session, SimpleSession) else session.command_handler

    def command_callback(self, reference: CapabilityReference, args: Sequence[str], *, async_: bool) -> Callable[..., object]:
        """
        保留真实调用来源, 核验命令权限后取得内部回调

        参数:
        - reference: 命令受控引用
        - args: 原命令参数
        - async_: 当前调用要求的执行形态

        返回:
        - 内部命令回调, 不创建原生命令授权作用域
        """
        from satrap.core.plugin_authorization import require_plugin_entry_permission

        if isinstance(args, str) or any(not isinstance(arg, str) for arg in args):
            raise TypeError("命令参数必须是字符串序列")
        self._prepare(reference, {"args": list(args)})
        if async_ != self.async_:
            raise PluginInteractionError("dependency_unavailable", "命令调用形态不匹配")
        registry = self.command_registry()
        authorization = getattr(registry, "_permission_bindings", {}).get(reference._name)
        if authorization is not None:
            require_plugin_entry_permission(authorization, subcommand=args[0] if args else None)
        return cast(Callable[..., object], registry.commands[reference._name])

    def dependency(self, host: PluginHost, provider: str, kind: CapabilityKind | None = None, name: str = "") -> _Binding:
        """
        核验消费者声明, 不借提供者存在性泄露私有能力

        参数:
        - host: 消费方宿主接口
        - provider: 提供者名称
        - kind: 具体能力类别, None 仅查询插件资料
        - name: 具体能力名, 插件查询时为空

        返回:
        - 当前消费者绑定; 未声明时抛出 capability_not_declared
        """
        if not self.host_valid(host):
            raise PluginInteractionError("capability_stale", "消费方宿主接口已失效")
        binding = self.bindings[host._owner]
        dependency = binding.spec.requires.get(provider)
        if dependency is None or kind is not None and name not in dependency.capabilities.get(kind, ()):
            raise PluginInteractionError("capability_not_declared", f"未声明依赖: {provider}.{kind or ''}.{name}")
        return binding

    def available(self, provider: str, kind: CapabilityKind, name: str) -> bool:
        """
        检查已开放能力的当前安装与独立状态

        参数:
        - provider: 提供者名称
        - kind: 能力类别
        - name: 能力名称

        返回:
        - 已安装, 启用且开放时为 True
        """
        binding = self.bindings.get(provider)
        if binding is None or not self.plugin_available(provider) or name not in binding.spec.exports.get(kind, ()):
            return False
        return self.capability_available(provider, kind, name)

    def capability_available(self, provider: str, kind: CapabilityKind, name: str) -> bool:
        """
        计算能力的结构可用性, 不要求对外导出

        参数:
        - provider: 实际插件名
        - kind: 能力类别
        - name: 实际能力名

        返回:
        - 依赖满足且未被有效替代的能力为 True
        """
        if any(binding.active and binding.spec.replaces for binding in self.bindings.values()):
            return _RelationView(self._relation_nodes()).value((provider, kind, name))
        return self.plugin_available(provider) and self._independent(provider, kind, name)

    def capability_reason(self, owner: str, kind: CapabilityKind, name: str) -> tuple[str, tuple[str, ...]]:
        """
        说明结构不可用原因, 不混入当前发话者的权限

        参数:
        - owner: 插件实际名称
        - kind: 能力类别
        - name: 能力名称

        返回:
        - 原因及生效替代者名称, 可用时返回空原因
        """
        binding = self.bindings.get(owner)
        plugin = binding.plugin if binding else None
        if plugin is None:
            return "not_installed", ()
        if not plugin.enabled:
            return "plugin_disabled", ()
        if not self._independent(owner, kind, name):
            return "disabled", ()
        view = _RelationView(self._relation_nodes())
        implementations = view.replacements.get((owner, kind, name), [])
        replacements = tuple(f"{plugin}.{category}.{capability}" for plugin, category, capability in implementations
                             if view.value((plugin, category, capability)))
        if replacements:
            return "replaced_by", replacements
        if not view.value((owner, None, "")):
            return "dependency_unavailable", ()
        return "", ()

    def model_tool_available(self, owner: str, tool_name: str) -> bool:
        """
        合成普通工具或 MCP 适配器的模型入口状态

        参数:
        - owner: 工具所属插件
        - tool_name: 实际模型工具注册名

        返回:
        - 插件, 依赖及对应能力独立位均满足时为 True
        """
        binding = self.bindings.get(owner)
        plugin = binding.plugin if binding else None
        if plugin is None or not self.plugin_available(owner):
            return False
        if tool_name in plugin.tools:
            return self.capability_available(owner, "tools", tool_name)
        return any(self.capability_available(owner, "mcp", name)
                   and any(adapter.get_tool_name() == tool_name and adapter.is_enabled() for adapter in adapters)
                   for name, (_, adapters) in plugin._mcp_clients.items())

    def reference(self, host: PluginHost, provider: str, kind: CapabilityKind, name: str) -> CapabilityReference:
        """
        创建调用引用或报告明确失败

        参数:
        - host: 消费方宿主接口
        - provider: 提供者名称
        - kind: 能力类别
        - name: 能力名称

        返回:
        - 绑定目标代次的受控引用
        """
        self.dependency(host, provider, kind, name)
        binding = self.bindings.get(provider)
        if binding and name not in binding.spec.exports.get(kind, ()):
            raise PluginInteractionError("capability_private", f"能力未开放: {provider}.{kind}.{name}")
        if binding is None or not self.available(provider, kind, name):
            reason, replacements = self.capability_reason(provider, kind, name)
            raise PluginInteractionError("dependency_unavailable", f"能力不可用: {provider}.{kind}.{name} ({reason})",
                plugin=provider, capability=name, reason_code=reason, replaced_by=replacements)
        return CapabilityReference(host, provider, kind, name, binding.token, binding.reference_revision)

    def find(self, host: PluginHost, provider: str, kind: CapabilityKind, name: str) -> CapabilityReference | None:
        """
        查找当前可用能力, 正常缺失不抛出异常

        参数:
        - host: 消费方宿主接口
        - provider: 提供者名称
        - kind: 能力类别
        - name: 能力名称

        返回:
        - 当前受控引用, 已声明但不可用时返回 None
        """
        self.dependency(host, provider, kind, name)
        return self.reference(host, provider, kind, name) if self.available(provider, kind, name) else None

    @contextmanager
    def execution(self, owner: str, kind: CapabilityKind, name: str) -> Iterator[None]:
        """
        绑定一个真实入口的可撤销执行作用域

        参数:
        - owner: 当前实际执行插件
        - kind: 当前入口类别
        - name: 当前入口名

        返回:
        - 退出或取消时恢复外层身份的执行作用域
        """
        binding = self.bindings.get(owner)
        if binding is None or not self.capability_available(owner, kind, name):
            raise PluginInteractionError("dependency_unavailable", f"执行插件不可用: {owner}")
        scope = _Invocation(self, owner, kind, name, _INVOCATION.get(), binding.token, binding.reference_revision)
        token = _INVOCATION.set(scope)
        try:
            yield
        finally:
            scope.active = False
            _INVOCATION.reset(token)

    def _prepare(self, reference: CapabilityReference, arguments: Mapping[str, object]) -> tuple[_Binding, dict[str, ServiceValue]]:
        """
        逐次核验引用, 来源与参数数据

        参数:
        - reference: 消费方保存的受控引用
        - arguments: 本次业务参数

        返回:
        - 当前提供者绑定与复制后的参数映射
        """
        binding = self.validate_reference(reference)
        self.require_execution(reference._host)
        self.reference(reference._host, reference._plugin, reference._kind, reference._name)
        if reference._kind == "services":
            from satrap.core.plugin_authorization import PluginEntryBinding, require_plugin_entry_permission, PluginPermissionDenied
            service = (binding.services or {}).get(reference._name)
            authorization: object = getattr(service, "_plugin_entry_binding", None)
            if not isinstance(authorization, PluginEntryBinding):
                raise PluginInteractionError("permission_denied", "服务没有有效授权绑定")
            try:
                require_plugin_entry_permission(authorization)
            except PluginPermissionDenied as error:
                raise PluginInteractionError("permission_denied", str(error)) from error
        data = copy_data(dict(arguments))
        if not isinstance(data, dict):
            raise TypeError("服务参数必须是具名映射")
        return binding, data

    def require_execution(self, host: PluginHost) -> _Invocation:
        """
        核验宿主访问来自当前真实插件及仍有效的外层步骤

        参数:
        - host: 当前消费者的安装期宿主接口

        返回:
        - 当前插件步骤; 无执行作用域或已失效时拒绝
        """
        if not self.host_valid(host):
            raise PluginInteractionError("capability_stale", "插件宿主接口已失效")
        consumer = self.bindings[host._owner].plugin
        invocation = _INVOCATION.get()
        scope = invocation
        while scope is not None:
            if not scope.active:
                raise PluginInteractionError("permission_denied", "外层插件调用作用域已结束")
            if not scope.is_current():
                raise PluginInteractionError("capability_stale", "外层插件入口已经失效")
            scope = scope.parent
        if consumer is None or not self.plugin_available(consumer.name) or invocation is None or not invocation.active or invocation.runtime is not self or invocation.plugin != consumer.name:
            raise PluginInteractionError("permission_denied", "当前插件没有有效执行作用域")
        return invocation

    def validate_reference(self, reference: CapabilityReference) -> _Binding:
        """
        核验公开引用的声明, 目标代次和当前状态

        参数:
        - reference: 待查询或执行的公开引用

        返回:
        - 当前目标绑定; 引用失效时拒绝透明换绑
        """
        self.dependency(reference._host, reference._plugin, reference._kind, reference._name)
        binding = self.bindings.get(reference._plugin)
        if binding is None or binding.token is not reference._token or binding.reference_revision != reference._revision:
            raise PluginInteractionError("capability_stale", "目标能力引用已失效")
        self.reference(reference._host, reference._plugin, reference._kind, reference._name)
        return binding

    def call_sync(self, reference: CapabilityReference, arguments: Mapping[str, object]) -> ServiceValue:
        """
        经受控边界同步调用工具或服务

        参数:
        - reference: 目标能力引用
        - arguments: 本次业务参数

        返回:
        - 已复制校验的普通业务数据
        """
        try:
            binding, data = self._prepare(reference, arguments)
            if self.async_:
                raise PluginInteractionError("dependency_unavailable", "异步会话不提供隐式同步适配")
            if reference._kind in ("tools", "mcp"):
                workflow = self.session()._wf
                if workflow is None:
                    raise PluginInteractionError("dependency_unavailable", "工作流尚未初始化")
                from satrap.core.utils.TCBuilder.manager import ToolsManager
                if not isinstance(workflow.tools_manager, ToolsManager):
                    raise PluginInteractionError("dependency_unavailable", "工具执行形态不匹配")
                tool_name = self.adapter_name(reference)
                with self.execution(reference._plugin, reference._kind, reference._name):
                    result = copy_data(workflow.tools_manager.execute_tool(tool_name, data))
                self._prepare(reference, {})
                return result
            service = (binding.services or {}).get(reference._name)
            if not isinstance(service, Service):
                raise PluginInteractionError("dependency_unavailable", "服务没有同步实现")
            bound = inspect.signature(service.execute).bind(**data)
            bound.apply_defaults()
            with self.execution(reference._plugin, "services", reference._name):
                result = copy_data(service.execute(*bound.args, **bound.kwargs))
            self._prepare(reference, {})
            logger.debug(f"[插件交互] 同步服务完成, 插件={reference._plugin}, 能力={reference._name}")
            return result
        except PluginInteractionError as error:
            logger.warning(f"[插件交互] 同步调用拒绝, 插件={reference._plugin}, 能力={reference._name}, code={error.code}")
            raise
        except Exception:
            logger.error(f"[插件交互] 同步调用失败, 插件={reference._plugin}, 能力={reference._name}\n{traceback.format_exc()}")
            raise

    async def call_async(self, reference: CapabilityReference, arguments: Mapping[str, object]) -> ServiceValue:
        """
        经受控边界异步调用工具或服务

        参数:
        - reference: 目标能力引用
        - arguments: 本次业务参数

        返回:
        - 已复制校验的普通业务数据
        """
        try:
            binding, data = self._prepare(reference, arguments)
            if not self.async_:
                raise PluginInteractionError("dependency_unavailable", "同步会话不提供隐式异步适配")
            if reference._kind in ("tools", "mcp"):
                workflow = self.session()._wf
                if workflow is None:
                    raise PluginInteractionError("dependency_unavailable", "工作流尚未初始化")
                from satrap.core.utils.TCBuilder.async_manager import AsyncToolsManager
                if not isinstance(workflow.tools_manager, AsyncToolsManager):
                    raise PluginInteractionError("dependency_unavailable", "工具执行形态不匹配")
                tool_name = self.adapter_name(reference)
                with self.execution(reference._plugin, reference._kind, reference._name):
                    result = copy_data(await workflow.tools_manager.execute_tool(tool_name, data))
                self._prepare(reference, {})
                return result
            service = (binding.services or {}).get(reference._name)
            if not isinstance(service, AsyncService):
                raise PluginInteractionError("dependency_unavailable", "服务没有异步实现")
            bound = inspect.signature(service.execute).bind(**data)
            bound.apply_defaults()
            with self.execution(reference._plugin, "services", reference._name):
                result = copy_data(await service.execute(*bound.args, **bound.kwargs))
            self._prepare(reference, {})
            logger.debug(f"[插件交互] 异步服务完成, 插件={reference._plugin}, 能力={reference._name}")
            return result
        except PluginInteractionError as error:
            logger.warning(f"[插件交互] 异步调用拒绝, 插件={reference._plugin}, 能力={reference._name}, code={error.code}")
            raise
        except Exception:
            logger.error(f"[插件交互] 异步调用失败, 插件={reference._plugin}, 能力={reference._name}\n{traceback.format_exc()}")
            raise

    def adapter_name(self, reference: CapabilityReference) -> str:
        """
        核验 MCP 目标属于当前命名连接, 普通工具直接使用原名称

        参数:
        - reference: 工具或连接适配器引用

        返回:
        - 实际工具注册名; 跨连接或未注册适配器被拒绝
        """
        if reference._kind == "tools":
            return reference._name
        binding = self.bindings[reference._plugin]
        plugin = binding.plugin
        adapters = plugin._mcp_clients.get(reference._name, (None, []))[1] if plugin else []
        workflow = self.session()._wf
        if (not reference._adapter or workflow is None
                or not any(adapter.get_tool_name() == reference._adapter
                           and workflow.tools_manager.tools.get(reference._adapter) is adapter
                           and adapter.is_enabled() for adapter in adapters)):
            raise PluginInteractionError("dependency_unavailable", "MCP 工具未注册到该连接或已停用")
        return reference._adapter


@contextmanager
def bind_tool_plugin(tool: object) -> Iterator[None]:
    """
    在既有工具管理器入口绑定实际插件身份

    参数:
    - tool: 已通过既有管理器核验的工具实例

    返回:
    - 当前工具的插件执行作用域; 非目录插件工具保持原行为
    """
    from satrap.core.plugin_authorization import PluginEntryBinding

    binding: object = getattr(tool, "_plugin_entry_binding", None)
    session = binding.session_ref() if isinstance(binding, PluginEntryBinding) and binding.session_ref else None
    runtime: object = getattr(session, "_plugin_interop_runtime", None)
    if not isinstance(binding, PluginEntryBinding) or not isinstance(runtime, PluginInterop):
        yield
        return
    with runtime.execution(binding.plugin_name, "tools", binding.name):
        yield


def wrap_plugin_command(
    runtime: PluginInterop, owner: str, name: str, callback: Callable[..., object],
) -> Callable[..., object]:
    """
    为原命令回调绑定入口作用域, 保留原函数元数据和原生命令授权

    参数:
    - runtime: 实际会话的交互注册表
    - owner: 固定所属插件
    - name: 实际命令名
    - callback: 旧命令协议回调, 参数形态在命令注册边界确定

    返回:
    - 与原回调同步/异步形态相同的受控回调
    """
    runtime_ref = weakref.ref(runtime)

    @contextmanager
    def scope() -> Iterator[None]:
        """
        校验命令所属注册表并建立可撤销作用域

        返回:
        - 当前命令的执行作用域
        """
        current = runtime_ref()
        if current is None:
            raise PluginInteractionError("capability_stale", "命令宿主会话已结束")
        with current.execution(owner, "commands", name):
            yield

    if inspect.iscoroutinefunction(callback):
        @wraps(callback)
        async def asynchronous(*args: object, **kwargs: object) -> object:
            """
            在原异步命令期间绑定当前插件

            参数:
            - args: 原命令位置参数
            - kwargs: 原命令具名参数

            返回:
            - 原异步命令结果
            """
            with scope():
                return await callback(*args, **kwargs)
        return asynchronous

    @wraps(callback)
    def synchronous(*args: object, **kwargs: object) -> object:
        """
        在原同步命令期间绑定当前插件

        参数:
        - args: 原命令位置参数
        - kwargs: 原命令具名参数

        返回:
        - 原同步命令结果
        """
        with scope():
            return callback(*args, **kwargs)
    return synchronous
