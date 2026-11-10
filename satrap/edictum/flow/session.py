"""
流程会话占位实现 (P0-6)

P1 之前先注册类型并完成定义解析/静态检查/迁移管线;
真正的多 workflow 运行时在 P1-1 替换为完整实现
"""
from __future__ import annotations

from satrap.core.framework.Base import AsyncSession
from satrap.edictum.flow.definition import parse_flow_definition
from satrap.edictum.flow.registry import FlowNodeRegistry
from satrap.edictum.flow.validate import validate

from typing import Any


class AsyncFlowSession(AsyncSession):
    """流程会话: 多个 AsyncModelWorkflowFramework 由节点图编排 (P1-1 实现)"""

    def __init__(
        self,
        session_id: str,
        llm: Any,
        *,
        flow: dict[str, Any] | None = None,
        content_callback: Any = None,
        db_path: str | None = None,
        enable_checkpoint: bool = False,
        **kwargs: Any,
    ) -> None:
        """
        初始化流程会话 (占位)

        参数:
        - session_id: 会话 ID
        - llm: 主模型实例 (P1 起按节点 model_name 解析)
        - flow: params.flow 原始定义
        - content_callback: 内容回调
        - db_path: 上下文数据库路径
        - enable_checkpoint: 是否启用会话级检查点
        - kwargs: 其他会话参数
        """
        init_kwargs: dict[str, Any] = {}
        if db_path is not None:
            init_kwargs["db_path"] = db_path
        init_kwargs["enable_checkpoint"] = enable_checkpoint
        super().__init__(session_id, content_callback, **init_kwargs)
        self._raw_flow = flow or {}
        self._definition = None
        self._issues = []

    async def _async_init(self) -> None:
        """解析定义并做静态检查; 真正的会话初始化在 P1-1"""
        if self._definition is not None:
            return
        definition = parse_flow_definition(self._raw_flow)
        registry = FlowNodeRegistry()
        self._issues = validate(definition, registry)
        if any(i.level == "error" for i in self._issues):
            raise ValueError("流程定义存在错误, 请先通过 /config/edictum/flow/validate 检查")
        self._definition = definition

    @property
    def issues(self) -> list[Any]:
        """静态检查发现的问题列表"""
        return self._issues
