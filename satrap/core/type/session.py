"""会话与用户元数据契约"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SessionConfig:
    """会话配置数据结构"""
    session_id: Optional[str] = None
    """会话 ID, 如 `sr7dws`"""
    session_type_name: Optional[str] = None
    """会话类型名称"""
    provider_name: str = "session_class"
    """创建该会话实例的 Provider 名称"""
    created_at: float = 0.0
    """会话创建时间"""
    last_used_at: float = 0.0
    """会话最后使用时间"""
    message_count: int = 0
    """会话消息数量"""
    session_config: Dict[str, Any] = field(default_factory=dict[str, Any])
    """除去会话 ID 以外的会话实例初始化配置"""
    history_started_at: float | None = None
    """会话档案的持久起点, 老记录首次加载时按原创建时间建立"""

@dataclass
class UserInfo:
    """用户信息"""
    user_id: Optional[str] = None
    """用户 id"""
    user_platform: Optional[str] = None
    """用户所在的平台"""
    user_nickname: Optional[str] = None
    """用户昵称"""
    user_session: List[str] = field(default_factory=list[str])
    """用户会话列表 (会话 ID 列表)"""
