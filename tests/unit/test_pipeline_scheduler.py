"""
PipelineScheduler / RateLimiter 单元测试

覆盖:
- RateLimiter: token bucket 放行 / 限流 / 独立 key / 时间 refill
- PipelineScheduler: preprocessor 拦截, 限流反馈, 唤醒检查, 权限检查,
  空消息丢弃, UserManager 路由, 成功回复, 超时反馈, 异常兜底, 临时文件清理
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import pytest
from types import SimpleNamespace
from typing import Any, NamedTuple, cast
import time

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import BindingState, BindingStatus, SessionProviderRegistry
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform import PlatformConfig
from satrap.core.components import Plain
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType
from support.adapters import RecorderAdapter
from tests.support.flow_runtime import static_model_resolver


def _as_session_manager(fake: Any) -> SessionManager:
    """SessionManager 替身类型边界: 替身实现 handle_call_async/class_cfg_mgr 调用面, cast 集中在此工厂"""
    return cast(SessionManager, fake)


class _FakeProviderRegistry:
    """会话定义注册表替身: 按登记表答复三态并记录判定调用"""

    def __init__(self, definitions: dict[str, bool] | None = None) -> None:
        self.definitions = {"dummy": True} if definitions is None else definitions
        self.calls: list[tuple[str, str | None]] = []

    def binding_status(self, definition_name: str, provider_name: str | None = None) -> BindingStatus:
        """记录判定调用并按登记表答复三态"""
        self.calls.append((definition_name, provider_name))
        location = f"provider={provider_name or ''}, name={definition_name}"
        if definition_name not in self.definitions:
            return BindingStatus(BindingState.INVALID, f"会话定义不可用 {location}")
        if not self.definitions[definition_name]:
            return BindingStatus(BindingState.DISABLED, f"会话定义已禁用: {location}")
        return BindingStatus(BindingState.RUNNABLE)


def _constant_runnable(*_args: object) -> BindingStatus:
    """恒定答复可运行的绑定判定"""
    return BindingStatus(BindingState.RUNNABLE)


def _runnable_async_session_manager() -> Any:
    """构造只记录 handle_call_async 的会话管理器替身"""
    from unittest.mock import AsyncMock

    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    manager.provider_registry = SimpleNamespace(binding_status=_constant_runnable)
    return manager




class _FakeSessionManager:
    """记录 handle_call_async 调用的假 SessionManager"""

    def __init__(self, response: str = "回复", delay: float = 0.0, class_cfg_mgr: Any = None):
        self.response = response
        self.delay = delay
        self.calls: list[Any] = []
        self.class_cfg_mgr = class_cfg_mgr
        # 既有用例只读 binding_status, 共享状态源用例会整体替换为真实注册表
        self.provider_registry: Any = _FakeProviderRegistry()

    async def handle_call_async(self, user_call: Any) -> str:
        self.calls.append(user_call)
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.response


class _BoomSessionManager:
    """handle_call_async 抛异常的假 SessionManager"""

    class_cfg_mgr = None
    provider_registry = _FakeProviderRegistry()

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
    adapter: RecorderAdapter,
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
    """时间流逝后 token 按 rate 恢复"""
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

    adapter = RecorderAdapter()
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

    adapter = RecorderAdapter()
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

    adapter = RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert sm.calls == []
    assert len(adapter.sent) == 1
    assert "频率过高" in getattr(adapter.sent[0][1].components[0], "text", "")


@pytest.mark.asyncio
async def test_group_message_without_wake_dropped():
    """群消息无唤醒词/艾特时丢弃"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    adapter = RecorderAdapter()
    await sched.execute(
        _message_event(adapter, msg_type=PlatformMessageType.GROUP_MESSAGE)
    )
    assert sm.calls == []


@pytest.mark.asyncio
async def test_group_message_with_wake_passes():
    """群消息带唤醒标记时放行"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    adapter = RecorderAdapter()
    event = _message_event(adapter, msg_type=PlatformMessageType.GROUP_MESSAGE)
    event.is_wake = True
    await sched.execute(event)
    assert len(sm.calls) == 1


@pytest.mark.asyncio
async def test_permission_denied_drops():
    """权限检查拒绝时丢弃事件"""
    sched = _DenyScheduler(_as_session_manager(_FakeSessionManager()))

    adapter = RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_empty_message_dropped():
    """空消息丢弃"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    adapter = RecorderAdapter()
    await sched.execute(_message_event(adapter, message_str=""))
    assert sm.calls == []


@pytest.mark.asyncio
async def test_session_timeout_sends_feedback():
    """整轮会话执行超时发送明确反馈"""
    sm = _FakeSessionManager(response="", delay=5)
    sched = PipelineScheduler(_as_session_manager(sm), session_timeout=0.05, error_feedback=True)

    adapter = RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert len(adapter.sent) == 1
    assert "超时" in getattr(adapter.sent[0][1].components[0], "text", "")


@pytest.mark.asyncio
async def test_unlimited_session_completes_and_still_accepts_cancellation():
    """不限时允许慢会话完成, 同时保留宿主取消能力"""
    sm = _FakeSessionManager(response="完成", delay=0.03)
    scheduler = PipelineScheduler(_as_session_manager(sm), session_timeout=None)
    adapter = RecorderAdapter()
    await scheduler.execute(_message_event(adapter))
    assert sm.calls and len(adapter.sent) == 1
    sm.delay = 100
    task = asyncio.create_task(scheduler.execute(_message_event(adapter)))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("limit", [None, 0.05])
@pytest.mark.asyncio
async def test_inner_timeout_is_not_reported_as_session_deadline(limit):
    """网络或工具主动报超时时不冒充外层会话限时到期"""
    from unittest.mock import AsyncMock

    sm = _FakeSessionManager()
    sm.handle_call_async = AsyncMock(side_effect=TimeoutError("upstream timeout"))
    scheduler = PipelineScheduler(_as_session_manager(sm), session_timeout=limit)
    adapter = RecorderAdapter()
    await scheduler.execute(_message_event(adapter))
    assert len(adapter.sent) == 1
    assert "operation_timeout" in getattr(adapter.sent[0][1].components[0], "text", "")


@pytest.mark.asyncio
async def test_execute_error_sends_feedback():
    """管线异常时发送兜底反馈"""
    sched = PipelineScheduler(_as_session_manager(_BoomSessionManager()), error_feedback=True)

    adapter = RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert len(adapter.sent) == 1
    assert "internal_error" in getattr(adapter.sent[0][1].components[0], "text", "")


@pytest.mark.asyncio
async def test_temporary_files_cleaned(tmp_path: Path):
    """事件结束后跟踪的临时文件被清理"""
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    adapter = RecorderAdapter()
    event = _message_event(adapter)
    f = tmp_path / "tmp.png"
    f.write_bytes(b"x")
    event.track_temporary_local_file(str(f))

    await sched.execute(event)
    assert not f.exists()


@pytest.mark.asyncio
async def test_execute_resolves_session_via_user_manager(monkeypatch: pytest.MonkeyPatch):
    """配置 UserManager 时通过 resolve_session 解析目标会话"""
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

    adapter = RecorderAdapter()
    await sched.execute(_message_event(adapter))
    assert fake_um.resolved == [("user-1", "rec1", "session_class", "dummy")]
    assert sm.calls[0].session_id == "rec1:user-1:sid"


