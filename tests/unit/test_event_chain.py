"""
platform.event 消息链 / 结果构建 / 事件属性单元测试

覆盖:
- MessageSession: 序列化 / 反序列化 / 非法字符串 fallback
- MessageChain: 构造 / 迭代 / 拼接 / 索引 / 序列化
- MessageEventResult: 文本 / 图片 / 停止 / 继续 / 日志链式构建
- MessageEvent: 会话标识读写, 消息读取, 结果控制, 发送, 临时文件, 扩展字段
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from satrap.core.components import BaseMessageComponent, PlatformComponentType
from satrap.core.platform import PlatformAdapter, PlatformConfig
from satrap.core.platform.event import (
    MessageChain,
    MessageEvent,
    MessageEventResult,
    MessageSession,
    PlatformMetadata,
)
from satrap.core.type import MessageMember, PlatformMessage, PlatformMessageType


class _RecorderAdapter(PlatformAdapter):
    """记录 send_message 调用的测试适配器"""

    def __init__(self, adapter_id: str = "rec1"):
        self.config = PlatformConfig(id=adapter_id, type="rec")
        self.sent: list[tuple[str, MessageChain]] = []

    async def run(self) -> None:
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)

    async def send_message(self, session_id: str, message: MessageChain) -> Any:
        self.sent.append((session_id, message))
        return None


def _make_event(adapter: _RecorderAdapter, session_id: str = "s1") -> MessageEvent:
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.session_id = session_id
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="user-1", nickname="小美")
    message.message = []
    message.message_str = "你好"
    return MessageEvent(
        message_str="你好",
        platform_message=message,
        platform_meta=PlatformMetadata(name=adapter.config.id, id=adapter.config.id),
        session_id=session_id,
        adapter=adapter,
        session_type="dummy",
    )


# ================= MessageSession 测试 =================


def test_message_session_str_roundtrip():
    """session 三元组序列化与反序列化"""
    s = MessageSession(platform_name="misskey", message_type="FriendMessage", session_id="s1")
    assert str(s) == "misskey:FriendMessage:s1"
    parsed = MessageSession.from_str("misskey:FriendMessage:s1")
    assert parsed == s


def test_message_session_from_str_invalid_fallback():
    """非法格式字符串使用空值 fallback, session_id 保留原文"""
    parsed = MessageSession.from_str("invalid")
    assert parsed.platform_name == ""
    assert parsed.message_type == ""
    assert parsed.session_id == "invalid"


# ================= MessageChain 测试 =================


def test_message_chain_operations():
    """消息链构造 / 迭代 / 拼接 / 索引 / 布尔"""
    chain = MessageChain.from_text("你好")
    assert len(chain) == 1
    assert bool(chain) is True
    assert MessageChain().__bool__() is False
    assert getattr(chain[0], "text", "") == "你好"

    other = MessageChain.from_text("世界")
    merged = chain + other
    assert len(merged) == 2
    assert [getattr(c, "text", "") for c in merged] == ["你好", "世界"]


def test_message_chain_to_dict_list():
    """消息链转为字典列表 (type 为小写字符串)"""
    chain = MessageChain.from_text("你好")
    items = chain.to_dict_list()
    assert isinstance(items, list)
    assert items[0]["type"] == PlatformComponentType.Plain.value.lower()
    assert items[0]["data"]["text"] == "你好"


# ================= MessageEventResult 测试 =================


def test_event_result_chain_building():
    """结果构建器: 文本 / 图片 / 停止 / 继续 / 日志"""
    result = MessageEventResult()
    assert result.is_stopped() is False

    result.message("回复")
    assert result.chain is not None
    assert result.chain[0].type == PlatformComponentType.Plain

    result.url_image("http://x/a.png")
    assert result.chain[0].type == PlatformComponentType.Image
    assert getattr(result.chain[0], "url", "") == "http://x/a.png"

    result.file_image("local.png")
    assert getattr(result.chain[0], "file", "") == "local.png"

    result.stop_event()
    assert result.is_stopped() is True
    assert result.result_type.value == "stop"

    result.continue_event()
    assert result.is_stopped() is False

    result.set_console_log("log-line")
    assert result.console_log == "log-line"


# ================= MessageEvent 测试 =================


def test_event_session_identifiers():
    """会话标识读写: unified_msg_origin / session_id"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter, session_id="s1")
    assert event.unified_msg_origin == "rec1:FriendMessage:s1"
    assert event.session_id == "s1"
    assert event.get_session_id() == "s1"

    event.unified_msg_origin = "onebot:GroupMessage:g1"
    assert event.session_id == "g1"

    event.session_id = "g2"
    assert event.session_id == "g2"


