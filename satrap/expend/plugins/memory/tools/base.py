"""记忆工具的公共执行核心"""
from __future__ import annotations
from typing import Any
from satrap.core.memory.store import MemoryStore
from satrap.core.memory.service import MemoryService
from satrap.core.utils.TCBuilder.tool_base import _ToolBase


class _MemoryBinding(_ToolBase):
    def __init__(self, store: MemoryStore, *, service: MemoryService | None = None) -> None:
        """
        初始化 _MemoryToolBase

        参数:
        - store: 存储实例
        - service: 宿主业务实例, 独立调用时按存储构建
        """
        super().__init__()
        self.store = store
        self.service = service or MemoryService(store)


class _AddMemoryToolCore(_MemoryBinding):
    """添加一条长期记忆 (用户偏好/项目约定/关键决策)"""

    tool_name = "add_memory"
    description = "添加一条当前会话的长期记忆, 记忆仅会注入当前会话的后续上下文"
    params_dict = {
        "title": ("string", "简短记忆标题"),
        "content": ("string", "记忆内容"),
        "tags": ("array", "分类标签"),
        "importance": ("number", "重要程度 1-5, 默认 1"),
    }

    def _execute(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 1,
    ) -> str:
        """
        执行

        参数:
        - title: 标题
        - content: 内容
        - tags: 标签集合
        - importance: 重要度

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("添加")
        result = self.service.execute("add", title, content, tags, importance)
        if result.get("ok"):
            return f"记忆已添加: [{title}] {content}"
        return f"添加失败: {result.get('error')}"


class _UpdateMemoryToolCore(_MemoryBinding):
    """更新一条已有记忆"""

    tool_name = "update_memory"
    description = "按记忆 ID 更新已有长期记忆 (信息变化或修正时使用)"
    params_dict = {
        "memory_id": ("string", "要更新的记忆 ID"),
        "content": ("string", "更新后的内容"),
        "title": ("string", "更新后的标题"),
    }

    def _execute(self, memory_id: str, content: str = "", title: str = "") -> str:
        """
        执行

        参数:
        - memory_id: 记忆id
        - content: 内容
        - title: 标题

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("更新")
        fields: dict[str, Any] = {}
        if content:
            fields["content"] = content
        if title:
            fields["title"] = title
        result = self.service.execute("update", memory_id, **fields)
        if result.get("ok"):
            return f"记忆已更新: [{result['title']}] {result['content']}"
        return f"更新失败: {result.get('error')}"


class _DeleteMemoryToolCore(_MemoryBinding):
    """删除一条记忆"""

    tool_name = "delete_memory"
    description = "按记忆 ID 删除一条长期记忆"
    params_dict = {
        "memory_id": ("string", "要删除的记忆 ID"),
    }

    def _execute(self, memory_id: str) -> str:
        """
        执行

        参数:
        - memory_id: 记忆id

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("删除")
        result = self.service.execute("delete", memory_id)
        if result.get("ok"):
            return f"记忆已删除: {memory_id}"
        return f"删除失败: {result.get('error')}"


class _ListMemoriesToolCore(_MemoryBinding):
    """查看全部长期记忆"""

    recovery_policy = "retry"

    tool_name = "list_memories"
    description = "列出当前全部长期记忆 (含 ID, 供 update/delete 定位)"
    params_dict: dict[str, Any] = {}

    def _execute(self) -> str:
        """
        执行

        返回:
        - str: 执行
        """
        memories = self.service.execute("list_all")
        if isinstance(memories, dict):
            return f"查询失败: {memories.get('error')}"
        if not memories:
            return "当前没有长期记忆"
        lines = [f"共 {len(memories)} 条记忆:"]
        for m in memories:
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            lines.append(
                f"- {m['id'][:8]} [{m['title']}] {m['content']}{tags} (重要度 {m['importance']})"
            )
        return "\n".join(lines)


class _GetMemoryToolCore(_MemoryBinding):
    """读取一条长期记忆的完整记录"""

    tool_name = "get_memory"
    recovery_policy = "retry"
    description = "查看记忆列表返回的一条记忆, 返回完整 ID, 标题和正文; 不读取当前范围之外的数据"
    params_dict = {"memory_id": ("string", "记忆列表返回的完整 ID")}

    def _execute(self, memory_id: str) -> dict[str, Any]:
        """
        读取记忆详情

        参数:
        - memory_id: 当前范围内的记忆 ID

        返回:
        - 完整记忆记录或明确的错误结果
        """
        return self.service.execute("get", memory_id)
