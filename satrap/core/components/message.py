from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Dict

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from satrap.core.log import logger


_SATRAP_DATA_DIR = Path(__file__).resolve().parents[2] / "satrapdata"
_SATRAP_TEMP_DIR = _SATRAP_DATA_DIR / "temp"
_callback_api_base: str = ""


def get_satrap_temp_path() -> str:
    """获取 Satrap 临时目录, 不存在时自动创建"""
    _SATRAP_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return str(_SATRAP_TEMP_DIR)


def set_callback_api_base(base_url: str | None) -> None:
    """设置文件回调服务地址, 用于 register_to_file_service()"""
    global _callback_api_base
    _callback_api_base = (base_url or "").strip().rstrip("/")


def get_callback_api_base() -> str:
    """获取当前文件回调服务地址"""
    return _callback_api_base


def file_to_base64(path: str) -> str:
    """读取本地文件并转为 base64 字符串"""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


async def download_file(url: str, path: str) -> str:
    """异步下载文件到指定路径"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
    return os.path.abspath(path)


async def download_image_by_url(url: str) -> str:
    """下载图片到 Satrap 临时目录"""
    suffix = Path(url.split("?", 1)[0]).suffix or ".jpg"
    path = os.path.join(get_satrap_temp_path(), f"imgseg_{uuid.uuid4().hex}{suffix}")
    return await download_file(url, path)


def _strip_file_uri(path: str) -> str:
    """兼容 file:// 与 file:/// 路径, 并处理 Windows 盘符"""
    if not path.startswith("file://"):
        return path
    stripped = path[7:]
    if os.name == "nt" and len(stripped) > 2 and stripped[0] == "/" and stripped[2] == ":":
        stripped = stripped[1:]
    return stripped


class SatrapFileTokenService:
    """轻量文件 token 注册服务, 只维护 token 到本地路径的映射"""

    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    async def register_file(self, path: str) -> str:
        """注册文件路径并返回 token"""
        real_path = os.path.abspath(_strip_file_uri(path))
        if not os.path.exists(real_path):
            raise FileNotFoundError(f"文件不存在, 无法注册: {real_path}")
        token = uuid.uuid4().hex
        self._files[token] = real_path
        return token

    def get_file(self, token: str) -> str | None:
        """按 token 获取已注册的本地文件路径"""
        return self._files.get(token)


file_token_service = SatrapFileTokenService()


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

    type: PlatformComponentType

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)

    def toDict(self) -> Dict[str, Any]:
        """同步转换为通用消息组件格式"""
        data = self.model_dump(exclude_none=True, exclude={"type"}, by_alias=False)
        extras = getattr(self, "__pydantic_extra__", None) or {}
        data.update({k: v for k, v in extras.items() if v is not None})
        if "type_" in data:
            data["type"] = data.pop("type_")
        if "_type" in data:
            data["type"] = data.pop("_type")
        type_str = self.type.value if hasattr(self.type, "value") else str(self.type)
        return {"type": type_str.lower(), "data": data}

    async def to_dict(self) -> dict:
        """异步转换接口, 默认回退到同步 toDict()"""
        return self.toDict()


class Plain(BaseMessageComponent):
    """纯文本消息组件"""

    type: PlatformComponentType = PlatformComponentType.Plain
    text: str

    def __init__(self, text: str, convert: bool = True, **kwargs: Any) -> None:
        super().__init__(text=text, convert=convert, **kwargs)

    def toDict(self) -> dict:
        return {"type": "text", "data": {"text": self.text}}

    async def to_dict(self) -> dict:
        return self.toDict()


class Face(BaseMessageComponent):
    """表情消息组件"""

    type: PlatformComponentType = PlatformComponentType.Face
    id: int | str