@pytest.mark.asyncio
async def test_unresolved_session_is_logged_and_dropped(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    """resolve_session 返回空时消息丢弃且留下 warning"""
    import logging
    sm = _FakeSessionManager()
    sched = PipelineScheduler(_as_session_manager(sm))

    class _NoneUserManager:
        def resolve_session(self, *args: Any, **kwargs: Any) -> str:
            return ""

    monkeypatch.setattr(sched, "user_manager", _NoneUserManager())
    with caplog.at_level(logging.WARNING):
        await sched.execute(_message_event(RecorderAdapter()))
    assert not sm.calls
    assert any("未解析到会话, 消息丢弃" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_manual_request_state_outcome_is_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    """R1/E3 反例: 只有明确幂等 no-op 不告警, 缺失记录, 终态冲突, 落盘失败与证据查询失败都必须可见"""
    import logging
    from satrap.core.pipeline.manual_wake import ManualWakeTicket
    from satrap.core.pipeline.manual_wake_store import ManualWakeStore

    sched = PipelineScheduler(_as_session_manager(_FakeSessionManager()))
    store = ManualWakeStore(tmp_path / "wake.json")
    sched.manual_wake_store = store
    event = _message_event(RecorderAdapter())
    ticket = ManualWakeTicket("missing")

    # 已终结记录收到同一目标的重复写入属于明确幂等: 只留 debug
    store.accept_request("rec1", "missing", "fp", "group:20", "op")
    assert store.update_request("rec1", "missing", "sent", "ok") == "persisted"
    with caplog.at_level(logging.DEBUG):
        await sched._update_manual_request(event, ticket, "sent", "ok")
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert any("outcome=no_op" in r.getMessage() for r in caplog.records)

    caplog.clear()

    # 记录不存在与终态冲突都是状态推进未生效, 不能按正常未推进静默
    with caplog.at_level(logging.WARNING):
        await sched._update_manual_request(event, ManualWakeTicket("vanished"), "sent", "ok")
    assert any(
        "手动请求状态未落盘" in r.getMessage() and "adapter=rec1" in r.getMessage()
        and "request_id=vanished" in r.getMessage() and "outcome=missing" in r.getMessage()
        for r in caplog.records
    )

    caplog.clear()

    with caplog.at_level(logging.WARNING):
        await sched._update_manual_request(event, ticket, "failed", "late")
    assert any(
        "手动请求状态未落盘" in r.getMessage() and "request_id=missing" in r.getMessage()
        and "outcome=conflict" in r.getMessage()
        for r in caplog.records
    )

    caplog.clear()

    def _failed_write(*args: Any, **kwargs: Any) -> str:
        return "io"

    monkeypatch.setattr(store, "update_request", _failed_write)
    with caplog.at_level(logging.WARNING):
        await sched._update_manual_request(event, ticket, "sent", "ok")
    assert any(
        "手动请求状态未落盘" in r.getMessage() and "adapter=rec1" in r.getMessage()
        and "request_id=missing" in r.getMessage() and "status=sent" in r.getMessage()
        and "outcome=io" in r.getMessage()
        for r in caplog.records
    )

    caplog.clear()

    def _lock_timeout(*args: Any, **kwargs: Any) -> str:
        raise TimeoutError("锁超时")

    monkeypatch.setattr(store, "update_request", _lock_timeout)
    with caplog.at_level(logging.WARNING):
        await sched._update_manual_request(event, ticket, "sent", "ok")
    assert any("手动请求状态回写异常" in r.getMessage() and "TimeoutError" in r.getMessage() for r in caplog.records)

    caplog.clear()

    def _query_failure(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise OSError("锁超时")

    monkeypatch.setattr(store, "request_send_outcome", _query_failure)
    with caplog.at_level(logging.WARNING):
        assert await sched._await_send_settlement(store, "r1", "rec1") is None
    # 查询失败会让请求终态退化为仅按业务回执裁决, 不能只留 debug
    assert any(
        "发送证据查询失败" in r.getMessage() and "request_id=r1" in r.getMessage() and "adapter=rec1" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("configured", [None, "ghost", "rec1"])
def test_resolve_route_adapter_binds_event_source(configured: str | None):
    """入站路由以事件来源适配器为准, 类级 adapter_id 存在与否都不改变结果"""
    sm = _FakeSessionManager() if configured is None else _FakeSessionManager(class_cfg_mgr=_FakeClassCfgMgr(configured))
    sched = PipelineScheduler(_as_session_manager(sm))

    platform_id, extra = sched._resolve_route_adapter(_message_event(RecorderAdapter()))
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
    adapter = RecorderAdapter()
    event = _message_event(adapter)
    event.platform_message = message

    from satrap.core.pipeline.input_projection import media_sources
    urls = media_sources(event.get_messages(), "image")
    assert urls == ["http://x/1.png", "local.png"]


@pytest.mark.asyncio
async def test_platform_media_without_text_reaches_session(monkeypatch):
    from satrap.core.platform.onebot.onebot_utils import onebot_segments_to_components

    adapter = RecorderAdapter()
    manager = _FakeSessionManager()
    event = _message_event(adapter, message_str="")
    components, _ = onebot_segments_to_components([
        {"type": "image", "data": {"url": "https://example.com/image.png"}},
        {"type": "video", "data": {"url": "https://example.com/video.mp4"}},
    ])
    monkeypatch.setattr(event, "get_messages", lambda: components)
    await PipelineScheduler(_as_session_manager(manager)).execute(event)
    assert len(manager.calls) == 1
    assert manager.calls[0].img_urls == ["https://example.com/image.png"]
    assert manager.calls[0].video_urls == ["https://example.com/video.mp4"]


def test_session_video_signature_adaptation():
    from satrap.core.type import UserCall

    call = UserCall(message="内容", img_urls=["image.png"], video_urls=["video.mp4"])

    def keywords(message, *, video_urls=None):
        return message, video_urls

    def legacy(message, img_urls=None, *, video_urls=None):
        return message, img_urls, video_urls

    def structured(user_call, **kwargs):
        return user_call, kwargs

    def invoke(method):
        return method(*SessionManager._build_run_args(method, call), **SessionManager._build_media_kwargs(method, call))

    assert invoke(keywords) == ("内容", ["video.mp4"])
    assert invoke(legacy) == ("内容", ["image.png"], ["video.mp4"])
    assert invoke(structured) == (call, {})
    with pytest.raises(ValueError, match="未提供视频"):
        invoke(lambda message: message)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [True, False])
async def test_explicit_stop_prevents_model_and_rate_feedback(stop):
    manager = _FakeSessionManager()
    adapter = RecorderAdapter()
    event = _message_event(adapter)
    scheduler = PipelineScheduler(_as_session_manager(manager), RateLimiter(rate=1, burst=0))
    if stop:
        scheduler.add_preprocessor(lambda current: current.stop_event() or True)
    else:
        scheduler.add_preprocessor(lambda current: current.should_call_llm(False) or True)
    await scheduler.execute(event)
    assert manager.calls == []
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_final_session_turn_orders_model_and_reply_and_reclaims_locks():
    manager = _FakeSessionManager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    sending = asyncio.Event()
    release = asyncio.Event()

    class SlowAdapter(RecorderAdapter):
        async def send_message(
        self, session_id: str, message: MessageChain, *, request_id: str = "",
        purpose: str = "business", require_tracking: bool = False,
    ) -> Any:
            result = await super().send_message(
                session_id, message, request_id=request_id, purpose=purpose,
                require_tracking=require_tracking,
            )
            if len(self.sent) == 1:
                sending.set()
                await release.wait()
            return result

    adapter = SlowAdapter()
    first = asyncio.create_task(scheduler.execute(_message_event(adapter, message_str="first")))
    await asyncio.wait_for(sending.wait(), 1)
    later = asyncio.create_task(scheduler.execute(_message_event(adapter, message_str="second")))
    try:
        await asyncio.sleep(0)
        assert len(manager.calls) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(first, later), 1)
        assert [call.message for call in manager.calls] == ["first", "second"]
        assert len(adapter.sent) == 2
        assert scheduler._session_turns == {}
    finally:
        first.cancel()
        later.cancel()
        await asyncio.gather(first, later, return_exceptions=True)


@pytest.mark.asyncio
async def test_session_turn_reuses_lock_while_active_and_serializes():
    """并发轮次共享同一把锁串行执行, 结束后锁表清空"""
    manager = _as_session_manager(object())
    scheduler = PipelineScheduler(manager)
    order: list[str] = []

    async def turn(name: str) -> None:
        async with scheduler._session_turn(manager, "s1"):
            order.append(f"enter-{name}")
            await asyncio.sleep(0.01)
            order.append(f"exit-{name}")

    await asyncio.gather(turn("a"), turn("b"))
    assert order == ["enter-a", "exit-a", "enter-b", "exit-b"]
    assert scheduler._session_turns == {}


@pytest.mark.asyncio
async def test_unclaimed_automatic_batch_skips_resolution(monkeypatch: pytest.MonkeyPatch):
    """自动唤醒竞争认领失败时直接退出, 不做引用回源或附件下载"""
    from unittest.mock import AsyncMock
    from satrap.core.platform.onebot.adapter import OneBotAdapter

    manager = _runnable_async_session_manager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={
        "self_id": "10", "wake_mode": "frequency", "wake_message_threshold": 1, "wake_cooldown": 0}))
    adapter.started = True
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 1,
        "message_type": "group", "message": [{"type": "reply", "data": {"id": "5"}},
                                             {"type": "text", "data": {"text": "看看"}}]})
    event = adapter._event_queue.get_nowait()

    def no_batch(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        return ()

    monkeypatch.setattr(scheduler.wake_window, "claim", no_batch)
    await scheduler.execute(event)
    adapter._bot.get_msg.assert_not_called()
    manager.handle_call_async.assert_not_awaited()


# ================= A2 窗口批次与投影合并测试 =================


@pytest.mark.asyncio
async def test_real_message_merges_window_without_losing_projection():
    """frequency 模式下显式 @ + 引用: UserCall 同时保留引用块与先前窗口块, 当前消息不重复"""
    from unittest.mock import AsyncMock
    from satrap.core.platform.onebot.adapter import OneBotAdapter

    manager = _runnable_async_session_manager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={
        "self_id": "10", "wake_mode": "frequency", "wake_message_threshold": 5, "wake_cooldown": 0}))
    adapter.started = True
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 1,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "先聊着"}}]})
    first = adapter._event_queue.get_nowait()
    await scheduler.execute(first)
    manager.handle_call_async.assert_not_awaited()

    adapter._bot.get_msg.return_value = {"message_id": 5, "message_type": "group", "group_id": 20, "user_id": 31,
        "message": [{"type": "text", "data": {"text": "QUOTED_SECRET_CONTEXT"}}], "sender": {"user_id": 31, "nickname": "甲"}}
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 2,
        "message_type": "group", "message": [{"type": "reply", "data": {"id": "5"}},
                                             {"type": "at", "data": {"qq": "10"}},
                                             {"type": "text", "data": {"text": "please summarize"}}]})
    second = adapter._event_queue.get_nowait()
    await scheduler.execute(second)
    manager.handle_call_async.assert_awaited_once()
    user_call = manager.handle_call_async.await_args.args[0]
    assert "QUOTED_SECRET_CONTEXT" in user_call.message
    assert "[先前窗口消息" in user_call.message and "先聊着" in user_call.message
    assert user_call.message.count("please summarize") == 1


