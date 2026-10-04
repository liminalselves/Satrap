"""所有记忆入口共用的宿主业务边界, 不依赖插件实现"""
from __future__ import annotations

from typing import Any

from satrap.core.call_context import current_call_origin, current_tool_workflow
from satrap.core.log import logger
from satrap.core.memory.store import MemoryStore


class MemoryService:
    """管理绑定范围的记忆操作, 在入口统一处理权限与存储失败"""

    def __init__(self, store: MemoryStore, *, session: object | None = None, budget: int = 6000) -> None:
        """
        绑定存储与可选的主会话

        参数:
        - store: 宿主确定范围的存储实例
        - session: 模型调用所属主会话, 人工管理不传入
        - budget: 注入字符预算
        """
        self.store = store
        self.session = session
        self.budget = max(1000, min(20000, budget))

    def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """
        执行记忆操作并将失败转换为明确结果

        参数:
        - operation: 支持的记忆操作名称
        - args: 操作的位置参数
        - kwargs: 操作的命名参数

        返回:
        - 存储返回值, 拒绝或异常时返回 ok=false 的错误结果
        """
        try:
            if operation not in {"add", "update", "delete", "get", "list_all", "clear", "set_mode", "to_context_block"}:
                raise ValueError("不支持的记忆操作")
            writing = operation in {"add", "update", "delete", "clear", "set_mode"}
            workflow = current_tool_workflow()
            main = getattr(self.session, "_wf", None)
            if writing and workflow is not None and main is not None and workflow is not main:
                return {"ok": False, "code": "read_only_workflow", "error": "子工作流不能修改长期记忆"}
            origin = current_call_origin()
            if writing and self.session is not None and origin is not None:
                kind = origin.conversation_kind or origin.chat_type
                if kind in {"group", "GroupMessage"}:
                    return {"ok": False, "code": "group_memory_not_ready", "error": "群记忆写入尚未开放, 不能通过旧工具或命令绕过权限"}
            if operation in {"get", "list_all"} and self.session is not None and self.store.mode == "disabled":
                return {"ok": False, "code": "memory_disabled", "error": "记忆功能已禁用"}
            return getattr(self.store, operation)(*args, **kwargs)
        except (ValueError, TypeError) as exc:
            logger.warning(f"[长期记忆] 参数校验失败, 操作={operation}, 错误={exc}")
            return {"ok": False, "code": "invalid_argument", "error": str(exc)}
        except Exception:
            logger.exception(f"[长期记忆] 操作失败, 操作={operation}")
            return {"ok": False, "code": "storage_unavailable", "error": "记忆存储暂不可用"}

    def context_block(self) -> str:
        """
        读取本轮可注入的记忆

        返回:
        - 记忆数据块, 无内容或读取失败时返回空字符串
        """
        result = self.execute("to_context_block")
        if not isinstance(result, str):
            return ""
        if len(result) <= self.budget:
            return result
        return result[: self.budget - 80] + "\n[部分记忆未注入, 可通过记忆查询工具继续查看]\n</long-term-memory>"
