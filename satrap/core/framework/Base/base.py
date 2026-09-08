from __future__ import annotations
import copy
from typing import Callable, Any, TypeVar
from typing import TYPE_CHECKING
from satrap.core.utils.context_policy import apply_context_policy
from satrap.core.framework.command import CommandHandler, AsyncCommandHandler
from satrap.core.utils.context import (
    AsyncContextManager,
    ContextManager,
    PreparedModelContext,
)
from satrap.core.state import StateStore
from satrap.core.type import LLMConfig, ModelContextTurnStats, TokenUsage
from satrap.core.log import logger
from .utils import _build_context_request_stats

if TYPE_CHECKING:
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.storage.layout import StorageLayout
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.UserManager import UserManager

from typing import Generic


class _WorkflowCore:
    _context_turn_stats: ModelContextTurnStats

    def reset_context_stats(self) -> None:
        """清空当前轮次的模型上下文统计"""
        self._context_turn_stats = ModelContextTurnStats()

    def get_context_stats(self) -> dict[str, Any] | None:
        """
        返回当前轮次的模型上下文统计

        返回:
        - dict[str, Any] | None: 没有模型请求时为 None
        """
        payload = self._context_turn_stats.to_dict()
        return payload or None

    def _record_context_request(
        self,
        prepared: PreparedModelContext,
        usage: TokenUsage | None,
    ) -> None:
        """
        记录一次正式模型请求

        参数:
        - prepared: 上下文准备结果
        - usage: API 返回的真实 token 用量
        """
        self._context_turn_stats.requests.append(
            _build_context_request_stats(prepared, usage)
        )

    @staticmethod
    def get_bot_message(messages: list[dict[str, str | list[Any]]]) -> str:
        """
        获取最后一条 assistant 回复

        参数:
        - messages: 消息列表

        返回:
        - str: 最后一条 assistant 回复
        """
        for message in reversed(messages):
            if message["role"] == "assistant":
                return str(message["content"])
        return ""

    @staticmethod
    def _get_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        提取系统消息副本

        参数:
        - messages: 消息列表

        返回:
        - list[dict[str, Any]]: 提取系统消息副本
        """
        return [
            copy.deepcopy(message)
            for message in messages
            if message.get("role") == "system"
        ]


_ContextT = TypeVar("_ContextT", ContextManager, AsyncContextManager)


_CommandT = TypeVar("_CommandT", CommandHandler, AsyncCommandHandler)


class _SessionCore(Generic[_ContextT, _CommandT]):
    session_id: str
    wf_list: list[str]
    _state_store: StateStore | None
    _context_config: LLMConfig | None
    command_handler: _CommandT

    def _all_contexts(self) -> dict[str, _ContextT]:
        """由具体会话提供参与操作的上下文集合"""
        raise NotImplementedError

    def workflow_id_assign(self, wf_id: str) -> str:
        """
        为会话分配工作流 ID

        重复的 wf_id 自动追加序号 (如 main -> main_2) 并记录警告, 避免工作流上下文互相污染

        参数:
        - wf_id: 工作流 ID

        返回:
        - 工作流 ID (str) (session_id + "_" + wf_id)
        """
        workflow_id = self.session_id + "_" + wf_id
        if workflow_id in self.wf_list:
            logger.warning(f"[会话] 工作流 ID 重复, 自动追加序号: {wf_id} -> {wf_id}_2")
            suffix = 2
            while f"{self.session_id}_{wf_id}_{suffix}" in self.wf_list:
                suffix += 1
            workflow_id = f"{self.session_id}_{wf_id}_{suffix}"
        self.wf_list.append(workflow_id)
        return workflow_id

    def _require_session_store(self) -> StateStore:
        """
        获取会话级状态存储, 未启用时抛出 ValueError

        返回:
        - StateStore: 会话级状态存储, 未启用时抛出 ValueError
        """
        if self._state_store is None:
            raise ValueError(
                "未启用会话级状态检查点, 请传入 state_store 或设置 enable_checkpoint=True"
            )
        return self._state_store

    def apply_context_config(self, config: LLMConfig) -> None:
        """
        将模型上下文配置应用到会话及全部工作流上下文

        参数:
        - config: LLM 配置
        """
        self._context_config = config
        for context in self._all_contexts().values():
            apply_context_policy(context, config)

    def _resolve_batch_id(
        self, store: StateStore, checkpoint_id: str
    ) -> tuple[bool, str]:
        """
        把检查点 ID 或批次 ID 解析为 (是否批次, 目标 ID) (用于 rollback / fork)

        参数:
        - store: 存储实例
        - checkpoint_id: 检查点 ID

        单检查点 (无批次) 时返回 (False, 检查点 ID)

        返回:
        - tuple[bool, str]: 把检查点 ID 或批次 ID 解析为 (是否批次, 目标 ID) (用于 rollback / fork)
        """
        cp = store.get_checkpoint(checkpoint_id)
        if cp is not None:
            return (bool(cp.batch_id), cp.batch_id or cp.checkpoint_id)
        if store.list_checkpoints_by_batch(checkpoint_id):
            return (True, checkpoint_id)
        raise ValueError(f"检查点不存在: {checkpoint_id}")

    def register_command(
        self, name: str, handler: Callable[..., Any], intro: str = "None"
    ):
        """
        注册命令处理函数

        参数:
        - name: 名称
        - handler: 处理器
        - intro: 简介文本
        """
        self.command_handler.register_command(name, handler, intro)
