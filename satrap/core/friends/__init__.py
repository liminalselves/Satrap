"""平台账号级好友管理, 与模型插件和群管理独立"""


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
