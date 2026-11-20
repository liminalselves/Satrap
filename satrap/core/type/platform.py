"""平台消息、消息组件与命令结果契约"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import time

from pydantic import BaseModel, ConfigDict

from satrap.core.type.model import RequestContextBlock


class PlatformComponentType(str, Enum):
    """平台消息组件类型枚举"""

    Plain = "Plain"
    """普通文本消息"""
    Image = "Image"
    """图片消息"""
    Record = "Record"
    """语音消息"""
    Video = "Video"
    """视频消息"""
    File = "File"
    """文件消息"""
    Face = "Face"
    """表情消息"""
    At = "At"
    """@ 消息"""
    Node = "Node"
    """转发消息节点"""
    Nodes = "Nodes"
    """合并转发中的节点列表容器, 与单个 Node 组件区分"""
    Poke = "Poke"
    """戳一戳消息, 由支持该组件的平台适配器解释"""
    Reply = "Reply"
    """回复消息"""
    Forward = "Forward"
    """转发消息"""
    RPS = "RPS"
    """RPS 消息"""
    Dice = "Dice"
    """骰子消息"""
    Shake = "Shake"
    """抖动消息"""
    Contact = "Contact"
    """联系人消息"""
    Share = "Share"
    """分享消息"""
    Location = "Location"
    """位置消息"""
    Music = "Music"
    """音乐消息"""
    Json = "Json"
    """JSON 消息"""
    Unknown = "Unknown"
    """未知消息"""


ComponentType = PlatformComponentType


class BaseMessageComponent(BaseModel):
    """消息组件基类, 提供 Pydantic 校验与统一序列化"""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
        populate_by_name=True,
    )
    """Pydantic 组件配置, 允许任意组件类型和平台扩展字段, 并支持按别名填充"""

    type: PlatformComponentType
    """组件的平台无关类别, 序列化时转为小写 type 标识"""

    def __init__(self, **data: Any) -> None:
        """
        初始化 BaseMessageComponent

        参数:
        - data: 输入数据
        """
        super().__init__(**data)

    def toDict(self) -> Dict[str, Any]:
        """
        同步转换为通用消息组件格式

        返回:
        - Dict[str, Any]: 同步转换为通用消息组件格式
        """
        data = self.model_dump(exclude_none=True, exclude={"type"}, by_alias=False)
        extras = self.__pydantic_extra__ or {}
        data.update({k: v for k, v in extras.items() if v is not None})
        if "type_" in data:
            data["type"] = data.pop("type_")
        if "_type" in data:
            data["type"] = data.pop("_type")
        type_str = self.type.value if hasattr(self.type, "value") else str(self.type)
        return {"type": type_str.lower(), "data": data}

    async def to_dict(self) -> dict[str, Any]:
        """
        异步转换接口, 默认回退到同步 toDict()

        返回:
        - dict[str, Any]: 异步转换接口, 默认回退到同步 toDict()
        """
        return self.toDict()


class PlatformMessageType(Enum):
    GROUP_MESSAGE = "GroupMessage"
    """来自群组会话的消息"""
    FRIEND_MESSAGE = "FriendMessage"
    """来自好友或其他单聊会话的消息"""
    OTHER_MESSAGE = "OtherMessage"
    """不属于群聊或单聊的其他消息, 例如系统消息"""

@dataclass
class MessageMember:
    user_id: str
    """平台提供的用户身份标识, 用于区分消息发送者或群成员"""
    nickname: Optional[str] = None
    """平台提供的用户昵称快照, 未提供时为 None"""
    card: Optional[str] = None
    """接收时的本群昵称快照, 未提供时为 None"""
    role: str = ""
    """平台入站事件提供的本群角色, 执行管理操作前仍需回源复核"""

    def __str__(self) -> str:
        return (
            f"User ID: {self.user_id},"
            f"Nickname: {self.nickname if self.nickname else 'N/A'}"
        )   # 使用 f-string 来构建返回的字符串表示形式

@dataclass
class Group:
    group_id: str
    """平台提供的群身份标识, 用于定位群会话"""
    group_name: Optional[str] = None
    """平台提供的群名称快照, 未提供时为 None"""
    group_avatar: Optional[str] = None
    """平台提供的群头像地址或标识, 未提供时为 None"""
    group_owner: Optional[str] = None
    """平台报告的群主用户标识, 未提供时为 None"""
    group_admins: Optional[List[str]] = None
    """平台报告的群管理员用户标识列表, None 表示未提供而非没有管理员"""
    members: Optional[List[MessageMember]] = None
    """平台随消息提供的群成员列表, None 表示未附带完整成员资料"""

    def __str__(self) -> str:
        return (
            f"Group ID: {self.group_id}\n"
            f"Name: {self.group_name if self.group_name else 'N/A'}\n"
            f"Avatar: {self.group_avatar if self.group_avatar else 'N/A'}\n"
            f"Owner ID: {self.group_owner if self.group_owner else 'N/A'}\n"
            f"Admin IDs: {self.group_admins if self.group_admins else 'N/A'}\n"
            f"Members Len: {len(self.members) if self.members else 0}\n"
            f"First Member: {self.members[0] if self.members else 'N/A'}\n"
        )   # 使用 f-string 来构建返回的字符串表示形式

@dataclass
class PlatformMessage:
    type: PlatformMessageType
    """平台事件对应的消息会话类别"""
    self_id: str
    """接收该事件的机器人账号标识"""
    session_id: str
    """适配器生成的会话标识, 具体形式取决于 unique_session 设置"""
    message_id: str
    """平台提供的消息身份标识, 用于回复和档案定位"""
    group: Optional[Group]
    """群消息附带的群资料, 私聊或无群资料时为 None"""
    sender: MessageMember
    """平台事件中的发送者身份及群内资料快照"""
    message: List[BaseMessageComponent]
    """按平台原始顺序排列的结构化消息组件链"""
    message_str: str
    """由消息组件提取的纯文本形式, 便于不支持组件的消费者读取"""
    raw_message: object
    """适配器保留的平台原始事件或消息对象, 未做跨平台归一化"""
    timestamp: int
    """消息时间的 Unix 秒数, 默认初始化为宿主接收时的整数时间"""

    def __init__(self) -> None:
        """初始化 PlatformMessage"""
        self.timestamp = int(time.time())
        self.received_at = time.time()
        self.timestamp_source = "local"
        self.group = None

    def __str__(self) -> str:
        return str(self.__dict__)

    @property
    def group_id(self) -> str:
        """
        向后兼容的 group_id 属性
        群组id, 如果为私聊, 则为空

        返回:
        - str: 向后兼容的 group_id 属性
        """
        if self.group:
            return self.group.group_id
        return ""

    @group_id.setter
    def group_id(self, value: Optional[str]) -> None:
        """
        设置 group_id

        参数:
        - value: 输入值
        """
        if value:
            if self.group:
                self.group.group_id = value
            else:
                self.group = Group(group_id=value)
        else:
            self.group = None

class PlatformStatus(Enum):
    """平台状态"""
    PENDING = "pending"
    """平台已登记但尚未运行"""
    RUNNING = "running"
    """平台运行任务已启动, 不代表连接已就绪"""
    ERROR = "error"
    """平台运行发生错误"""
    STOPPED = "stopped"
    """平台已停止运行"""


@dataclass
class PlatformError:
    """平台错误信息"""
    message: str
    """平台适配器记录的错误说明, 用于状态诊断"""
    timestamp: datetime = field(default_factory=datetime.now)
    """错误发生时间"""
    traceback: Optional[str] = None
    """错误的完整 traceback 信息"""

@dataclass
class CommandAction:
    """命令执行后的动作指示"""
    action: str
    """
    动作类型, 可选值:
        - "switch_session": 切换会话
        - "new_session": 创建新会话
        - "switch": 创建并切换新会话, 兼容内置 /new 的既有动作名
        - "none": 无动作, 仅返回信息
    """
    target_session_id: Optional[str] = None
    """目标会话 ID, 用于切换会话等操作"""
    message: str = ""
    """返回给用户的信息"""
    user_input: str = ""
    """run_task 动作立即执行的用户任务, 不递归调用会话锁"""
    request_blocks: tuple[RequestContextBlock, ...] = ()
    """run_task 动作仅供本轮模型请求使用的资料"""
