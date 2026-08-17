"""edictum 简易单工作流 Agent 系统

SimpleSession / AsyncSimpleSession: 基于 Session 的高可扩展单 workflow Agent 框架
- 单个主 workflow / 单一主模型, 内部直接使用 full_agent (React 范式)
- 命令 / 工具 / MCP / skill / 处理器 / 插件六类能力的注入、删除、启用/停用、查看
- 处理器 (SessionHandler): 函数直注流程 (非 hook), 4 处理点, 优先级排序, before 可改写/拦截
- 插件 (Plugin): 目录化组合包 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py), 双层启停
- checkpoint 全套 (继承 Session 聚合检查点)
- 多模态 (img_urls) + 流式/非流式切换
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import (
    AsyncModelWorkflowFramework,
    AsyncSession,
    ModelWorkflowFramework,
    Session,
)
from satrap.core.log import logger
from satrap.core.type import safe_getattr_callable
from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager
from satrap.core.utils.paths import get_db_path
from satrap.core.utils.skills import SkillsManager
from satrap.edictum.plugin import (
    Plugin,
    collect_cleanup,
    collect_commands,
    collect_handlers,
    collect_mcp_clients,
    collect_skills,
    collect_tools,
    load_plugin_meta,
    parse_capability_descriptions,
)
from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema, schema_to_payload

# 处理器回调: 同步版仅调用同步函数; 异步版同步/异步均可 (运行期区分)
# 新签名统一携带 HandlerContext (破坏性升级, 见 SessionHandler docstring)
# 注意: 类型别名在模块加载时立即求值, HandlerResult 必须先定义 (不能用后置 ForwardRef)


class HandlerResult:
    """before_user_send 第三态返回: 短路指令 (文本均为 str, 与 run() -> str 契约对齐)

    - continue_with(text): 等价 str 改写, 继续执行后续 handler
    - respond(text): 跳过模型直接返回 text (后续 handler 短路, after_model_reply 仍执行)
    - reject(reason): 业务拒绝, reason 作为 run 的返回文本 (正常返回)
    - abort(reason): 故障终止, 抛 HandlerAbortError(reason)
    """

    __slots__ = ("action", "text")

    def __init__(self, action: str, text: str) -> None:
        if action not in ("continue", "respond", "reject", "abort"):
            raise ValueError(f"未知 HandlerResult action: {action!r}")
        self.action = action
        self.text = text

    @classmethod
    def continue_with(cls, text: str) -> "HandlerResult":
        """改写输入并继续"""
        return cls("continue", text)

    @classmethod
    def respond(cls, text: str) -> "HandlerResult":
        """跳过模型直接返回 text"""
        return cls("respond", text)

    @classmethod
    def reject(cls, reason: str) -> "HandlerResult":
        """业务拒绝, reason 作为返回文本"""
        return cls("reject", reason)

    @classmethod
    def abort(cls, reason: str) -> "HandlerResult":
        """故障终止, 抛 HandlerAbortError"""
        return cls("abort", reason)


class HandlerAbortError(Exception):
    """HandlerResult.abort 触发的故障终止异常"""


BeforeUserSend = Callable[[str, "HandlerContext"], str | None | HandlerResult]
AfterUserSend = Callable[[str, "HandlerContext"], None]
BeforeModelReply = Callable[["HandlerContext"], None]
AfterModelReply = Callable[[str | None, "HandlerContext"], str | None]


@dataclass(frozen=True)
class HandlerConfig:
    """一轮 run 的只读配置快照 (冻结, handler 不可改)"""

    original_input: str
    """原始用户输入"""
    img_urls: list[str] | None
    thinking: str
    max_iterations: int
    call_id: str
    """每轮 run 唯一标识"""


@dataclass
class HandlerContext:
    """一轮 run 的上下文: config 为冻结只读快照; text/error/outcome 为运行期可变状态

    - text: 当前文本 (before_user_send 改写后更新; handler 可读, 改动不影响 run 主流程)
    - error: 模型调用异常 (after_model_reply 可见; 非 None 时返回值被忽略)
    - outcome: 短路语义 (respond/reject/abort 置位, 正常路径为 None)
    """

    config: HandlerConfig
    text: str
    error: BaseException | None = None
    outcome: str | None = None


HANDLER_TIMEOUT = 30
"""异步处理器单次调用的默认超时秒数 (SessionHandler.timeout 可覆盖)"""


def _assert_sync_callbacks(handler: SessionHandler) -> None:
    """协议级校验: 全同步协议拒绝任何 coroutinefunction 回调 (add_handler 与插件安装共用)

    运行时类型约束无效: 误传 async def 会在同步路径静默忽略、在 to_thread 路径产生
    无人 await 的 coroutine, 均导致回调不生效——故统一显式拒绝
    """
    for fn in (
        handler.before_user_send, handler.after_user_send,
        handler.before_model_reply, handler.after_model_reply,
    ):
        if fn is not None and inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"处理器 {handler.name} 含异步回调, 全同步协议不支持 (回调请改为同步函数)"
            )


def _command_intro(handler: Callable[..., Any]) -> str:
    """从命令函数 docstring 提取简介 (首行)"""
    doc = inspect.getdoc(handler)
    if doc:
        first = doc.strip().splitlines()[0].strip()
        if first:
            return first
    return "None"


def _add_plugin_sys_path(plugin_dir: Path) -> None:
    """将插件目录加入 sys.path, 使插件内私有模块可互相 import (如 core.permission)"""
    plugin_path = str(plugin_dir.resolve())
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)


def _remove_plugin_sys_path(plugin_dir: Path) -> None:
    """卸载时尝试从 sys.path 移除插件目录 (多个插件共用时静默忽略)"""
    plugin_path = str(plugin_dir.resolve())
    try:
        sys.path.remove(plugin_path)
    except ValueError:
        pass


def _warn_undeclared_capabilities(
    plugin_name: str,
    descriptions: dict[str, dict[str, str]],
    scanned: dict[str, dict[str, bool]],
) -> None:
    """校验 meta.yaml 声明的能力在实际扫描结果中存在, 声明了但扫描不到仅警告 (不改变安装行为)

    descriptions: parse_capability_descriptions 结果 (kind -> {名字: 描述})
    scanned: 各类实际扫描到的能力 (kind -> {名字: 独立启用状态})
    """
    for kind, declared in descriptions.items():
        found = scanned.get(kind, {})
        for cap_name in declared:
            if cap_name not in found:
                logger.warning(f"[插件] 插件 {plugin_name} 声明的 {kind} 能力 {cap_name} 未在插件中发现")


@dataclass
class SessionHandler:
    """处理器: 4 个处理点直接注入流程, 非 hook

    - before_user_send: 用户消息处理前; 返回 str / HandlerResult.continue_with 改写输入,
      None 透传; respond / reject 短路跳过模型; abort 抛 HandlerAbortError
    - after_user_send: 用户消息处理后 (通知, 收到改写后的文本与上下文)
    - before_model_reply: Agent/模型调用前 (通知; 命名沿用历史, 语义为"Agent 调用前")
    - after_model_reply: 模型回复后 (通知, 收到最终回复文本; 返回 str 改写最终结果;
      异常路径 result=None 且 ctx.error 非 None 时返回值被忽略)
    - priority: 越小越先执行; 同值按注册顺序执行 (稳定契约)
    - enabled: 独立启用位; 实际生效 = enabled ∧ 所属插件 enabled (执行路径合成)
    - owner_plugin: 所属插件名 (插件禁用时执行路径过滤), None 表示独立注册
    - timeout: 异步调用超时秒数 (仅异步版生效), None 用全局默认; 超时 = "放弃等待"而非
      "终止执行" (底层 to_thread 工作线程仍跑完, handler 网络调用应自设超时); 负数拒绝
    - error_policy: 回调异常策略, "continue" 隔离继续 (默认), "abort" 抛 HandlerAbortError
    - 回调全同步协议: 所有回调必须为同步函数, 异步会话经 asyncio.to_thread 执行
    - 命令入口 (cmd_handler.process_message) 不经过处理器, 仅覆盖 run()
    """

    name: str
    priority: int = 100
    enabled: bool = True
    owner_plugin: str | None = None
    timeout: float | None = None
    error_policy: str = "continue"
    before_user_send: BeforeUserSend | None = None
    after_user_send: AfterUserSend | None = None
    before_model_reply: BeforeModelReply | None = None
    after_model_reply: AfterModelReply | None = None
    _seq: int = 0
    """全局注册序号, 由注册表分配, 同 priority 时按 _seq 排序"""

    def close(self) -> None:
        """释放 handler 持有的资源 (句柄/连接/线程/缓存任务)

        契约: 同步函数 (异步清理需以同步手段完成或经 to_thread 包装); 幂等 (可重复调用);
        容忍回调曾被超时取消的中间状态; 异常由调用方记日志不阻断; 子类覆盖实现具体清理
        """


class _HandlerRegistryMixin:
    """Handler 注册表: 增删启停 + 优先级 + 排序 (同步/异步会话共用)"""

    def _init_handler_registry(self) -> None:
        """初始化注册表与插件表 (宿主 __init__ 必须调用; 忘调时合成过滤 AttributeError 快速失败)"""
        self._handlers: dict[str, SessionHandler] = {}
        self._plugins: dict[str, Plugin] = {}
        self._handler_seq = 0
        """全局注册序号计数器: 同 priority 时按 _seq 排序"""
        self._registry_lock = threading.RLock()
        """注册表锁: 防护跨线程并发 (同线程协程交错由 run_lock 串行化兜底)"""
        self._active_runs = 0
        """进行中的 run 计数 (延迟 close 决策依据)"""
        self._pending_close: list[SessionHandler] = []
        """延迟 close 队列: run 结束后冲刷 (防 run 快照持有的 handler 被提前 close)"""

    def _validate_handler(self, handler: SessionHandler) -> None:
        """公共校验: name 非空 + timeout 非负 (add_handler 与插件安装共用)"""
        if not handler.name:
            raise ValueError("处理器 name 不能为空")
        if handler.timeout is not None and handler.timeout < 0:
            raise ValueError(f"处理器 {handler.name} 的 timeout 不能为负数")

    def add_handler(self, handler: SessionHandler, replace: bool = False) -> bool:
        """注册处理器 (协议级校验: 拒绝异步回调; 同名冲突抛 ValueError; replace=True 覆盖并 close 旧对象)"""
        self._validate_handler(handler)
        _assert_sync_callbacks(handler)
        old: SessionHandler | None = None
        with self._registry_lock:
            prev = self._handlers.get(handler.name)
            if prev is not None and prev is not handler:
                if not replace:
                    raise ValueError(f"处理器 {handler.name} 已存在 (replace=True 显式覆盖)")
                if prev.owner_plugin is not None:
                    logger.info(f"[edictum] 覆盖插件 {prev.owner_plugin} 的处理器 {handler.name}, 继承归属")
                    handler.owner_plugin = handler.owner_plugin or prev.owner_plugin
                self._sync_owner_roster(prev)
                if self._active_runs > 0:
                    self._pending_close.append(prev)
                else:
                    old = prev
            self._handler_seq += 1
            handler._seq = self._handler_seq
            self._handlers[handler.name] = handler
        if old is not None:
            self._close_handler(old.name, old)
        return True

    def remove_handler(self, name: str) -> bool:
        """注销处理器 (run 进行中延迟 close 到 run 结束; 否则立即 close)"""
        with self._registry_lock:
            existed = name in self._handlers
            handler = self._take_handler_locked(name)
        if handler is not None:
            self._close_handler(name, handler)
        return existed

    def enable_handler(self, name: str) -> bool:
        """启用处理器 (独立位)

        返回 True 表示独立位已更新; 实际生效 = 独立位 ∧ 插件聚合开关,
        以 list_capabilities 合成值为准
        """
        with self._registry_lock:
            handler = self._handlers.get(name)
            if handler is None:
                return False
            handler.enabled = True
        return True

    def disable_handler(self, name: str) -> bool:
        """停用处理器 (独立位; 所属插件禁用时执行路径合成仍过滤)"""
        with self._registry_lock:
            handler = self._handlers.get(name)
            if handler is None:
                return False
            handler.enabled = False
        return True

    def list_handlers(self) -> list[SessionHandler]:
        """列出处理器 (按优先级升序, 同值按注册序)"""
        with self._registry_lock:
            return sorted(self._handlers.values(), key=lambda h: (h.priority, h._seq))

    def set_handler_priority(self, name: str, priority: int) -> bool:
        """调整处理器优先级 (越小越先执行)"""
        with self._registry_lock:
            handler = self._handlers.get(name)
            if handler is None:
                return False
            handler.priority = priority
        return True

    def _enabled_handlers(self) -> list[SessionHandler]:
        """返回生效中的处理器快照: 独立启用 ∧ 所属插件启用, 按优先级升序 (同值按注册序)

        一致性快照: 同一次 run 内 handler 增删/启停/优先级变更不生效, 下次 run 生效
        """
        with self._registry_lock:
            return sorted(
                (h for h in self._handlers.values()
                 if h.enabled and (h.owner_plugin is None
                     or (self._plugins.get(h.owner_plugin) is not None and self._plugins[h.owner_plugin].enabled))),
                key=lambda h: (h.priority, h._seq),
            )

    def _take_handler_locked(self, name: str) -> SessionHandler | None:
        """从注册表移除 handler (调用方须持 _registry_lock)

        同步清理所属插件名册; run 进行中 → 入延迟 close 队列并返回 None, 否则返回 handler
        """
        handler = self._handlers.pop(name, None)
        if handler is None:
            return None
        self._sync_owner_roster(handler)
        if self._active_runs > 0:
            self._pending_close.append(handler)
            return None
        return handler

    def _sync_owner_roster(self, handler: SessionHandler) -> None:
        """remove/replace 时同步清理所属插件的 handlers 名册, 防 list_capabilities 残留
        (调用方须持 _registry_lock)"""
        owner = handler.owner_plugin
        if owner is None:
            return
        plugin = self._plugins.get(owner)
        if plugin is not None:
            plugin.handlers.pop(handler.name, None)

    @staticmethod
    def _close_handler(name: str, handler: SessionHandler) -> None:
        """调用 handler.close(), 异常记日志不阻断"""
        try:
            handler.close()
        except Exception as e:
            logger.warning(f"[edictum] 处理器 {name}.close() 异常: {e}")


class SimpleSession(Session, _HandlerRegistryMixin):
    """同步简易会话: 单 workflow 单主模型, 直接使用 full_agent (React 范式)

    使用示例:
    ``` python
    session = SimpleSession("conv-1", llm, system_prompt="你是助手")
    session.add_tool(my_tool)
    result = session("你好")
    ```
    """

    def __init__(
        self,
        session_id: str,
        llm: LLM,
        *,
        system_prompt: str | None = None,
        tools: Iterable[Tool] | None = None,
        content_callback: Callable[[str], None] | None = None,
        db_path: str = get_db_path("chat_history.db"),
        enable_checkpoint: bool = True,
        stream: bool = False,
        return_thinking: bool = False,
        thinking_callback: Callable[[str], None] | None = None,
    ):
        """初始化简易会话

        参数:
        - session_id: 会话 ID
        - llm: 主模型实例
        - system_prompt: 系统提示词
        - tools: 初始工具列表
        - content_callback: 内容回调 (流式输出与命令输出)
        - db_path: 上下文/检查点数据库路径
        - enable_checkpoint: 是否启用会话级检查点
        - stream: 默认是否流式输出
        - return_thinking: 是否回传思考内容
        - thinking_callback: 思考内容回调
        """
        super().__init__(
            session_id, content_callback, db_path=db_path, enable_checkpoint=enable_checkpoint,
        )
        wf_id = self.workflow_id_assign("main")
        self._wf = ModelWorkflowFramework(
            llm,
            context_id=wf_id,
            tools_manager=ToolsManager(),
            system_prompt=system_prompt,
            content_callback=content_callback,
            return_thinking=return_thinking,
            thinking_callback=thinking_callback,
            db_path=db_path,
        )
        self._track_workflow_context(wf_id, self._wf.ctx)
        self._init_handler_registry()
        self._wf.tools_manager.effectiveness_guard = self._tool_effective
        self._skills_manager: SkillsManager | None = None
        self._mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
        """插件 MCP 连接: 连接名 -> (客户端, 同步适配器列表) (同步版经后台事件循环桥接)"""
        self.user_input_provider: Callable[[str], str] | None = None
        """用户输入通道: 供 ask_user / 审批询问使用 (CLI 可注入 input, Streamlit 可注入 st.text_input)"""
        self.stream = stream
        if tools:
            for tool in tools:
                self._wf.tools_manager.register_tool(tool)

    # ---------------- 属性代理 ----------------

    @property
    def llm(self) -> LLM:
        """主模型实例"""
        return self._wf.llm

    @llm.setter
    def llm(self, value: LLM) -> None:
        self._wf.llm = value

    @property
    def ctx(self):
        """主工作流上下文"""
        return self._wf.ctx

    @property
    def tools_manager(self) -> ToolsManager:
        """主工作流工具管理器"""
        return self._wf.tools_manager

    # ---------------- 调用入口 ----------------

    def run(
        self,
        user_input: str,
        img_urls: list[str] | None = None,
        *,
        thinking: str = "off",
        max_iterations: int = 10,
    ) -> str:
        """执行一轮 Agent 流程 (React 范式), 返回最终模型输出

        参数:
        - user_input: 用户输入
        - img_urls: 图片 URL 列表 (多模态)
        - thinking: 是否要求模型思考
        - max_iterations: 最大工具调用迭代次数

        处理器语义:
        - before_user_send 可链式改写 (str/None/HandlerResult); respond/reject 短路跳过模型;
          abort 抛 HandlerAbortError (ctx.outcome 记录短路语义)
        - after_model_reply 在 finally 中执行; 异常路径 result=None 且 ctx.error 有值, 改写被忽略
        - handler 异常隔离 (error_policy="abort" 时抛 HandlerAbortError); 同一次 run 内
          handler 状态变更为一致性快照 (下次生效)
        - run 期间 remove/replace 的 handler 延迟 close, 本轮 run 结束后冲刷
        """
        with self._registry_lock:
            self._active_runs += 1
            handlers = self._enabled_handlers()
        try:
            call_id = uuid4().hex
            ctx = HandlerContext(
                config=HandlerConfig(
                    original_input=user_input, img_urls=img_urls, thinking=thinking,
                    max_iterations=max_iterations, call_id=call_id,
                ),
                text=user_input,
            )
            text = user_input
            final_override: str | None = None
            for p in handlers:
                if p.before_user_send is None:
                    continue
                out = self._invoke_handler(p, "before_user_send", p.before_user_send, text, ctx)
                if isinstance(out, HandlerResult):
                    if out.action == "continue":
                        text = out.text
                        ctx.text = text
                        continue
                    if out.action == "abort":
                        ctx.outcome = "abort"
                        raise HandlerAbortError(out.text)
                    # respond / reject: 短路, 跳过模型调用
                    final_override = out.text
                    ctx.outcome = out.action
                    logger.info(f"[edictum] 处理器 {p.name}.before_user_send 短路 ({out.action})")
                    break
                if isinstance(out, str):
                    text = out
                    ctx.text = text
                elif out is not None:
                    logger.warning(
                        f"[edictum] 处理器 {p.name}.before_user_send 返回不支持的类型 {type(out).__name__}, 已忽略"
                    )

            if final_override is None:
                for p in handlers:
                    if p.after_user_send is None:
                        continue
                    self._invoke_handler(p, "after_user_send", p.after_user_send, text, ctx)

                for p in handlers:
                    if p.before_model_reply is None:
                        continue
                    self._invoke_handler(p, "before_model_reply", p.before_model_reply, ctx)

            result: str | None = None
            ctx.error = None
            try:
                if final_override is not None:
                    result = final_override
                elif self.stream:
                    result = self._wf.stream_full_agent(
                        text, img_urls=img_urls, thinking=thinking, max_iterations=max_iterations,
                    )
                else:
                    if thinking != "off":
                        raise NotImplementedError("thinking 参数仅在流式模式 (stream=True) 下生效")
                    result = self._wf.full_agent(
                        text, img_urls=img_urls, max_iterations=max_iterations,
                    )
            except BaseException as e:
                ctx.error = e
                raise
            finally:
                for p in handlers:
                    if p.after_model_reply is None:
                        continue
                    out = self._invoke_handler(p, "after_model_reply", p.after_model_reply, result, ctx)
                    if ctx.error is None and isinstance(out, str):
                        result = out

            return result if result is not None else ""
        finally:
            pending: list[SessionHandler] = []
            with self._registry_lock:
                self._active_runs -= 1
                if self._active_runs == 0:
                    pending, self._pending_close = self._pending_close, []
            for h in pending:
                self._close_handler(h.name, h)

    def _invoke_handler(self, handler: SessionHandler, stage: str, fn: Callable[..., Any], *args: Any) -> Any:
        """调用单个处理器回调: 异常隔离 (error_policy="abort" 抛 HandlerAbortError) + 耗时观测"""
        t0 = time.perf_counter()
        try:
            return fn(*args)
        except HandlerAbortError:
            raise
        except Exception as e:
            if handler.error_policy == "abort":
                raise HandlerAbortError(f"处理器 {handler.name}.{stage} 异常: {e}") from e
            logger.error(f"[edictum] 处理器 {handler.name}.{stage} 异常: {e}")
            return None
        finally:
            logger.debug(f"[edictum] 处理器 {handler.name}.{stage} 耗时 {time.perf_counter() - t0:.3f}s")

    def __call__(self, user_input: str, **kwargs: Any) -> str:
        return self.run(user_input, **kwargs)

    # ---------------- 命令管理 ----------------

    def add_command(self, name: str, handler: Callable[..., Any], intro: str = "None"):
        """注册命令"""
        self.cmd_handler.register_command(name, handler, intro=intro)

    def remove_command(self, name: str) -> bool:
        """注销命令"""
        return self.cmd_handler.unregister_command(name)

    def enable_command(self, name: str) -> bool:
        """启用命令"""
        return self.cmd_handler.enable_command(name)

    def disable_command(self, name: str) -> bool:
        """停用命令 (停用后消息不再按命令处理)"""
        return self.cmd_handler.disable_command(name)

    def is_command_enabled(self, name: str) -> bool:
        """检查命令是否启用"""
        return self.cmd_handler.is_command_enabled(name)

    def list_commands(self) -> dict[str, str]:
        """列出已注册命令及其简介"""
        return self.cmd_handler.list_commands()

    # ---------------- 工具管理 ----------------

    def add_tool(self, tool: Tool):
        """注册工具 (任意时刻可注入, 立即生效)"""
        self._wf.tools_manager.register_tool(tool)

    def add_tools(self, *tools: Tool):
        """批量注册工具"""
        for tool in tools:
            self._wf.tools_manager.register_tool(tool)

    def remove_tool(self, name: str) -> bool:
        """注销工具"""
        return self._wf.tools_manager.unregister_tool(name)

    def enable_tool(self, name: str) -> bool:
        """启用工具"""
        return self._wf.tools_manager.enable_tool(name)

    def disable_tool(self, name: str) -> bool:
        """停用工具"""
        return self._wf.tools_manager.disable_tool(name)

    def is_tool_enabled(self, name: str) -> bool:
        """检查工具是否启用"""
        return self._wf.tools_manager.is_tool_enabled(name)

    def list_tools(self) -> list[str]:
        """列出已注册工具名"""
        return list(self._wf.tools_manager.tools.keys())

    # ---------------- skill 管理 ----------------

    def _get_skills_manager(self) -> SkillsManager:
        """获取技能管理器 (惰性创建)"""
        if self._skills_manager is None:
            self._skills_manager = SkillsManager()
        return self._skills_manager

    def add_skill(self, skill_name: str, skills_manager: SkillsManager | None = None) -> bool:
        """加载并激活技能到主工作流"""
        mgr = skills_manager or self._get_skills_manager()
        if skills_manager is not None:
            self._skills_manager = skills_manager
        return mgr.activate(skill_name, self._wf)

    def remove_skill(self, skill_name: str) -> bool:
        """从工作流卸载并从管理器移除技能定义"""
        mgr = self._skills_manager
        if mgr is None:
            return False
        removed = mgr.deactivate(skill_name, self._wf)
        mgr.unregister_skill(skill_name)
        return removed

    def enable_skill(self, skill_name: str) -> bool:
        """重新激活技能"""
        return self.add_skill(skill_name)

    def disable_skill(self, skill_name: str) -> bool:
        """停用技能 (从工作流卸载, 保留注册)"""
        if self._skills_manager is None:
            return False
        return self._skills_manager.deactivate(skill_name, self._wf)

    def list_skills(self) -> list[str]:
        """列出技能管理器已加载的技能"""
        if self._skills_manager is None:
            return []
        return self._skills_manager.list_skills()

    # ---------------- 处理器管理 (注册表实现在 _HandlerRegistryMixin) ----------------

    def _tool_effective(self, tool_name: str) -> bool:
        """工具生效过滤 (执行路径合成): 所属插件缺失或启用时生效"""
        tool = self._wf.tools_manager.tools.get(tool_name)
        owner = tool.owner_plugin if tool is not None else None
        if owner is None:
            return True
        with self._registry_lock:
            plugin = self._plugins.get(owner)
        return plugin is not None and plugin.enabled

    # ---------------- 插件管理 (目录插件包) ----------------

    def install_plugin(self, path: str, config: dict[str, Any] | None = None) -> Plugin:
        """安装目录插件 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py)

        mcp.py 同样支持: 远端工具经后台事件循环线程桥接为同步适配器注册;
        任一步失败时回滚已注册能力与 sys.path, 不留孤儿

        config: 会话级插件配置覆盖, 与全局配置合成后注入 get_tools 工厂
        """
        plugin_dir = Path(path)
        _add_plugin_sys_path(plugin_dir)
        tool_states: dict[str, bool] = {}
        skill_states: dict[str, bool] = {}
        handler_states: dict[str, bool] = {}
        command_states: dict[str, bool] = {}
        mcp_states: dict[str, bool] = {}
        mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
        try:
            meta = load_plugin_meta(plugin_dir)
            name = str(meta.get("name") or "").strip()
            if not name:
                raise ValueError(f"插件 {path} 的 meta.yaml 缺少 name")
            with self._registry_lock:
                if name in self._plugins:
                    raise ValueError(f"插件 {name} 已安装")

            # 合成插件配置: schema.default < 全局 json < 会话覆盖
            config_schema = parse_config_schema(meta)
            plugin_config = PluginConfigManager().resolve(name, config_schema, config)

            for t in collect_tools(plugin_dir, name, Tool, self, plugin_config):
                tname = t.get_tool_name()
                if tname in self._wf.tools_manager.tools or tname in tool_states:
                    raise ValueError(f"插件 {name} 的工具 {tname} 与已注册工具冲突")
                t.owner_plugin = name
                self._wf.tools_manager.register_tool(t)
                tool_states[tname] = True

            mgr = self._get_skills_manager()
            for s in collect_skills(plugin_dir, name):
                key = s.skill_id or s.name
                if key in mgr.skills or key in skill_states:
                    raise ValueError(f"插件 {name} 的技能 {key} 与已加载技能冲突")
                mgr._register_skill(s)
                skill_states[key] = True

            for h in collect_handlers(plugin_dir, name, self):
                self._validate_handler(h)
                _assert_sync_callbacks(h)
                with self._registry_lock:
                    if h.name in self._handlers or h.name in handler_states:
                        raise ValueError(f"插件 {name} 的处理器 {h.name} 与已注册处理器冲突")
                    h.owner_plugin = name
                    self._handler_seq += 1
                    h._seq = self._handler_seq
                    self._handlers[h.name] = h
                handler_states[h.name] = True

            sync_commands, _async_commands = collect_commands(plugin_dir, name, self)
            for cname, chandler in sync_commands.items():
                if cname in self.cmd_handler.commands or cname in command_states:
                    raise ValueError(f"插件 {name} 的命令 {cname} 与已注册命令冲突")
                self.cmd_handler.register_command(cname, chandler, intro=_command_intro(chandler))
                command_states[cname] = True

            for mcp_name, client in collect_mcp_clients(plugin_dir, name).items():
                if mcp_name in self._mcp_clients or mcp_name in mcp_states:
                    raise ValueError(f"插件 {name} 的 MCP 连接 {mcp_name} 与已接入连接冲突")
                try:
                    adapters = client.sync_register_tools(self._wf.tools_manager, name_prefix=name)
                except Exception:
                    close = safe_getattr_callable(client, "sync_close")
                    if close is not None:
                        try:
                            close()
                        except Exception:
                            pass
                    raise
                mcp_clients[mcp_name] = (client, list(adapters))
                mcp_states[mcp_name] = True

            plugin = Plugin(
                name=name,
                version=str(meta.get("version") or ""),
                author=str(meta.get("author") or ""),
                repo=str(meta.get("repo") or ""),
                description=str(meta.get("description") or ""),
                path=str(plugin_dir),
            )
            plugin._session = self
            plugin._cleanup = collect_cleanup(plugin_dir, name, self)
            plugin.tools = tool_states
            plugin.skills = skill_states
            plugin.mcp = mcp_states
            plugin._mcp_clients = mcp_clients
            plugin.handlers = handler_states
            plugin.commands = command_states
            # meta.yaml 能力声明: 读入描述并校验未匹配项 (仅警告, 不改变安装行为)
            plugin.capability_descriptions = parse_capability_descriptions(meta)
            plugin.config_schema = schema_to_payload(config_schema)
            _warn_undeclared_capabilities(name, plugin.capability_descriptions, {
                "tools": tool_states,
                "skills": skill_states,
                "mcp": mcp_states,
                "handlers": handler_states,
                "commands": command_states,
            })
            with self._registry_lock:
                self._plugins[name] = plugin
            logger.info(f"[edictum] 插件 {name} 已安装")
            return plugin
        except Exception:
            _remove_plugin_sys_path(plugin_dir)
            for tname in tool_states:
                self._wf.tools_manager.unregister_tool(tname)
            mgr = self._skills_manager
            if mgr is not None:
                for key in skill_states:
                    mgr.unregister_skill(key)
            for hname in handler_states:
                with self._registry_lock:
                    handler = self._take_handler_locked(hname)
                if handler is not None:
                    self._close_handler(hname, handler)
            for cname in command_states:
                self.cmd_handler.unregister_command(cname)
            for mcp_name, (client, adapters) in mcp_clients.items():
                for adapter in adapters:
                    self._wf.tools_manager.unregister_tool(adapter.get_tool_name())
                close = safe_getattr_callable(client, "sync_close")
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
            raise

    def uninstall_plugin(self, name: str) -> bool:
        """卸载插件: 回收其全部能力 (含清理回调), 不留孤儿"""
        with self._registry_lock:
            plugin = self._plugins.pop(name, None)
        if plugin is None:
            return False
        _remove_plugin_sys_path(Path(plugin.path))
        for tname in plugin.tools:
            self._wf.tools_manager.unregister_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for sname in plugin.skills:
                mgr.deactivate(sname, self._wf)
                mgr.unregister_skill(sname)
        for hname in list(plugin.handlers):
            with self._registry_lock:
                handler = self._take_handler_locked(hname)
            if handler is not None:
                self._close_handler(hname, handler)
        for cname in plugin.commands:
            self.cmd_handler.unregister_command(cname)
        for mcp_name, (client, adapters) in plugin._mcp_clients.items():
            for adapter in adapters:
                self._wf.tools_manager.unregister_tool(adapter.get_tool_name())
            close = safe_getattr_callable(client, "sync_close")
            if close is not None:
                try:
                    close()
                except Exception as e:
                    logger.warning(f"[edictum] 插件 {name} MCP 断开失败: {e}")
        cleanup = plugin._cleanup
        if cleanup is not None:
            try:
                cleanup(self)
            except Exception as e:
                logger.warning(f"[edictum] 插件 {name} 清理回调失败: {e}")
        logger.info(f"[edictum] 插件 {name} 已卸载")
        return True

    def enable_plugin(self, name: str) -> bool:
        """启用插件 (按独立状态恢复名下能力, 独立停用的保持停用)"""
        with self._registry_lock:
            plugin = self._plugins.get(name)
            if plugin is None:
                return False
            if plugin.enabled:
                return True
            plugin.enabled = True
        # 工具/处理器由执行路径合成承担 (effectiveness_guard / _enabled_handlers), 无需恢复循环
        mgr = self._skills_manager
        if mgr is not None:
            for sname, st in plugin.skills.items():
                if st:
                    mgr.activate(sname, self._wf)
        for cname, st in plugin.commands.items():
            if st:
                self.cmd_handler.enable_command(cname)
        return True

    def disable_plugin(self, name: str) -> bool:
        """停用插件 (压制名下全部能力, 不改独立状态; 工具/处理器由执行路径合成压制)"""
        with self._registry_lock:
            plugin = self._plugins.get(name)
            if plugin is None:
                return False
            if not plugin.enabled:
                return True
            plugin.enabled = False
        mgr = self._skills_manager
        if mgr is not None:
            for sname in plugin.skills:
                mgr.deactivate(sname, self._wf)
        for cname in plugin.commands:
            self.cmd_handler.disable_command(cname)
        return True

    def list_plugins(self) -> list[Plugin]:
        """列出已安装插件"""
        with self._registry_lock:
            return list(self._plugins.values())

    def _activate_plugin_skill(self, name: str) -> bool:
        """插件技能独立启用钩子 (同步版)"""
        return self._get_skills_manager().activate(name, self._wf)

    def _deactivate_plugin_skill(self, name: str) -> bool:
        """插件技能独立停用钩子 (同步版)"""
        return self._get_skills_manager().deactivate(name, self._wf)

    # ---------------- 模型与流式 ----------------

    def set_llm(self, llm: LLM):
        """替换主模型"""
        self._wf.llm = llm

    def set_model_parameters(self, **kwargs: Any):
        """调整主模型调用参数 (temperature / top_p / max_tokens 等)"""
        self._wf.llm.set_parameters(**kwargs)

    def set_stream_mode(self, stream: bool):
        """切换流式 / 非流式输出"""
        self.stream = bool(stream)

    def reload_llm(self, llm: LLM):
        """重载 LLM 实例 (Session 兼容接口)"""
        self.set_llm(llm)


class AsyncSimpleSession(AsyncSession, _HandlerRegistryMixin):
    """异步简易会话: 单 workflow 单主模型, 直接使用 full_agent (React 范式)

    - 支持 MCP 工具注入 (add_mcp / remove_mcp)
    - 插件回调支持同步与异步函数
    - 使用前需 `await session.initialize()` (或直接调用 run, 会自动初始化)
    """

    def __init__(
        self,
        session_id: str,
        llm: AsyncLLM,
        *,
        system_prompt: str | None = None,
        tools: Iterable[AsyncTool] | None = None,
        content_callback: Callable[[str], Any] | None = None,
        db_path: str = get_db_path("chat_history.db"),
        enable_checkpoint: bool = True,
        stream: bool = False,
        return_thinking: bool = False,
        thinking_callback: Callable[[str], Any] | None = None,
    ):
        """初始化异步简易会话 (工作流在 initialize 中构建)"""
        super().__init__(
            session_id, content_callback, db_path=db_path, enable_checkpoint=enable_checkpoint,
        )
        self._init_llm = llm
        self._init_system_prompt = system_prompt
        self._init_content_callback = content_callback
        self._init_return_thinking = return_thinking
        self._init_thinking_callback = thinking_callback
        self._init_tools: list[AsyncTool] = list(tools or [])
        self._init_skills: list[str] = []
        self._init_model_params: dict[str, Any] = {}
        self._db_path = db_path
        self._wf: AsyncModelWorkflowFramework | None = None
        self._init_handler_registry()
        self._skills_manager: SkillsManager | None = None
        self._mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
        self._init_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        """run 串行化锁: 同一会话不支持并发 run (第二个 run 排队等待)"""
        self.user_input_provider: Callable[[str], str | Awaitable[str]] | None = None
        """用户输入通道: 供 ask_user / 审批询问使用 (CLI 可注入 input, Streamlit 可注入 st.text_input)"""
        self.stream = stream

    async def _async_init(self):
        """构建主工作流 (AsyncSession.initialize 自动调用, run 前就绪, 并发安全)"""
        if self._wf is not None:
            return
        async with self._init_lock:
            if self._wf is not None:
                return
            wf_id = self.workflow_id_assign("main")
            wf = AsyncModelWorkflowFramework(
                self._init_llm,
                context_id=wf_id,
                tools_manager=AsyncToolsManager(),
                system_prompt=self._init_system_prompt,
                content_callback=self._init_content_callback,
                return_thinking=self._init_return_thinking,
                thinking_callback=self._init_thinking_callback,
                db_path=self._db_path,
            )
            await wf.initialize()
            self._track_workflow_context(wf_id, wf.ctx)
            self._wf = wf
            wf.tools_manager.effectiveness_guard = self._tool_effective
            if self._init_model_params:
                wf.llm.set_parameters(**self._init_model_params)
            try:
                for tool in self._init_tools:
                    wf.tools_manager.register_tool(tool)
                for skill_name in self._init_skills:
                    if not await self._get_skills_manager().activate_async(skill_name, wf):
                        logger.warning(f"[edictum] 初始化时技能 {skill_name} 激活失败")
            finally:
                self._init_tools = []
                self._init_skills = []

    def _require_wf(self) -> AsyncModelWorkflowFramework:
        """获取主工作流, 未初始化时抛错"""
        if self._wf is None:
            raise RuntimeError("工作流未初始化, 请先 await session.initialize()")
        return self._wf

    # ---------------- 属性代理 ----------------

    @property
    def llm(self) -> AsyncLLM:
        """主模型实例"""
        return self._require_wf().llm

    @llm.setter
    def llm(self, value: AsyncLLM) -> None:
        if self._wf is None:
            self._init_llm = value
            return
        self._wf.llm = value

    @property
    def ctx(self):
        """主工作流上下文"""
        return self._require_wf().ctx

    @property
    def tools_manager(self) -> AsyncToolsManager:
        """主工作流工具管理器"""
        return self._require_wf().tools_manager

    # ---------------- 调用入口 ----------------

    async def run(
        self,
        user_input: str,
        img_urls: list[str] | None = None,
        *,
        thinking: str = "off",
        max_iterations: int = 10,
    ) -> str:
        """执行一轮 Agent 流程 (React 范式), 返回最终模型输出

        参数:
        - user_input: 用户输入
        - img_urls: 图片 URL 列表 (多模态)
        - thinking: 是否要求模型思考
        - max_iterations: 最大工具调用迭代次数

        处理器语义 (与同步版一致): 短路/改写/隔离/超时/finally, 见 SimpleSession.run

        并发: 同一会话的 run 经 _run_lock 串行执行 (不支持并发 run, 第二个 run 排队等待)
        """
        async with self._run_lock:
            wf = self._require_wf()
            with self._registry_lock:
                self._active_runs += 1
                handlers = self._enabled_handlers()
            try:
                ctx = HandlerContext(
                    config=HandlerConfig(
                        original_input=user_input, img_urls=img_urls, thinking=thinking,
                        max_iterations=max_iterations, call_id=uuid4().hex,
                    ),
                    text=user_input,
                )
                text = user_input
                final_override: str | None = None
                for p in handlers:
                    if p.before_user_send is None:
                        continue
                    out = await self._invoke_handler_async(p, "before_user_send", p.before_user_send, text, ctx)
                    if isinstance(out, HandlerResult):
                        if out.action == "continue":
                            text = out.text
                            ctx.text = text
                            continue
                        if out.action == "abort":
                            ctx.outcome = "abort"
                            raise HandlerAbortError(out.text)
                        # respond / reject: 短路, 跳过模型调用
                        final_override = out.text
                        ctx.outcome = out.action
                        logger.info(f"[edictum] 处理器 {p.name}.before_user_send 短路 ({out.action})")
                        break
                    if isinstance(out, str):
                        text = out
                        ctx.text = text
                    elif out is not None:
                        logger.warning(
                            f"[edictum] 处理器 {p.name}.before_user_send 返回不支持的类型 {type(out).__name__}, 已忽略"
                        )

                if final_override is None:
                    for p in handlers:
                        if p.after_user_send is None:
                            continue
                        await self._invoke_handler_async(p, "after_user_send", p.after_user_send, text, ctx)

                    for p in handlers:
                        if p.before_model_reply is None:
                            continue
                        await self._invoke_handler_async(p, "before_model_reply", p.before_model_reply, ctx)

                result: str | None = None
                ctx.error = None
                try:
                    if final_override is not None:
                        result = final_override
                    elif self.stream:
                        result = await wf.stream_full_agent(
                            text, img_urls=img_urls, thinking=thinking, max_iterations=max_iterations,
                        )
                    else:
                        if thinking != "off":
                            raise NotImplementedError("thinking 参数仅在流式模式 (stream=True) 下生效")
                        result = await wf.full_agent(
                            text, img_urls=img_urls, max_iterations=max_iterations,
                        )
                except BaseException as e:
                    ctx.error = e
                    raise
                finally:
                    for p in handlers:
                        if p.after_model_reply is None:
                            continue
                        out = await self._invoke_handler_async(p, "after_model_reply", p.after_model_reply, result, ctx)
                        if ctx.error is None and isinstance(out, str):
                            result = out

                return result if result is not None else ""
            finally:
                pending: list[SessionHandler] = []
                with self._registry_lock:
                    self._active_runs -= 1
                    if self._active_runs == 0:
                        pending, self._pending_close = self._pending_close, []
                for h in pending:
                    await asyncio.to_thread(self._close_handler, h.name, h)

    async def _invoke_handler_async(
        self, handler: SessionHandler, stage: str, fn: Callable[..., Any], *args: Any,
    ) -> Any:
        """调用单个处理器回调 (异步): 同步回调投入工作线程 + wait_for 超时, 异常/超时隔离 + 耗时观测

        超时 = "放弃等待"而非"终止执行": 超时后框架不再等结果, 但底层 to_thread
        工作线程仍跑完——handler 应避免无限阻塞, 网络调用自设超时
        """
        t0 = time.perf_counter()
        timeout = HANDLER_TIMEOUT if handler.timeout is None else handler.timeout
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)
        except HandlerAbortError:
            raise
        except Exception as e:                      # 含 asyncio.TimeoutError
            if handler.error_policy == "abort":
                raise HandlerAbortError(f"处理器 {handler.name}.{stage} 异常: {e}") from e
            logger.error(f"[edictum] 处理器 {handler.name}.{stage} 异常/超时: {e}")
            return None
        finally:
            logger.debug(f"[edictum] 处理器 {handler.name}.{stage} 耗时 {time.perf_counter() - t0:.3f}s")

    async def __call__(self, user_input: str, **kwargs: Any) -> str:
        return await self.run(user_input, **kwargs)

    # ---------------- 命令管理 ----------------

    def add_command(self, name: str, handler: Callable[..., Any], intro: str = "None"):
        """注册命令"""
        self.command_handler.register_command(name, handler, intro=intro)

    def remove_command(self, name: str) -> bool:
        """注销命令"""
        return self.command_handler.unregister_command(name)

    def enable_command(self, name: str) -> bool:
        """启用命令"""
        return self.command_handler.enable_command(name)

    def disable_command(self, name: str) -> bool:
        """停用命令"""
        return self.command_handler.disable_command(name)

    def is_command_enabled(self, name: str) -> bool:
        """检查命令是否启用"""
        return self.command_handler.is_command_enabled(name)

    def list_commands(self) -> dict[str, str]:
        """列出已注册命令及其简介"""
        return self.command_handler.list_commands()

    # ---------------- 工具管理 ----------------

    def add_tool(self, tool: AsyncTool):
        """注册工具 (初始化前注册会延迟到 initialize 时生效)"""
        if self._wf is None:
            self._init_tools.append(tool)
        else:
            self._wf.tools_manager.register_tool(tool)

    def add_tools(self, *tools: AsyncTool):
        """批量注册工具"""
        for tool in tools:
            self.add_tool(tool)

    def remove_tool(self, name: str) -> bool:
        """注销工具"""
        return self._require_wf().tools_manager.unregister_tool(name)

    def enable_tool(self, name: str) -> bool:
        """启用工具"""
        return self._require_wf().tools_manager.enable_tool(name)

    def disable_tool(self, name: str) -> bool:
        """停用工具"""
        return self._require_wf().tools_manager.disable_tool(name)

    def is_tool_enabled(self, name: str) -> bool:
        """检查工具是否启用"""
        return self._require_wf().tools_manager.is_tool_enabled(name)

    def list_tools(self) -> list[str]:
        """列出已注册工具名"""
        return list(self._require_wf().tools_manager.tools.keys())

    # ---------------- skill 管理 ----------------

    def _get_skills_manager(self) -> SkillsManager:
        """获取技能管理器 (惰性创建)"""
        if self._skills_manager is None:
            self._skills_manager = SkillsManager()
        return self._skills_manager

    async def add_skill(self, skill_name: str, skills_manager: SkillsManager | None = None) -> bool:
        """加载并激活技能到主工作流 (初始化前注册会延迟到 initialize 时生效)"""
        mgr = skills_manager or self._get_skills_manager()
        if skills_manager is not None:
            self._skills_manager = skills_manager
        if self._wf is None:
            if mgr.get_skill(skill_name) is None:
                return False
            self._init_skills.append(skill_name)
            return True
        return await mgr.activate_async(skill_name, self._require_wf())

    async def remove_skill(self, skill_name: str) -> bool:
        """从工作流卸载并从管理器移除技能定义"""
        mgr = self._skills_manager
        if mgr is None:
            return False
        removed = await mgr.deactivate_async(skill_name, self._require_wf())
        mgr.unregister_skill(skill_name)
        return removed

    async def enable_skill(self, skill_name: str) -> bool:
        """重新激活技能"""
        return await self.add_skill(skill_name)

    async def disable_skill(self, skill_name: str) -> bool:
        """停用技能 (从工作流卸载, 保留注册)"""
        if self._skills_manager is None:
            return False
        return await self._skills_manager.deactivate_async(skill_name, self._require_wf())

    def list_skills(self) -> list[str]:
        """列出技能管理器已加载的技能"""
        if self._skills_manager is None:
            return []
        return self._skills_manager.list_skills()

    # ---------------- MCP 管理 ----------------

    async def add_mcp(self, name: str, client: Any, name_prefix: str | None = None) -> list[Any]:
        """接入 MCP Server, 将远程工具注册到主工作流

        参数:
        - name: MCP 连接名 (用于后续启停/删除)
        - client: MCPClient 实例
        - name_prefix: 工具名前缀

        返回:
        - 注册的工具适配器列表
        """
        if self._wf is None:
            await self.initialize()
        wf = self._require_wf()
        try:
            adapters = await client.register_tools(wf.tools_manager, name_prefix=name_prefix)
        except Exception:
            close = safe_getattr_callable(client, "close")
            if close is not None:
                await close()
            raise
        self._mcp_clients[name] = (client, list(adapters))
        return list(adapters)

    async def remove_mcp(self, name: str) -> bool:
        """移除 MCP 连接: 注销其全部工具并断开连接"""
        entry = self._mcp_clients.pop(name, None)
        if entry is None:
            return False
        client, adapters = entry
        wf = self._require_wf()
        for adapter in adapters:
            wf.tools_manager.unregister_tool(adapter.get_tool_name())
        close = safe_getattr_callable(client, "close")
        if close is not None:
            try:
                await close()
            except Exception as e:
                logger.warning(f"[edictum] MCP {name} 断开失败: {e}")
        return True

    def enable_mcp(self, name: str) -> bool:
        """启用 MCP 连接的全部工具"""
        entry = self._mcp_clients.get(name)
        if entry is None:
            return False
        wf = self._require_wf()
        for adapter in entry[1]:
            wf.tools_manager.enable_tool(adapter.get_tool_name())
        return True

    def disable_mcp(self, name: str) -> bool:
        """停用 MCP 连接的全部工具"""
        entry = self._mcp_clients.get(name)
        if entry is None:
            return False
        wf = self._require_wf()
        for adapter in entry[1]:
            wf.tools_manager.disable_tool(adapter.get_tool_name())
        return True

    def list_mcp(self) -> list[str]:
        """列出已接入的 MCP 连接名"""
        return list(self._mcp_clients.keys())

    # ---------------- 处理器管理 (注册表实现在 _HandlerRegistryMixin) ----------------

    def _tool_effective(self, tool_name: str) -> bool:
        """工具生效过滤 (执行路径合成): 所属插件缺失或启用时生效"""
        wf = self._wf
        if wf is None:
            return True
        tool = wf.tools_manager.tools.get(tool_name)
        owner = tool.owner_plugin if tool is not None else None
        if owner is None:
            return True
        with self._registry_lock:
            plugin = self._plugins.get(owner)
        return plugin is not None and plugin.enabled

    # ---------------- 插件管理 (目录插件包) ----------------

    async def install_plugin(self, path: str, config: dict[str, Any] | None = None) -> Plugin:
        """安装目录插件 (异步版支持 mcp.py, 自动接入 MCP 客户端)

        任一步失败时回滚已注册能力/MCP 连接与 sys.path, 不留孤儿

        config: 会话级插件配置覆盖, 与全局配置合成后注入 get_tools 工厂
        """
        if self._wf is None:
            await self.initialize()
        plugin_dir = Path(path)
        _add_plugin_sys_path(plugin_dir)
        tool_states: dict[str, bool] = {}
        skill_states: dict[str, bool] = {}
        handler_states: dict[str, bool] = {}
        command_states: dict[str, bool] = {}
        mcp_states: dict[str, bool] = {}
        mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
        try:
            meta = load_plugin_meta(plugin_dir)
            name = str(meta.get("name") or "").strip()
            if not name:
                raise ValueError(f"插件 {path} 的 meta.yaml 缺少 name")
            with self._registry_lock:
                if name in self._plugins:
                    raise ValueError(f"插件 {name} 已安装")
            wf = self._require_wf()

            # 合成插件配置: schema.default < 全局 json < 会话覆盖
            config_schema = parse_config_schema(meta)
            plugin_config = PluginConfigManager().resolve(name, config_schema, config)

            for t in collect_tools(plugin_dir, name, AsyncTool, self, plugin_config):
                tname = t.get_tool_name()
                if tname in wf.tools_manager.tools or tname in tool_states:
                    raise ValueError(f"插件 {name} 的工具 {tname} 与已注册工具冲突")
                t.owner_plugin = name
                wf.tools_manager.register_tool(t)
                tool_states[tname] = True

            mgr = self._get_skills_manager()
            for s in collect_skills(plugin_dir, name):
                key = s.skill_id or s.name
                if key in mgr.skills or key in skill_states:
                    raise ValueError(f"插件 {name} 的技能 {key} 与已加载技能冲突")
                mgr._register_skill(s)
                skill_states[key] = True

            for h in collect_handlers(plugin_dir, name, self):
                self._validate_handler(h)
                _assert_sync_callbacks(h)
                with self._registry_lock:
                    if h.name in self._handlers or h.name in handler_states:
                        raise ValueError(f"插件 {name} 的处理器 {h.name} 与已注册处理器冲突")
                    h.owner_plugin = name
                    self._handler_seq += 1
                    h._seq = self._handler_seq
                    self._handlers[h.name] = h
                handler_states[h.name] = True

            _sync_commands, async_commands = collect_commands(plugin_dir, name, self)
            for cname, chandler in async_commands.items():
                if cname in self.command_handler.commands or cname in command_states:
                    raise ValueError(f"插件 {name} 的命令 {cname} 与已注册命令冲突")
                self.command_handler.register_command(cname, chandler, intro=_command_intro(chandler))
                command_states[cname] = True

            for mcp_name, client in collect_mcp_clients(plugin_dir, name).items():
                if mcp_name in self._mcp_clients or mcp_name in mcp_states:
                    raise ValueError(f"插件 {name} 的 MCP 连接 {mcp_name} 与已接入连接冲突")
                try:
                    adapters = await client.register_tools(wf.tools_manager, name_prefix=name)
                except Exception:
                    close = safe_getattr_callable(client, "close")
                    if close is not None:
                        await close()
                    raise
                for adapter in adapters:
                    adapter.owner_plugin = name
                mcp_clients[mcp_name] = (client, list(adapters))
                mcp_states[mcp_name] = True

            plugin = Plugin(
                name=name,
                version=str(meta.get("version") or ""),
                author=str(meta.get("author") or ""),
                repo=str(meta.get("repo") or ""),
                description=str(meta.get("description") or ""),
                path=str(plugin_dir),
            )
            plugin._session = self
            plugin._cleanup = collect_cleanup(plugin_dir, name, self)
            plugin.tools = tool_states
            plugin.skills = skill_states
            plugin.mcp = mcp_states
            plugin._mcp_clients = mcp_clients
            plugin.handlers = handler_states
            plugin.commands = command_states
            # meta.yaml 能力声明: 读入描述并校验未匹配项 (仅警告, 不改变安装行为)
            plugin.capability_descriptions = parse_capability_descriptions(meta)
            plugin.config_schema = schema_to_payload(config_schema)
            _warn_undeclared_capabilities(name, plugin.capability_descriptions, {
                "tools": tool_states,
                "skills": skill_states,
                "mcp": mcp_states,
                "handlers": handler_states,
                "commands": command_states,
            })
            with self._registry_lock:
                self._plugins[name] = plugin
            logger.info(f"[edictum] 插件 {name} 已安装")
            return plugin
        except Exception:
            _remove_plugin_sys_path(plugin_dir)
            wf = self._wf
            if wf is not None:
                for tname in tool_states:
                    wf.tools_manager.unregister_tool(tname)
            mgr = self._skills_manager
            if mgr is not None:
                for key in skill_states:
                    mgr.unregister_skill(key)
            for hname in handler_states:
                with self._registry_lock:
                    handler = self._take_handler_locked(hname)
                if handler is not None:
                    await asyncio.to_thread(self._close_handler, hname, handler)
            for cname in command_states:
                self.command_handler.unregister_command(cname)
            for mcp_name, (client, adapters) in mcp_clients.items():
                if wf is not None:
                    for adapter in adapters:
                        wf.tools_manager.unregister_tool(adapter.get_tool_name())
                close = safe_getattr_callable(client, "close")
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        pass
            raise

    async def uninstall_plugin(self, name: str) -> bool:
        """卸载插件: 回收其全部能力 (含断开 MCP 连接), 不留孤儿"""
        with self._registry_lock:
            plugin = self._plugins.pop(name, None)
        if plugin is None:
            return False
        _remove_plugin_sys_path(Path(plugin.path))
        wf = self._require_wf()
        for tname in plugin.tools:
            wf.tools_manager.unregister_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for sname in plugin.skills:
                await mgr.deactivate_async(sname, wf)
                mgr.unregister_skill(sname)
        for mcp_name, (client, adapters) in plugin._mcp_clients.items():
            for adapter in adapters:
                wf.tools_manager.unregister_tool(adapter.get_tool_name())
            close = safe_getattr_callable(client, "close")
            if close is not None:
                try:
                    await close()
                except Exception as e:
                    logger.warning(f"[edictum] 插件 {name} 的 MCP {mcp_name} 断开失败: {e}")
        for hname in list(plugin.handlers):
            with self._registry_lock:
                handler = self._take_handler_locked(hname)
            if handler is not None:
                await asyncio.to_thread(self._close_handler, hname, handler)
        for cname in plugin.commands:
            self.command_handler.unregister_command(cname)
        cleanup = plugin._cleanup
        if cleanup is not None:
            try:
                cleanup(self)
            except Exception as e:
                logger.warning(f"[edictum] 插件 {name} 清理回调失败: {e}")
        logger.info(f"[edictum] 插件 {name} 已卸载")
        return True

    async def enable_plugin(self, name: str) -> bool:
        """启用插件 (按独立状态恢复名下能力, 独立停用的保持停用)"""
        with self._registry_lock:
            plugin = self._plugins.get(name)
            if plugin is None:
                return False
            if plugin.enabled:
                return True
            plugin.enabled = True
        wf = self._require_wf()
        # 工具/处理器/MCP 由执行路径合成承担 (effectiveness_guard / _enabled_handlers), 无需恢复循环
        mgr = self._skills_manager
        if mgr is not None:
            for sname, st in plugin.skills.items():
                if st:
                    await mgr.activate_async(sname, wf)
        for cname, st in plugin.commands.items():
            if st:
                self.command_handler.enable_command(cname)
        return True

    async def disable_plugin(self, name: str) -> bool:
        """停用插件 (压制名下全部能力, 不改独立状态; 工具/处理器由执行路径合成压制)"""
        with self._registry_lock:
            plugin = self._plugins.get(name)
            if plugin is None:
                return False
            if not plugin.enabled:
                return True
            plugin.enabled = False
        wf = self._require_wf()
        mgr = self._skills_manager
        if mgr is not None:
            for sname in plugin.skills:
                await mgr.deactivate_async(sname, wf)
        for cname in plugin.commands:
            self.command_handler.disable_command(cname)
        return True

    def list_plugins(self) -> list[Plugin]:
        """列出已安装插件"""
        with self._registry_lock:
            return list(self._plugins.values())

    async def _activate_plugin_skill(self, name: str) -> bool:
        """插件技能独立启用钩子 (异步版)"""
        return await self._get_skills_manager().activate_async(name, self._require_wf())

    async def _deactivate_plugin_skill(self, name: str) -> bool:
        """插件技能独立停用钩子 (异步版)"""
        return await self._get_skills_manager().deactivate_async(name, self._require_wf())

    # ---------------- 模型与流式 ----------------

    def set_llm(self, llm: AsyncLLM):
        """替换主模型 (未初始化时延迟到 initialize 生效)"""
        if self._wf is None:
            self._init_llm = llm
            return
        self._wf.llm = llm

    def set_model_parameters(self, **kwargs: Any):
        """调整主模型调用参数 (未初始化时延迟到 initialize 生效)"""
        if self._wf is None:
            self._init_model_params.update(kwargs)
            return
        self._wf.llm.set_parameters(**kwargs)

    def set_stream_mode(self, stream: bool):
        """切换流式 / 非流式输出"""
        self.stream = bool(stream)

    def reload_llm(self, llm: AsyncLLM):
        """重载 LLM 实例 (AsyncSession 兼容接口)"""
        self.set_llm(llm)
