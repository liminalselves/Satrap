"""插件流程声明契约。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

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