@pytest.mark.asyncio
async def test_manual_window_wake_uses_claimed_batch_only(monkeypatch: pytest.MonkeyPatch):
    """无 prompt 待处理手动唤醒: 输入仅为实际认领批次, 不叠加合成事件正文"""
    from unittest.mock import AsyncMock
    from satrap.core.pipeline.manual_wake import ManualWakeTicket
    from satrap.core.pipeline.wake_window import PendingMessage
    from satrap.core.platform.onebot.adapter import OneBotAdapter

    manager = _runnable_async_session_manager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"self_id": "10"}))
    adapter.started = True
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 9,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "窗口甲\n窗口乙"}}]})
    event = adapter._event_queue.get_nowait()
    snapshot = (PendingMessage("r1", "30", "1", "窗口甲", 0.0), PendingMessage("r2", "30", "2", "窗口乙", 0.0))
    scheduler.manual_wakes.tickets[event] = ManualWakeTicket("req-1", snapshot)

    def claimed(*args: Any, **kwargs: Any) -> tuple[PendingMessage, ...]:
        return snapshot

    monkeypatch.setattr(scheduler.wake_window, "claim", claimed)
    await scheduler.execute(event)
    manager.handle_call_async.assert_awaited_once()
    user_call = manager.handle_call_async.await_args.args[0]
    assert user_call.message == "[用户 30, 消息 1] 窗口甲\n[用户 30, 消息 2] 窗口乙"


@pytest.mark.asyncio
async def test_deadline_wake_uses_claimed_batch_only(monkeypatch: pytest.MonkeyPatch):
    """定时补偿唤醒: 以到期实际认领为准, 定时器保存的陈旧正文副本不进入输入"""
    from unittest.mock import AsyncMock
    from satrap.core.pipeline.wake_timers import DeadlineTicket
    from satrap.core.pipeline.wake_window import PendingMessage
    from satrap.core.platform.onebot.adapter import OneBotAdapter

    manager = _runnable_async_session_manager()
    scheduler = PipelineScheduler(_as_session_manager(manager))
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={
        "self_id": "10", "wake_mode": "frequency", "wake_message_threshold": 1, "wake_cooldown": 0}))
    adapter.started = True
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 9,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "旧副本"}}]})
    event = adapter._event_queue.get_nowait()
    snapshot = (PendingMessage("r1", "30", "1", "实际认领", 0.0),)
    scheduler.wake_timers.tickets[event] = DeadlineTicket(snapshot=snapshot)

    def claimed(*args: Any, **kwargs: Any) -> tuple[PendingMessage, ...]:
        return snapshot

    monkeypatch.setattr(scheduler.wake_window, "claim", claimed)
    await scheduler.execute(event)
    manager.handle_call_async.assert_awaited_once()
    user_call = manager.handle_call_async.await_args.args[0]
    assert user_call.message == "[用户 30, 消息 1] 实际认领"
    assert "旧副本" not in user_call.message


# ================= flow 路由与定义变更测试 =================


class _FlowSchedulerManager(_FakeSessionManager):
    """使用真实 UserManager 路由、可变定义和轻量流程会话的调度测试替身。"""

    def __init__(self) -> None:
        from unittest.mock import AsyncMock
        from satrap.edictum.flow.session import AsyncFlowSession

        super().__init__()
        self.kind = "flow"
        self.enabled = True
        self.created_calls: list[Any] = []
        self.entries: dict[str, Any] = {}
        self.observed: list[tuple[str, MessageEvent]] = []
        self.provider_registry = SimpleNamespace(binding_status=self.binding_status, get=lambda _: self)
        self.pool = SimpleNamespace(list_entries=lambda: self.entries, release=lambda entry: None)
        self.flow_class = AsyncFlowSession
        self.async_mock = AsyncMock

    def binding_status(self, *_args: Any) -> BindingStatus:
        return BindingStatus(BindingState.RUNNABLE if self.enabled else BindingState.DISABLED)

    def get_definition(self, _name: str) -> Any:
        return SimpleNamespace(metadata={"edictum_type": self.kind})

    def register_session_from_provider_context(self, **kwargs: Any) -> Any:
        return SimpleNamespace(session_id=f"mapped:{kwargs['session_identity'] or kwargs['context_value']}")

    def _resolve_or_create_session_config(self, call: Any) -> Any:
        self.created_calls.append(call)
        return call

    async def _acquire_or_create_entry_async(self, call: Any) -> Any:
        session = self.flow_class(call.session_id, llm=None)

        async def observe(event: MessageEvent) -> None:
            self.observed.append((call.session_id, event))

        session.observe = self.async_mock(side_effect=observe)
        entry = SimpleNamespace(session=session)
        self.entries[call.session_id] = entry
        return entry

    async def drain_observations(self) -> None:
        tasks = [entry.session._scheduler_observer_task for entry in self.entries.values()
                 if getattr(entry.session, "_scheduler_observer_task", None) is not None]
        if tasks:
            await asyncio.gather(*tasks)


class _RealFlowSchedulerManager(_FlowSchedulerManager):
    """路由管理保持轻量, 会话本身使用未替换 observe 的真实 AsyncFlowSession。"""

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self.session_factory = session_factory

    async def _acquire_or_create_entry_async(self, call: Any) -> Any:
        session = self.session_factory(call.session_id)
        entry = SimpleNamespace(session=session)
        self.entries[call.session_id] = entry
        return entry


def _manual_flow(*, use_platform_policy: bool = False, on_busy: str = "queue") -> dict[str, Any]:
    """真实手动唤醒测试图: source 输入、wake 触发和 final_text 业务回复"""
    return {
        "version": 3,
        "nodes": [
            {"id": "src", "type": "source", "title": "消息", "config": {}},
            {"id": "wake", "type": "wake", "title": "唤醒", "config": {
                "use_platform_policy": use_platform_policy, "mention": True,
                "new_messages": 99, "cooldown": 3600,
            }},
            {"id": "w1", "type": "react_workflow", "title": "WF", "config": {
                "model_name": "test", "on_busy": on_busy, "max_steps": 2, "timeout_seconds": 10,
            }},
            {"id": "reply", "type": "output", "title": "回复", "config": {"send_mode": "final_text"}},
        ],
        "edges": [
            {"from": "src.out", "to": "wake.messages"},
            {"from": "src.out", "to": "w1.input"},
            {"from": "wake.event", "to": "w1.trigger"},
            {"from": "w1.text", "to": "reply.reply"},
        ],
    }


def _manual_event(
    adapter: Any, scope: Any, text: str, index: int, request_id: str,
    *, snapshot: tuple[Any, ...] = (), source_message: bool = True,
) -> tuple[MessageEvent, Any]:
    """构造 BackendManager.wake_platform 同形的 management 事件和票据"""
    from satrap.core.pipeline.manual_wake import ManualWakeTicket
    from tests.support.flow_runtime import make_event

    event = make_event(adapter, scope, text, index)
    if not source_message:
        event.platform_message.message_id = ""
    event._call_origin = replace(
        event.call_origin, actor_id="panel-operator", actor_kind="management",
        source_message_id=event.platform_message.message_id, request_id=request_id,
        route_user_id=event.get_sender_id(),
    )
    return event, ManualWakeTicket(request_id, snapshot)


def _accept_manual(
    scheduler: PipelineScheduler, store: Any, event: MessageEvent, ticket: Any,
) -> None:
    """复用生产登记入口建立进程内票据和持久化 accepted 占位"""
    scheduler.manual_wakes.register(event, "fingerprint", ticket)
    store.accept_request(event.call_origin.adapter_id, ticket.request_id, "fingerprint", "group:chat1", "panel-operator")


def _model_input(call: dict[str, Any]) -> str:
    """取 MockLLM 收到的最后一条用户输入"""
    return next(item["content"] for item in reversed(call["messages"]) if item["role"] == "user")


async def _drain_manual_settlements(scheduler: PipelineScheduler, manager: Any) -> None:
    """等待串行观察与已派生的手动结算任务全部结束"""
    await manager.drain_observations()
    pending = [task for task in scheduler._flow_manual_tasks if not task.done()]
    if pending:
        await asyncio.gather(*pending)


def _manual_real_runtime(
    tmp_path: Path, llm: Any, flow: dict[str, Any], **session_kwargs: Any,
) -> tuple[Any, ...]:
    """建立真实 scheduler/flow/session 工厂, 仅替换模型与最底层适配器发送"""
    from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
    from satrap.core.pipeline.manual_wake_store import ManualWakeStore
    from satrap.edictum.flow.session import AsyncFlowSession
    from tests.support.flow_runtime import FlowAdapter, database

    scope = MessageScope("adapter1", "self1", "group", "chat1")
    store = PlatformMessageStore(database(tmp_path / "flow.db"), scope.adapter_id)
    adapter = FlowAdapter(store)

    def factory(session_id: str) -> AsyncFlowSession:
        return AsyncFlowSession(session_id, model_resolver=static_model_resolver(llm), flow=flow, db_path=str(store.database),
                                message_store=store, flow_name="manual-real", **session_kwargs)

    manager = _RealFlowSchedulerManager(factory)
    scheduler = PipelineScheduler(_as_session_manager(manager))
    requests = ManualWakeStore(tmp_path / "wake.json")
    scheduler.manual_wake_store = requests
    return scope, store, adapter, scheduler, manager, requests


