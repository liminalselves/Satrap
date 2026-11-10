"""
状态槽读写工具: snapshot_write / snapshot_read

WF 运行时通过这两个工具读写它连着的状态槽; snapshot_write 只挂到
WF 的 tools_manager 上, 内容在 WF 成功结束时由宿主按 CAS 提交
"""
from __future__ import annotations

from satrap.core.utils.TCBuilder import AsyncTool
from satrap.edictum.flow.store import FlowStore

from typing import Any


class AsyncSnapshotWriteTool(AsyncTool):
    """写入所连状态槽; 内容在 WF 成功结束时统一提交"""

    tool_name = "snapshot_write"
    description = "写入所连状态槽的新内容; 同一 WF 可写多个槽, 只有最后一次生效"
    params_dict = {
        "state_id": ("string", "状态槽节点 id, 必须是本 WF 连着的状态槽"),
        "content": ("string", "要写入的新版本内容"),
    }

    def __init__(self, session: Any, store: FlowStore, scope_key: str,
                 allowed: dict[str, int]) -> None:
        """
        初始化写入工具

        参数:
        - session: 所属流程会话
        - store: 状态槽持久化存取
        - scope_key: 当前对话键
        - allowed: {state_id: max_chars} 本 WF 允许写的槽及长度上限
        """
        super().__init__(owner_plugin="flow_snapshot")
        self.session = session
        self.store = store
        self.scope_key = scope_key
        self.allowed = allowed
        self.config: dict[str, Any] = {}
        self._pending: dict[str, str] = {}

    async def execute(self, state_id: str, content: str) -> dict[str, Any]:
        """
        登记一次状态槽写入, 不立即提交

        参数:
        - state_id: 状态槽节点 id
        - content: 新内容; 超过该槽 max_chars 时拒绝

        返回:
        - 已登记的字典; 实际提交发生在 WF 成功结束时
        """
        if state_id not in self.allowed:
            return {"ok": False, "error": f"本 WF 未连接状态槽 {state_id}"}
        limit = self.allowed[state_id]
        if len(content) > limit:
            return {"ok": False, "error": f"内容 {len(content)} 字符超过上限 {limit}"}
        self._pending[state_id] = content
        return {"ok": True, "state_id": state_id, "queued": True}

    def pending(self) -> dict[str, str]:
        """当前待提交的写入"""
        return dict(self._pending)

    def clear(self) -> None:
        """WF 运行结束 (成功或失败) 后清空待写"""
        self._pending.clear()


class AsyncSnapshotReadTool(AsyncTool):
    """读取所连状态槽当前版本"""

    tool_name = "snapshot_read"
    description = "读取所连状态槽当前版本内容, 版本号和覆盖位置"
    params_dict = {
        "state_id": ("string", "状态槽节点 id, 必须是本 WF 连着的状态槽"),
    }

    def __init__(self, session: Any, store: FlowStore, scope_key: str,
                 allowed: dict[str, int]) -> None:
        """
        初始化读取工具

        参数:
        - session: 所属流程会话
        - store: 状态槽持久化存取
        - scope_key: 当前对话键
        - allowed: {state_id: max_chars} 本 WF 允许读的槽
        """
        super().__init__(owner_plugin="flow_snapshot")
        self.session = session
        self.store = store
        self.scope_key = scope_key
        self.allowed = allowed
        self.config: dict[str, Any] = {}

    async def execute(self, state_id: str) -> dict[str, Any]:
        """
        读取状态槽当前版本

        参数:
        - state_id: 状态槽节点 id

        返回:
        - {version, content, covers_to, committed_at}
        """
        if state_id not in self.allowed:
            return {"ok": False, "error": f"本 WF 未连接状态槽 {state_id}"}
        state = self.store.state_by_scope_key(self.scope_key, state_id)
        return {"ok": True, **state}


def get_tools(session: Any, config: dict[str, Any] | None = None,
              resources: Any = None) -> list[AsyncTool]:
    """
    构建流程快照工具

    参数:
    - session: 所属流程会话 (AsyncFlowSession)
    - config: 插件配置 (未使用, 允许槽由会话注入)
    - resources: 插件资源 (未使用)

    返回:
    - snapshot_write / snapshot_read 工具; 非流程会话返回空
    """
    bound = getattr(session, "_flow_snapshot_binding", None)
    if bound is None:
        return []
    store, scope_key, allowed = bound
    return [
        AsyncSnapshotWriteTool(session, store, scope_key, allowed),
        AsyncSnapshotReadTool(session, store, scope_key, allowed),
    ]
