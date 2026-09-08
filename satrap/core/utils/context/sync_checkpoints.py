from __future__ import annotations
from typing import List, Optional, TYPE_CHECKING
from satrap.core.state.mutation import state_mutation_context
from satrap.core.type import StateCheckpoint
from satrap.core.log import logger

if TYPE_CHECKING:
    from .sync import ContextManager
    from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from .sync_storage import _SyncStorage


class _SyncCheckpoints(_SyncStorage):

    def _maybe_auto_checkpoint(self) -> None:
        """消息写入后自动保存稳定检查点 (同水位去重, 指针式零快照, 失败不阻断主流程)"""
        if not self.auto_checkpoint or self.state_store is None:
            return
        try:
            self.state_store.ensure_stable_checkpoint(self._scope())
        except Exception as e:
            logger.warning(f"[上下文管理器] 自动检查点保存失败: {e}")

    def _protect_before_edit(self) -> None:
        """
        编辑类操作前: 物化当前状态为保护检查点, 并清理失效的指针检查点

        追加式消息被改写/删除后, 旧指针检查点的水位截断语义失效,
        因此物化保护后删除该作用域全部指针检查点 (保护检查点保留, 可撤销编辑)
        失败不阻断编辑主流程
        """
        if self.state_store is None:
            return
        try:
            with state_mutation_context(source="edit_protect", reason="编辑前状态保护"):
                self.state_store.materialize_edit_protection(self._scope())
        except Exception as e:
            logger.warning(f"[上下文管理器] 编辑前状态保护失败: {e}")

    def create_checkpoint(
        self, name: str = "", description: str = "", batch_id: str = ""
    ) -> StateCheckpoint:
        """
        为当前对话创建状态检查点

        参数:
        - name: 检查点显示名称
        - description: 检查点说明
        - batch_id: 会话级聚合检查点的批次 ID, 同批检查点共享; 非聚合时留空

        返回:
        - StateCheckpoint: 创建的检查点
        """
        return self._require_state_store().create_checkpoint(
            self._scope(), name=name, description=description, batch_id=batch_id
        )

    def list_checkpoints(self) -> List[StateCheckpoint]:
        """
        列出当前对话的全部检查点 (按创建时间升序)

        返回:
        - List[StateCheckpoint]: 检查点列表
        """
        return self._require_state_store().list_checkpoints(self._scope())

    def rollback(self, checkpoint_id: str) -> None:
        """
        回滚当前对话到指定检查点并重载上下文

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_rollback", reason=f"回滚到检查点 {checkpoint_id}"
        ):
            store.rollback(checkpoint_id)
        self.load_context()
        self._invalidate_summary(persist=True)

    def retry(self, checkpoint_id: str) -> None:
        """
        从指定检查点重试并重载上下文 (保留未来检查点)

        参数:
        - checkpoint_id: 目标检查点 ID
        """
        store = self._require_state_store()
        with state_mutation_context(
            source="checkpoint_retry", reason=f"重试到检查点 {checkpoint_id}"
        ):
            store.retry(checkpoint_id)
        self.load_context()
        self._invalidate_summary(persist=True)

    def fork(
        self,
        branch_name: str,
        checkpoint_id: Optional[str] = None,
    ) -> "ContextManager":
        """
        从指定检查点 (默认最近一个) fork 一条新剧情线, 返回新的上下文管理器

        参数:
        - branch_name: 分支名称, 新对话 ID 形如 "{原ID}:fork:{分支名}"
        - checkpoint_id: 源检查点 ID, 默认最近一个检查点

        返回:
        - ContextManager: 新分支的上下文管理器

        异常:
        - ValueError: 当前对话没有检查点
        """
        from .sync import ContextManager

        store = self._require_state_store()
        checkpoints = store.list_checkpoints(self._scope())
        if not checkpoints:
            raise ValueError("当前对话没有检查点, 请先创建检查点")
        source_id = checkpoint_id or checkpoints[-1].checkpoint_id
        new_conversation_id = f"{self.conversation_id}:fork:{branch_name}"
        with state_mutation_context(
            source="checkpoint_fork", reason=f"从检查点 {source_id} 分支"
        ):
            store.fork(source_id, new_conversation_id)
        return ContextManager(
            new_conversation_id,
            keep_in_memory=self.keep_in_memory,
            db_path=self.db_path,
            max_context=self.max_context,
            history_ratio=self.history_ratio,
            context_threshold=self.context_threshold,
            truncation_floor=self.truncation_floor,
            exceed_process=self.exceed_process,
            summary_keep_recent_turns=self.summary_keep_recent_turns,
            state_store=store,
            auto_checkpoint=self.auto_checkpoint,
        )
