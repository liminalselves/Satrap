from typing import Optional, List, Dict, Any, Iterator
from dataclasses import dataclass, field

@dataclass
class LLMCallResponse:
    """LLM 调用响应数据结构"""
    type: str
    """LLM 调用响应类型"""
    content: str
    """LLM 调用响应内容"""
    thinking: str | None = None
    """LLM 调用响应思考"""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    """LLM 调用响应工具调用, 包含 name, id, arguments"""

    def __iter__(self) -> Iterator:
        """支持解包操作"""
        yield self.type
        yield self.content
        yield self.thinking
        yield self.tool_calls or field(default_factory=list)
        
    def __len__(self) -> int:
        """返回可解包的元素数量"""
        return 4

@dataclass
class LLMCallRequest:
    """LLM 调用请求数据结构"""
    role: str
    """LLM 调用请求角色"""
    content: str
    """LLM 调用请求内容"""
    tools: Optional[List[Dict[str, Any]]] = None
    """工具定义列表, 用于 Function Calling"""
    tool_choice: Optional[str] = None
    """工具选择策略, 如 "auto" 或 "none" , 或 {"type": "function", "function": {"name": "工具名"}}"""
    img_urls: Optional[List[str]] = None
    """LLM 调用请求图片 URL 列表"""

@dataclass
class UserCall:
    """用户调用数据结构"""
    session_id: str | None = None
    """会话 ID, 如 `sr7dws`"""
    session_type: str | None = None
    """会话类型"""
    message: str | None = None
    """用户输入消息"""
    img_urls: Optional[List[str]] = None
    """用户输入图片 URL 列表"""

