"""当前平台机器人身份的不可变资料"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BotIdentity:
    """平台确认的机器人账号昵称及所在群名片, 不使用消息发送者身份"""

    self_id: str
    nickname: str = ""
    group_id: str = ""
    group_card: str = ""
