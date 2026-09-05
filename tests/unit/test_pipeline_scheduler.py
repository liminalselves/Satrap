"""
PipelineScheduler / RateLimiter 单元测试

覆盖:
- RateLimiter: token bucket 放行 / 限流 / 独立 key / 时间 refill
- PipelineScheduler: preprocessor 拦截, 限流反馈, 唤醒检查, 权限检查,
  空消息丢弃, UserManager 路由, 成功回复, 超时反馈, 异常兜底, 临时文件清理
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from typing import Any, cast
import time

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform import PlatformAdapter, PlatformConfig
from satrap.core.type import MessageMember, PlatformMessage, PlatformMessageType


def _as_session_manager(fake: Any) -> SessionManager:
    """SessionManager 替身类型边界: 替身实现 handle_call_async/class_cfg_mgr 调用面, cast 集中在此工厂"""
    return cast(SessionManager, fake)


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


class _FakeSessionManager:
    """记录 handle_call_async 调用的假 SessionManager"""

    def __init__(self, response: str = "回复", delay: float = 0.0, class_cfg_mgr: Any = None):
        self.response = response
        self.delay = delay
        self.calls: list[Any] = []
        self.class_cfg_mgr = class_cfg_mgr

    async def handle_call_async(self, user_call: Any) -> str:
        self.calls.append(user_call)
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.response


class _BoomSessionManager:
    """handle_call_async 抛异常的假 SessionManager"""

    class_cfg_mgr = None

    async def handle_call_async(self, user_call: Any) -> str:
        raise RuntimeError("boom")


class _FakeClassCfgMgr:
    """固定返回 adapter_id 参数的假 SessionClassConfigManager"""

    def __init__(self, adapter_id: str):
        self._adapter_id = adapter_id

    def get_params(self, name: str) -> dict[str, Any]:
        return {"adapter_id": self._adapter_id}


class _DenyScheduler(PipelineScheduler):
    """权限检查拒绝的调度器"""

    async def _check_permission(self, event: MessageEvent) -> bool:
        return False


def _message_event(
    adapter: _RecorderAdapter,
    message_str: str = "hello",
    session_id: str = "s1",
    sender_id: str = "user-1",
    msg_type: PlatformMessageType = PlatformMessageType.FRIEND_MESSAGE,
) -> MessageEvent:
    message = PlatformMessage()
    message.type = msg_type
    message.self_id = "bot"
    message.session_id = session_id
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id=sender_id, nickname="User")
    message.message = []
    message.message_str = message_str
    return MessageEvent(
        message_str=message_str,
        platform_message=message,
        platform_meta=PlatformMetadata(name=adapter.config.id, id=adapter.config.id),
        session_id=session_id,
        adapter=adapter,
        session_type="dummy",
    )


# ================= RateLimiter 测试 =================


@pytest.mark.asyncio
async def test_rate_limiter_allows_burst_then_blocks():
    """burst 内放行, 超过后限流并返回等待时间"""
    rl = RateLimiter(rate=1.0, burst=3)
    for _ in range(3):
        allowed, wait = await rl.check("k")
        assert allowed is True
        assert wait == 0.0

    allowed, wait = await rl.check("k")
    assert allowed is False
    assert wait > 0.0


@pytest.mark.asyncio
async def test_rate_limiter_zero_rate_disables_limiting():
    """rate 非正数按禁用限流处理, 不发生除零"""
    limiter = RateLimiter(rate=0, burst=0)
    assert await limiter.check("key") == (True, 0.0)


@pytest.mark.asyncio
async def test_rate_limiter_bucket_count_is_bounded():
    """大量独立 key 不会让桶表超过配置上限"""
    limiter = RateLimiter(rate=1, burst=1, max_buckets=3, idle_ttl=3600)
    for index in range(10):
        await limiter.check(f"key-{index}")
    assert len(limiter._buckets) == 3


@pytest.mark.asyncio
async def test_rate_limiter_keys_are_independent():
    """不同 key 的桶互不影响"""
    rl = RateLimiter(rate=1.0, burst=1)
    assert (await rl.check("a"))[0] is True
    assert (await rl.check("b"))[0] is True
    assert (await rl.check("a"))[0] is False


@pytest.mark.asyncio
async def test_rate_limiter_refills_after_time(monkeypatch: pytest.MonkeyPatch):
    """
    时间流逝后 token 按 rate 恢复

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    rl = RateLimiter(rate=10.0, burst=5)
    now = time.monotonic()
    current = {"value": now}

    def fake_monotonic() -> float:
        return current["value"]

    monkeypatch.setattr(
        "satrap.core.pipeline.rate_limiter.time.monotonic", fake_monotonic
    )

    assert (await rl.check("k"))[0] is True
    # Step.1 消耗 1 token (桶 5 -> 4)
    # Step.2 0.2s 后 refill 2 token -> 4 + 2 = 6 -> 上限 5, 放行 (5 -> 4)
    current["value"] = now + 0.2
    assert (await rl.check("k"))[0] is True
    # 连续消耗: 4 -> 3 -> 2 -> 1 -> 0, 均放行 (共 6 次)
    for _ in range(4):
        assert (await rl.check("k"))[0] is True
    # 桶已空且时间未变 -> 限流
    allowed, wait = await rl.check("k")
    assert allowed is False
    assert wait > 0.0


