"""平台消息档案身份与记录契约"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json


class MessageArchiveError(RuntimeError):
    """携带公共错误码的档案错误, 由请求或消息接收边界记录和捕获"""

    def __init__(self, code: str, message: str) -> None:
        """
        初始化档案错误

        参数:
        - code: 对外稳定的错误码
        - message: 不包含原始消息正文的错误说明
        """
        super().__init__(message)
        self.code = code


def _identity(value: object) -> str:
    """
    校验平台身份标识

    参数:
    - value: 不依赖平台数字格式的字符串标识

    返回:
    - 原字符串, 非法输入抛出 ValueError
    """
    from satrap.core.utils.validation import is_bounded_text

    if not is_bounded_text(value):
        raise ValueError("消息档案身份必须是长度不超过 256 的非空字符串")
    return value


@dataclass(frozen=True)
class MessageScope:
    """平台实例, 账号和实际对话组成的档案身份, 不包含 Agent 配置"""

    adapter_id: str
    """平台适配器实例标识, 与账号和对话共同隔离档案, 不可为空"""
    self_id: str
    """接收或发送消息的机器人账号标识, 不可为空"""
    conversation_kind: str
    """实际对话类型, 如 group 或 private, 不可为空"""
    chat_id: str
    """实际群或私聊对端标识, 在所属账号和对话类型内定位档案"""

    def __post_init__(self) -> None:
        """校验全部身份字段, 不接受空账号或未知对话身份"""
        for value in (self.adapter_id, self.self_id, self.conversation_kind, self.chat_id):
            _identity(value)

    @property
    def key(self) -> str:
        """
        编码不受分隔符冲突影响的版本化对话键

        返回:
        - 包含全部档案身份的 JSON 对话键
        """
        return "messages:v1:" + json.dumps(
            [self.adapter_id, self.self_id, self.conversation_kind, self.chat_id], ensure_ascii=False, separators=(",", ":"),
        )


@dataclass(frozen=True)
class ArchiveMessage:
    """已经由适配器归一和核验归属的消息; 不持有原始事件或本地文件内容"""

    message_id: str
    """平台侧消息标识, 用于同一档案范围内定位和去重"""
    sender_id: str
    """平台归一后的发送者账号标识, 出站消息通常是机器人账号"""
    message_time: float
    """消息发生的 Unix 时间戳, 单位秒, 来源由 time_source 标识"""
    text: str
    """纯文本组件拼接的正文, 无纯文本时可为空字符串"""
    nickname: str = ""
    """发送者昵称快照, 默认空表示平台未提供"""
    card: str = ""
    """发送者群名片快照, 默认空表示平台未提供"""
    direction: str = "inbound"
    """消息方向, 默认 inbound 入站, outbound 表示已确认发送或自身回显"""
    components: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    """归一化组件摘要, 默认空集合, 不保存本地文件内容或原始事件"""
    reply_to_message_id: str | None = None
    """被引用的首条消息标识, 默认 None 表示没有可用引用"""
    mentions: Sequence[str] = field(default_factory=tuple)
    """结构化提及的用户标识, 默认空集合表示没有可用提及"""
    media: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    """媒体类型及可安全保存的远程引用摘要, 默认空集合"""
    source: str = "platform_event"
    """档案来源, 默认 platform_event, 也可来自确认发送或适配器回源"""
    verified: bool = True
    """消息与对话归属是否已核验, 默认 True, 未核验消息不允许入档"""
    truncated: bool = False
    """摘要是否因数量或长度限制被截断, 默认 False"""
    time_source: str = "platform"
    """消息时间的来源, 默认 platform, 本地回退时间标为 local"""
    received_at: float | None = None
    """宿主首次接收的 Unix 秒数, 在异步转换或等待前冻结, 默认 None 时入档取当前时间"""