class _FileLikeComponent(BaseMessageComponent):
    """带文件来源的消息组件基类"""

    file: str | None = ""
    url: str | None = ""
    path: str | None = ""

    @classmethod
    def fromFileSystem(cls, path: str, **kwargs: Any):
        return cls(file=f"file:///{os.path.abspath(path)}", path=path, **kwargs)

    @classmethod
    def fromURL(cls, url: str, **kwargs: Any):
        if url.startswith(("http://", "https://")):
            return cls(file=url, **kwargs)
        raise ValueError("not a valid url")

    @classmethod
    def fromBase64(cls, bs64_data: str, **kwargs: Any):
        return cls(file=f"base64://{bs64_data}", **kwargs)

    def _source(self) -> str:
        source = self.url or self.file or ""
        if not source:
            raise ValueError("No valid file or URL provided")
        return source

    async def convert_to_file_path(self) -> str:
        """将消息段统一转换为本地文件路径"""
        source = self._source()
        if source.startswith("file://"):
            path = _strip_file_uri(source)
            if os.path.exists(path):
                return os.path.abspath(path)
            raise FileNotFoundError(f"not a valid file: {source}")
        if source.startswith("http"):
            filename = f"{self.type.value.lower()}seg_{uuid.uuid4().hex}"
            suffix = Path(source.split("?", 1)[0]).suffix
            return await download_file(source, os.path.join(get_satrap_temp_path(), filename + suffix))
        if source.startswith("base64://"):
            bs64_data = source.removeprefix("base64://")
            file_path = os.path.join(get_satrap_temp_path(), f"{self.type.value.lower()}seg_{uuid.uuid4().hex}")
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(bs64_data))
            return os.path.abspath(file_path)
        if os.path.exists(source):
            return os.path.abspath(source)
        raise FileNotFoundError(f"not a valid file: {source}")

    async def convert_to_base64(self) -> str:
        """将消息段统一转换为 base64 字符串"""
        source = self._source()
        if source.startswith("file://"):
            bs64_data = file_to_base64(_strip_file_uri(source))
        elif source.startswith("http"):
            file_path = await self.convert_to_file_path()
            bs64_data = file_to_base64(file_path)
        elif source.startswith("base64://"):
            bs64_data = source
        elif os.path.exists(source):
            bs64_data = file_to_base64(source)
        else:
            raise FileNotFoundError(f"not a valid file: {source}")
        return bs64_data.removeprefix("base64://")

    async def register_to_file_service(self) -> str:
        """将消息段文件注册到 Satrap 文件 token 服务"""
        callback_host = get_callback_api_base()
        if not callback_host:
            raise RuntimeError("未配置 callback_api_base, 文件服务不可用")
        file_path = await self.convert_to_file_path()
        token = await file_token_service.register_file(file_path)
        logger.debug(f"已注册: {callback_host}/api/file/{token}")
        return f"{callback_host}/api/file/{token}"


class Record(_FileLikeComponent):
    """语音消息组件"""

    type: PlatformComponentType = PlatformComponentType.Record
    text: str | None = None

    def __init__(self, file: str | None, **kwargs: Any) -> None:
        super().__init__(file=file, **kwargs)


class Video(_FileLikeComponent):
    """视频消息组件"""

    type: PlatformComponentType = PlatformComponentType.Video
    file: str | None = ""
    cover: str | None = ""

    def __init__(self, file: str, **kwargs: Any) -> None:
        super().__init__(file=file, **kwargs)

    @classmethod
    def fromBase64(cls, bs64_data: str, **kwargs: Any):
        return cls(file=f"base64://{bs64_data}", **kwargs)

    async def to_dict(self) -> dict:
        """异步序列化视频, 支持按 callback 地址暴露本地文件"""
        payload_file = self.file
        if payload_file and not payload_file.startswith("http"):
            callback_host = get_callback_api_base()
            if callback_host:
                file_path = await self.convert_to_file_path()
                token = await file_token_service.register_file(file_path)
                payload_file = f"{callback_host}/api/file/{token}"
        return {"type": "video", "data": {"file": payload_file}}


class At(BaseMessageComponent):
    """用户提及消息组件"""

    type: PlatformComponentType = PlatformComponentType.At
    qq: int | str
    name: str | None = ""

    def toDict(self) -> dict:
        return {"type": "at", "data": {"qq": str(self.qq)}}


class AtAll(At):
    """全体成员提及消息组件"""

    qq: int | str = "all"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class RPS(BaseMessageComponent):
    """猜拳消息组件"""

    type: PlatformComponentType = PlatformComponentType.RPS


class Dice(BaseMessageComponent):
    """骰子消息组件"""

    type: PlatformComponentType = PlatformComponentType.Dice


class Shake(BaseMessageComponent):
    """窗口抖动消息组件"""

    type: PlatformComponentType = PlatformComponentType.Shake


class Share(BaseMessageComponent):
    """链接分享消息组件"""

    type: PlatformComponentType = PlatformComponentType.Share
    url: str
    title: str
    content: str | None = ""
    image: str | None = ""


class Contact(BaseMessageComponent):
    """联系人分享消息组件"""

    type: PlatformComponentType = PlatformComponentType.Contact
    type_: str = Field(default="", alias="_type")
    id: int | str | None = 0


class Location(BaseMessageComponent):
    """地理位置消息组件"""

    type: PlatformComponentType = PlatformComponentType.Location
    lat: float
    lon: float
    title: str | None = ""
    content: str | None = ""


