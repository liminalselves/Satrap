"""状态检查点模块: 提供 StateStore 检查点存储与 fork / rollback 能力

数据模型定义在 `satrap.core.type`, 本模块只包含机制实现:
- DomainRegistry: 领域注册表, 框架与领域数据的唯一契约
- StateStore: 检查点 CRUD、回滚、分支
- state_mutation_context: 状态变更审计上下文
"""
from satrap.core.state.mutation import current_mutation_context, state_mutation_context
from satrap.core.state.registry import DomainRegistry
from satrap.core.state.store import StateStore

__all__ = [
    "DomainRegistry",
    "StateStore",
    "current_mutation_context",
    "state_mutation_context",
]
