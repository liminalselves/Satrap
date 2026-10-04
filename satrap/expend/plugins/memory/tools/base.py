"""记忆工具的公共执行核心"""
from __future__ import annotations
from typing import Any
from satrap.core.memory.store import MemoryStore
from satrap.core.memory.service import MemoryService
from satrap.expend.plugins.memory.runtime import active_config, group_call, wait_host
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
        self.config: dict[str, Any] = {}


    def _access(self) -> dict[str, Any]:
        """
        复核当前工具仍属于原主会话

        返回:
        - 当前安装配置, 停用时抛出业务错误
        """
        return active_config(self.service.session, self.config, "tools", str(self.tool_name), self)

    async def _group(self, operation: str, values: dict[str, Any]) -> dict[str, Any]:
        """
        将有界模型参数交给群记忆宿主

        参数:
        - operation: 宿主业务操作
        - values: 模型参数

        返回:
        - 真实业务结果或明确错误
        """
        return await self.service.group_operation(operation, values, access=self._access)

    def get_tool_defined(self) -> dict[str, Any]:
        """
        声明普通会话与群聊共用的工具, 不让模型填写身份范围

        返回:
        - 包含来源与修订参数的完整 JSON schema
        """
        import copy
        description, properties, required = DEFINITIONS[str(self.tool_name)]
        properties = copy.deepcopy(properties)
        if group_call() and self.tool_name == "add_memory":
            properties.pop("tags", None)
            properties.pop("importance", None)
        if group_call() and self.tool_name == "add_memory":
            required = [*required, "kind", "key", "source_message_ids"]
        if group_call() and self.tool_name == "update_memory":
            required = [*required, "content", "expected_revision", "source_message_ids"]
        if group_call() and self.tool_name == "delete_memory":
            required = [*required, "expected_revision", "request_message_id"]
        return {"type": "function", "function": {"name": self.tool_name, "description": description,
                "parameters": {"type": "object", "properties": copy.deepcopy(properties), "required": required, "additionalProperties": False}}}


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
        **group_values: Any,
    ) -> str | dict[str, Any]:
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
        if group_call():
            return wait_host(self._group("create", {"title": title, "content": content, **group_values}))
        if group_values:
            return {"ok": False, "error": "普通会话不能写入群范围记忆"}
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

    def _execute(self, memory_id: str, content: str = "", title: str = "", **group_values: Any) -> str | dict[str, Any]:
        """
        执行

        参数:
        - memory_id: 记忆id
        - content: 内容
        - title: 标题

        返回:
        - str: 执行
        """
        if group_call():
            values = {"memory_id": memory_id, **group_values}
            if content:
                values["content"] = content
            if title:
                values["title"] = title
            return wait_host(self._group("update", values))
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

    def _execute(self, memory_id: str, **group_values: Any) -> str | dict[str, Any]:
        """
        执行

        参数:
        - memory_id: 记忆id

        返回:
        - str: 执行
        """
        if group_call():
            return wait_host(self._group("delete", {"memory_id": memory_id, **group_values}))
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

    def _execute(self, **filters: Any) -> str | dict[str, Any]:
        """
        执行

        返回:
        - str: 执行
        """
        if group_call():
            return wait_host(self._group("list", filters))
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
        if group_call():
            return wait_host(self._group("get", {"memory_id": memory_id}))
        return self.service.execute("get", memory_id)


_TEXT = {"type": "string", "minLength": 1, "maxLength": 256}
_ID = {**_TEXT, "description": "记忆查询结果中的完整 ID"}
_SOURCES = {"type": "array", "minItems": 1, "maxItems": 10, "items": _TEXT, "description": "支持这条信息的来源消息 ID; 群内本人偏好必须包含本轮本人发言"}
_REVISION = {"type": "integer", "minimum": 1, "description": "查询结果中的 revision; 内容已变化时需要重新查询"}
DEFINITIONS = {
    "add_memory": ("用户明确要求记住时, 保存一条长期信息; 群内只能保存本人偏好或提交群约定. pending 表示待审批, 不能说群约定已经生效", {
        "title": {**_TEXT, "maxLength": 120, "description": "方便查找的简短标题"},
        "content": {**_TEXT, "maxLength": 2000, "description": "用户明确要求保存的信息, 不推测未表达的偏好"},
        "kind": {"type": "string", "enum": ["member_preference", "group_rule"], "description": "群聊必填: 本人的偏好, 或全群约定提案"},
        "key": {**_TEXT, "maxLength": 64, "description": "稳定的用途键, 例如 preferred_name 或 response_style"},
        "source_message_ids": _SOURCES,
        "tags": {"type": "array", "maxItems": 20, "items": _TEXT, "description": "普通会话记忆的分类标签"},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "普通会话记忆的重要程度, 默认 1"},
    }, ["title", "content"]),
    "update_memory": ("用户明确更正已保存的信息时使用; 先查询记忆和修订号. 群内只能改本人偏好, 群约定修改仍需审批", {
        "memory_id": _ID, "content": {**_TEXT, "maxLength": 2000, "description": "更正后的信息"},
        "title": {**_TEXT, "maxLength": 120, "description": "需要更名时填写"}, "source_message_ids": _SOURCES, "expected_revision": _REVISION,
    }, ["memory_id"]),
    "delete_memory": ("用户在本轮明确要求忘记时删除记忆; 群内只能删除本人偏好, 群约定删除需要审批, 不删除聊天原文", {
        "memory_id": _ID, "expected_revision": _REVISION,
        "request_message_id": {**_TEXT, "description": "本轮明确要求忘记的消息 ID, 不能使用旧聊天中的删除请求"},
    }, ["memory_id"]),
    "list_memories": ("查找当前范围内的有效长期记忆; 群内可筛选群约定或已核验成员的偏好, 不跨群读取", {
        "kind": {"type": "string", "enum": ["member_preference", "group_rule"], "description": "只查该类型"},
        "user_id": {**_TEXT, "description": "只查当前群这位已确认成员的偏好"},
        "keyword": {**_TEXT, "description": "标题或正文包含的文字"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "最多返回多少条, 默认 20"},
        "cursor": {**_TEXT, "maxLength": 4096, "description": "上次查询返回的 next_cursor, 沿用相同筛选"},
    }, []),
    "get_memory": ("查看一条记忆的完整正文, 所有者, 来源状态与修订号; 只能读取当前范围内的数据", {"memory_id": _ID}, ["memory_id"]),
}
