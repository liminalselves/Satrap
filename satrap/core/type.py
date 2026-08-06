from satrap.core.components import BaseMessageComponent, PlatformComponentType
from typing import Optional, List, Dict, Any, Iterator, Callable, Tuple, cast
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import sqlite3
import time

@dataclass
class LLMCallResponse:
    """LLM 调用响应数据结构"""
    type: str
    """LLM 调用响应类型"""
    content: str
    """LLM 调用响应内容"""
    thinking: Optional[str] = None
    """LLM 调用响应思考"""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    """LLM 调用响应工具调用, 包含 name, id, arguments"""

    def __iter__(self) -> Iterator[Any]:
        """支持解包操作"""
        yield self.type
        yield self.content
        yield self.thinking
        yield self.tool_calls or field(default_factory=list)
        
    def __len__(self) -> int:
        """返回可解包的元素数量"""
        return 4


@dataclass
class LLMCallStreamEvent:
    """LLM 流式调用事件"""
    kind: str
    """事件类型, 如 content_delta, thinking_delta, tool_call_delta, done 或 error"""
    delta: str = ""
    """本次事件携带的文本或工具参数增量"""
    tool_call: Optional[Dict[str, Any]] = None
    """工具调用增量, 包含 index, id, name 和 arguments 字段"""
    response: Optional[object] = None
    """done 事件携带的完整 LLMCallResponse 或 False"""
    finish_reason: Optional[str] = None
    """模型结束原因"""
    error: Optional[str] = None
    """error 事件的错误信息"""

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
    session_id: Optional[str] = None
    """会话 ID, 如 `sr7dws`"""
    session_type: Optional[str] = None
    """会话类型"""
    message: Optional[str] = None
    """用户输入消息"""
    img_urls: Optional[List[str]] = None
    """用户输入图片 URL 列表"""

@dataclass
class LLMConfig:
    """LLM 配置数据结构"""
    name: Optional[str] = None
    """LLM 名称"""
    model: Optional[str] = None
    """LLM 模型"""
    base_url: Optional[str] = None
    """LLM 基础 URL"""
    api_key: Optional[str] = None
    """LLM API 密钥"""
    temperature: Optional[float] = None
    """LLM 温度"""
    top_p: Optional[float] = None
    """LLM top_p 参数"""
    max_tokens: Optional[int] = None
    """LLM 最大 token 数量"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""

@dataclass
class EmbeddingConfig:
    """Embedding 配置数据结构"""
    name: Optional[str] = None
    """Embedding 名称"""
    model: Optional[str] = None
    """Embedding 模型"""
    base_url: Optional[str] = None
    """Embedding 基础 URL"""
    api_key: Optional[str] = None
    """Embedding API 密钥"""
    dimensions: Optional[int] = None
    """Embedding 维度"""
    max_batch_size: Optional[int] = None
    """Embedding 最大批量大小"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""

