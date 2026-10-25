"""
高级诊断的稳定对象与设置协议

平台身份来自可信入站, 上下文只用于索引, 不作为群开关的生命周期
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cached_property
import hashlib
import json
from typing import Literal, Mapping, TypeAlias, TypedDict, cast

from satrap.core.call_context import CallOrigin, current_call_origin, is_group_origin, is_private_origin


DebugMode = Literal["inherit", "on", "off"]
DebugData: TypeAlias = None | str | int | float | bool | list["DebugData"] | dict[str, "DebugData"]


class ObjectPayload(TypedDict):
    """可持久保存的完整对象身份"""
    platform_id: str
    self_id: str
    conversation_kind: str
    target_id: str


class EventSummary(TypedDict):
    """事件分页的固定摘要协议, 不包含正文摘要或内容"""
    event_id: str
    request_id: str
    sequence: int
    category: str
    phase: str
    span_id: str
    parent_span_id: str
    recorded_at: float
    status: str
    metadata: dict[str, DebugData]
    body_slots: list[str]


class EventPage(TypedDict):
    """按序号分页的事件摘要"""
    events: list[EventSummary]
    has_more: bool


@dataclass(frozen=True)
class DebugObject:
    """独立于 /new 和发话者的诊断设置对象"""
    platform_id: str
    self_id: str
    conversation_kind: str
    target_id: str

    def __post_init__(self) -> None:
        """拒绝缺失, 过长或可能发生跨对象混用的身份"""
        for name, value in asdict(self).items():
            if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512 or any(ord(char) < 32 for char in value):
                raise ValueError(f"Debug 对象的 {name} 无效")
        if self.conversation_kind not in {"group", "private", "chat"}:
            raise ValueError("Debug 对象类型必须为 group, private 或 chat")

    @cached_property
    def key(self) -> str:
        """
        计算包含完整身份的稳定查找键

        返回:
        - 不依赖上下文或发话人的内容摘要
        """
        return hashlib.sha256(json.dumps(asdict(self), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def payload(self) -> ObjectPayload:
        """
        导出结构化身份, 不向调用方暴露运行时对象

        返回:
        - 完整身份字段
        """
        return ObjectPayload(platform_id=self.platform_id, self_id=self.self_id,
                             conversation_kind=self.conversation_kind, target_id=self.target_id)

    @classmethod
    def parse(cls, value: object) -> DebugObject:
        """
        校验配置或已授权 API 中的对象身份

        参数:
        - value: 含完整身份字段的对象

        返回:
        - 规范对象; 非法结构抛出 ValueError
        """
        fields = {"platform_id", "self_id", "conversation_kind", "target_id"}
        if not isinstance(value, dict) or set(value) != fields or any(not isinstance(item, str) for item in value.values()):
            raise ValueError("Debug 对象必须包含完整的平台, 账号, 类型与目标身份")
        data = cast(Mapping[str, str], value)
        return cls(data["platform_id"], data["self_id"], data["conversation_kind"], data["target_id"])

    @classmethod
    def from_origin(cls, origin: CallOrigin) -> DebugObject:
        """
        从可信入站身份构建平台对象

        参数:
        - origin: 后端冻结的本轮来源

        返回:
        - 群或私聊对象, 身份不足时抛出 ValueError
        """
        if is_group_origin(origin):
            kind = "group"
        elif is_private_origin(origin):
            kind = "private"
        else:
            raise ValueError("当前来源不支持对象级 Debug 设置")
        return cls(origin.adapter_id, origin.self_id, kind, origin.chat_id)


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


@dataclass(frozen=True)
class DebugEvent:
    """采集边界冻结的单条诊断事实, 动态正文不混入控制字段"""
    request_id: str
    object: DebugObject
    context_id: str
    title: str
    event_id: str
    sequence: int
    category: str
    phase: str
    span_id: str
    parent_span_id: str
    recorded_at: float
    status: str
    metadata: dict[str, DebugData]
    bodies: dict[str, DebugData]
    settings_revision: str
    parent_request_id: str = ""
    incomplete: bool = False
