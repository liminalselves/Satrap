from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Any

from satrap.core.log import logger
from satrap.core.components import BaseMessageComponent, PlatformComponentType
from satrap.core.type import PlatformMessage, PlatformMessageType

# 模块级导入, 避免每个方法重复 lazy import; 无循环依赖风险 (__init__.py 仅在 TYPE_CHECKING 下引用 event.py)
from satrap.core.platform import PlatformAdapter


@dataclass
class MessageSession:
    """消息会话标识

    由平台名, 消息类型, 会话 ID 三元组构成, 用于唯一标识一个对话上下文
    """

    platform_name: str
    message_type: str
    session_id: str

    def __str__(self) -> str:
        """返回 format: platform_name:message_type:session_id"""
        return f"{self.platform_name}:{self.message_type}:{self.session_id}"

    @classmethod
    def from_str(cls, s: str) -> MessageSession:
        """从 "platform_name:message_type:session_id" 格式字符串反序列化"""
        parts = s.split(":", 2)
        if len(parts) < 3:
            logger.error(
                f"[MessageSession.from_str] 无效的 session 字符串: {s!r}, "
                f"将使用空值 fallback"
            )
            return cls(platform_name="", message_type="", session_id=s)
        return cls(platform_name=parts[0], message_type=parts[1], session_id=parts[2])


@dataclass
class PlatformMetadata:
    """平台元信息"""
    name: str
    id: str
    adapter_display_name: str | None = None
    description: str = ""
    support_streaming_message: bool = False
    support_proactive_message: bool = False


class MessageChain:
    """消息链

    由多个 BaseMessageComponent 组成的消息序列, 支持迭代, 拼接, 索引等操作
    """

    def __init__(self, components: list[BaseMessageComponent] | None = None):
        """初始化消息链, 可传入初始组件列表"""
        self._components = components or []

    @classmethod
    def from_text(cls, text: str) -> MessageChain:
        """从纯文本快速构造消息链"""
        return cls([BaseMessageComponent(type=PlatformComponentType.Plain, text=text)])   # type: ignore

    @property
    def components(self) -> list[BaseMessageComponent]:
        """获取所有消息组件"""
        return self._components

    def __iter__(self):
        """迭代消息组件"""
        return iter(self._components)

    def __len__(self) -> int:
        """组件数量"""
        return len(self._components)

    def __bool__(self) -> bool:
        """是否包含组件"""
        return len(self._components) > 0

    def __add__(self, other: MessageChain) -> MessageChain:
        """拼接两条消息链"""
        return MessageChain(self._components + other._components)

    def __getitem__(self, index):
        """按索引获取组件"""
        return self._components[index]

    def to_dict_list(self) -> list[dict]:
        """转为字典列表, 用于序列化或传递给下层"""
        return [c.toDict() for c in self._components]


class EventResultType(Enum):
    """事件处理结果类型"""

    CONTINUE = "continue"
    STOP = "stop"


class MessageEventResult:
    """消息事件处理结果

    链式构建回复消息 (文本/图片)并控制事件传播行为 (继续/停止).
    """

    def __init__(self):
        self.chain: list[BaseMessageComponent] | None = None
        self.console_log: str | None = None
        self.result_type: EventResultType = EventResultType.CONTINUE
        self._stop: bool = False

    def message(self, text: str) -> MessageEventResult:
        """设置纯文本回复"""
        self.chain = [BaseMessageComponent(type=PlatformComponentType.Plain, text=text)]   # type: ignore
        return self

    def url_image(self, url: str) -> MessageEventResult:
        """设置网络图片回复"""
        self.chain = [BaseMessageComponent(type=PlatformComponentType.Image, url=url)]   # type: ignore
        return self

    def file_image(self, path: str) -> MessageEventResult:
        """设置本地图片文件回复"""
        self.chain = [BaseMessageComponent(type=PlatformComponentType.Image, file=path)]   # type: ignore
        return self

    def stop_event(self) -> MessageEventResult:
        """标记事件处理停止 (后续处理器不再执行)"""
        self.result_type = EventResultType.STOP
        self._stop = True
        return self

    def continue_event(self) -> MessageEventResult:
        """标记事件处理继续传递"""
        self.result_type = EventResultType.CONTINUE
        self._stop = False
        return self

    def is_stopped(self) -> bool:
        """是否已被标记为停止"""
        return self._stop

    def set_console_log(self, msg: str) -> MessageEventResult:
        """设置控制台日志输出"""
        self.console_log = msg
        return self