@pytest.mark.asyncio
async def test_flow_scheduler_real_session_observes_serially(tmp_path: Path):
    """公开 execute 入口投递到真实会话, 两个唤醒事件按顺序运行真实 WF。"""
    from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
    from satrap.edictum.flow.session import AsyncFlowSession
    from tests.support.flow_runtime import (
        FlowAdapter, MockLLM, archive_event, database, make_event, response, wait_until,
    )

    scope = MessageScope("adapter1", "self1", "group", "chat1")
    store = PlatformMessageStore(database(tmp_path / "flow.db"), scope.adapter_id)
    adapter = FlowAdapter(store)
    started, release = asyncio.Event(), asyncio.Event()
    active, max_active = 0, 0

    async def script(messages: list[Any], tools: Any, count: int):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            if count == 1:
                started.set()
                await release.wait()
            return response(f"回复 {count}")
        finally:
            active -= 1

    llm = MockLLM(script)
    flow = {
        "version": 3,
        "nodes": [
            {"id": "src", "type": "source", "title": "消息", "config": {}},
            {"id": "bucket", "type": "bucket", "title": "桶", "config": {}},
            {"id": "wake", "type": "wake", "title": "唤醒",
             "config": {"use_platform_policy": False, "mention": True, "new_messages": 0, "cooldown": 0}},
            {"id": "w1", "type": "react_workflow", "title": "WF",
             "config": {"model_name": "test", "on_busy": "queue", "max_steps": 2}},
            {"id": "reply", "type": "output", "title": "回复", "config": {"send_mode": "final_text"}},
        ],
        "edges": [
            {"from": "src.out", "to": "bucket.in"}, {"from": "bucket.out", "to": "wake.messages"},
            {"from": "wake.event", "to": "w1.trigger"}, {"from": "w1.text", "to": "reply.reply"},
        ],
    }

    def factory(session_id: str) -> AsyncFlowSession:
        return AsyncFlowSession(session_id, model_resolver=static_model_resolver(llm), flow=flow, db_path=str(store.database),
                                message_store=store, flow_name="scheduler-real")

    manager = _RealFlowSchedulerManager(factory)
    scheduler = PipelineScheduler(_as_session_manager(manager))
    first = make_event(adapter, scope, "第一", 1, mention=True)
    second = make_event(adapter, scope, "第二", 2, mention=True)
    cleaned = []
    for event in (first, second):
        event.cleanup_temporary_local_files = lambda event=event: cleaned.append(event)
    session = None
    try:
        archive_event(store, scope, first)
        await scheduler.execute(first)
        await asyncio.wait_for(started.wait(), 2)
        session = next(iter(manager.entries.values())).session
        archive_event(store, scope, second)
        await scheduler.execute(second)
        await manager.drain_observations()
        assert len(llm.calls) == 1
        release.set()
        await wait_until(lambda: len(llm.calls) == 2)
        await wait_until(lambda: len(adapter.scheduled) == 2)
        await wait_until(lambda: not session._engine.running_workflows())
        assert max_active == 1
        assert [item["text"] for item in adapter.scheduled] == ["回复 1", "回复 2"]
        assert cleaned == [first, second]
    finally:
        release.set()
        if session is not None:
            await session.close_flow_nodes()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_platform_policy", [True, False])
@pytest.mark.parametrize("form", ["prompt", "message_id", "snapshot"])
async def test_flow_manual_wake_real_scheduler_inputs_and_reply(
    tmp_path: Path, form: str, use_platform_policy: bool,
):
    """三种控制面板输入均绕过两种唤醒策略, 由真实 WF 输入并发送可追踪回复"""
    from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
    from satrap.core.pipeline.manual_wake_store import ManualWakeStore
    from satrap.edictum.flow.session import AsyncFlowSession
    from tests.support.flow_runtime import FlowAdapter, MockLLM, archive_event, database, make_event, response

    scope = MessageScope("adapter1", "self1", "group", "chat1")
    store = PlatformMessageStore(database(tmp_path / "flow.db"), scope.adapter_id)
    adapter = FlowAdapter(store)
    llm = MockLLM(response("手动回复"))
    flow = _manual_flow(use_platform_policy=use_platform_policy)

    def factory(session_id: str) -> AsyncFlowSession:
        return AsyncFlowSession(session_id, model_resolver=static_model_resolver(llm), flow=flow, db_path=str(store.database),
                                message_store=store, flow_name="manual-real")

    manager = _RealFlowSchedulerManager(factory)
    scheduler = PipelineScheduler(_as_session_manager(manager))
    requests = ManualWakeStore(tmp_path / "wake.json")
    scheduler.manual_wake_store = requests
    session = None
    try:
        if form == "snapshot":
            original = make_event(adapter, scope, "档案正文", 1)
            archive_event(store, scope, original)
            original.message_str = "陈旧快照"
            original.platform_message.message_str = "陈旧快照"
            original.platform_message.message = [Plain("陈旧快照")]
            scheduler.wake_window.observe(original)
            event, ticket = _manual_event(
                adapter, scope, "", 2, f"manual-{form}-{use_platform_policy}",
                source_message=False,
            )
            ticket.snapshot = scheduler.wake_window.peek(event)
        elif form == "message_id":
            event, ticket = _manual_event(
                adapter, scope, "回源消息", 1, f"manual-{form}-{use_platform_policy}")
            archive_event(store, scope, event)
        else:
            event, ticket = _manual_event(
                adapter, scope, "面板原始 prompt", 1, f"manual-{form}-{use_platform_policy}",
                source_message=False,
            )
        _accept_manual(scheduler, requests, event, ticket)
        await scheduler.execute(event)
        assert event.is_wake and event.get_extra("require_send_tracking") is True
        await _drain_manual_settlements(scheduler, manager)

        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "sent"
        assert len(llm.calls) == 1
        model_input = _model_input(llm.calls[0])
        if form == "prompt":
            assert model_input == "面板原始 prompt"
            assert "[用户" not in model_input and "[先前窗口消息" not in model_input
            assert store.flow_window_by_scope_key(scope.key, latest_n=10) == []
        elif form == "message_id":
            assert model_input == "[用户 u1, 消息 m1] 回源消息"
            assert model_input.count("回源消息") == 1
        else:
            assert model_input == "[用户 u1, 消息 m1] 档案正文"
            assert "陈旧快照" not in model_input and model_input.count("档案正文") == 1
        assert [item["text"] for item in adapter.sent] == ["手动回复"]
        assert adapter.scheduled == []
        session = next(iter(manager.entries.values())).session
    finally:
        if session is not None:
            await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_flow_manual_request_waits_for_real_workflow_completion(tmp_path: Path):
    """WF 阻塞期间请求保持 executing, 真实回复完成后才进入 sent"""
    from tests.support.flow_runtime import MockLLM, response, wait_until

    started, release = asyncio.Event(), asyncio.Event()

    async def script(messages: list[Any], tools: Any, count: int):
        started.set()
        await release.wait()
        return response("延迟回复")

    scope, store, adapter, scheduler, manager, requests = _manual_real_runtime(
        tmp_path, MockLLM(script), _manual_flow())
    event, ticket = _manual_event(adapter, scope, "等待执行", 1, "manual-delayed", source_message=False)
    _accept_manual(scheduler, requests, event, ticket)
    try:
        await scheduler.execute(event)
        await asyncio.wait_for(started.wait(), 2)
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "executing"
        assert ticket.status == "pending" and adapter.sent == []
        release.set()
        await _drain_manual_settlements(scheduler, manager)
        await wait_until(lambda: len(adapter.sent) == 1)
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "sent"
        assert adapter.sent[0]["text"] == "延迟回复"
    finally:
        release.set()
        for entry in manager.entries.values():
            await entry.session.close_flow_nodes()


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_fails", [False, True])
async def test_flow_manual_request_silence_and_failure_have_terminal_state(
    tmp_path: Path, workflow_fails: bool,
):
    """沉默与 WF 异常都在真实运行结束后 failed, 不再提前报 no_response"""
    from tests.support.flow_runtime import MockLLM, response

    async def script(messages: list[Any], tools: Any, count: int):
        if workflow_fails:
            raise RuntimeError("模型失败")
        return response("")

    llm = MockLLM(script)
    scope, store, adapter, scheduler, manager, requests = _manual_real_runtime(
        tmp_path, llm, _manual_flow())
    event, ticket = _manual_event(adapter, scope, "无回复", 1, f"manual-silent-{workflow_fails}", source_message=False)
    _accept_manual(scheduler, requests, event, ticket)
    try:
        await scheduler.execute(event)
        await _drain_manual_settlements(scheduler, manager)
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "failed"
        assert record["detail"] == ("flow_workflow_failed" if workflow_fails else "no_confirmed_send")
        assert len(llm.calls) == 1
        assert not any(item["purpose"] == "business" for item in adapter.sent)
    finally:
        for entry in manager.entries.values():
            await entry.session.close_flow_nodes()