class Music(BaseMessageComponent):
    """音乐分享消息组件"""

    type: PlatformComponentType = PlatformComponentType.Music
    type_: str = Field(default="", alias="_type")
    id: int | str | None = 0
    url: str | None = ""
    audio: str | None = ""
    title: str | None = ""
    content: str | None = ""
    image: str | None = ""


class Image(_FileLikeComponent):
    """图片消息组件"""

    type: PlatformComponentType = PlatformComponentType.Image
    type_: str | None = Field(default="", alias="_type")

    def __init__(self, file: str | None, **kwargs: Any) -> None:
        super().__init__(file=file, **kwargs)

    @staticmethod
    def fromBytes(data: bytes):
        """从字节数据创建图片消息段"""
        return Image.fromBase64(base64.b64encode(data).decode("utf-8"))

    @staticmethod
    def fromIO(io_obj: Any):
        """从类文件对象创建图片消息段"""
        return Image.fromBytes(io_obj.read())


class Reply(BaseMessageComponent):
    """回复引用消息组件"""

    type: PlatformComponentType = PlatformComponentType.Reply
    id: str | int
    """所引用的消息 ID"""
    chain: list[BaseMessageComponent] | None = Field(default_factory=list)
    """被引用的消息段列表"""
    sender_id: int | str | None = 0
    """被引用消息发送者 ID"""
    sender_nickname: str | None = ""
    """被引用消息发送者昵称"""
    time: int | None = 0
    """被引用消息发送时间"""
    message_str: str | None = ""
    """被引用消息解析后的纯文本内容"""
    text: str | None = ""
    """deprecated"""
    qq: int | None = 0
    """deprecated"""
    seq: int | None = 0
    """deprecated"""


class Poke(BaseMessageComponent):
    """戳一戳消息组件"""

    type: PlatformComponentType = PlatformComponentType.Poke
    type_: str | int = Field(default="126", alias="_type")
    id: int | str | None = 0
    qq: int | str | None = 0

    def __init__(self, poke_type: str | int | None = None, **kwargs: Any) -> None:
        legacy_type = kwargs.pop("type", None)
        if poke_type is None:
            poke_type = legacy_type
        if poke_type in (None, "", "poke", "Poke"):
            poke_type = "126"
        super().__init__(_type=str(poke_type), **kwargs)

    def target_id(self) -> str | None:
        """获取规范化目标 ID, 兼容旧 qq 字段"""
        for value in (self.id, self.qq):
            if value is None:
                continue
            text = str(value).strip()
            if text and text != "0":
                return text
        return None

    def toDict(self) -> dict:
        data = {"type": str(self.type_ or "126")}
        if target_id := self.target_id():
            data["id"] = target_id
        return {"type": "poke", "data": data}


class Forward(BaseMessageComponent):
    """转发消息组件"""

    type: PlatformComponentType = PlatformComponentType.Forward
    id: str


class Node(BaseMessageComponent):
    """合并转发消息节点"""

    type: PlatformComponentType = PlatformComponentType.Node
    id: int | None = 0
    name: str | None = ""
    uin: str | None = "0"
    content: list[BaseMessageComponent] = Field(default_factory=list)
    seq: str | list | None = ""
    time: int | None = 0

    def __init__(self, content: list[BaseMessageComponent] | BaseMessageComponent, **kwargs: Any) -> None:
        if isinstance(content, BaseMessageComponent):
            content = [content]
        super().__init__(content=content, **kwargs)

    async def to_dict(self) -> dict:
        data_content = []
        for comp in self.content:
            if isinstance(comp, (Image, Record)):
                bs64_data = await comp.convert_to_base64()
                data_content.append(
                    {
                        "type": comp.type.value.lower(),
                        "data": {"file": f"base64://{bs64_data}"},
                    }
                )
            elif isinstance(comp, (Plain, File, Node, Nodes)):
                data_content.append(await comp.to_dict())
            else:
                data_content.append(comp.toDict())
        return {
            "type": "node",
            "data": {
                "user_id": str(self.uin),
                "nickname": self.name,
                "content": data_content,
            },
        }


class Nodes(BaseMessageComponent):
    """合并转发消息节点列表组件"""

    type: PlatformComponentType = PlatformComponentType.Nodes
    nodes: list[Node]

    def __init__(self, nodes: list[Node], **kwargs: Any) -> None:
        super().__init__(nodes=nodes, **kwargs)

    def toDict(self) -> dict:
        return {"messages": [node.toDict() for node in self.nodes]}

    async def to_dict(self) -> dict:
        """将 Nodes 转换为 OneBot 风格的消息列表"""
        return {"messages": [await node.to_dict() for node in self.nodes]}