@dataclass
class ReRankConfig:
    """ReRank 配置数据结构"""
    name: Optional[str] = None
    """ReRank 名称"""
    model: Optional[str] = None
    """ReRank 模型"""
    base_url: Optional[str] = None
    """ReRank 最大批量大小"""
    api_key: Optional[str] = None
    """ReRank API 密钥"""
    top_k: Optional[int] = None
    """ReRank top_k 参数"""
    min_score: Optional[float] = None
    """ReRank 返回最小分数"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""

@dataclass
class SessionConfig:
    """会话配置数据结构"""
    session_id: Optional[str] = None
    """会话 ID, 如 `sr7dws`"""
    session_type_name: Optional[str] = None
    """会话类型名称"""
    created_at: float = 0.0
    """会话创建时间"""
    last_used_at: float = 0.0
    """会话最后使用时间"""
    message_count: int = 0
    """会话消息数量"""
    session_config: Dict[str, Any] = field(default_factory=dict[str, Any])
    """除去会话 ID 以外的会话实例初始化配置"""

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


class PlatformMessageType(Enum):
    GROUP_MESSAGE = "GroupMessage"     # 群组形式的消息
    FRIEND_MESSAGE = "FriendMessage"   # 私聊, 好友等单聊消息
    OTHER_MESSAGE = "OtherMessage"     # 其他类型的消息, 如系统消息等

@dataclass
class MessageMember:
    user_id: str
    """用户 id"""
    nickname: Optional[str] = None
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
    group_name: Optional[str] = None
    """群名称"""
    group_avatar: Optional[str] = None
    """群头像"""
    group_owner: Optional[str] = None
    """群主 id"""
    group_admins: Optional[List[str]] = None
    """群管理员 id"""
    members: Optional[List[MessageMember]] = None
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
    group: Optional[Group]
    """群组"""
    sender: MessageMember
    """发送者"""
    message: List[BaseMessageComponent]
    """消息组件链"""
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
    def group_id(self, value: Optional[str]) -> None:
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
    """错误信息发出的平台"""
    timestamp: datetime = field(default_factory=datetime.now)
    """错误发生时间"""
    traceback: Optional[str] = None
    """错误的完整 traceback 信息"""

@dataclass
class CommandAction:
    """命令执行后的动作指示"""
    action: str
    """动作类型, 可选值:
        - "switch_session": 切换会话
        - "new_session": 创建新会话
        - "none": 无动作, 仅返回信息
    """
    target_session_id: Optional[str] = None
    """目标会话 ID, 用于切换会话等操作"""
    message: str = ""
    """返回给用户的信息"""

# ==================== State (检查点 / 回滚 / 分支) ====================

CURRENT_SNAPSHOT_VERSION = 1
"""当前状态快照格式版本"""
SUPPORTED_SNAPSHOT_VERSIONS = {CURRENT_SNAPSHOT_VERSION}
"""支持读取的快照格式版本集合"""

JsonRow = Dict[str, object]
"""领域数据行: 可 JSON 序列化的字段字典, 是快照的持久化单元"""


@dataclass(frozen=True)
class StateScope:
    """状态作用域: 定位一份可被检查点管理的状态"""
    namespace: str
    """命名空间, 如 "conversation" (对话) / "session" (会话)"""
    scope_id: str
    """作用域 ID, 如 conversation_id / session_id"""
    branch_id: str = ""
    """分支 ID, 由 fork 产生的剧情线; 无分支时为空字符串"""


@dataclass
class StateSnapshot:
    """版本化状态快照: 各领域数据以行字典列表存放"""
    version: int = CURRENT_SNAPSHOT_VERSION
    """快照格式版本"""
    scope: Dict[str, str] = field(default_factory=dict[str, str])
    """创建快照时的作用域字段"""
    domains: Dict[str, List[JsonRow]] = field(default_factory=dict[str, List[JsonRow]])
    """领域名 -> 领域数据行列表"""

    @classmethod
    def from_dict(cls, value: Dict[str, object]) -> "StateSnapshot":
        """从持久化字典读取快照, 校验版本与结构

        参数:
        - value: 持久化字典 (to_dict 的输出)

        返回:
        - StateSnapshot: 解析后的快照

        异常:
        - ValueError: 版本不支持或结构不完整
        - TypeError: 字段类型错误
        """
        version = value.get("snapshot_version")
        if not isinstance(version, int) or version not in SUPPORTED_SNAPSHOT_VERSIONS:
            raise ValueError(
                f"不支持的状态快照版本: {version!r}, 当前版本为 {CURRENT_SNAPSHOT_VERSION}"
            )
        scope = value.get("scope")
        if not isinstance(scope, dict):
            raise TypeError("快照 scope 必须是字典")
        raw_domains = cast(dict[str, Any], value.get("domains"))
        if not isinstance(raw_domains, dict):
            raise TypeError("快照 domains 必须是字典")
        domains: Dict[str, List[JsonRow]] = {}
        for name, rows in raw_domains.items():
            if not isinstance(name, str) or not isinstance(rows, list):
                raise TypeError(f"快照领域 {name!r} 必须是行列表")
            rows = cast(list[Any], rows)
            domains[name] = [cast(JsonRow, row) for row in rows if isinstance(row, dict)]
        return cls(version=CURRENT_SNAPSHOT_VERSION, scope={**scope}, domains=domains)

    def to_dict(self) -> Dict[str, object]:
        """转换为稳定的持久化字典"""
        return {
            "snapshot_version": self.version,
            "scope": dict(self.scope),
            "domains": {name: list(rows) for name, rows in self.domains.items()},
        }


@dataclass
class StateCheckpoint:
    """状态检查点: 某一时刻的完整状态快照引用"""
    checkpoint_id: str
    """检查点唯一标识"""
    namespace: str
    """所属命名空间"""
    scope_id: str
    """所属作用域 ID"""
    branch_id: str = ""
    """所属分支 ID"""
    name: str = ""
    """检查点显示名称"""
    description: str = ""
    """检查点说明"""
    snapshot_id: str = ""
    """引用的独立快照 ID"""
    batch_id: str = ""
    """会话级聚合检查点的批次 ID (同一批检查点共享), 非聚合时为空"""
    state_revision: int = 0
    """检查点创建时的状态版本"""
    position: int = 0
    """检查点创建时的领域水位 (如最大消息 ID)"""
    checkpoint_kind: str = "manual"
    """检查点类型, manual (手动) 或 stable (自动)"""
    parent_checkpoint_id: Optional[str] = None
    """fork 来源检查点 ID"""
    source: str = ""
    """创建时的变更来源 (审计), 如 session_checkpoint / checkpoint_fork"""
    reason: str = ""
    """创建时的变更原因 (审计)"""
    created_at: float = 0.0
    """创建时间戳"""


@dataclass(frozen=True)
class RestoreOptions:
    """恢复选项: 由框架统一传给各领域恢复器"""
    preserve_ids: bool
    """True=原位恢复 (回滚), False=新作用域恢复 (fork, 引用需重映射)"""
    id_map: Dict[str, str] = field(default_factory=dict[str, str])
    """引用字段的旧值 -> 新值映射表, fork 时由框架构建"""


@dataclass(frozen=True)
class SnapshotDomain:
    """领域注册声明: 框架与领域数据的唯一契约"""
    name: str
    """领域名称, 如 "messages" / "session_config" """
    builder: Callable[[sqlite3.Connection, StateScope], List[JsonRow]]
    """读取领域数据, 返回行字典列表"""
    restorer: Callable[[sqlite3.Connection, StateScope, List[JsonRow], RestoreOptions], None]
    """恢复领域数据 (清空后重写)"""
    cleaner: Callable[[sqlite3.Connection, StateScope], None]
    """恢复前清理领域数据"""
    reference_fields: Tuple[str, ...] = ()
    """fork 时需要重映射的跨领域引用字段名"""
    position_provider: Optional[Callable[[sqlite3.Connection, StateScope], int]] = None
    """提供领域水位 (如最大消息 ID), 用于检查点排序与级联清理"""


@dataclass(frozen=True)
class MutationContext:
    """一次原子状态变更的审计上下文"""
    source: str = "manual"
    """变更来源, 如 "checkpoint_rollback" / "checkpoint_fork" """
    reason: str = ""
    """变更原因说明"""
    change_set_id: str = ""
    """变更批次唯一标识"""

