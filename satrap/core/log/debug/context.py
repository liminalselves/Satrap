"""绑定和解析当前会话的诊断对象。"""
from __future__ import annotations

from satrap.core.call_context import current_call_origin
from satrap.core.type.debug import DebugObject


def current_debug_object(session: object | None = None) -> DebugObject:
    """
    解析当前对象, 平台优先使用本轮真实来源

    参数:
    - session: 可选的宿主会话, Chat 由宿主显式绑定对象

    返回:
    - 不从 Session 名称推测的稳定对象
    """
    origin = current_call_origin()
    if origin is not None:
        return DebugObject.from_origin(origin)
    bound = getattr(session, "_debug_object", None)
    if isinstance(bound, DebugObject):
        return bound
    raise ValueError("当前会话尚未绑定可识别的 Debug 对象")


def bind_debug_object(session: object, obj: DebugObject) -> None:
    """
    在宿主创建会话时固定显式对象归属

    参数:
    - session: 实际宿主会话
    - obj: 由宿主解析的完整对象
    """
    setattr(session, "_debug_object", obj)
