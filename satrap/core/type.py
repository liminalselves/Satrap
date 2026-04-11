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

@dataclass
class LLMConfig:
    """LLM 配置数据结构"""
    name: str | None = None
    """LLM 名称"""
    model: str | None = None
    """LLM 模型"""
    base_url: str | None = None
    """LLM 基础 URL"""
    api_key: str | None = None
    """LLM API 密钥"""
    temperature: float | None = None
    """LLM 温度"""
    top_p: float | None = None
    """LLM top_p 参数"""
    max_tokens: int | None = None
    """LLM 最大 token 数量"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""

@dataclass
class EmbeddingConfig:
    """Embedding 配置数据结构"""
    name: str | None = None
    """Embedding 名称"""
    model: str | None = None
    """Embedding 模型"""
    base_url: str | None = None
    """Embedding 基础 URL"""
    api_key: str | None = None
    """Embedding API 密钥"""
    dimensions: int | None = None
    """Embedding 维度"""
    max_batch_size: int | None = None
    """Embedding 最大批量大小"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""

@dataclass
class ReRankConfig:
    """ReRank 配置数据结构"""
    name: str | None = None
    """ReRank 名称"""
    model: str | None = None
    """ReRank 模型"""
    base_url: str | None = None
    """ReRank 最大批量大小"""
    api_key: str | None = None
    """ReRank API 密钥"""
    top_k: int | None = None
    """ReRank top_k 参数"""
    min_score: float | None = None
    """ReRank 返回最小分数"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""

@dataclass
class SessionConfig:
    """会话配置数据结构"""
    session_id: str | None = None
    """会话 ID, 如 `sr7dws`"""
    session_type_name: str | None = None
    """会话类型名称"""
    created_at: float = 0.0
    """会话创建时间"""
    last_used_at: float = 0.0
    """会话最后使用时间"""
    message_count: int = 0
    """会话消息数量"""
    session_config: Dict[str, Any] = field(default_factory=dict)
    """除去会话 ID 以外的会话实例初始化配置"""
