"""不依赖平台协议的群聊能力与已核验读取结果"""
from __future__ import annotations

from dataclasses import dataclass

from satrap.core.type.archive import ArchiveMessage, MessageScope


CAPABILITIES = ("group_list", "group_info", "member_list", "member_info", "self_nickname", "message_lookup", "archive_search", "text", "quote", "mention", "image", "sticker")


class GroupChatError(RuntimeError):
    """工具边界可归一的错误, 不把读取失败当成没有消息或成员"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        """
        初始化群聊错误

        参数:
        - code: 稳定的公共错误码
        - message: 不含原始群消息的错误说明
        - retryable: 是否可以在来源仍有效时重试
        """
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class MemberRecord:
    """成员读取结果, 不携带群管理权限"""

    user_id: str
    """平台返回并核验的成员身份, 用于区分当前群成员"""
    nickname: str = ""
    """平台昵称, 来源未提供时默认为空字符串"""
    card: str = ""
    """成员在本群的群昵称, 来源未提供时默认为空字符串"""
    role: str = ""
    """平台报告的群内角色, 默认空字符串表示未提供, 不构成管理授权"""


@dataclass(frozen=True)
class GroupRecord:
    """平台无关的群资料, 缺失人数不伪装为零"""

    group_id: str
    """平台返回的群身份, 用于核验目标群或区分列表条目"""
    name: str = ""
    """平台提供的群名称, 未提供时默认为空字符串"""
    member_count: int | None = None
    """平台报告的当前成员人数, 默认 None 表示未知而非零人"""
    max_member_count: int | None = None
    """平台报告的群成员容量, 默认 None 表示未知而非容量为零"""


@dataclass(frozen=True)
class VerifiedGroup:
    """绑定当前对话身份的群资料"""

    scope: MessageScope
    """读取时核验的平台, 账号和当前群对话范围"""
    group: GroupRecord
    """适配器回源并核对群身份后的群资料"""
    fetched_at: float
    """适配器取得资料的 Unix 时间戳, 单位为秒"""


@dataclass(frozen=True)
class GroupSnapshot:
    """管理者私聊中读取的当前账号群列表"""

    scope: MessageScope
    """发起群列表读取的管理者私聊范围, 用于绑定当前平台账号"""
    groups: tuple[GroupRecord, ...]
    """适配器返回的已核验群资料, 空元组不保证平台没有其他群"""
    fetched_at: float
    """适配器取得列表的 Unix 时间戳, 单位为秒"""
    complete: bool
    """是否完整返回来源列表, 为 False 时不能把结果视为全部群"""
    truncated: bool = False
    """列表是否因数量限制或无效条目等不完整, 默认为 False"""


@dataclass(frozen=True)
class MemberSnapshot:
    """适配器已核验归属的成员快照, 显式声明完整性"""

    scope: MessageScope
    """成员归属核验所用的当前平台账号和群对话范围"""
    members: tuple[MemberRecord, ...]
    """适配器核验后保留的成员, 空元组不代表来源列表完整"""
    fetched_at: float
    """适配器取得成员列表的 Unix 时间戳, 单位为秒"""
    complete: bool
    """是否完整返回来源成员列表, 为 False 时不可断定未列出的成员不存在"""
    truncated: bool = False
    """列表是否因数量限制或无效条目等不完整, 默认为 False"""
    reason: str | None = None
    """来源给出的不完整原因, 默认 None 表示没有提供原因"""


@dataclass(frozen=True)
class VerifiedMessage:
    """适配器核验身份后返回的单条原始消息"""

    scope: MessageScope
    """适配器核验消息归属时使用的当前群对话范围"""
    message: ArchiveMessage
    """回源并核验身份后的原始消息, 不自动展开附件或引用"""


@dataclass(frozen=True)
class VerifiedMember:
    """适配器核验当前群归属后返回的成员资料"""

    scope: MessageScope
    """适配器核验成员归属时使用的当前群对话范围"""
    member: MemberRecord
    """回源并核对群和成员身份后的成员资料"""
    fetched_at: float
    """适配器取得成员资料的 Unix 时间戳, 单位为秒"""


@dataclass(frozen=True)
class GroupChatLimits:
    """宿主或插件配置提供的查询上限, 模型不能改变档案归属"""

    message_limit: int = 100
    """单次记录查询最多返回的消息条数, 默认 100, 允许 1 到 100"""
    member_limit: int = 50
    """单次成员查询最多返回的人数, 默认 50, 允许 1 到 50"""
    text_budget: int = 12000
    """单次查询的文本字符预算, 默认 12000, 允许 128 到 100000"""
    member_cache_ttl: int = 60
    """成员查询缓存有效秒数, 默认 60, 允许 0 到 300, 0 时新查询刷新但翻页仍用短期快照"""
    summary_enabled: bool = True
    """是否允许按时间范围生成和查询摘要, 默认启用"""
    summary_message_limit: int = 500
    """一份摘要来源最多选取的消息条数, 默认 500, 允许 1 到 2000, 超限标记截断"""
    summary_text_budget: int = 60000
    """一份摘要来源的正文字符预算, 默认 60000, 允许 1000 到 200000"""
    summary_retention_days: int = 30
    """生成的摘要最多保留天数, 默认 30, 允许 1 到 3650, 来源提前过期时也失效"""
    summary_input_budget: int = 24000
    """摘要来源序列化后的 UTF-8 字节预算, 默认 24000, 允许 0 到 1000000, 小于 1000 时拒绝读取"""
    media_reply_enabled: bool = True
    """是否允许使用当前群图片及已授权表情等媒体回复, 默认启用"""
    max_reply_images: int = 4
    """单条回复的图片数量上限, 默认 4, 允许 0 到 8, 实际还受平台上限约束"""
    max_reply_stickers: int = 4
    """单条回复的表情数量上限, 默认 4, 允许 0 到 8, 实际还受平台上限约束"""
    allowed_groups: tuple[str, ...] = ()
    """允许使用群聊工具的群 ID 名单, 默认空元组表示不额外限制, 仍受平台范围约束"""
    history_scope: str = "current_session"
    """记录读取范围, 默认 current_session, group_archive 为本群保留档案, disabled 禁止记录及摘要查询"""

    def __post_init__(self) -> None:
        """拒绝无界条数, 非整数预算和无法失效的成员缓存"""
        for value, lower, upper in ((self.message_limit, 1, 100), (self.member_limit, 1, 50),
                                    (self.text_budget, 128, 100000), (self.member_cache_ttl, 0, 300),
                                    (self.summary_message_limit, 1, 2000), (self.summary_text_budget, 1000, 200000),
                                    (self.summary_retention_days, 1, 3650), (self.summary_input_budget, 0, 1000000),
                                    (self.max_reply_images, 0, 8), (self.max_reply_stickers, 0, 8)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("群聊工具查询配置超出允许范围")
        if type(self.summary_enabled) is not bool or type(self.media_reply_enabled) is not bool:
            raise ValueError("摘要开关必须是布尔值")
        if self.history_scope not in {"current_session", "group_archive", "disabled"}:
            raise ValueError("群消息查询范围无效")
