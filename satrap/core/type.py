from components import PlatformComponentType
from typing import Optional, List, Dict, Any, Iterator
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import time

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

@dataclass
class UserInfo:
    """用户信息"""
    user_id: str | None = None
    """用户 id"""
    user_platform: str | None = None
    """用户所在的平台"""
    user_nickname: str | None = None
    """用户昵称"""
    user_session: list[str] = field(default_factory=list)
    """用户会话列表 (会话 ID 列表)"""


class PlatformMessageType(Enum):
    GROUP_MESSAGE = "GroupMessage"     # 群组形式的消息
    FRIEND_MESSAGE = "FriendMessage"   # 私聊, 好友等单聊消息
    OTHER_MESSAGE = "OtherMessage"     # 其他类型的消息, 如系统消息等

@dataclass
class MessageMember:
    user_id: str
    """用户 id"""
    nickname: str | None = None
    """用户昵称"""

    def __str__(self) -> str:
        return (
            f"User ID: {self.user_id},"
            f"Nickname: {self.nickname if self.nickname else 'N/A'}"
        )   # 使用 f-string 来构建返回的字符串表示形式

@dataclass
class Group:
    group_id: str
    """群号"""
    group_name: str | None = None
    """群名称"""
    group_avatar: str | None = None
    """群头像"""
    group_owner: str | None = None
    """群主 id"""
    group_admins: list[str] | None = None
    """群管理员 id"""
    members: list[MessageMember] | None = None
    """所有群成员"""

    def __str__(self) -> str:
        return (
            f"Group ID: {self.group_id}\n"
            f"Name: {self.group_name if self.group_name else 'N/A'}\n"
            f"Avatar: {self.group_avatar if self.group_avatar else 'N/A'}\n"
            f"Owner ID: {self.group_owner if self.group_owner else 'N/A'}\n"
            f"Admin IDs: {self.group_admins if self.group_admins else 'N/A'}\n"
            f"Members Len: {len(self.members) if self.members else 0}\n"
            f"First Member: {self.members[0] if self.members else 'N/A'}\n"
        )   # 使用 f-string 来构建返回的字符串表示形式

@dataclass
class PlatformMessage:
    type: PlatformMessageType
    """消息类型"""
    self_id: str
    """机器人的识别id"""
    session_id: str
    """会话id, 取决于 unique_session 的设置"""
    message_id: str
    """消息id"""
    group: Group | None
    """群组"""
    sender: MessageMember
    """发送者"""
    message: list[PlatformComponentType]
    """消息链使用 Nakuru 的消息链格式"""
    message_str: str
    """最直观的纯文本消息字符串"""
    raw_message: object
    """原始消息对象"""
    timestamp: int
    """消息时间戳"""

    def __init__(self) -> None:
        self.timestamp = int(time.time())
        self.group = None

    def __str__(self) -> str:
        return str(self.__dict__)

    @property
    def group_id(self) -> str:
        """向后兼容的 group_id 属性
        群组id, 如果为私聊, 则为空
        """
        if self.group:
            return self.group.group_id
        return ""

    @group_id.setter
    def group_id(self, value: str | None) -> None:
        """设置 group_id"""
        if value:
            if self.group:
                self.group.group_id = value
            else:
                self.group = Group(group_id=value)
        else:
            self.group = None


class PlatformStatus(Enum):
    """平台状态"""
    PENDING = "pending"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class PlatformError:
    """平台错误信息"""
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    traceback: str | None = None

