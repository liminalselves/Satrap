from __future__ import annotations
import asyncio
import inspect, uuid
from pathlib import Path
from typing import Optional, Callable, Any, Awaitable
from typing import TYPE_CHECKING
from satrap.core.utils.context_policy import apply_context_policy
from satrap.core.framework.command import AsyncCommandHandler
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.state.mutation import state_mutation_context
from satrap.core.utils.context import AsyncContextManager, _messages_domain
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
from .utils import _new_async_context
from .sync_session import Session


class AsyncSession(_SessionCore[AsyncContextManager, AsyncCommandHandler]):
    """异步版会话类"""

    def __init_subclass__(cls: type, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        run = cls.__dict__.get("run")
        if run is not None and inspect.iscoroutinefunction(run):

            async def _wrapped_run(self: Any, *args: Any, **kw: Any):
                await self._ensure_initialized()
                return await run(self, *args, **kw)

            cls.run = _wrapped_run

    plugin_override_store: "SessionOverrideStore | None" = None
    plugin_model_manager: "ModelConfigManager | None" = None
    storage_layout: "StorageLayout | None" = None
    storage_platform_id: str | None = None

    def __init__(
        self,
        session_id: str,
        content_callback: Optional[Callable[[str], Awaitable[None]]] | None = None,
        command_handler: Optional[AsyncCommandHandler] | None = None,
        *,
        db_path: str = get_db_path(),
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
    ):
        """
        异步会话框架, 用于管理多个异步模型工作流协作的会话
        任何依赖多模型的复杂异步 Agent 都应继承该类, 并实现 `async run` 方法

        子类初始化时应调用:
        `super().__init__(session_id, content_callback)`

        ``` python
        class MySession(AsyncSession):
            def __init__(self, session_id: str,
                content_callback: Optional[Callable[[str], Awaitable[None]]] | None = None,
                command_handler: Optional[AsyncCommandHandler] | None = None
            ):
                super().__init__(session_id, content_callback, command_handler)

            async def _async_init(self):
                self.workflow = await MyWorkflow.create(...)
                # 异步初始化钩子, 用于创建工作流等

            async def run(self, user_input: str) -> str:
                return await self.workflow.forward(user_input)

        # 直接使用, 无需 create 或 initialize
        session = MySession("user_123", content_callback=print)
        reply = await session.run("你好")
        ```

        参数:
        - session_id: 会话 ID
        - content_callback: 内容回调函数, 用于在复杂调用流程中回传模型内容
        - command_handler: 命令处理器实例, 用于处理用户输入的命令
        - db_path: 会话上下文数据库路径 (与检查点存储同库)
        - state_store: 状态检查点存储实例, 传入后启用会话级检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向 db_path 的 StateStore, 与显式传入 state_store 二选一
        """
        self._state_store = state_store
        if self._state_store is None and enable_checkpoint:
            self._state_store = StateStore(db_path=db_path)
        self._workflow_contexts: dict[str, AsyncContextManager] = {}
        """工作流 ID -> 工作流上下文, 供会话级检查点聚合"""
        self._context_config: LLMConfig | None = None
        """当前会话使用的模型上下文配置"""

        self.session_ctx = _new_async_context(session_id, db_path=db_path)
        self.session_id = session_id
        self.wf_list: list[str] = []
        self.content_callback = content_callback
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._user_manager: UserManager | None = None

        if self._state_store is not None:
            self._state_store.register_domain(_messages_domain())
            self.session_ctx.state_store = self._state_store

        self.command_handler = (
            command_handler if command_handler else AsyncCommandHandler()
        )
        # 如果未提供命令处理器, 则创建一个空的命令处理器实例

    async def _ensure_initialized(self) -> None:
        """确保异步初始化完成 (幂等)"""
        await self.initialize()

    async def initialize(self) -> None:
        """执行实际初始化, 可被子类重写, 但需调用 super().initialize()"""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self.session_ctx.initialize()
            await self._async_init()  # 钩子: 子类可在此创建工作流等
            self._initialized = True

    async def _async_init(self):
        """子类可重写的异步初始化钩子"""
        pass

    async def _content_callback(self, content: str):
        """
        调用回调返回模型回复内容

        参数:
        - content: 内容
        """
        if self.content_callback and content:
            await self.content_callback(content)

    @property
    def user_contexts(self) -> list[str]:
        """
        获取当前用户的所有上下文 session_id 列表

        返回:
        - list[str]: 当前用户的所有上下文 session_id 列表
        """
        if not self._user_manager:
            return []
        user_id = Session._parse_user_id(self.session_id)
        if not user_id:
            return []
        return self._user_manager.get_user_session_ids(user_id)

    async def on_session_switched(
        self, old_session_id: str, new_session_id: str
    ) -> None:
        """
        上下文切换后调用, 子类可重写以刷新工作流

        参数:
        - old_session_id: old会话ID
        - new_session_id: new会话ID

        返回:
        - None: 上下文切换后调用, 子类可重写以刷新工作流
        """
        return None

    def reload_llm(self, llm: AsyncLLM):
        """
        重载 LLM 实例, 子类可重写以更新工作流内的 LLM 引用

        参数:
        - llm: 模型实例
        """

    async def run(self, *input: Any, **kwargs: Any) -> Any:
        """
        执行会话

        参数:
        - input: 输入
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行会话
        """
        return None

    def _all_contexts(self) -> dict[str, AsyncContextManager]:
        """
        返回 {上下文名: AsyncContextManager}, 含会话共享上下文与全部工作流上下文

        返回:
        - dict[str, AsyncContextManager]:  {上下文名: AsyncContextManager}, 含会话共享上下文与全部工作流上下文
        """
        contexts: dict[str, AsyncContextManager] = {"session": self.session_ctx}
        contexts.update(self._workflow_contexts)
        return contexts

    def _track_workflow_context(self, wf_id: str, ctx: AsyncContextManager) -> None:
        """
        注册工作流上下文, 使其纳入会话级检查点聚合

        参数:
        - wf_id: 工作流 ID (workflow_id_assign 的返回值)
        - ctx: 工作流的 AsyncContextManager 实例
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

    async def create_checkpoint(self, name: str = "", description: str = "") -> str:
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
                    await ctx.create_checkpoint(
                        name=f"{name}[{ctx_name}]" if name else ctx_name,
                        description=description,
                        batch_id=batch_id,
                    )
        except Exception:
            store = self._require_session_store()
            # 补偿: 删除已创建的残批检查点, 避免部分作用域回滚的不一致状态
            for ctx in self._all_contexts().values():
                for cp in await asyncio.to_thread(store.list_checkpoints, ctx._scope()):
                    if cp.batch_id == batch_id:
                        await asyncio.to_thread(
                            store.delete_checkpoint, cp.checkpoint_id
                        )
            raise
        return batch_id

    async def list_checkpoints(self) -> list[StateCheckpoint]:
        """
        列出会话的全部聚合检查点 (按批次去重, 时间升序)

        返回:
        - list[StateCheckpoint]: 检查点列表, 每个批次一个代表检查点
        """
        store = self._require_session_store()
        seen: dict[str, StateCheckpoint] = {}
        for ctx in self._all_contexts().values():
            for cp in await asyncio.to_thread(store.list_checkpoints, ctx._scope()):
                if cp.batch_id:
                    seen.setdefault(cp.batch_id, cp)
                else:
                    seen.setdefault(cp.checkpoint_id, cp)
        return list(seen.values())

    async def rollback(self, checkpoint_id: str) -> None:
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
                await asyncio.to_thread(store.rollback_batch, target)
            else:
                await asyncio.to_thread(store.rollback, target)  # 单检查点 (非聚合)
        for ctx in self._all_contexts().values():
            await ctx.load_context()

    async def retry(self, checkpoint_id: str) -> None:
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
                await asyncio.to_thread(store.retry_batch, target)
            else:
                await asyncio.to_thread(store.retry, target)  # 单检查点 (非聚合)
        for ctx in self._all_contexts().values():
            await ctx.load_context()

    async def list_branches(self) -> list[StateCheckpoint]:
        """
        列出从本会话 fork 出的全部分支起点检查点 (会话共享 + 各工作流)

        返回:
        - list[StateCheckpoint]: 分支检查点列表 (按时间升序)
        """
        store = self._require_session_store()
        branches: list[StateCheckpoint] = []
        for ctx in self._all_contexts().values():
            branches.extend(
                await asyncio.to_thread(
                    store.list_branches, f"{ctx.conversation_id}:fork:"
                )
            )
        return branches

    async def list_mutations(self) -> list[StateCheckpoint]:
        """
        列出会话全部上下文的检查点变更记录 (最新在前), 含审计字段 source / reason

        返回:
        - list[StateCheckpoint]: 变更记录列表 (按创建时间倒序)
        """
        store = self._require_session_store()
        mutations: list[StateCheckpoint] = []
        for ctx in self._all_contexts().values():
            mutations.extend(
                await asyncio.to_thread(store.list_mutations, ctx._scope())
            )
        mutations.sort(key=lambda cp: cp.created_at, reverse=True)
        return mutations

    async def fork(
        self,
        branch_name: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, AsyncContextManager]:
        """
        从指定检查点 (默认最近一个) fork 会话下全部上下文

        参数:
        - branch_name: 分支名称, 新上下文 ID 形如 "{原ID}:fork:{分支名}"
        - checkpoint_id: 源检查点 ID, 默认最近一个批次

        返回:
        - dict[str, AsyncContextManager]: {上下文名: 新上下文管理器} (已初始化)

        异常:
        - ValueError: 会话没有检查点, 或指定检查点不存在
        """
        store = self._require_session_store()
        checkpoints = await self.list_checkpoints()
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
        batch = await asyncio.to_thread(store.list_checkpoints_by_batch, batch_id)
        by_scope = {cp.scope_id: cp for cp in (batch if batch else [source])}
        new_ctxs: dict[str, AsyncContextManager] = {}
        for ctx_name, ctx in self._all_contexts().items():
            cp = by_scope.get(ctx.conversation_id)
            if cp is None:
                logger.warning(f"[会话] 上下文 {ctx_name} 无对应检查点, fork 跳过")
                continue
            new_ctxs[ctx_name] = await ctx.fork(
                branch_name, checkpoint_id=cp.checkpoint_id
            )
        return new_ctxs

    async def clear_memory(self):
        """清除会话内存"""
        try:
            await self.initialize()
            contexts = self._all_contexts()
            for context in contexts.values():
                await context.initialize()
                await context.del_context()
            tracked_ids = {context.conversation_id for context in contexts.values()}
            for workflow_id in self.wf_list:
                if workflow_id in tracked_ids:
                    continue
                workflow_context = _new_async_context(
                    workflow_id,
                    db_path=self.session_ctx.db_path,
                )
                await workflow_context.initialize()
                await workflow_context.del_context()
            logger.info("[会话管理器] 清除工作流上下文完成")

        except Exception as e:
            logger.error(f"[会话管理器] 清除会话上下文错误: {e}")

    async def cmd_process(self, msg: str) -> tuple[Any, bool]:
        """
        处理命令字符串

        参数:
        - msg: 输入消息

        返回:
        - (Any, bool): 命令执行结果和是否为命令消息的元组
        """
        return await self.command_handler.process_message(msg)

    async def __call__(self, *input: Any, **kwargs: Any):
        await self._ensure_initialized()
        result = await self.run(*input, **kwargs)
        return result
