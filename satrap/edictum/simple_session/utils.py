"""
edictum 简易单工作流 Agent 系统

SimpleSession / AsyncSimpleSession: 基于 Session 的高可扩展单 workflow Agent 框架
- 单个主 workflow / 单一主模型, 内部直接使用 full_agent (React 范式)
- 命令 / 工具 / MCP / skill / 处理器 / 插件六类能力的注入, 删除, 启用/停用, 查看
- 处理器 (SessionHandler): 函数直注流程 (非 hook), 4 处理点, 优先级排序, before 可改写/拦截
- 插件 (Plugin): 目录化组合包 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py), 双层启停
- checkpoint 全套 (继承 Session 聚合检查点)
- 多模态 (img_urls) + 流式/非流式切换
"""

from __future__ import annotations
from dataclasses import dataclass
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable
import sys
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.core.log import logger


SyncUserInputProvider = Callable[..., str]

AsyncUserInputProvider = Callable[..., str | Awaitable[str]]


class HandlerResult:
    """
    before_user_send 第三态返回: 短路指令 (文本均为 str, 与 run() -> str 契约对齐)

    - continue_with(text): 等价 str 改写, 继续执行后续 handler
    - respond(text): 跳过模型直接返回 text (后续 handler 短路, after_model_reply 仍执行)
    - reject(reason): 业务拒绝, reason 作为 run 的返回文本 (正常返回)
    - abort(reason): 故障终止, 抛 HandlerAbortError(reason)
    """

    __slots__ = ("action", "text")

    def __init__(self, action: str, text: str) -> None:
        """
        初始化 HandlerResult

        参数:
        - action: 操作类型
        - text: 文本内容
        """
        if action not in ("continue", "respond", "reject", "abort"):
            raise ValueError(f"未知 HandlerResult action: {action!r}")
        self.action = action
        self.text = text

    @classmethod
    def continue_with(cls, text: str) -> "HandlerResult":
        """
        改写输入并继续

        参数:
        - text: 待处理文本

        返回:
        - 'HandlerResult': 改写输入并继续
        """
        return cls("continue", text)

    @classmethod
    def respond(cls, text: str) -> "HandlerResult":
        """
        跳过模型直接返回 text

        参数:
        - text: 待处理文本

        返回:
        - 'HandlerResult':  text
        """
        return cls("respond", text)

    @classmethod
    def reject(cls, reason: str) -> "HandlerResult":
        """
        业务拒绝, reason 作为返回文本

        参数:
        - reason: 原因说明

        返回:
        - 'HandlerResult': 文本
        """
        return cls("reject", reason)

    @classmethod
    def abort(cls, reason: str) -> "HandlerResult":
        """
        故障终止, 抛 HandlerAbortError

        参数:
        - reason: 原因说明

        返回:
        - 'HandlerResult': 故障终止, 抛 HandlerAbortError
        """
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
    """
    一轮 run 的上下文: config 为冻结只读快照; text/error/outcome 为运行期可变状态

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
    """
    协议级校验: 全同步协议拒绝任何 coroutinefunction 回调 (add_handler 与插件安装共用)

    参数:
    - handler: 处理器

    运行时类型约束无效: 误传 async def 会在同步路径静默忽略, 在 to_thread 路径产生
    无人 await 的 coroutine, 均导致回调不生效 -- 故统一显式拒绝
    """
    for fn in (
        handler.before_user_send,
        handler.after_user_send,
        handler.before_model_reply,
        handler.after_model_reply,
    ):
        if fn is not None and inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"处理器 {handler.name} 含异步回调, 全同步协议不支持 (回调请改为同步函数)"
            )


def _command_intro(handler: Callable[..., Any]) -> str:
    """
    从命令函数 docstring 提取简介 (首行)

    参数:
    - handler: 处理器

    返回:
    - str: 从命令函数 docstring 提取简介 (首行)
    """
    doc = inspect.getdoc(handler)
    if doc:
        first = doc.strip().splitlines()[0].strip()
        if first:
            return first
    return "None"


def _add_plugin_sys_path(plugin_dir: Path) -> None:
    """
    将插件目录加入 sys.path, 使插件内私有模块可互相 import (如 core.permission)

    参数:
    - plugin_dir: 插件目录
    """
    plugin_path = str(plugin_dir.resolve())
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)


def _remove_plugin_sys_path(plugin_dir: Path) -> None:
    """
    卸载时尝试从 sys.path 移除插件目录 (多个插件共用时静默忽略)

    参数:
    - plugin_dir: 插件目录
    """
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
    """
    校验 meta.yaml 声明的能力在实际扫描结果中存在, 声明了但扫描不到仅警告 (不改变安装行为)

    参数:
    - plugin_name: 插件名称
    - descriptions: 说明集合
    - scanned: scanned 输入值

    descriptions: parse_capability_descriptions 结果 (kind -> {名字: 描述})
    scanned: 各类实际扫描到的能力 (kind -> {名字: 独立启用状态})
    """
    for kind, declared in descriptions.items():
        found = scanned.get(kind, {})
        for cap_name in declared:
            if cap_name not in found:
                logger.warning(
                    f"[插件] 插件 {plugin_name} 声明的 {kind} 能力 {cap_name} 未在插件中发现"
                )


@dataclass
class SessionHandler:
    """
    处理器: 4 个处理点直接注入流程, 非 hook

    - before_user_send: 用户消息处理前; 返回 str / HandlerResult.continue_with 改写输入,
      None 透传; respond / reject 短路跳过模型; abort 抛 HandlerAbortError
    - after_user_send: 用户消息处理后 (通知, 收到改写后的文本与上下文)
    - before_model_reply: Agent/模型调用前 (通知; 命名沿用历史, 语义为"Agent 调用前")
    - after_model_reply: 模型回复后 (通知, 收到最终回复文本; 返回 str 改写最终结果;
      异常路径 result=None 且 ctx.error 非 None 时返回值被忽略)
    - priority: 越小越先执行; 同值按注册顺序执行 (稳定契约)
    - enabled: 独立启用位; 实际生效 = enabled AND 所属插件 enabled (执行路径合成)
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
        """
        释放 handler 持有的资源 (句柄/连接/线程/缓存任务)

        契约: 同步函数 (异步清理需以同步手段完成或经 to_thread 包装); 幂等 (可重复调用);
        容忍回调曾被超时取消的中间状态; 异常由调用方记日志不阻断; 子类覆盖实现具体清理
        """


def _plugin_config_manager() -> PluginConfigManager:
    """通过兼容入口创建插件配置服务, 保留调用方注入能力"""
    from . import PluginConfigManager

    return PluginConfigManager()