def test_event_message_readers():
    """消息读取: 文本 / 发送者 / 平台 / 类型 / 群组"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter)
    assert event.get_message_str() == "你好"
    assert event.get_sender_id() == "user-1"
    assert event.get_sender_name() == "小美"
    assert event.get_platform_name() == "rec1"
    assert event.get_platform_id() == "rec1"
    assert event.get_message_type() == PlatformMessageType.FRIEND_MESSAGE.value
    assert event.is_private_chat() is True
    assert event.is_wake_up() is False
    assert event.is_admin() is False
    assert event.get_self_id() == "bot"
    assert event.get_group_id() == ""


def test_event_message_outline():
    """消息摘要: 纯文本 / 图片 / 引用 混合"""
    from satrap.core.components import BaseMessageComponent, Image, Plain
    from satrap.core.type import MessageMember as _MM

    adapter = _RecorderAdapter()
    event = _make_event(adapter)
    msg = PlatformMessage()
    components: list[BaseMessageComponent] = [
        Plain(text="正文"),
        Image(file="http://x/a.png"),
    ]
    msg.message = components
    msg.sender = _MM(user_id="u", nickname="n")
    event.platform_message = msg

    outline = event.get_message_outline()
    assert "正文" in outline
    assert "[图片]" in outline


def test_event_result_control():
    """事件结果控制: set_result / stop / continue / 快速构建"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter)

    event.set_result("纯文本结果")
    result = event.get_result()
    assert result is not None
    assert result.chain and getattr(result.chain[0], "text", "") == "纯文本结果"

    event.stop_event()
    assert event.is_stopped() is True
    event.continue_event()
    assert event.is_stopped() is False

    plain = event.plain_result("hi")
    assert plain.chain is not None
    assert getattr(plain.chain[0], "text", "") == "hi"

    img = event.image_result("http://x/b.png")
    assert img.chain is not None
    assert getattr(img.chain[0], "url", "") == "http://x/b.png"
    local = event.image_result("local.png")
    assert local.chain is not None
    assert getattr(local.chain[0], "file", "") == "local.png"

    from satrap.core.components import Plain as _Plain

    chain_result = event.chain_result([_Plain(text="x")])
    assert len(chain_result.chain or []) == 1

    event.clear_result()
    assert event.get_result() is None


def test_event_request_llm_defaults():
    """LLM 请求参数: 默认值填充"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter)
    req = event.request_llm(prompt="p", session_id="s1")
    assert req.prompt == "p"
    assert req.session_id == "s1"
    assert req.image_urls == []
    assert req.audio_urls == []
    assert req.contexts == []


@pytest.mark.asyncio
async def test_event_send_records_and_flags():
    """发送消息记录到适配器并标记 has_send_operation"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter)
    assert event.has_send_operation() is False

    await event.send(MessageChain.from_text("回复"))
    assert event.has_send_operation() is True
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0] == "s1"


@pytest.mark.asyncio
async def test_event_send_failure_silently_caught():
    """适配器发送失败被捕获, 不冒泡"""
    class _BoomAdapter(_RecorderAdapter):
        async def send_message(self, session_id: str, message: MessageChain) -> Any:
            raise RuntimeError("network down")

    adapter = _BoomAdapter()
    event = _make_event(adapter)
    await event.send(MessageChain.from_text("x"))
    assert event.has_send_operation() is False


def test_event_temporary_files():
    """临时文件跟踪与清理"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter)

    f1 = Path("_tmp_test_1.png")
    f2 = Path("_tmp_test_2.png")
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")
    event.track_temporary_local_file(str(f1))
    event.track_temporary_local_file(str(f1))   # 重复跟踪不生效
    event.track_temporary_local_file(str(f2))

    event.cleanup_temporary_local_files()
    assert not f1.exists()
    assert not f2.exists()
    assert event._temporary_local_files == []


def test_event_extras():
    """扩展字段: set / get / 全部 / 清除"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter)

    event.set_extra("k1", "v1")
    assert event.get_extra("k1") == "v1"
    assert event.get_extra("missing", "default") == "default"
    assert event.get_extra() == {"k1": "v1"}

    event.clear_extra()
    assert event.get_extra() == {}


@pytest.mark.asyncio
async def test_event_process_buffer(monkeypatch: pytest.MonkeyPatch):
    """
    缓冲区分段提取: 匹配内容逐段发送, 返回剩余部分

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("satrap.core.platform.event.asyncio.sleep", no_sleep)

    adapter = _RecorderAdapter()
    event = _make_event(adapter)
    rest = await event.process_buffer("【A】【B】tail", re.compile(r"【[^】]+】"))
    assert rest == "tail"
    assert len(adapter.sent) == 2
    assert getattr(adapter.sent[0][1].components[0], "text", "") == "【A】"


@pytest.mark.asyncio
async def test_event_process_buffer_stops_on_zero_width_match():
    """可匹配空串的正则不会发送空消息或进入死循环"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter)

    rest = await event.process_buffer("tail", re.compile(r".*?"))

    assert rest == "tail"
    assert adapter.sent == []


def test_event_should_call_llm_flag():
    """call_llm 开关读写"""
    adapter = _RecorderAdapter()
    event = _make_event(adapter)
    assert event.call_llm is True
    event.should_call_llm(False)
    assert event.call_llm is False
