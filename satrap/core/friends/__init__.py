"""平台账号级好友管理, 与模型插件和群管理独立"""
from typing import TypedDict


class FriendDirectoryEntry(TypedDict):
    """适配器明确提供的好友资料, 未知资料保持为空"""

    user_id: str
    nickname: str
    remark: str
    avatar_url: str | None
    online: bool | None
    added_at: float | None


class FriendError(Exception):
    """携带稳定错误码的好友操作失败"""

    def __init__(self, code: str, message: str) -> None:
        """
        固定可公开的失败说明

        参数:
        - code: 稳定错误码
        - message: 不含平台凭据的用户说明
        """
        super().__init__(message)
        self.code = code