class Json(BaseMessageComponent):
    """JSON 消息组件"""

    type: PlatformComponentType = PlatformComponentType.Json
    data: dict

    def __init__(self, data: str | dict, **kwargs: Any) -> None:
        if isinstance(data, str):
            data = json.loads(data)
        super().__init__(data=data, **kwargs)


class Unknown(BaseMessageComponent):
    """未知消息组件"""

    type: PlatformComponentType = PlatformComponentType.Unknown
    text: str


class File(BaseMessageComponent):
    """文件消息组件"""

    type: PlatformComponentType = PlatformComponentType.File
    name: str | None = ""
    file_: str | None = ""
    url: str | None = ""

    def __init__(self, name: str, file: str = "", url: str = "") -> None:
        super().__init__(name=name, file_=file, url=url)

    def toDict(self) -> dict:
        """同步序列化文件消息段, 不触发网络下载"""
        payload_file = self.file_ or self.url or ""
        return {"type": "file", "data": {"name": self.name, "file": payload_file}}

    @property
    def file(self) -> str:
        """同步获取文件路径, 异步上下文中不会阻塞下载"""
        if self.file_:
            path = _strip_file_uri(self.file_)
            if os.path.exists(path):
                return os.path.abspath(path)
        if self.url:
            try:
                asyncio.get_running_loop()
                logger.warning(
                    "不可以在异步上下文中同步等待下载! "
                    "请使用 await get_file() 代替直接获取 <File>.file 字段"
                )
                return ""
            except RuntimeError:
                try:
                    asyncio.run(self._download_file())
                except Exception as e:
                    logger.error(f"文件下载失败: {e}")
                if self.file_ and os.path.exists(_strip_file_uri(self.file_)):
                    return os.path.abspath(_strip_file_uri(self.file_))
        return ""

    @file.setter
    def file(self, value: str) -> None:
        """向前兼容 file 属性设置"""
        if value.startswith(("http://", "https://")):
            self.url = value
        else:
            self.file_ = value

    async def get_file(self, allow_return_url: bool = False) -> str:
        """异步获取文件路径, 可选择直接返回 URL"""
        if allow_return_url and self.url:
            return self.url
        if self.file_:
            path = _strip_file_uri(self.file_)
            if os.path.exists(path):
                return os.path.abspath(path)
        if self.url:
            await self._download_file()
            if self.file_:
                return os.path.abspath(_strip_file_uri(self.file_))
        return ""

    async def _download_file(self) -> None:
        """下载文件到 Satrap 临时目录"""
        if not self.url:
            raise ValueError("Download failed: No URL provided in File component.")
        if self.name:
            stem, suffix = os.path.splitext(self.name)
            filename = f"fileseg_{stem}_{uuid.uuid4().hex[:8]}{suffix}"
        else:
            filename = f"fileseg_{uuid.uuid4().hex}"
        self.file_ = await download_file(self.url, os.path.join(get_satrap_temp_path(), filename))

    async def register_to_file_service(self) -> str:
        """将文件注册到 Satrap 文件 token 服务"""
        callback_host = get_callback_api_base()
        if not callback_host:
            raise RuntimeError("未配置 callback_api_base, 文件服务不可用")
        file_path = await self.get_file()
        token = await file_token_service.register_file(file_path)
        logger.debug(f"已注册: {callback_host}/api/file/{token}")
        return f"{callback_host}/api/file/{token}"

    async def to_dict(self) -> dict:
        """异步序列化文件, 支持按 callback 地址暴露本地文件"""
        payload_file = await self.get_file(allow_return_url=True)
        if payload_file and not payload_file.startswith("http"):
            callback_host = get_callback_api_base()
            if callback_host:
                token = await file_token_service.register_file(payload_file)
                payload_file = f"{callback_host}/api/file/{token}"
        return {"type": "file", "data": {"name": self.name, "file": payload_file}}


ComponentTypes = {
    "plain": Plain,
    "text": Plain,
    "image": Image,
    "record": Record,
    "video": Video,
    "file": File,
    "face": Face,
    "at": At,
    "rps": RPS,
    "dice": Dice,
    "shake": Shake,
    "share": Share,
    "contact": Contact,
    "location": Location,
    "music": Music,
    "reply": Reply,
    "poke": Poke,
    "forward": Forward,
    "node": Node,
    "nodes": Nodes,
    "json": Json,
    "unknown": Unknown,
}
