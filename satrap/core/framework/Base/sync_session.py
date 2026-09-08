from __future__ import annotations
import uuid
from pathlib import Path
from typing import Optional, Callable, Any
from typing import TYPE_CHECKING
from satrap.core.utils.context_policy import apply_context_policy
from satrap.core.framework.command import CommandHandler
from satrap.core.APICall.LLMCall import LLM
from satrap.core.state.mutation import state_mutation_context
from satrap.core.utils.context import ContextManager, _messages_domain
from satrap.core.utils.paths import get_db_path
from satrap.core.state import StateStore
from satrap.core.type import LLMConfig, StateCheckpoint
from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.core.storage.layout import StorageLayout
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.framework.UserManager import UserManager

from .base import _SessionCore
from .utils import _new_context


class Session(_SessionCore[ContextManager, CommandHandler]):
    """会话类, 用于管理多个模型工作流的会话"""

    plugin_override_store: "SessionOverrideStore | None" = None
    plugin_model_manager: "ModelConfigManager | None" = None
    storage_layout: "StorageLayout | None" = None
    storage_platform_id: str | None = None

    def __init__(
        self,
        session_id: str,
        content_callback: Optional[Callable[[str], None]] | None = None,
        command_handler: Optional[CommandHandler] | None = None,
        *,
        db_path: str = get_db_path(),
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
    ):
        """
        会话框架, 用于管理多个模型工作流的会话
        任何依赖多模型的复杂 Agent 都应当继承自该类, 并实现 `forward` 方法

        并在初始化时进行 `super().__init__(session_id, content_callback, command_handler)`

        参数:
        - session_id: 会话 ID
        - content_callback: 内容回调函数, 用于在复杂模型调用过程中抛出模型回复内容; 如果只取最终回复, 则可以设置为 None
        - command_handler: 命令处理程序实例
        - db_path: 会话上下文数据库路径 (与检查点存储同库)
        - state_store: 状态检查点存储实例, 传入后启用会话级检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向 db_path 的 StateStore, 与显式传入 state_store 二选一
        """
        self._state_store = state_store
        if self._state_store is None and enable_checkpoint:
            self._state_store = StateStore(db_path=db_path)
        self._workflow_contexts: dict[str, ContextManager] = {}
        """工作流 ID -> 工作流上下文, 供会话级检查点聚合"""
        self._context_config: LLMConfig | None = None
        """当前会话使用的模型上下文配置"""

        self.session_ctx = _new_context(session_id, db_path=db_path)
        """会话共享上下文"""

        if self._state_store is not None:
            self._state_store.register_domain(_messages_domain())
            self.session_ctx.state_store = self._state_store

        if (
            command_handler is None
        ):  # 创建默认命令处理器, 输出回调指向 _content_callback
            self.cmd_handler = CommandHandler(output_callback=self._content_callback)

        else:  # 确保输出回调被设置, 默认指向 _content_callback
            self.cmd_handler = command_handler
            if self.cmd_handler.output_callback is None:
                self.cmd_handler.output_callback = self._content_callback

        self.command_handler = self.cmd_handler
        self.session_ctx.load_context()
        self.session_id = session_id
        self.wf_list: list[str] = []

        self.content_callback = content_callback
        self._user_manager: UserManager | None = None

    def _content_callback(self, content: str):
        """
        调用回调返回模型回复内容

        参数:
        - content: 内容
        """
        if self.content_callback and content:
            self.content_callback(content)

    @staticmethod
    def _parse_user_id(session_id: str) -> str:
        """
        从 session_id 中提取 user_id, 兼容新旧格式

        参数:
        - session_id: 会话 ID

        返回:
        - str: 从 session_id 中提取 user_id, 兼容新旧格式
        """
        parts = session_id.split(":")
        if len(parts) >= 4:
            return parts[2]
        if len(parts) == 3:
            return parts[1]
        return ""

    @property
    def user_contexts(self) -> list[str]:
        """
        获取当前用户的所有上下文 session_id 列表

        返回:
        - list[str]: 当前用户的所有上下文 session_id 列表
        """
        if not self._user_manager:
            return []
        user_id = self._parse_user_id(self.session_id)
        if not user_id:
            return []
        return self._user_manager.get_user_session_ids(user_id)

    def on_session_switched(self, old_session_id: str, new_session_id: str) -> None:
        """
        上下文切换后调用, 子类可重写以刷新工作流

        参数:
        - old_session_id: old会话ID
        - new_session_id: new会话ID

        返回:
        - None: 上下文切换后调用, 子类可重写以刷新工作流
        """
        return None

    def reload_llm(self, llm: LLM):
        """
        重载 LLM 实例, 子类可重写以更新工作流内的 LLM 引用

        参数:
        - llm: 模型实例
        """

    def run(self, *input: Any, **kwargs: Any) -> Any:
        """
        执行会话; 调用模型并返回结果

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行会话; 调用模型并返回结果
        """
        return None

    def _all_contexts(self) -> dict[str, ContextManager]:
        """
        返回 {上下文名: ContextManager}, 含会话共享上下文与全部工作流上下文

        返回:
        - dict[str, ContextManager]:  {上下文名: ContextManager}, 含会话共享上下文与全部工作流上下文
        """
        contexts: dict[str, ContextManager] = {"session": self.session_ctx}
        contexts.update(self._workflow_contexts)
        return contexts

    def _track_workflow_context(self, wf_id: str, ctx: ContextManager) -> None:
        """
        注册工作流上下文, 使其纳入会话级检查点聚合

        参数:
        - wf_id: 工作流 ID (workflow_id_assign 的返回值)
        - ctx: 工作流的 ContextManager 实例
        """
        if wf_id in self._workflow_contexts:
            logger.warning(f"[会话] 工作流 {wf_id} 已注册, 将被覆盖")
        self._workflow_contexts[wf_id] = ctx
        if self._context_config is not None:
            apply_context_policy(ctx, self._context_config)
        if self._state_store is not None:
            if (
                Path(str(ctx.db_path)).resolve()
                != Path(str(self._state_store.db_path)).resolve()
            ):
                raise ValueError(
                    f"工作流 {wf_id} 的上下文库 {ctx.db_path} 与会话状态库 {self._state_store.db_path} 不一致"
                )
            if ctx.state_store is None:
                ctx.state_store = self._state_store
                self._state_store.register_domain(_messages_domain())

    def create_checkpoint(self, name: str = "", description: str = "") -> str:
        """
        为会话创建聚合检查点 (会话共享上下文 + 全部工作流上下文), 返回批次 ID

        参数:
        - name: 检查点显示名称
        - description: 检查点说明

        返回:
        - str: 批次 ID, 用于 list_checkpoints / rollback
        """
        self._require_session_store()
        batch_id = f"batch-{uuid.uuid4().hex[:16]}"
        try:
            with state_mutation_context(
                source="session_checkpoint", reason=f"创建会话检查点 {name or batch_id}"
            ):
                for ctx_name, ctx in self._all_contexts().items():
                    ctx.create_checkpoint(
                        name=f"{name}[{ctx_name}]" if name else ctx_name,
                        description=description,
                        batch_id=batch_id,
                    )
        except Exception:
            store = self._require_session_store()
            # 补偿: 删除已创建的残批检查点, 避免部分作用域回滚的不一致状态
            for ctx in self._all_contexts().values():
                for cp in store.list_checkpoints(ctx._scope()):
                    if cp.batch_id == batch_id:
                        store.delete_checkpoint(cp.checkpoint_id)
            raise
        return batch_id

    def list_checkpoints(self) -> list[StateCheckpoint]:
        """
        列出会话的全部聚合检查点 (按批次去重, 时间升序)

        返回:
        - list[StateCheckpoint]: 检查点列表, 每个批次一个代表检查点
        """
        store = self._require_session_store()
        seen: dict[str, StateCheckpoint] = {}
        for ctx in self._all_contexts().values():
            for cp in store.list_checkpoints(ctx._scope()):
                if cp.batch_id:
                    seen.setdefault(cp.batch_id, cp)
                else:
                    seen.setdefault(cp.checkpoint_id, cp)
        return list(seen.values())

    def rollback(self, checkpoint_id: str) -> None:
        """
        回滚会话到指定检查点批次并重载全部上下文

        参数:
        - checkpoint_id: 批次 ID 或批次内任一检查点的 ID
        """
        store = self._require_session_store()
        is_batch, target = self._resolve_batch_id(store, checkpoint_id)
        with state_mutation_context(
            source="session_checkpoint_rollback", reason=f"回滚到检查点 {checkpoint_id}"
        ):
            if is_batch:
                store.rollback_batch(target)
            else:
                store.rollback(target)  # 单检查点 (非聚合)
        for ctx in self._all_contexts().values():
            ctx.load_context()

    def retry(self, checkpoint_id: str) -> None:
        """
        从指定检查点批次重试并重载全部上下文 (保留未来检查点)

        参数:
        - checkpoint_id: 批次 ID 或批次内任一检查点的 ID
        """
        store = self._require_session_store()
        is_batch, target = self._resolve_batch_id(store, checkpoint_id)
        with state_mutation_context(
            source="session_checkpoint_retry", reason=f"重试到检查点 {checkpoint_id}"
        ):
            if is_batch:
                store.retry_batch(target)
            else:
                store.retry(target)  # 单检查点 (非聚合)
        for ctx in self._all_contexts().values():
            ctx.load_context()

    def list_branches(self) -> list[StateCheckpoint]:
        """
        列出从本会话 fork 出的全部分支起点检查点 (会话共享 + 各工作流)

        返回:
        - list[StateCheckpoint]: 分支检查点列表 (按时间升序)
        """
        store = self._require_session_store()
        branches: list[StateCheckpoint] = []
        for ctx in self._all_contexts().values():
            branches.extend(store.list_branches(f"{ctx.conversation_id}:fork:"))
        return branches

    def list_mutations(self) -> list[StateCheckpoint]:
        """
        列出会话全部上下文的检查点变更记录 (最新在前), 含审计字段 source / reason

        返回:
        - list[StateCheckpoint]: 变更记录列表 (按创建时间倒序)
        """
        store = self._require_session_store()
        mutations: list[StateCheckpoint] = []
        for ctx in self._all_contexts().values():
            mutations.extend(store.list_mutations(ctx._scope()))
        mutations.sort(key=lambda cp: cp.created_at, reverse=True)
        return mutations

    def fork(
        self,
        branch_name: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, ContextManager]:
        """
        从指定检查点 (默认最近一个) fork 会话下全部上下文

        参数:
        - branch_name: 分支名称, 新上下文 ID 形如 "{原ID}:fork:{分支名}"
        - checkpoint_id: 源检查点 ID, 默认最近一个批次

        返回:
        - dict[str, ContextManager]: {上下文名: 新上下文管理器} ("session" 与会话内各工作流)

        异常:
        - ValueError: 会话没有检查点, 或指定检查点不存在
        """
        store = self._require_session_store()
        checkpoints = self.list_checkpoints()
        if not checkpoints:
            raise ValueError("当前会话没有检查点, 请先创建检查点")
        if checkpoint_id is not None:
            source = next(
                (cp for cp in checkpoints if cp.checkpoint_id == checkpoint_id), None
            )
            if source is None:
                _, batch_id = self._resolve_batch_id(store, checkpoint_id)
                source = next(
                    (cp for cp in checkpoints if cp.batch_id == batch_id), None
                )

            if source is None:
                raise ValueError(f"检查点不存在: {checkpoint_id}")
        else:
            source = checkpoints[-1]

        batch_id = source.batch_id or source.checkpoint_id
        batch = store.list_checkpoints_by_batch(batch_id)
        by_scope = {cp.scope_id: cp for cp in (batch if batch else [source])}
        new_ctxs: dict[str, ContextManager] = {}

        for ctx_name, ctx in self._all_contexts().items():
            cp = by_scope.get(ctx.conversation_id)
            if cp is None:
                logger.warning(f"[会话] 上下文 {ctx_name} 无对应检查点, fork 跳过")
                continue

            new_ctxs[ctx_name] = ctx.fork(branch_name, checkpoint_id=cp.checkpoint_id)

        return new_ctxs

    def clear_memory(self):
        """清除会话内存"""
        try:
            contexts = self._all_contexts()
            for context in contexts.values():
                context.del_context()

            tracked_ids = {context.conversation_id for context in contexts.values()}
            for workflow_id in self.wf_list:
                if workflow_id in tracked_ids:
                    continue
                workflow_context = _new_context(
                    workflow_id,
                    db_path=self.session_ctx.db_path,
                )
                try:
                    workflow_context.del_context()
                finally:
                    workflow_context.close()

            logger.info("[会话管理器] 清除工作流上下文完成")

        except Exception as e:
            logger.error(f"[会话管理器] 清除会话上下文错误: {e}")

    def cmd_process(self, msg: str) -> tuple[Any, bool]:
        """
        处理命令字符串

        参数:
        - msg: 输入消息

        返回:
        - (Any, bool): 命令执行结果和是否为命令消息的元组
        """
        return self.command_handler.process_message(msg)

    def __call__(self, *input: Any, **kwargs: Any):
        result = self.run(*input, **kwargs)
        return result
