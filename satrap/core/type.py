"""
Satrap 共享类型定义

集中声明模型响应, 会话配置, 用户信息, 平台消息和状态检查点等数据结构,
同时提供处理未知对象属性的类型安全访问函数
"""
from dataclasses import dataclass, field
from datetime import datetime
import sqlite3
from typing import Optional, List, Dict, Any, Iterator, Callable, Tuple, TypeVar, cast, overload
from enum import Enum
import time

from satrap.core.components import BaseMessageComponent, PlatformComponentType

THINKING_LEVEL_VALUES = ("low", "medium", "high", "xhigh", "max", "ultra")


def validate_thinking_levels(value: object) -> Optional[List[str]]:
    """
    校验并规范化模型支持的思考强度列表

    参数:
    - value: 待校验的思考强度列表或 None

    返回:
    - Optional[List[str]]: 去重后的思考强度列表或 None
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("thinking_levels 必须是字符串列表")
    raw_levels = cast(List[object], value)
    if not all(isinstance(item, str) for item in raw_levels):
        raise ValueError("thinking_levels 必须是字符串列表")
    levels = cast(List[str], raw_levels)
    invalid = [item for item in levels if item not in THINKING_LEVEL_VALUES]
    if invalid:
        raise ValueError(f"不支持的思考强度: {', '.join(invalid)}")
    return list(dict.fromkeys(levels))


@dataclass
class TokenUsage:
    """LLM 请求的 token 使用量"""
    input_tokens: Optional[int] = None
    """输入 token 数, 对应 prompt_tokens 或 input_tokens"""
    output_tokens: Optional[int] = None
    """输出 token 数, 对应 completion_tokens 或 output_tokens"""
    total_tokens: Optional[int] = None
    """总 token 数"""
    cached_tokens: Optional[int] = None
    """输入中命中供应商缓存的 token 数"""


@dataclass(frozen=True)
class ContextUsageSnapshot:
    """ContextManager 当前预算和最近一次模型 usage 快照"""
    history_tokens: int
    """当前完整历史的本地 token 估算值"""
    context_window_tokens: int
    """模型允许的总上下文长度"""
    reserved_output_tokens: int
    """为模型输出预留的 token 长度"""
    history_upper_tokens: int
    """历史上下文硬上限"""
    history_lower_tokens: int
    """历史上下文压缩后的目标下限"""
    last_output_tokens: Optional[int]
    """上一次正式模型请求的输出 token 数"""
    cache_hit_tokens: Optional[int]
    """上一次正式模型请求命中的缓存 token 数"""
    history_token_source: str
    """历史 token 的本地估算来源"""

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为可直接序列化的字典

        返回:
        - Dict[str, Any]: 上下文 usage 快照
        """
        return {
            "history_tokens": self.history_tokens,
            "context_window_tokens": self.context_window_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "history_upper_tokens": self.history_upper_tokens,
            "history_lower_tokens": self.history_lower_tokens,
            "last_output_tokens": self.last_output_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "history_token_source": self.history_token_source,
        }


