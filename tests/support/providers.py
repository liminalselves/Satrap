"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from satrap.core.framework.providers import BindingState, BindingStatus


class RunnableRegistry:
    """绑定判定恒为可运行的会话定义注册表替身"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """恒定答复可运行"""
        return BindingStatus(BindingState.RUNNABLE)
