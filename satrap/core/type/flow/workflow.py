"""插件流程声明契约。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from satrap.core.type.model_config import LLMConfig


@dataclass(frozen=True)
class ResolvedFlowModel:
    """一个 WF 的模型客户端及其配置, 由生产解析器或测试替身返回"""

    client: Any
    """供 AsyncModelWorkflowFramework 调用的异步模型客户端"""
    config: LLMConfig | None = None
    """客户端对应的完整模型配置; 测试替身未提供时不应用上下文策略"""
    owned: bool = False
    """是否由 flow 会话负责在替换或关闭时释放该客户端"""


@dataclass(frozen=True)
class FlowWorkflowDecl:
    """插件 flow.workflow 声明: 装到 WF 节点上时的限制与能力"""

    foreground_only: bool = False
    """只能装在前台 WF; 装在纯后台 WF 上静态检查报错"""
    provides: tuple[str, ...] = ()
    """声明的能力, 首版固定 memory_write / state_write"""

    def to_payload(self) -> dict[str, Any]:
        """序列化给前端"""
        return {
            "foreground_only": self.foreground_only,
            "provides": list(self.provides),
        }
