from typing import Optional, List, Dict, Any, Iterator
from dataclasses import dataclass, field

@dataclass
class LLMCallResponse:
    """LLM 调用响应数据结构"""
    role: str
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None

    def __iter__(self) -> Iterator:
        """支持解包操作"""
        yield self.role
        yield self.content
        yield self.tool_calls or field(default_factory=list)
        
    def __len__(self) -> int:
        """返回可解包的元素数量"""
        return 3