@pytest.mark.asyncio
async def test_flow_manual_request_wake_when_false_is_failed(tmp_path: Path):
    """management 绕过唤醒策略但不绕过 wake.when, 条件拦截原因可读"""
    from tests.support.flow_runtime import MockLLM, response

    flow = _manual_flow()
    flow["nodes"].append({"id": "never", "type": "cond_message", "title": "仅提及", "config": {"kind": "mention"}})
    flow["edges"].append({"from": "never.out", "to": "wake.when"})
    scope, store, adapter, scheduler, manager, requests = _manual_real_runtime(
        tmp_path, MockLLM(response("不应执行")), flow)
    event, ticket = _manual_event(adapter, scope, "普通 prompt", 1, "manual-when-false", source_message=False)
    _accept_manual(scheduler, requests, event, ticket)
    try:
        await scheduler.execute(event)
        await _drain_manual_settlements(scheduler, manager)
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "failed"
        assert record["detail"] == "flow_when_false"
        assert adapter.sent == []
        session = manager.entries[event.session_id].session
        assert not any(item["kind"] == "run_start" for item in session._events)
    finally:
        for entry in manager.entries.values():
            await entry.session.close_flow_nodes()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_enqueue", [False, True])
async def test_flow_manual_cancelled_ticket_never_triggers(
    tmp_path: Path, after_enqueue: bool,
):
    """execute 入口与已入队未 observe 的取消均不调用模型并写取消终态"""
    from tests.support.flow_runtime import MockLLM, response

    llm = MockLLM(response("不应执行"))
    scope, store, adapter, scheduler, manager, requests = _manual_real_runtime(
        tmp_path, llm, _manual_flow())
    event, ticket = _manual_event(adapter, scope, "取消请求", 1, f"manual-cancel-{after_enqueue}", source_message=False)
    _accept_manual(scheduler, requests, event, ticket)
    try:
        if not after_enqueue:
            scheduler.manual_wakes.clear_adapter(scope.adapter_id)
        await scheduler.execute(event)
        if after_enqueue:
            scheduler.manual_wakes.clear_adapter(scope.adapter_id)
        await manager.drain_observations()
        assert llm.calls == [] and adapter.sent == []
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "failed"
        assert "cancelled" in record["detail"]
    finally:
        for entry in manager.entries.values():
            await entry.session.close_flow_nodes()


@pytest.mark.asyncio
async def test_flow_manual_request_waits_for_all_workflows(tmp_path: Path):
    """同一 wake 驱动两个 WF 时, 首个完成不能提前结算手动请求"""
    from tests.support.flow_runtime import MockLLM, response, wait_until

    release = asyncio.Event()

    async def script(messages: list[Any], tools: Any, count: int):
        await release.wait()
        return response(f"分支回复 {count}")

    llm = MockLLM(script)
    flow = _manual_flow()
    flow["nodes"].insert(3, {"id": "w2", "type": "react_workflow", "title": "WF2", "config": {
        "model_name": "test", "on_busy": "queue", "max_steps": 2, "timeout_seconds": 10,
    }})
    flow["edges"].extend([
        {"from": "src.out", "to": "w2.input"},
        {"from": "wake.event", "to": "w2.trigger"},
        {"from": "w2.text", "to": "reply.reply"},
    ])
    scope, store, adapter, scheduler, manager, requests = _manual_real_runtime(tmp_path, llm, flow)
    event, ticket = _manual_event(adapter, scope, "两个分支", 1, "manual-multi", source_message=False)
    _accept_manual(scheduler, requests, event, ticket)
    try:
        await scheduler.execute(event)
        await wait_until(lambda: len(llm.calls) == 2)
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "executing"
        release.set()
        await _drain_manual_settlements(scheduler, manager)
        assert len(adapter.sent) == 2
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "sent"
    finally:
        release.set()
        for entry in manager.entries.values():
            await entry.session.close_flow_nodes()


@pytest.mark.asyncio
@pytest.mark.parametrize("on_busy", ["queue", "merge", "drop"])
async def test_flow_manual_request_respects_busy_strategy_and_completion(
    tmp_path: Path, on_busy: str,
):
    """手动事件在真实 WF 忙碌时按 queue/merge/drop 结算, 不绕过 busy 策略"""
    from tests.support.flow_runtime import MockLLM, archive_event, make_event, response, wait_until

    started, release = asyncio.Event(), asyncio.Event()

    async def script(messages: list[Any], tools: Any, count: int):
        if count == 1:
            started.set()
            await release.wait()
        return response(f"回复 {count}")

    llm = MockLLM(script)
    scope, store, adapter, scheduler, manager, requests = _manual_real_runtime(
        tmp_path, llm, _manual_flow(on_busy=on_busy))
    try:
        normal = make_event(adapter, scope, "普通唤醒", 1, mention=True)
        archive_event(store, scope, normal)
        await scheduler.execute(normal)
        await asyncio.wait_for(started.wait(), 2)
        event, ticket = _manual_event(
            adapter, scope, "手动忙碌策略", 2, f"manual-busy-{on_busy}", source_message=False)
        _accept_manual(scheduler, requests, event, ticket)
        await scheduler.execute(event)
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "executing"
        if on_busy == "drop":
            await _drain_manual_settlements(scheduler, manager)
            record = requests.lookup_request(ticket.request_id, scope.adapter_id)
            assert record is not None and record["status"] == "failed"
            assert record["detail"] == "no_confirmed_send"
            assert len(llm.calls) == 1 and adapter.sent == []
        release.set()
        await _drain_manual_settlements(scheduler, manager)
        session = manager.entries[event.session_id].session
        await wait_until(lambda: not session._engine.running_workflows())
        if on_busy != "drop":
            assert len(llm.calls) == 2
            assert _model_input(llm.calls[1]).split("\n\n")[0] == "手动忙碌策略"
            assert [item["text"] for item in adapter.sent] == ["回复 2"]
            record = requests.lookup_request(ticket.request_id, scope.adapter_id)
            assert record is not None and record["status"] == "sent"
    finally:
        release.set()
        for entry in manager.entries.values():
            await entry.session.close_flow_nodes()


@pytest.mark.asyncio
async def test_flow_manual_settlement_does_not_block_next_observation(tmp_path: Path):
    """手动唤醒的 WF 阻塞期间, 同会话的下一条消息仍被观察并触发自己的 WF。"""
    from tests.support.flow_runtime import MockLLM, archive_event, make_event, response, wait_until

    release_manual = asyncio.Event()

    async def script(messages: list[Any], tools: Any, count: int):
        if count == 1:
            await release_manual.wait()
            return response("手动回复")
        return response("普通回复")

    # 阈值磁贴驱动第二个 WF: on_busy=queue 的单 WF 按设计串行,
    # 用第二个 WF 证明观察没有被手动结算等待挡住, 而不是被 WF 级排队挡住。
    flow = _manual_flow()
    flow["nodes"].insert(2, {"id": "th", "type": "threshold", "title": "阈值", "config": {
        "messages": 1, "chars": 0, "idle_seconds": 0, "max_batch": 10,
    }})
    flow["nodes"].insert(4, {"id": "w2", "type": "react_workflow", "title": "WF2", "config": {
        "model_name": "test", "on_busy": "queue", "max_steps": 2, "timeout_seconds": 10,
        "plugins": "flow_snapshot",
    }})
    flow["nodes"].append({"id": "sink", "type": "state", "title": "状态", "config": {}})
    flow["edges"].extend([
        {"from": "src.out", "to": "th.messages"},
        {"from": "th.event", "to": "w2.trigger"},
        {"from": "w2.text", "to": "sink.write"},
    ])
    llm = MockLLM(script)
    scope, store, adapter, scheduler, manager, requests = _manual_real_runtime(tmp_path, llm, flow)
    event, ticket = _manual_event(adapter, scope, "手动阻塞", 1, "manual-unblocked", source_message=False)
    _accept_manual(scheduler, requests, event, ticket)
    cleaned: list[MessageEvent] = []
    event.cleanup_temporary_local_files = lambda: cleaned.append(event)
    session = None
    try:
        await scheduler.execute(event)
        await wait_until(lambda: len(llm.calls) == 1)
        # 手动 WF 已阻塞但结算任务不占用观察队列: 下一条消息应立即被观察,
        # 阈值 (1 条新消息) 命中后第二个 WF 立即被触发, 第二个模型调用不需要先释放手动 WF。
        normal = make_event(adapter, scope, "普通提及", 2)
        archive_event(store, scope, normal)
        await scheduler.execute(normal)
        await wait_until(lambda: len(llm.calls) == 2, timeout=5)
        # 结算尚未发生: 请求仍在执行, 临时文件也未被 drain 提前清理
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "executing"
        assert cleaned == []
        release_manual.set()
        await _drain_manual_settlements(scheduler, manager)
        # 结算恰好一次: 终态 sent 且临时文件清理由结算任务执行
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        assert record is not None and record["status"] == "sent"
        assert [item["text"] for item in adapter.sent] == ["手动回复"]
        assert cleaned == [event]
        assert not scheduler._flow_manual_tasks
        session = next(iter(manager.entries.values())).session
    finally:
        release_manual.set()
        for entry in manager.entries.values():
            await entry.session.close_flow_nodes()