# ================= PipelineScheduler 测试 =================


@pytest.mark.asyncio
async def test_preprocessor_drop_event():
    """preprocessor 返回 False 丢弃事件"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))
    sched.add_preprocessor(lambda e: False)

    adapter = _RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert sm.calls == []


@pytest.mark.asyncio
async def test_preprocessor_async_and_full_success_path():
    """异步 preprocessor 通过后, 全链路: 回复通过 event.send 发出"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    async def ok(event: MessageEvent) -> bool:
        return True

    sched.add_preprocessor(ok)

    adapter = _RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert len(sm.calls) == 1
    assert sm.calls[0].session_id == "s1"
    assert sm.calls[0].message == "hello"
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0] == "s1"
    assert getattr(adapter.sent[0][1].components[0], "text", "") == "回复"


@pytest.mark.asyncio
async def test_rate_limited_sends_feedback():
    """限流时发送频率反馈 (error_feedback=True)"""
    rl = RateLimiter(rate=1.0, burst=0)
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm), rate_limiter=rl, error_feedback=True)

    adapter = _RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert sm.calls == []
    assert len(adapter.sent) == 1
    assert "频率过高" in getattr(adapter.sent[0][1].components[0], "text", "")


@pytest.mark.asyncio
async def test_group_message_without_wake_dropped():
    """群消息无唤醒词/艾特时丢弃"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    adapter = _RecorderAdapter()
    await sched.execute(
        _message_event(adapter, msg_type=PlatformMessageType.GROUP_MESSAGE)
    )
    assert sm.calls == []


@pytest.mark.asyncio
async def test_group_message_with_wake_passes():
    """群消息带唤醒标记时放行"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    adapter = _RecorderAdapter()
    event = _message_event(adapter, msg_type=PlatformMessageType.GROUP_MESSAGE)
    event.is_wake = True
    await sched.execute(event)
    assert len(sm.calls) == 1