@dataclass
class ProviderRequest:
    """LLM 提供者请求参数

    封装向 LLM 发起请求所需的全部参数, 包括提示词, 会话 ID, 多媒体 URL, 上下文列表等.
    """

    prompt: str = ""
    session_id: str = ""
    image_urls: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)
    contexts: list = field(default_factory=list)
    system_prompt: str = ""
    conversation: Any = None


class MessageEvent:
    """消息事件

    封装从平台接收的一条完整消息及其上下文, 提供统一的 API 用于:
    - 读取消息内容, 发送者, 来源平台等信息
    - 回复消息 (文本, 图片, 流式)
    - 控制事件传播 (继续/停止)
    - 管理临时文件
    """

    def __init__(
        self,
        message_str: str,
        platform_message: PlatformMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        adapter: Any,
    ):
        """初始化消息事件

        参数:
        - message_str: 原始消息文本
        - platform_message: 平台消息对象
        - platform_meta: 平台元信息
        - session_id: 会话 ID
        - adapter: 所属平台适配器实例
        """
        self.message_str = message_str
        self.platform_message = platform_message
        self.platform_meta = platform_meta
        self.adapter = adapter

        mt = platform_message.type.value if isinstance(platform_message.type, PlatformMessageType) else str(platform_message.type)
        self.session = MessageSession(
            platform_name=platform_meta.id,
            message_type=mt,
            session_id=session_id,
        )

        self.role = "member"
        self.is_wake = False
        self.is_at_or_wake_command = False

        self._result: MessageEventResult | None = None
        self.created_at = time()
        self._has_send_oper = False
        self.call_llm = True
        self._temporary_local_files: list[str] = []
        self.plugins_name: list[str] | None = None
        self._extras: dict[str, Any] = {}

    @property
    def unified_msg_origin(self) -> str:
        """统一消息来源标识, 格式: platform_name:message_type:session_id"""
        return str(self.session)

    @unified_msg_origin.setter
    def unified_msg_origin(self, value: str) -> None:
        self.session = MessageSession.from_str(value)

    @property
    def session_id(self) -> str:
        """当前会话 ID"""
        return self.session.session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.session.session_id = value

    def get_platform_name(self) -> str:
        """获取来源平台显示名称"""
        return self.platform_meta.name

    def get_platform_id(self) -> str:
        """获取来源平台标识 ID"""
        return self.platform_meta.id

    def get_message_str(self) -> str:
        """获取原始消息文本"""
        return self.message_str

    def _outline_chain(self, chain: list[BaseMessageComponent] | None) -> str:
        """将消息组件列表转为可读的文本摘要 (内部方法)
        
        参数:
        - chain: 消息组件列表
        """
        if not chain:
            return ""
        parts = []
        for c in chain:
            t = c.type
            if t == PlatformComponentType.Plain:
                parts.append(getattr(c, 'text', '') or '')
            elif t == PlatformComponentType.Image:
                parts.append("[图片]")
            elif t == PlatformComponentType.Face:
                parts.append(f"[表情:{getattr(c, 'id', '')}]")
            elif t == PlatformComponentType.At:
                parts.append(f"[At:{getattr(c, 'qq', '')}]")
            elif t == PlatformComponentType.Reply:
                msg_str = getattr(c, 'message_str', None) or ''
                nickname = getattr(c, 'sender_nickname', None) or ''
                if msg_str:
                    parts.append(f"[引用消息({nickname}: {msg_str})]")
                else:
                    parts.append("[引用消息]")
            elif t == PlatformComponentType.Forward:
                parts.append("[转发消息]")
            else:
                parts.append(f"[{t.value if hasattr(t, 'value') else t}]")
            parts.append(" ")
        return "".join(parts)

    def get_message_outline(self) -> str:
        """获取消息的文本摘要 (纯文本, 图片, 引用等的简短描述)"""
        chain = getattr(self.platform_message, 'message', None)
        return self._outline_chain(chain)

    def get_messages(self) -> list[BaseMessageComponent]:
        """获取消息组件列表"""
        return getattr(self.platform_message, 'message', []) or []

    def get_message_type(self) -> str:
        """获取消息类型 (如 friend_message / group_message)"""
        mt = getattr(self.platform_message, 'type', None)
        if isinstance(mt, PlatformMessageType):
            return mt.value
        if mt is not None:
            return str(mt)
        return self.session.message_type

    def get_session_id(self) -> str:
        """获取会话 ID (同 session_id 属性)"""
        return self.session_id

    def get_group_id(self) -> str:
        """获取群组 ID (私聊时返回空字符串)"""
        g = getattr(self.platform_message, 'group', None)
        if g:
            return getattr(g, 'group_id', '')
        return ''

    def get_self_id(self) -> str:
        """获取机器人自身的平台 ID"""
        return getattr(self.platform_message, 'self_id', '')

    def get_sender_id(self) -> str:
        """获取发送者 ID"""
        sender = getattr(self.platform_message, 'sender', None)
        if sender:
            uid = getattr(sender, 'user_id', None)
            if uid is not None:
                return str(uid)
        return ''

    def get_sender_name(self) -> str:
        """获取发送者昵称"""
        sender = getattr(self.platform_message, 'sender', None)
        if sender:
            return getattr(sender, 'nickname', '') or ''
        return ''

    def is_private_chat(self) -> bool:
        """是否为私聊消息"""
        mt = self.get_message_type()
        return mt == PlatformMessageType.FRIEND_MESSAGE.value

    def is_admin(self) -> bool:
        """当前会话角色是否为管理员"""
        return self.role == "admin"

    def is_wake_up(self) -> bool:
        """是否被唤醒 (艾特或唤醒词触发)"""
        return self.is_wake

    def track_temporary_local_file(self, path: str) -> None:
        """跟踪一个临时文件, 事件结束后自动清理"""
        if path and path not in self._temporary_local_files:
            self._temporary_local_files.append(path)

    def cleanup_temporary_local_files(self) -> None:
        """清理所有跟踪的临时文件"""
        paths = list(self._temporary_local_files)
        self._temporary_local_files.clear()
        for path in paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                logger.warning(
                    f"Failed to remove temporary local file {path}: {e}",
                )

    def set_extra(self, key: str, value: Any) -> None:
        """设置自定义扩展字段
        
        参数:
        - key: 扩展字段键
        - value: 扩展字段值
        """
        
        self._extras[key] = value

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        """获取自定义扩展字段; key 为 None 时返回全部扩展字段
        
        参数:
        - key: 扩展字段键
        - default: 若键不存在则返回的默认值
        """
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def clear_extra(self) -> None:
        """清除所有扩展字段"""
        self._extras.clear()

    def set_result(self, result: MessageEventResult | str) -> None:
        """设置事件处理结果; 若传入字符串则自动构造纯文本结果
        
        参数:
        - result: 事件处理结果或纯文本结果字符串
        """
        if isinstance(result, str):
            result = MessageEventResult().message(result)
        if isinstance(result, MessageEventResult) and result.chain is None:
            result.chain = []
        self._result = result

    def get_result(self) -> MessageEventResult | None:
        """获取事件处理结果"""
        return self._result

    def clear_result(self) -> None:
        """清除事件处理结果"""
        self._result = None

    def stop_event(self) -> None:
        """停止事件传播"""
        if self._result is None:
            self.set_result(MessageEventResult().stop_event())
        else:
            self._result.stop_event()

    def continue_event(self) -> None:
        """继续事件传播"""
        if self._result is None:
            self.set_result(MessageEventResult().continue_event())
        else:
            self._result.continue_event()

    def is_stopped(self) -> bool:
        """事件是否已被标记为停止"""
        if self._result is None:
            return False
        return self._result.is_stopped()

    def should_call_llm(self, call_llm: bool) -> None:
        """设置是否应该调用 LLM
        
        参数:
        - call_llm: 是否调用 LLM
        """
        self.call_llm = call_llm

    def make_result(self) -> MessageEventResult:
        """创建一个空的事件结果"""
        return MessageEventResult()

    def plain_result(self, text: str) -> MessageEventResult:
        """快速创建纯文本事件结果
        
        参数:
        - text: 纯文本内容
        """
        return MessageEventResult().message(text)

    def image_result(self, url_or_path: str) -> MessageEventResult:
        """快速创建图片事件结果 (自动识别 URL 或本地路径)
        
        参数:
        - url_or_path: 图片 URL 或本地路径
        """
        if url_or_path.startswith("http"):
            return MessageEventResult().url_image(url_or_path)
        return MessageEventResult().file_image(url_or_path)

    def chain_result(self, chain: list[BaseMessageComponent]) -> MessageEventResult:
        """从消息组件列表创建事件结果
        
        参数:
        - chain: 消息组件列表
        """
        mer = MessageEventResult()
        mer.chain = chain
        return mer

    def request_llm(
        self,
        prompt: str = "",
        session_id: str = "",
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        contexts: list | None = None,
        system_prompt: str = "",
        conversation: Any = None,
    ) -> ProviderRequest:
        """构造 LLM 请求参数; 各参数默认值与事件上下文无关时可留空
        
        参数:
        - prompt: LLM 提示词
        - session_id: 会话 ID
        - image_urls: 图片 URL 列表
        - audio_urls: 音频 URL 列表
        - contexts: 上下文列表
        - system_prompt: 系统提示
        - conversation: 会话上下文

        返回:
        - ProviderRequest: LLM 请求参数
        """
        if image_urls is None:
            image_urls = []
        if audio_urls is None:
            audio_urls = []
        if contexts is None:
            contexts = []
        return ProviderRequest(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            contexts=contexts,
            system_prompt=system_prompt,
            conversation=conversation,
        )

    async def send(self, message: MessageChain) -> None:
        """发送消息到当前会话
        
        参数:
        - message: 要发送的消息链
        """
        if isinstance(self.adapter, PlatformAdapter):
            try:
                await self.adapter.send_message(self.session_id, message)
                self._has_send_oper = True
            except Exception as e:
                logger.error(
                    f"[MessageEvent.send] 发送消息失败: session_id={self.session_id}, "
                    f"错误={e}",
                )

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        """流式发送消息; use_fallback=True 时可由平台降级为非流式
        
        参数:
        - generator: 消息链异步生成器
        - use_fallback: 是否使用降级流式
        """
        if isinstance(self.adapter, PlatformAdapter):
            try:
                await self.adapter.send_stream(
                    self.session_id, generator, use_fallback=use_fallback
                )
                self._has_send_oper = True
            except Exception as e:
                logger.error(
                    f"[MessageEvent.send_streaming] 流式发送消息失败: session_id={self.session_id}, "
                    f"错误={e}",
                )

    async def send_typing(self) -> None:
        """发送"输入中"状态指示"""
        if isinstance(self.adapter, PlatformAdapter):
            try:
                await self.adapter.send_typing(self.session_id)
            except Exception as e:
                logger.error(
                    f"[MessageEvent.send_typing] 发送输入状态失败: session_id={self.session_id}, "
                    f"错误={e}",
                )

    async def stop_typing(self) -> None:
        """停止"输入中"状态指示"""
        if isinstance(self.adapter, PlatformAdapter):
            try:
                await self.adapter.stop_typing(self.session_id)
            except Exception as e:
                logger.error(
                    f"[MessageEvent.stop_typing] 停止输入状态失败: session_id={self.session_id}, "
                    f"错误={e}",
                )

    async def react(self, emoji: str) -> None:
        """对消息添加表情回应
        
        参数:
        - emoji: 表情字符串
        """
        if isinstance(self.adapter, PlatformAdapter):
            try:
                await self.adapter.react(self.session_id, emoji)
            except Exception as e:
                logger.error(
                    f"[MessageEvent.react] 表情回应失败: session_id={self.session_id}, "
                    f"错误={e}",
                )

    async def get_group(self, group_id: str | None = None) -> Any:
        """获取群聊信息; 不传 group_id 时自动使用消息中的群 ID
        
        参数:
        - group_id: 群 ID
        """
        if isinstance(self.adapter, PlatformAdapter):
            try:
                return await self.adapter.get_group(group_id or self.get_group_id())
            except Exception as e:
                logger.error(
                    f"[MessageEvent.get_group] 获取群信息失败: group_id={group_id or self.get_group_id()}, "
                    f"错误={e}",
                )
                return None
        return None

    async def process_buffer(self, buffer: str, pattern: re.Pattern) -> str:
        """按正则模式逐步从缓冲区提取并发送匹配内容, 每次发送后等待 1.5 秒
        
        参数:
        - buffer: 缓冲区字符串
        - pattern: 正则表达式模式
        """
        while True:
            match = re.search(pattern, buffer)
            if not match:
                break
            matched_text = match.group()
            await self.send(MessageChain.from_text(matched_text))
            buffer = buffer[match.end():]
            await asyncio.sleep(1.5)
        return buffer