@pytest.mark.asyncio
async def test_flow_manual_settlement_close_while_waiting_reaches_terminal(tmp_path: Path):
    """会话关闭时仍在等待的手动结算必须终结: 引擎解除等待后按证据归并, 不悬挂 pending。"""
    from tests.support.flow_runtime import MockLLM, response, wait_until

    release_manual = asyncio.Event()

    async def script(messages: list[Any], tools: Any, count: int):
        await release_manual.wait()
        return response("不会送达")

    llm = MockLLM(script)
    scope, store, adapter, scheduler, manager, requests = _manual_real_runtime(tmp_path, llm, _manual_flow())
    event, ticket = _manual_event(adapter, scope, "关闭等待", 1, "manual-close-wait", source_message=False)
    _accept_manual(scheduler, requests, event, ticket)
    cleaned: list[MessageEvent] = []
    event.cleanup_temporary_local_files = lambda: cleaned.append(event)
    session = None
    try:
        await scheduler.execute(event)
        await wait_until(lambda: len(llm.calls) == 1)
        session = next(iter(manager.entries.values())).session
        # 结算任务已在等待 WF; 关闭会话取消 WF, 引擎释放等待者, 结算按已记录证据归并终态
        await session.close_flow_nodes()
        await wait_until(
            lambda: (record := requests.lookup_request(ticket.request_id, scope.adapter_id)) is not None
            and record["status"] in {"sent", "partial", "failed", "unknown"})
        record = requests.lookup_request(ticket.request_id, scope.adapter_id)
        # 结算任务不属于会话观察任务, 不会被 close 取消; WF 取消后按发送证据归并而非 cancelled
        assert record is not None and record["status"] == "failed"
        assert record["detail"] == "no_confirmed_send"
        assert cleaned == [event]
        assert not scheduler._flow_manual_tasks
    finally:
        release_manual.set()
        for entry in manager.entries.values():
            if not entry.session._closed:
                await entry.session.close_flow_nodes()