@dataclass
class ModelContextRequestStats:
    """一次正式模型请求的上下文准备和 API usage 统计"""
    model: Optional[str]
    strategy: str
    compressed: bool
    original_turns: int
    prepared_turns: int
    original_estimated_input_tokens: int
    estimated_input_tokens: int
    effective_input_tokens: int
    preflight_token_source: str
    history_budget: int
    trigger_tokens: int
    floor_tokens: int
    api_input_tokens: Optional[int] = None
    api_output_tokens: Optional[int] = None
    api_total_tokens: Optional[int] = None
    api_cached_tokens: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为可直接写入 JSON 的字典

        返回:
        - Dict[str, Any]: 请求统计字典
        """
        return {
            "model": self.model,
            "strategy": self.strategy,
            "compressed": self.compressed,
            "original_turns": self.original_turns,
            "prepared_turns": self.prepared_turns,
            "original_estimated_input_tokens": self.original_estimated_input_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "effective_input_tokens": self.effective_input_tokens,
            "preflight_token_source": self.preflight_token_source,
            "history_budget": self.history_budget,
            "trigger_tokens": self.trigger_tokens,
            "floor_tokens": self.floor_tokens,
            "api_input_tokens": self.api_input_tokens,
            "api_output_tokens": self.api_output_tokens,
            "api_total_tokens": self.api_total_tokens,
            "api_cached_tokens": self.api_cached_tokens,
        }


@dataclass
class ModelContextTurnStats:
    """一轮会话内全部正式模型请求的上下文统计"""
    requests: List[ModelContextRequestStats] = field(
        default_factory=lambda: list[ModelContextRequestStats]()
    )

    def to_dict(self) -> Dict[str, Any]:
        """
        汇总并转换为可直接写入 JSON 的字典

        返回:
        - Dict[str, Any]: 轮次统计字典
        """
        if not self.requests:
            return {}

        def sum_usage(name: str) -> Optional[int]:
            """
            汇总存在的 API usage 字段

            参数:
            - name: ModelContextRequestStats 字段名

            返回:
            - Optional[int]: 汇总值, 所有请求均无该字段时为 None
            """
            values = [getattr(request, name) for request in self.requests]
            known = [value for value in values if isinstance(value, int)]
            return sum(known) if known else None

        return {
            "request_count": len(self.requests),
            "last_request": self.requests[-1].to_dict(),
            "total_api_input_tokens": sum_usage("api_input_tokens"),
            "total_api_output_tokens": sum_usage("api_output_tokens"),
            "total_api_tokens": sum_usage("api_total_tokens"),
            "total_api_cached_tokens": sum_usage("api_cached_tokens"),
        }


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
    usage: Optional[TokenUsage] = None
    """API 返回的真实 token 使用量, 供应商未返回时为 None"""

    def __iter__(self) -> Iterator[Any]:
        """
        支持解包操作

        返回:
        - Iterator[Any]: 支持解包操作
        """
        yield self.type
        yield self.content
        yield self.thinking
        yield self.tool_calls or field(default_factory=list)
        
    def __len__(self) -> int:
        """
        返回可解包的元素数量

        返回:
        - int: 可解包的元素数量
        """
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
    session_provider: Optional[str] = None
    """会话 Provider 名称, 未指定时使用 session_class"""
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
    context_window: Optional[int] = None
    """总上下文窗口, 与 CM max_context 同源"""
    history_ratio: Optional[float] = None
    """历史上下文比例, 输出预算 = context_window x (1 - history_ratio)"""
    context_strategy: str = "sliding"
    """上下文超限处理策略, 可选 sliding, mid_truncate 或 summarize"""
    context_threshold: float = 0.8
    """触发上下文处理的历史预算比例"""
    truncation_floor: float = 0.4
    """滑动窗口或截取策略处理后的目标历史预算比例"""
    summary_keep_recent_turns: int = 6
    """总结压缩时必须原样保留的最近对话轮数"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""
    allow_insecure_base_url: bool = False
    """是否显式允许非回环 HTTP API 地址"""
    thinking_field_name: Optional[str] = None
    """思考内容的字段名称"""
    thinking_fields: Optional[List[str]] = None
    """该模型需要的思考字段列表, 如 ["reasoning_effort", "thinking.type"]"""
    thinking_levels: Optional[List[str]] = None
    """该模型在前端开放的思考强度列表"""
    omit_none_thinking_fields: bool = False
    """关闭思考时是否省略值为 none 的思考字段"""

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
    allow_insecure_base_url: bool = False
    """是否显式允许非回环 HTTP API 地址"""

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
    allow_insecure_base_url: bool = False
    """是否显式允许非回环 HTTP API 地址"""

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
    GROUP_MESSAGE = "GroupMessage"   # 群组形式的消息
    FRIEND_MESSAGE = "FriendMessage"   # 私聊, 好友等单聊消息
    OTHER_MESSAGE = "OtherMessage"   # 其他类型的消息, 如系统消息等

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
        """初始化 PlatformMessage"""
        self.timestamp = int(time.time())
        self.group = None

    def __str__(self) -> str:
        return str(self.__dict__)

    @property
    def group_id(self) -> str:
        """
        向后兼容的 group_id 属性
        群组id, 如果为私聊, 则为空

        返回:
        - str: 向后兼容的 group_id 属性
        """
        if self.group:
            return self.group.group_id
        return ""

    @group_id.setter
    def group_id(self, value: Optional[str]) -> None:
        """
        设置 group_id

        参数:
        - value: 输入值
        """
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
    """
    动作类型, 可选值:
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
        """
        从持久化字典读取快照, 校验版本与结构

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
        """
        转换为稳定的持久化字典

        返回:
        - Dict[str, object]: 转换为稳定的持久化字典
        """
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
    """领域名称, 如 \"messages\" / \"session_config\""""
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
    """变更来源, 如 \"checkpoint_rollback\" / \"checkpoint_fork\""""
    reason: str = ""
    """变更原因说明"""
    change_set_id: str = ""
    """变更批次唯一标识"""


