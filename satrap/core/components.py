from pydantic import BaseModel
from typing import Any, Dict
from enum import Enum

class PlatformComponentType(str, Enum):
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
    """转发消息节点列表"""
    Poke = "Poke"
    """戳消息"""
    Reply = "Reply"
    """回复消息"""
    Forward = "Forward"
    """转发消息"""
    RPS = "RPS"
    """RPS 消息"""
    Dice = "Dice"
    """骰子消息"""
    Shake = "Shake"
    """分享消息"""
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

class BaseMessageComponent(BaseModel):
    model_config = {'extra': 'allow'}
    type: PlatformComponentType
    def toDict(self) -> Dict[str, Any]:
        """
        同步转换为消息组件格式
        {
            "type": "plain",   # ComponentType 枚举转小写
            "data": {...}      # 排除 type 字段和 None 值
        }
        """
        data = self.model_dump(exclude_none=True, exclude={'type'})   # 利用 Pydantic 导出所有字段
        type_str = self.type.value if hasattr(self.type, 'value') else str(self.type)
        return {"type": type_str.lower(), "data": data}

    async def to_dict(self) -> dict:
        """异步接口
        
        转换为消息组件格式
        {
            "type": "plain",   # ComponentType 枚举转小写
            "data": {...}      # 排除 type 字段和 None 值
        }"""
        return self.toDict()