@pytest.mark.asyncio
async def test_flow_scheduler_real_session_close_drops_queued_observation_with_diagnostic(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实 /new 结算占用串行观察时, close/evict 清理未观察事件并写诊断。"""
    import sqlite3
    from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore
    from satrap.edictum.flow import session as session_module
    from satrap.edictum.flow.session import AsyncFlowSession
    from satrap.edictum.flow.store import FlowStore
    from tests.support.flow_runtime import (
        FlowAdapter, MockLLM, archive_event, database, make_event, response,
    )

    scope = MessageScope("adapter1", "self1", "group", "chat1")
    db = database(tmp_path / "flow.db")
    store = PlatformMessageStore(db, scope.adapter_id)
    adapter = FlowAdapter(store)
    flow = {
        "version": 3,
        "nodes": [
            {"id": "src", "type": "source", "title": "消息", "config": {}},
            {"id": "bucket", "type": "bucket", "title": "桶", "config": {}},
            {"id": "th", "type": "threshold", "title": "阈值",
             "config": {"messages": 1, "max_batch": 10, "on_new": "drain"}},
            {"id": "w1", "type": "react_workflow", "title": "WF",
             "config": {"model_name": "test", "on_busy": "queue", "max_steps": 2,
                        "plugins": "flow_snapshot"}},
            {"id": "state", "type": "state", "title": "状态", "config": {}},
        ],
        "edges": [
            {"from": "src.out", "to": "bucket.in"}, {"from": "bucket.out", "to": "th.messages"},
            {"from": "th.event", "to": "w1.trigger"}, {"from": "w1.text", "to": "state.write"},
        ],
    }
    fs = FlowStore(db, scope.adapter_id, "scheduler-close")
    with sqlite3.connect(db) as connection:
        connection.execute("BEGIN")
        fs.commit_position(connection, scope.key, "th", "w1", 0)
        connection.execute("COMMIT")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(messages: list[Any], tools: Any, count: int):
        started.set()
        await release.wait()
        return response("done")

    llm = MockLLM(slow)

    def factory(session_id: str) -> AsyncFlowSession:
        return AsyncFlowSession(session_id, model_resolver=static_model_resolver(llm), flow=flow, db_path=str(db), new_from="old",
                                message_store=store, flow_name="scheduler-close")

    manager = _RealFlowSchedulerManager(factory)
    scheduler = PipelineScheduler(_as_session_manager(manager))
    diagnostics = []
    original_record = session_module.record_debug

    def record(category: str, phase: str, **kwargs: Any) -> None:
        diagnostics.append((category, phase, kwargs))
        original_record(category, phase, **kwargs)

    monkeypatch.setattr(session_module, "record_debug", record)
    first = make_event(adapter, scope, "结算", 2)
    second = make_event(adapter, scope, "未观察", 3)
    cleaned = []
    for event in (first, second):
        event.cleanup_temporary_local_files = lambda event=event: cleaned.append(event)
    session = None
    try:
        old = make_event(adapter, scope, "旧消息", 1)
        archive_event(store, scope, old)
        archive_event(store, scope, first)
        await scheduler.execute(first)
        session = next(iter(manager.entries.values())).session
        try:
            await asyncio.wait_for(started.wait(), 3)
        except TimeoutError:
            assert started.is_set(), [issue.to_payload() for issue in session.issues if issue.level == "error"]
        archive_event(store, scope, second)
        await scheduler.execute(second)
        assert list(session._scheduler_observations) == [second]
        await session.close_flow_nodes()
        assert not session._scheduler_observations
        assert set(cleaned) == {first, second}
        drops = [item for item in diagnostics if item[:2] == ("flow_observation", "queue_drop")]
        assert drops and drops[-1][2]["metadata"]["dropped_messages"] == 1
        task = session._spawn_observer_task(
            session._observe_outbound(scope, None), name="closed-must-not-start")
        assert task is None and not session._observer_tasks
    finally:
        release.set()
        if session is not None and not session._closed:
            await session.close_flow_nodes()


@pytest.mark.asyncio
async def test_flow_scheduler_observes_unwoken_scoped_and_switched_mapping(tmp_path: Path):
    """实际调度器在未唤醒时使用真实 UserManager 的群范围和更新后映射。"""
    from satrap.core.framework.UserManager import UserManager

    manager = _FlowSchedulerManager()
    users = UserManager(_as_session_manager(manager), db_path=tmp_path / "users.db")
    adapter = RecorderAdapter(settings={"context_scope": "group_member", "wake_mode": "mention"})
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=users)
    first = _group_event(adapter)
    second_group = _group_event(adapter, group_id="g2")
    await scheduler.execute(first)
    await scheduler.execute(second_group)
    await manager.drain_observations()
    first_id, second_id = [row[0] for row in manager.observed]
    assert first_id != second_id and first_id != first.session_id
    call = manager.created_calls[0]
    assert call.route is not None and call.route.scope == "group_member"
    assert call.session_id == first_id
    users.store.upsert_context_session(call.route.owner, call.route.platform, call.route.session_type,
                                       "mapped:after-new", call.route.provider, context_key=call.route.key)
    after_new = _group_event(adapter)
    await scheduler.execute(after_new)
    await manager.drain_observations()
    assert manager.observed[-1] == ("mapped:after-new", after_new)
    assert manager.created_calls[-1].route == call.route
    assert not first.is_wake_up() and not after_new.is_wake_up()
    assert manager.calls == []


@pytest.mark.asyncio
async def test_flow_scheduler_enqueues_before_mapping_switch_without_holding_route_lock(tmp_path: Path):
    """真实调度锁中只投递; 慢观察不阻塞切换, 同一会话按投递顺序消费。"""
    from satrap.core.framework.UserManager import UserManager

    manager = _FlowSchedulerManager()
    users = UserManager(_as_session_manager(manager), db_path=tmp_path / "users.db")
    adapter = RecorderAdapter(settings={"context_scope": "group_member", "wake_mode": "mention"})
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=users)
    first, second, switched = (_group_event(adapter) for _ in range(3))
    route, _ = scheduler._conversation_route(first, users)
    assert route is not None
    started, release = asyncio.Event(), asyncio.Event()
    original_create = manager._acquire_or_create_entry_async
    seen: list[tuple[str, MessageEvent]] = []

    async def create(call: Any) -> Any:
        entry = await original_create(call)

        async def observe(event: MessageEvent) -> None:
            lock_entry = scheduler._session_turns.get((id(manager), "route", route.lock_key))
            assert lock_entry is None or not lock_entry[0].locked()
            seen.append((call.session_id, event))
            if event is first:
                started.set()
                await release.wait()

        entry.session.observe = observe
        return entry

    manager._acquire_or_create_entry_async = create
    await scheduler.execute(first)
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(scheduler.execute(second), 1)
    assert seen == [(manager.created_calls[0].session_id, first)]
    async with scheduler._session_turn(_as_session_manager(manager), route.lock_key, namespace="route"):
        users.store.upsert_context_session(route.owner, route.platform, route.session_type,
                                           "mapped:next", route.provider, context_key=route.key)
    await asyncio.wait_for(scheduler.execute(switched), 1)
    old_id = manager.created_calls[0].session_id
    release.set()
    await asyncio.wait_for(manager.drain_observations(), 1)
    assert [event for session_id, event in seen if session_id == old_id] == [first, second]
    assert [(session_id, event) for session_id, event in seen if event is switched] == [("mapped:next", switched)]


@pytest.mark.asyncio
async def test_flow_scheduler_settlement_preserves_order_and_event_cleanup(tmp_path: Path):
    """后台 /new 结算完成之前不得处理下一条或清理上一条的临时文件。"""
    from satrap.core.framework.UserManager import UserManager

    manager = _FlowSchedulerManager()
    users = UserManager(_as_session_manager(manager), db_path=tmp_path / "users.db")
    adapter = RecorderAdapter(settings={"context_scope": "group_member", "wake_mode": "mention"})
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=users)
    first, second = _group_event(adapter), _group_event(adapter)
    started, release = asyncio.Event(), asyncio.Event()
    seen: list[MessageEvent] = []
    cleaned: list[MessageEvent] = []
    first.cleanup_temporary_local_files = lambda: cleaned.append(first)
    second.cleanup_temporary_local_files = lambda: cleaned.append(second)
    original_create = manager._acquire_or_create_entry_async

    async def create(call: Any) -> Any:
        entry = await original_create(call)
        session = entry.session

        async def settle() -> None:
            started.set()
            await release.wait()
            seen.append(first)
            session._settle_task = None

        async def observe(event: MessageEvent) -> None:
            if event is first:
                session._settle_task = session._spawn_observer_task(settle(), name="test-settle")
            else:
                seen.append(event)

        session.observe = observe
        return entry

    manager._acquire_or_create_entry_async = create
    await scheduler.execute(first)
    await asyncio.wait_for(started.wait(), 1)
    await scheduler.execute(second)
    assert not seen and not cleaned
    release.set()
    await asyncio.wait_for(manager.drain_observations(), 1)
    assert seen == [first, second]
    assert cleaned == [first, second]


@pytest.mark.asyncio
async def test_flow_scheduler_observation_queue_is_bounded_and_cleans_drop(tmp_path: Path, monkeypatch):
    """持续慢观察时上游队列也有界, 溢出事件按 drop 清理而不转移无界积压"""
    from satrap.core.framework.UserManager import UserManager
    from satrap.core.pipeline import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "_FLOW_OBSERVATION_QUEUE_LIMIT", 1)
    manager = _FlowSchedulerManager()
    users = UserManager(_as_session_manager(manager), db_path=tmp_path / "users.db")
    adapter = RecorderAdapter(settings={"context_scope": "group_member", "wake_mode": "mention"})
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=users)
    first, second, third = (_group_event(adapter) for _ in range(3))
    started, release = asyncio.Event(), asyncio.Event()
    cleaned: list[MessageEvent] = []
    for event in (first, second, third):
        event.cleanup_temporary_local_files = lambda event=event: cleaned.append(event)
    original_create = manager._acquire_or_create_entry_async
    seen: list[MessageEvent] = []

    async def create(call: Any) -> Any:
        entry = await original_create(call)

        async def observe(event: MessageEvent) -> None:
            seen.append(event)
            if event is first:
                started.set()
                await release.wait()

        entry.session.observe = observe
        return entry

    manager._acquire_or_create_entry_async = create
    await scheduler.execute(first)
    await asyncio.wait_for(started.wait(), 1)
    await scheduler.execute(second)
    await scheduler.execute(third)
    session = next(iter(manager.entries.values())).session
    assert list(session._scheduler_observations) == [third]
    assert cleaned == [second]
    release.set()
    await asyncio.wait_for(manager.drain_observations(), 1)
    assert seen == [first, third]
    assert cleaned == [second, first, third]


@pytest.mark.asyncio
async def test_flow_scheduler_close_cleans_unstarted_observation(tmp_path: Path):
    """会话在投递后、任务首轮执行前关闭时仍释放事件资源。"""
    from satrap.core.framework.UserManager import UserManager

    manager = _FlowSchedulerManager()
    users = UserManager(_as_session_manager(manager), db_path=tmp_path / "users.db")
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=users)
    event = _group_event(RecorderAdapter(settings={"context_scope": "group_member"}))
    cleaned: list[MessageEvent] = []
    event.cleanup_temporary_local_files = lambda: cleaned.append(event)
    await scheduler.execute(event)
    session = next(iter(manager.entries.values())).session
    await session.close_flow_nodes()
    await asyncio.sleep(0)
    assert cleaned == [event]
    assert not manager.observed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["mapping", "disabled", "simple"])
async def test_flow_scheduler_rechecks_after_route_lock_wait(tmp_path: Path, change: str):
    """等待稳定路由锁期间切换映射或撤销绑定后, 不得接管旧会话。"""
    from satrap.core.framework.UserManager import UserManager

    manager = _FlowSchedulerManager()
    users = UserManager(_as_session_manager(manager), db_path=tmp_path / "users.db")
    adapter = RecorderAdapter(settings={"context_scope": "group_member", "wake_mode": "mention"})
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=users)
    event = _group_event(adapter)
    route, _ = scheduler._conversation_route(event, users)
    assert route is not None
    async with scheduler._session_turn(_as_session_manager(manager), route.lock_key, namespace="route"):
        task = asyncio.create_task(scheduler.execute(event))
        await asyncio.sleep(0)
        assert not task.done()
        if change == "mapping":
            users.store.upsert_context_session(route.owner, route.platform, route.session_type,
                                               "mapped:switched", route.provider, context_key=route.key)
        elif change == "disabled":
            manager.enabled = False
        else:
            manager.kind = "simple"
    await task
    await manager.drain_observations()
    if change == "mapping":
        assert manager.observed == [("mapped:switched", event)]
    else:
        assert not manager.observed and not manager.created_calls


@pytest.mark.asyncio
async def test_flow_scheduler_rechecks_definition_and_disabled_binding(tmp_path: Path):
    """simple 与 flow 双向切换及禁用后均使用当前配置, 不复用旧类型。"""
    from satrap.core.framework.UserManager import UserManager

    manager = _FlowSchedulerManager()
    users = UserManager(_as_session_manager(manager), db_path=tmp_path / "users.db")
    adapter = RecorderAdapter()
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=users)
    manager.kind = "simple"
    await scheduler.execute(_message_event(adapter))
    assert len(manager.calls) == 1 and not manager.observed
    manager.kind = "flow"
    await scheduler.execute(_message_event(adapter))
    await manager.drain_observations()
    assert len(manager.observed) == 1
    manager.enabled = False
    await scheduler.execute(_message_event(adapter))
    assert len(manager.observed) == 1 and len(manager.calls) == 1
    manager.enabled = True
    manager.kind = "simple"
    await scheduler.execute(_message_event(adapter))
    assert len(manager.observed) == 1 and len(manager.calls) == 2
    manager.kind = "flow"
    await scheduler.execute(_message_event(adapter))
    await manager.drain_observations()
    assert len(manager.observed) == 2


# ================= 会话绑定闸门测试 =================


class _GroupBindingAdapter(RecorderAdapter):
    """携带群路由绑定的测试适配器"""

    def __init__(self, binding: dict[str, str] | None = None, adapter_id: str = "rec1") -> None:
        super().__init__(adapter_id)
        self.binding = binding

    def group_route(self, group_id: str) -> tuple[dict[str, Any], int]:
        """返回群路由快照"""
        if self.binding is None:
            return ({}, 1)
        return ({"binding": {"mode": "value", "value": self.binding}}, 1)


class _RecordingUserManager:
    """记录会话解析调用的假 UserManager"""

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def resolve_session(self, user_id: str, platform: str, session_type: str, **kwargs: Any) -> str:
        """记录解析入参并返回稳定会话 ID"""
        self.resolved.append(session_type)
        return f"{platform}:{user_id}:sid"


class _GateHarness(NamedTuple):
    """绑定闸门用例的装配件"""

    scheduler: PipelineScheduler
    manager: _FakeSessionManager
    users: _RecordingUserManager
    adapter: RecorderAdapter
    registry: _FakeProviderRegistry


def _gate_harness(
    definitions: dict[str, bool], *, adapter: RecorderAdapter | None = None, rate_limiter: RateLimiter | None = None,
) -> _GateHarness:
    """装配绑定闸门用例: 注册表替身按定义表答复三态, 会话与用户管理器记录调用"""
    registry = _FakeProviderRegistry(definitions)
    manager = _FakeSessionManager()
    manager.provider_registry = registry
    users = _RecordingUserManager()
    scheduler = PipelineScheduler(
        _as_session_manager(manager), rate_limiter=rate_limiter, user_manager=cast(Any, users),
    )
    return _GateHarness(scheduler, manager, users, adapter or RecorderAdapter(), registry)


def _group_event(adapter: RecorderAdapter, *, message_str: str = "hello", group_id: str = "g1") -> MessageEvent:
    """构造带群号的群消息事件, 使群路由绑定参与最终绑定解析"""
    message = PlatformMessage()
    message.type = PlatformMessageType.GROUP_MESSAGE
    message.self_id = "bot"
    message.session_id = "s1"
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="user-1", nickname="User")
    message.group = Group(group_id=group_id, group_name=group_id)
    message.message = [Plain(text=message_str)]
    message.message_str = message_str
    return MessageEvent(
        message_str=message_str, platform_message=message,
        platform_meta=PlatformMetadata(name=adapter.config.id, id=adapter.config.id),
        session_id="s1", adapter=adapter, session_type="dummy",
    )


def _spy_window_paths(scheduler: PipelineScheduler, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """记录入窗与排程调用并转发真实实现"""
    observes: list[str] = []
    schedules: list[str] = []
    original_observe = scheduler.wake_window.observe
    original_schedule = scheduler.wake_timers.schedule

    def spy_observe(current: MessageEvent) -> tuple[Any, ...]:
        observes.append(current.session_id)
        return original_observe(current)

    def spy_schedule(current: MessageEvent, **kwargs: Any) -> None:
        schedules.append(current.session_id)
        original_schedule(current, **kwargs)

    monkeypatch.setattr(scheduler.wake_window, "observe", spy_observe)
    monkeypatch.setattr(scheduler.wake_timers, "schedule", spy_schedule)
    return observes, schedules


def _diagnostic_rows(scheduler: PipelineScheduler, event: MessageEvent) -> list[dict[str, Any]]:
    """取事件对应的诊断阶段记录"""
    detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
    if detail is None:
        raise AssertionError("缺少诊断记录")
    return cast(list[dict[str, Any]], detail["records"])


def _projection_rows(scheduler: PipelineScheduler, event: MessageEvent) -> list[tuple[Any, Any]]:
    """取事件的投影阶段结论"""
    return [
        (row["decision"], row["reason_code"]) for row in _diagnostic_rows(scheduler, event)
        if row["stage"] == "projection"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["disabled", "invalid", "runnable"])
async def test_binding_gate_state_before_window_and_timers(
    state: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    """调度器入口绑定门: 禁用静默 DEBUG, 失效 WARNING, 均在入窗排程前拒绝; 可运行时行为不变"""
    import logging

    harness = _gate_harness({"runnable": {"dummy": True}, "disabled": {"dummy": False}, "invalid": {}}[state])
    harness.adapter.config.settings.update({"wake_mode": "frequency", "wake_message_threshold": 5})
    observes, schedules = _spy_window_paths(harness.scheduler, monkeypatch)
    event = _group_event(harness.adapter)
    with caplog.at_level(logging.DEBUG):
        await harness.scheduler.execute(event)

    if state == "runnable":
        assert observes == ["s1"] and schedules == ["s1"]
        assert [item.text for item in harness.scheduler.wake_window.peek(event)] == ["hello"]
        assert harness.manager.calls == []
        return

    assert harness.manager.calls == [] and harness.users.resolved == []
    assert observes == [] and schedules == []
    assert event.get_extra("input_projection") is None
    # 被拒正文不得进入窗口, 重新启用后不会被补进模型
    assert harness.scheduler.wake_window.peek(event) == ()
    if state == "disabled":
        assert _projection_rows(harness.scheduler, event) == [("dropped", "binding_disabled")]
        assert any(record.levelno == logging.DEBUG and "会话定义已禁用" in record.getMessage() for record in caplog.records)
        # 定义被禁用是正常配置状态: 不得逐条 WARNING
        assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == []
    else:
        assert _projection_rows(harness.scheduler, event) == [("dropped", "binding_invalid")]
        assert "会话定义不可用 provider=session_class, name=dummy" in str(_diagnostic_rows(harness.scheduler, event)[0]["reason"])
        assert any(record.levelno == logging.WARNING and "会话绑定不可用" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_binding_gate_judges_group_override_binding():
    """闸门判定的是群级覆盖后的绑定: 该群被拒不影响平台绑定自身有效"""
    adapter = _GroupBindingAdapter({"provider": "session_class", "config_name": "group-session"})
    harness = _gate_harness({"dummy": True, "group-session": False}, adapter=adapter)
    overridden = _group_event(adapter)
    overridden.is_wake = True
    assert (overridden.session_provider, overridden.session_type) == ("session_class", "group-session")
    await harness.scheduler.execute(overridden)
    assert harness.manager.calls == []
    assert _projection_rows(harness.scheduler, overridden) == [("dropped", "binding_disabled")]

    # 反向: 平台绑定失效但该群绑定有效, 只拒被判定的那个绑定
    harness.registry.definitions["dummy"] = False
    harness.registry.definitions["group-session"] = True
    allowed = _group_event(adapter)
    allowed.is_wake = True
    await harness.scheduler.execute(allowed)
    assert [call.session_type for call in harness.manager.calls] == ["group-session"]


@pytest.mark.asyncio
async def test_rejected_message_does_not_consume_rate_limit_and_recovers_without_rebuild():
    """闸门命中不消耗额度; 定义恢复后同一调度器与适配器立即恢复, 无需重建平台"""
    harness = _gate_harness({"dummy": False}, rate_limiter=RateLimiter(rate=1.0, burst=1))
    rejected = _group_event(harness.adapter, message_str="禁用期消息")
    rejected.is_wake = True
    await harness.scheduler.execute(rejected)
    assert harness.manager.calls == []

    harness.registry.definitions["dummy"] = True
    accepted = _group_event(harness.adapter, message_str="恢复后消息")
    accepted.is_wake = True
    await harness.scheduler.execute(accepted)
    assert [call.message for call in harness.manager.calls] == ["[用户 User (ID user-1), 消息 msg-1] 恢复后消息"]
    # 恢复后仍走完整管线: 回复经同一适配器外发
    assert [getattr(chain.components[0], "text", "") for _, chain, _purpose in harness.adapter.sent] == ["回复"]


@pytest.mark.asyncio
async def test_manual_and_deadline_sources_are_rejected_when_binding_disabled():
    """手动唤醒与定时补偿来源同样在闸门被拒, 快照不进入会话"""
    from satrap.core.pipeline.manual_wake import ManualWakeTicket
    from satrap.core.pipeline.wake_timers import DeadlineTicket
    from satrap.core.pipeline.wake_window import PendingMessage

    harness = _gate_harness({"dummy": False})
    snapshot = (PendingMessage("r1", "user-1", "1", "禁用期窗口正文", 0.0),)
    manual = _group_event(harness.adapter, message_str="手动唤醒")
    harness.scheduler.manual_wakes.tickets[manual] = ManualWakeTicket("req-manual", snapshot)
    deadline = _group_event(harness.adapter, message_str="定时补偿")
    harness.scheduler.wake_timers.tickets[deadline] = DeadlineTicket(snapshot=snapshot)

    for event in (manual, deadline):
        await harness.scheduler.execute(event)

    assert harness.manager.calls == [] and harness.users.resolved == []
    assert [_projection_rows(harness.scheduler, event) for event in (manual, deadline)] == [
        [("dropped", "binding_disabled")], [("dropped", "binding_disabled")],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["shared_registry", "real_registry"])
async def test_binding_gate_reads_registry_judgment(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """闸门读取注册表共享判定: 恒失效即使定义存在也拒绝; 真实注册表下禁用即拒收, 启用即恢复"""
    if source == "shared_registry":
        harness = _gate_harness({"dummy": True})
        harness.manager.provider_registry = SessionProviderRegistry()
        calls: list[tuple[str, str | None]] = []

        def spy(self: SessionProviderRegistry, definition_name: str, provider_name: str | None = None) -> BindingStatus:
            calls.append((definition_name, provider_name))
            return BindingStatus(BindingState.INVALID, "共享判定失效")

        monkeypatch.setattr(SessionProviderRegistry, "binding_status", spy)
        event = _group_event(harness.adapter)
        await harness.scheduler.execute(event)

        assert calls == [("dummy", "session_class")]
        assert harness.manager.calls == [] and harness.users.resolved == []
        assert _projection_rows(harness.scheduler, event) == [("dropped", "binding_invalid")]
        return

    from satrap.core.framework.Base import Session
    from satrap.core.framework.SessionClassManager import SessionClassConfigManager

    class _EchoSession(Session):
        """最小同步会话: 原样回显输入"""

        def run(self, message: str) -> str:
            return message

    class _RecordingManager(SessionManager):
        """真实会话绑定的注册表之上记录模型调用"""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.calls: list[Any] = []

        async def handle_call_async(self, user_call: Any) -> str:
            self.calls.append(user_call)
            return "回复"

    class_cfg = SessionClassConfigManager(storage_path=tmp_path / "session_classes.json")
    class_cfg.register("dummy", _EchoSession)
    manager = _RecordingManager(default_session_type="dummy", db_path=tmp_path / "sessions.db")
    manager.register_session_type("dummy", _EchoSession)
    manager.class_cfg_mgr = class_cfg
    users = _RecordingUserManager()
    scheduler = PipelineScheduler(_as_session_manager(manager), user_manager=cast(Any, users))
    adapter = RecorderAdapter()

    class_cfg.update_entry("dummy", enabled=False)
    rejected = _group_event(adapter, message_str="禁用期消息")
    rejected.is_wake = True
    await scheduler.execute(rejected)
    assert manager.calls == [] and users.resolved == []
    assert _projection_rows(scheduler, rejected) == [("dropped", "binding_disabled")]

    # 重新启用定义即可恢复: 平台与调度器实例不变
    class_cfg.update_entry("dummy", enabled=True)
    accepted = _group_event(adapter, message_str="恢复后消息")
    accepted.is_wake = True
    await scheduler.execute(accepted)
    assert [call.message for call in manager.calls] == ["[用户 User (ID user-1), 消息 msg-1] 恢复后消息"]
