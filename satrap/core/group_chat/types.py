"""不依赖平台协议的群聊能力与已核验读取结果"""
from __future__ import annotations

from dataclasses import dataclass

from satrap.core.config.platform_messages import ArchiveMessage, MessageScope


CAPABILITIES = ("member_list", "member_info", "message_lookup", "archive_search", "text", "quote", "mention", "image", "sticker")


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
    nickname: str = ""
    card: str = ""


@dataclass(frozen=True)
class MemberSnapshot:
    """适配器已核验归属的成员快照, 显式声明完整性"""

    scope: MessageScope
    members: tuple[MemberRecord, ...]
    fetched_at: float
    complete: bool
    truncated: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class VerifiedMessage:
    """适配器核验身份后返回的单条原始消息"""

    scope: MessageScope
    message: ArchiveMessage


@dataclass(frozen=True)
class VerifiedMember:
    """适配器核验当前群归属后返回的成员资料"""

    scope: MessageScope
    member: MemberRecord
    fetched_at: float


@dataclass(frozen=True)
class GroupChatLimits:
    """宿主或插件配置提供的查询上限, 模型不能改变档案归属"""

    message_limit: int = 100
    member_limit: int = 50
    text_budget: int = 12000
    member_cache_ttl: int = 60
    summary_enabled: bool = True
    summary_message_limit: int = 500
    summary_text_budget: int = 60000
    summary_retention_days: int = 30
    summary_input_budget: int = 24000
    media_reply_enabled: bool = True
    max_reply_images: int = 4
    max_reply_stickers: int = 4

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
