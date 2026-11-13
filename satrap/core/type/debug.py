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

from satrap.core.type.conversation import CallOrigin


DebugMode = Literal["inherit", "on", "off"]
DebugData: TypeAlias = None | str | int | float | bool | list["DebugData"] | dict[str, "DebugData"]


class ObjectPayload(TypedDict):
    """可持久保存的完整对象身份"""
    platform_id: str
    """可信入站提供的平台适配器身份, 与账号及目标共同区分对象"""
    self_id: str
    """可信入站的机器人账号身份, 避免同平台不同账号共用设置"""
    conversation_kind: str
    """会话类型, 支持 group, private 或 chat, 区分不同类型的目标"""
    target_id: str
    """会话目标身份, 对群聊为群 ID, 对私聊为对话目标 ID"""


class EventSummary(TypedDict):
    """事件分页的固定摘要协议, 不包含正文摘要或内容"""
    event_id: str
    """采集时生成的事件身份, 用于关联当前事件可展开的正文"""
    request_id: str
    """所属宿主请求身份, 事件分页限定在该请求内"""
    sequence: int
    """请求内从 0 开始递增的采集序号, 分页游标按此顺序排他续读"""
    category: str
    """事件所属阶段类别, 如 request, model 或 tool"""
    phase: str
    """该类别中的执行阶段, 如 start, update 或 end"""
    span_id: str
    """当前执行步骤身份, 空字符串表示未关联步骤"""
    parent_span_id: str
    """父执行步骤身份, 空字符串表示没有关联的父步骤"""
    recorded_at: float
    """事件采集时的 Unix 时间戳, 单位为秒"""
    status: str
    """该阶段的执行状态, 空字符串表示采集时未提供状态"""
    metadata: dict[str, DebugData]
    """脱敏且有界的控制资料, 查询时还合并诊断分类, 不包含正文槽位内容"""
    body_slots: list[str]
    """此事件实际保存的正文槽位名称, 空列表表示没有可展开槽位"""


class EventPage(TypedDict):
    """按序号分页的事件摘要"""
    events: list[EventSummary]
    """当前请求中大于给定序号的事件摘要, 按序号升序排列, 无结果时为空列表"""
    has_more: bool
    """本页之后是否还有符合条件的事件, 由多取一条记录判断"""


@dataclass(frozen=True)
class DebugObject:
    """独立于 /new 和发话者的诊断设置对象"""
    platform_id: str
    """可信入站的平台适配器身份, 不随上下文切换而改变"""
    self_id: str
    """平台上的机器人账号身份, 用于隔离不同账号的对象设置"""
    conversation_kind: str
    """对话类型, 只能为 group, private 或 chat"""
    target_id: str
    """对话目标身份, 从可信入站的聊天 ID 获取而非发话者身份"""

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
        from satrap.core.call_context import is_group_origin, is_private_origin

        if is_group_origin(origin):
            kind = "group"
        elif is_private_origin(origin):
            kind = "private"
        else:
            raise ValueError("当前来源不支持对象级 Debug 设置")
        return cls(origin.adapter_id, origin.self_id, kind, origin.chat_id)


@dataclass(frozen=True)
class DebugEvent:
    """采集边界冻结的单条诊断事实, 动态正文不混入控制字段"""
    request_id: str
    """宿主真实请求身份, 用于聚合该请求的诊断事件"""
    object: DebugObject
    """受理时冻结的诊断对象身份, 决定设置归属和数据隔离"""
    context_id: str
    """实际路由绑定的上下文身份, 未绑定时可为空, 仅用于索引"""
    title: str
    """上下文展示名称, 未提供时可为空"""
    event_id: str
    """采集时生成的唯一事件身份, 用于关联正文槽位"""
    sequence: int
    """请求内从 0 开始递增的采集序号, 写入失败可能留下缺口"""
    category: str
    """事件所属阶段类别, 如 request, model 或 tool"""
    phase: str
    """类别内的执行阶段, 如 start, update 或 end"""
    span_id: str
    """当前执行步骤身份, 空字符串表示未关联步骤"""
    parent_span_id: str
    """父执行步骤身份, 空字符串表示没有关联的父步骤"""
    recorded_at: float
    """事件采集时的 Unix 时间戳, 单位为秒"""
    status: str
    """当前阶段的执行状态, 空字符串表示未提供状态"""
    metadata: dict[str, DebugData]
    """采集时冻结和脱敏的有界控制资料, 与正文分开保存"""
    bodies: dict[str, DebugData]
    """按槽位名保存的已冻结和脱敏正文, 可因预算省略或截断"""
    settings_revision: str
    """请求受理时冻结的诊断设置版本, 用于追溯采集依据"""
    parent_request_id: str = ""
    """重试或分支来源的请求身份, 默认空字符串表示未关联来源"""
    incomplete: bool = False
    """采集或写入是否已知不完整, 默认 False, 后续事件仍可能报告缺失"""