@pytest.mark.asyncio
async def test_permission_denied_drops():
    """权限检查拒绝时丢弃事件"""
    sched = _DenyScheduler(_as_session_manager(_FakeSessionManager()))

    adapter = _RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_empty_message_dropped():
    """空消息丢弃"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    adapter = _RecorderAdapter()
    await sched.execute(_message_event(adapter, message_str=""))
    assert sm.calls == []


@pytest.mark.asyncio
async def test_llm_timeout_sends_feedback():
    """LLM 调用超时发送超时反馈"""
    sm = _FakeSessionManager(response="", delay=5)
    sched = PipelineScheduler(_as_session_manager(sm), llm_timeout=0.05, error_feedback=True)

    adapter = _RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert len(adapter.sent) == 1
    assert "超时" in getattr(adapter.sent[0][1].components[0], "text", "")


@pytest.mark.asyncio
async def test_execute_error_sends_feedback():
    """管线异常时发送兜底反馈"""
    sched = PipelineScheduler(_as_session_manager(_BoomSessionManager()), error_feedback=True)

    adapter = _RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert len(adapter.sent) == 1
    assert "处理失败" in getattr(adapter.sent[0][1].components[0], "text", "")


@pytest.mark.asyncio
async def test_temporary_files_cleaned(tmp_path: Path):
    """
    事件结束后跟踪的临时文件被清理

    参数:
    - tmp_path: tmp路径
    """
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    adapter = _RecorderAdapter()
    event = _message_event(adapter)
    f = tmp_path / "tmp.png"
    f.write_bytes(b"x")
    event.track_temporary_local_file(str(f))

    await sched.execute(event)
    assert not f.exists()


@pytest.mark.asyncio
async def test_execute_resolves_session_via_user_manager(monkeypatch: pytest.MonkeyPatch):
    """
    配置 UserManager 时通过 resolve_session 解析目标会话

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    class _FakeUserManager:
        def __init__(self):
            self.resolved: list[tuple[str, str, str, str]] = []

        def resolve_session(self, user_id: str, platform: str, session_type: str,
                            class_cfg_mgr: Any, extra_params: Any,
                            session_provider: str) -> str:
            self.resolved.append((user_id, platform, session_provider, session_type))
            return f"{platform}:{user_id}:sid"

    fake_um = _FakeUserManager()
    monkeypatch.setattr(sched, "user_manager", fake_um)

    adapter = _RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert fake_um.resolved == [("user-1", "rec1", "session_class", "dummy")]
    assert sm.calls[0].session_id == "rec1:user-1:sid"


def test_resolve_route_adapter_no_requested_uses_source():
    """入站路由应使用事件来源适配器并写入会话配置"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))
    sched.set_adapter_ids({"rec1"})

    adapter = _RecorderAdapter()
    platform_id, extra = sched._resolve_route_adapter(_message_event(adapter))
    assert platform_id == "rec1"
    assert extra == {"adapter_id": "rec1"}


def test_resolve_route_adapter_requested_missing_falls_back():
    """会话类中的旧 adapter_id 不应覆盖实际事件来源"""
    sm = _FakeSessionManager(class_cfg_mgr=_FakeClassCfgMgr("ghost"))
    sched = PipelineScheduler(_as_session_manager(sm))
    sched.set_adapter_ids({"rec1"})

    adapter = _RecorderAdapter()
    platform_id, extra = sched._resolve_route_adapter(_message_event(adapter))
    assert platform_id == "rec1"
    assert extra == {"adapter_id": "rec1"}


def test_resolve_route_adapter_requested_exists():
    """存在同名 adapter_id 参数时仍应绑定事件来源适配器"""
    sm = _FakeSessionManager(class_cfg_mgr=_FakeClassCfgMgr("rec1"))
    sched = PipelineScheduler(_as_session_manager(sm))
    sched.set_adapter_ids({"rec1", "rec2"})

    adapter = _RecorderAdapter()
    platform_id, extra = sched._resolve_route_adapter(_message_event(adapter))
    assert platform_id == "rec1"
    assert extra == {"adapter_id": "rec1"}


def test_extract_img_urls_from_components():
    """从消息组件中提取图片 URL / 本地路径"""
    from satrap.core.components import BaseMessageComponent, Image, Plain

    message = PlatformMessage()
    components: list[BaseMessageComponent] = [
        Plain(text="hi"),
        Image(file="http://x/1.png"),
        Image(file="local.png"),
    ]
    message.message = components
    adapter = _RecorderAdapter()
    event = _message_event(adapter)
    event.platform_message = message

    urls = PipelineScheduler._extract_img_urls(event)
    assert urls == ["http://x/1.png", "local.png"]