T = TypeVar("T")


@overload
def safe_getattr(obj: Any, name: str) -> Any | None: ...

@overload
def safe_getattr(obj: Any, name: str, default: T) -> Any | T: ...

def safe_getattr(obj: Any, name: str, default: T | None = None) -> Any | T | None:
    """
    类型安全的 getattr: 返回值类型 = 属性类型 | default 类型

    用于替代裸 getattr(obj, "attr", None), 让 pyright 能推断返回值类型

    参数:
    - obj: 目标对象
    - name: 属性名
    - default: 属性不存在时的默认值

    返回:
    - 属性值(若存在)或 default
    """
    return getattr(obj, name, default)


def safe_getattr_str(obj: Any, name: str, default: str = "") -> str:
    """
    类型安全的 getattr, 返回 str

    参数:
    - obj: 目标对象
    - name: 名称
    - default: 默认值

    返回:
    - str:  str
    """
    val = getattr(obj, name, default)
    return str(val) if val is not None else default


def safe_getattr_int(obj: Any, name: str, default: int = 0) -> int:
    """
    类型安全的 getattr, 返回 int

    参数:
    - obj: 目标对象
    - name: 名称
    - default: 默认值

    返回:
    - int:  int
    """
    val = getattr(obj, name, default)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def safe_getattr_float(obj: Any, name: str, default: float = 0.0) -> float:
    """
    类型安全的 getattr, 返回 float

    参数:
    - obj: 目标对象
    - name: 名称
    - default: 默认值

    返回:
    - float:  float
    """
    val = getattr(obj, name, default)
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def safe_getattr_bool(obj: Any, name: str, default: bool = False) -> bool:
    """
    类型安全的 getattr, 返回 bool

    参数:
    - obj: 目标对象
    - name: 名称
    - default: 默认值

    返回:
    - bool:  bool
    """
    val = getattr(obj, name, default)
    if val is None:
        return default
    return bool(val)


def safe_getattr_list(obj: Any, name: str) -> list[Any]:
    """
    类型安全的 getattr, 返回 list(不存在或 None 时返回空列表)

    参数:
    - obj: 目标对象
    - name: 名称

    返回:
    - list[Any]:  list(不存在或 None 时返回空列表)
    """
    val: Any = getattr(obj, name, None)
    if val is None:
        return []
    if isinstance(val, list):
        return cast(list[Any], val)
    return [val]


def safe_getattr_dict(obj: Any, name: str) -> dict[str, Any]:
    """
    类型安全的 getattr, 返回 dict(不存在或 None 时返回空字典)

    参数:
    - obj: 目标对象
    - name: 名称

    返回:
    - dict[str, Any]:  dict(不存在或 None 时返回空字典)
    """
    val: Any = getattr(obj, name, None)
    if val is None:
        return {}
    if isinstance(val, dict):
        return cast(dict[str, Any], val)
    return {}


def safe_getattr_callable(obj: Any, name: str) -> Callable[..., Any] | None:
    """
    类型安全的 getattr, 返回可调用对象(不存在或不可调用时返回 None)

    参数:
    - obj: 目标对象
    - name: 名称

    返回:
    - Callable[..., Any] | None: 可调用对象(不存在或不可调用时返回 None)
    """
    val = getattr(obj, name, None)
    return val if callable(val) else None

