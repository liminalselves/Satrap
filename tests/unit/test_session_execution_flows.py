"""业务链路场景与集中边界契约"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
import threading
import asyncio
from pathlib import Path
import pytest
from typing import cast
import time
from satrap.core.framework.SessionManager import SessionConfig as _session_manager_concurrency_SessionConfig
from satrap.core.framework.SessionManager import SessionEntry, SessionManager, SessionPool, SessionPoolCapacityError
from satrap.core.framework.UserManager import UserManager
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.type import LLMConfig, UserCall
from dataclasses import FrozenInstanceError
from satrap.core.framework.Base import Session as FrameworkSession
from satrap.core.utils.async_worker import BoundedAsyncWorker
from satrap.core.utils.TCBuilder.async_tool import AsyncTool as _call_origin_AsyncTool
from satrap.core.call_context import CallOrigin, bind_call_origin, current_call_origin, require_call_origin
from satrap.core.call_context import is_group_origin, is_private_origin
from typing import Any
from satrap.core.framework.providers import EdictumProvider, SessionClassProvider
from satrap.core.framework.SessionManager import SessionRegistry
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.simple_session import SimpleSession, AsyncSimpleSession
from satrap.core.utils.TCBuilder import Tool, AsyncTool as _provider_call_origin_AsyncTool
from satrap.edictum.config import EdictumConfigManager
from satrap.core.type import LLMCallResponse, SessionConfig as _provider_call_origin_SessionConfig
import re
from satrap.core.components import PlatformComponentType
from satrap.core.platform.event import MessageChain, MessageEvent, MessageEventResult, MessageSession, PlatformMetadata
from satrap.core.type import MessageMember, PlatformMessage, PlatformMessageType
from support.adapters import RecorderAdapter

class _EchoSession(Session):
    """测试用同步会话"""

    def run(self, message: str) -> str:
        """返回输入消息"""
        return message

class _BlockingAsyncSession(AsyncSession):
    """测试异步删除与活动调用互斥的会话"""

    def __init__(
        self,
        session_id: str,
        db_path: str,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        """初始化可控异步会话"""
        super().__init__(session_id, db_path=db_path)
        self.entered = entered
        self.release = release

    async def run(self, message: str) -> str:
        """等待测试放行后返回输入消息"""
        self.entered.set()
        await self.release.wait()
        return message

def _make_manager(tmp_path: Path, max_size: int = 1000) -> SessionManager:
    """创建隔离的会话管理器"""
    return SessionManager(
        default_session_type="echo",
        default_session_class=_EchoSession,
        max_size=max_size,
        db_path=tmp_path / "platform.db",
        default_checkpoint_db=str(tmp_path / "context.db"),
    )

@pytest.mark.parametrize("_iteration", range(3))
def test_sync_calls_create_one_runtime_for_same_session(
    _iteration: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发同步调用同一会话时只创建一个运行时实例"""
    manager = _make_manager(tmp_path)
    manager.register_session(_EchoSession, "echo", session_id="shared")
    original_create = manager._create_entry
    creation_count = 0
    count_lock = threading.Lock()
    start = threading.Barrier(2)

    def delayed_create(
        session_config: _session_manager_concurrency_SessionConfig,
        *,
        add_to_pool: bool = True,
    ):
        """扩大创建竞态窗口并记录创建次数"""
        nonlocal creation_count
        with count_lock:
            creation_count += 1
        time.sleep(0.05)
        return original_create(session_config, add_to_pool=add_to_pool)

    monkeypatch.setattr(manager, "_create_entry", delayed_create)

    def invoke(message: str) -> str:
        """等待并发起跑后调用共享会话"""
        start.wait()
        return manager.handle_call(UserCall(session_id="shared", message=message))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, ["one", "two"]))

    assert sorted(results) == ["one", "two"]
    assert creation_count == 1
    assert list(manager.pool.list_entries()) == ["shared"]

def test_pool_does_not_collect_or_evict_leased_entry() -> None:
    """池满且所有条目忙碌时拒绝新条目并保持硬容量上限"""
    pool = SessionPool(max_size=1, idle_timeout=0)
    pool.put("busy", MagicMock(spec=Session), "echo")
    busy = pool.acquire("busy")
    assert busy is not None

    with pytest.raises(SessionPoolCapacityError, match="所有条目均在使用"):
        pool.put("new", MagicMock(spec=Session), "echo")

    assert len(pool.list_entries()) == 1
    assert pool.get("busy") is busy

    pool.release(busy)
    evicted = pool.put("new", MagicMock(spec=Session), "echo")
    assert evicted is not None and evicted[0] == "busy"
    assert list(pool.list_entries()) == ["new"]

def test_sync_remove_session_refuses_active_lease(tmp_path: Path) -> None:
    """同步删除不得移除仍有活动调用租约的会话"""
    manager = _make_manager(tmp_path)
    manager.register_session(_EchoSession, "echo", session_id="shared")
    session = _EchoSession("shared", db_path=str(tmp_path / "context.db"))
    manager.pool.put("shared", session, "echo")
    entry = manager.pool.acquire("shared")
    assert entry is not None

    assert manager.remove_session("shared") is False
    assert manager.pool.get("shared") is entry

    manager.pool.release(entry)
    assert manager.remove_session("shared") is True
    assert manager.pool.get("shared") is None

@pytest.mark.asyncio
async def test_async_remove_waits_for_running_session(tmp_path: Path) -> None:
    """异步删除等待正在执行的会话完成后再关闭运行时"""
    manager = _make_manager(tmp_path)
    manager.register_session(_EchoSession, "echo", session_id="shared")
    entered = asyncio.Event()
    release = asyncio.Event()
    session = _BlockingAsyncSession(
        "shared",
        str(tmp_path / "context.db"),
        entered,
        release,
    )
    manager.pool.put("shared", session, "echo")

    call_task = asyncio.create_task(
        manager.handle_call_async(UserCall(session_id="shared", message="answer"))
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    remove_task = asyncio.create_task(manager.remove_session_async("shared"))
    await asyncio.sleep(0.02)
    assert remove_task.done() is False

    release.set()
    assert await asyncio.wait_for(call_task, timeout=1) == "answer"
    assert await asyncio.wait_for(remove_task, timeout=1) is True
    assert manager.pool.get("shared") is None

@pytest.mark.asyncio
async def test_async_creation_lock_wait_does_not_block_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """异步创建等待线程锁时事件循环仍可调度定时器"""
    manager = _make_manager(tmp_path)
    config = manager.register_session(_EchoSession, "echo", session_id="shared")
    candidate = manager._create_entry(config, add_to_pool=False)
    assert candidate is not None
    main_thread = threading.get_ident()

    def create_prepared_entry(session_cfg: _session_manager_concurrency_SessionConfig) -> SessionEntry | None:
        """在真实创建锁内提交已准备实例, 检查此回调确实运行于工作线程"""
        assert threading.get_ident() != main_thread
        manager.pool.put(str(session_cfg.session_id), candidate.session, "echo")
        return manager.pool.get(str(session_cfg.session_id))

    monkeypatch.setattr(manager, "_create_entry", create_prepared_entry)
    lock_entered = threading.Event()
    release_lock = threading.Event()

    def hold_creation_lock() -> None:
        """持有创建锁直到异步断言完成"""
        with manager._entry_creation_lock:
            lock_entered.set()
            release_lock.wait(timeout=2)

    worker = threading.Thread(target=hold_creation_lock)
    worker.start()
    assert lock_entered.wait(timeout=1)
    timer_fired = asyncio.Event()
    asyncio.get_running_loop().call_later(0.02, timer_fired.set)
    creation_task = asyncio.create_task(manager._get_or_create_entry_async(config))

    try:
        await asyncio.wait_for(timer_fired.wait(), timeout=0.2)
        assert creation_task.done() is False
    finally:
        release_lock.set()
        worker.join(timeout=1)

    assert await asyncio.wait_for(creation_task, timeout=1) is not None

@pytest.mark.asyncio
async def test_async_model_reload_waits_for_session_operation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步模型重载等待正在执行的会话操作结束"""
    manager = _make_manager(tmp_path)
    manager.register_session(_EchoSession, "echo", session_id="shared")
    assert manager.activate_session("shared") is True
    entry = manager.pool.get("shared")
    assert entry is not None
    manager._model_cfg_mgr = MagicMock()
    applied = asyncio.Event()

    def prepare_reload(
        _session_id: str,
        _entry: SessionEntry,
    ) -> tuple[LLM | AsyncLLM, LLMConfig]:
        """返回不访问网络的模型重载占位对象"""
        return cast(LLM, MagicMock()), cast(LLMConfig, MagicMock())

    def apply_reload(
        _session_id: str,
        _session: Session | AsyncSession,
        _new_llm: LLM | AsyncLLM,
        _llm_config: LLMConfig,
    ) -> None:
        """记录模型重载已进入应用阶段"""
        applied.set()

    monkeypatch.setattr(manager, "_prepare_model_reload", prepare_reload)
    monkeypatch.setattr(manager, "_apply_model_reload", apply_reload)

    async with entry.async_operation_lock:
        reload_task = asyncio.create_task(manager.reload_model_configs_async())
        await asyncio.sleep(0)
        assert reload_task.done() is False
        assert applied.is_set() is False

    await reload_task
    assert applied.is_set() is True

class _BlockingSessionManager:
    """用于观察 UserManager 锁范围的会话管理器替身"""

    def __init__(self) -> None:
        """初始化阻塞控制事件"""
        self.entered = threading.Event()
        self.release = threading.Event()

    def handle_call(self, user_call: UserCall) -> str:
        """阻塞处理调用直到测试放行"""
        self.entered.set()
        self.release.wait(timeout=2)
        return user_call.message or ""

def test_user_manager_releases_global_lock_before_session_call(tmp_path: Path) -> None:
    """一个用户的慢会话调用不得阻塞其他用户管理操作"""
    session_manager = _BlockingSessionManager()
    user_manager = UserManager(
        cast(SessionManager, session_manager),
        db_path=tmp_path / "users.db",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        route_future = executor.submit(
            user_manager.route_call,
            UserCall(message="done"),
            "slow-user",
        )
        assert session_manager.entered.wait(timeout=1)
        create_future = executor.submit(user_manager.get_or_create_user, "other-user")
        assert create_future.result(timeout=1) is not None
        session_manager.release.set()
        assert route_future.result(timeout=1) == "done"

def origin(actor: str) -> CallOrigin:
    """构造同一共享群内不同成员的来源"""
    return CallOrigin("bot", "10", "group_message", "20", actor, "message", f"request-{actor}")

class IdentityTool(_call_origin_AsyncTool):
    """模拟接受模型传入主体字段的平台工具"""

    tool_name = "identity_probe"
    description = "读取本轮来源"
    params_dict = {"actor_id": ("string", "模型提供的普通参数")}

    async def execute(self, **kwargs):
        await asyncio.sleep(0)
        return require_call_origin().actor_id

@pytest.mark.asyncio
async def test_concurrent_shared_session_calls_keep_actor_and_ignore_model_identity():
    tool = IdentityTool()

    class SharedSession:
        async def run(self, message):
            return await tool(actor_id="forged-admin", origin={"actor_id": "forged-admin"})

    # 鸭子类型替身: 只实现 run, 不经框架基类构造
    session = cast(AsyncSession, SharedSession())
    results = await asyncio.gather(*[
        SessionManager._invoke_async_session(session, UserCall(message="hello", origin=origin(actor)))
        for actor in ["admin", "member"]
    ])
    assert results == ["admin", "member"]
    assert current_call_origin() is None
    with pytest.raises(PermissionError):
        await tool(actor_id="admin")

@pytest.mark.asyncio
async def test_background_task_cannot_reuse_identity_after_turn_finishes():
    release = asyncio.Event()
    tasks = []

    async def delayed():
        await release.wait()
        return current_call_origin()

    class Session:
        async def run(self, message):
            tasks.append(asyncio.create_task(delayed()))
            return "done"

    await SessionManager._invoke_async_session(cast(AsyncSession, Session()), UserCall(message="hello", origin=origin("admin")))
    release.set()
    assert await tasks[0] is None

@pytest.mark.asyncio
async def test_cancellation_revokes_child_context():
    started = asyncio.Event()
    release = asyncio.Event()
    children = []

    async def child():
        await release.wait()
        return current_call_origin()

    class Session:
        async def run(self, message):
            children.append(asyncio.create_task(child()))
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(SessionManager._invoke_async_session(cast(AsyncSession, Session()), UserCall(origin=origin("admin"))))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await children[0] is None

def test_sync_entry_masks_outer_identity_and_resets_after_failure():
    class Session:
        def run(self, message):
            assert current_call_origin() is None
            raise RuntimeError("failed")

    actor = origin("admin")
    with pytest.raises(FrozenInstanceError):
        setattr(actor, "actor_id", "other")
    with bind_call_origin(actor):
        assert SessionManager._invoke_sync_session(cast(FrameworkSession, Session()), UserCall(message="hello")) == ""
        assert current_call_origin() == actor
    assert current_call_origin() is None

@pytest.mark.asyncio
async def test_sync_worker_keeps_per_call_identity_without_persisting_it():
    worker = BoundedAsyncWorker("identity-test", workers=2, capacity=2)

    class Session:
        def run(self, message):
            return require_call_origin().actor_id

    try:
        results = await asyncio.gather(*[
            worker.run(SessionManager._invoke_sync_session, Session(), UserCall(message="hello", origin=origin(actor)))
            for actor in ["admin", "member"]
        ])
        assert results == ["admin", "member"]
        assert await worker.run(current_call_origin) is None
    finally:
        worker.close()

def kind_origin(chat_type: str, conversation_kind: str) -> CallOrigin:
    """构造指定消息类型与会话类型的来源"""
    return CallOrigin("bot", "10", chat_type, "20", "actor", "message", "request", conversation_kind=conversation_kind)

@pytest.mark.parametrize(("chat_type", "conversation_kind", "group", "private"), [
    ("GroupMessage", "group", True, False),
    ("FriendMessage", "private", False, True),
    ("GroupMessage", "", True, False),
    ("FriendMessage", "group", True, False),
])
def test_origin_kind_prefers_conversation_kind_and_falls_back_only_when_empty(
        chat_type: str, conversation_kind: str, group: bool, private: bool) -> None:
    origin_value = kind_origin(chat_type, conversation_kind)
    assert is_group_origin(origin_value) is group
    assert is_private_origin(origin_value) is private

def test_missing_origin_is_neither_group_nor_private() -> None:
    assert is_group_origin(None) is False
    assert is_private_origin(None) is False

class ProbeLLM(LLM):
    """模型替身发送伪造身份参数, 下一步返回真实工具结果"""

    def __init__(self):
        self.count = 0

    def call(self, messages, **kwargs):
        self.count += 1
        if self.count % 2:
            return LLMCallResponse(type="tools_call", content="", tool_calls=[{
                "name": "identity_probe", "id": f"tool-{self.count}", "arguments": {"actor_id": "forged-admin"},
            }])
        result = next(item for item in reversed(messages) if item["role"] == "tool")
        return LLMCallResponse(type="answer", content=str(result["content"]))

class AsyncProbeLLM(AsyncLLM):
    """异步模型替身复用相同工具循环"""

    def __init__(self):
        self.delegate = ProbeLLM()

    async def call(self, messages, **kwargs):
        return self.delegate.call(messages, **kwargs)

class ProbeTool(Tool):
    """由真实工具注册表执行, 不信任模型主体参数"""

    tool_name = "identity_probe"
    description = "返回本轮身份"
    params_dict = {"actor_id": ("string", "模型参数")}

    def execute(self, actor_id: str):
        identity = require_call_origin()
        return f"{identity.adapter_id}/{identity.chat_id}/{identity.actor_id}"

class AsyncProbeTool(_provider_call_origin_AsyncTool):
    """由异步工具注册表执行相同身份检查"""

    tool_name = ProbeTool.tool_name
    description = ProbeTool.description
    params_dict = ProbeTool.params_dict

    async def execute(self, actor_id: str):
        return ProbeTool().execute(actor_id)

@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["session_class", "edictum"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_provider_tool_loop_uses_each_shared_session_actor(tmp_path, provider_name, asynchronous):
    """实际 Provider, 会话工作流和工具注册表不得缓存上一位成员身份"""
    db_path = str(tmp_path / "sessions.db")
    if provider_name == "session_class":
        registry = SessionRegistry()
        registry.register("probe", AsyncSimpleSession if asynchronous else SimpleSession)
        provider: Any = SessionClassProvider(registry, default_checkpoint_db=db_path)
    else:
        types = create_default_edictum_type_registry()
        configs = EdictumConfigManager(types, tmp_path / "edictum.json")
        configs.create("probe", {"edictum_type": "async_simple" if asynchronous else "simple", "plugins": []})
        provider = EdictumProvider(configs, types, default_checkpoint_db=db_path)
    session = provider.create_session(
        _provider_call_origin_SessionConfig(session_id="shared-group", session_type_name="probe", provider_name=provider_name),
        llm=AsyncProbeLLM() if asynchronous else ProbeLLM(),
    )
    if provider_name == "edictum":
        if asynchronous:
            await provider.prepare_session_async(session)
        else:
            provider.prepare_session(session)
    session.add_tool(AsyncProbeTool() if asynchronous else ProbeTool())
    for actor in ["admin", "member"]:
        origin = CallOrigin("bot", "10", "group_message", "20", actor, actor, f"request-{actor}")
        call = UserCall(message="报告本轮身份", origin=origin)
        result = (await SessionManager._invoke_async_session(session, call)) if asynchronous else SessionManager._invoke_sync_session(session, call)
        assert f"bot/20/{actor}" in result
        assert "forged-admin" not in result
        assert current_call_origin() is None
    if provider_name == "edictum":
        if asynchronous:
            await provider.release_session_async(session)
        else:
            provider.release_session(session)


def _make_event(adapter: RecorderAdapter, session_id: str = "s1") -> MessageEvent:
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

def test_message_chain_contract():
    """消息链构造/迭代/拼接/布尔与协议字典转换"""
    chain = MessageChain.from_text("你好")
    assert len(chain) == 1
    assert bool(chain) is True
    assert MessageChain().__bool__() is False
    assert getattr(chain[0], "text", "") == "你好"
    assert chain.to_dict_list() == [{"type": "text", "data": {"text": "你好"}}]

    merged = chain + MessageChain.from_text("世界")
    assert len(merged) == 2
    assert [getattr(c, "text", "") for c in merged] == ["你好", "世界"]

def test_event_result_builders_contract():
    """结果构建器与事件结果控制: 文本/图片/停止/继续/日志/快速构建/清空"""
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

    from satrap.core.components import Plain as _Plain

    adapter = RecorderAdapter()
    event = _make_event(adapter)
    event.set_result("纯文本结果")
    event_result = event.get_result()
    assert event_result is not None
    assert event_result.chain and getattr(event_result.chain[0], "text", "") == "纯文本结果"

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
    chain_result = event.chain_result([_Plain(text="x")])
    assert len(chain_result.chain or []) == 1

    event.clear_result()
    assert event.get_result() is None

def test_event_accessors_contract():
    """会话标识/消息读取/LLM 请求默认值/扩展字段/唤醒开关读写"""
    adapter = RecorderAdapter()
    event = _make_event(adapter, session_id="s1")
    assert event.unified_msg_origin == "rec1:FriendMessage:s1"
    assert event.session_id == "s1"
    assert event.get_session_id() == "s1"
    event.unified_msg_origin = "onebot:GroupMessage:g1"
    assert event.session_id == "g1"
    event.session_id = "g2"
    assert event.session_id == "g2"

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

    req = event.request_llm(prompt="p", session_id="s1")
    assert req.prompt == "p"
    assert req.session_id == "s1"
    assert req.image_urls == []
    assert req.audio_urls == []
    assert req.contexts == []

    event.set_extra("k1", "v1")
    assert event.get_extra("k1") == "v1"
    assert event.get_extra("missing", "default") == "default"
    assert event.get_extra() == {"k1": "v1"}
    event.clear_extra()
    assert event.get_extra() == {}

    assert event.call_llm is True
    event.should_call_llm(False)
    assert event.call_llm is False

@pytest.mark.asyncio
async def test_event_message_outline():
    """消息摘要: 纯文本 / 图片 / 引用 混合"""
    from satrap.core.components import BaseMessageComponent, Image, Plain
    from satrap.core.type import MessageMember as _MM

    adapter = RecorderAdapter()
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

@pytest.mark.asyncio
async def test_event_send_records_and_flags():
    """发送消息记录到适配器并标记 has_send_operation"""
    adapter = RecorderAdapter()
    event = _make_event(adapter)
    assert event.has_send_operation() is False

    await event.send(MessageChain.from_text("回复"))
    assert event.has_send_operation() is True
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0] == "s1"

@pytest.mark.asyncio
async def test_event_send_failure_silently_caught():
    """适配器发送失败被捕获, 不冒泡"""
    class _BoomAdapter(RecorderAdapter):
        async def send_message(
        self, session_id: str, message: MessageChain, *, request_id: str = "",
        purpose: str = "business", require_tracking: bool = False,
    ) -> Any:
            raise RuntimeError("network down")

    adapter = _BoomAdapter()
    event = _make_event(adapter)
    await event.send(MessageChain.from_text("x"))
    assert event.has_send_operation() is False

def test_event_temporary_files():
    """临时文件跟踪与清理"""
    adapter = RecorderAdapter()
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

@pytest.mark.asyncio
async def test_event_process_buffer(monkeypatch: pytest.MonkeyPatch):
    """缓冲区分段提取: 匹配内容逐段发送, 返回剩余部分"""
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("satrap.core.platform.event.asyncio.sleep", no_sleep)

    adapter = RecorderAdapter()
    event = _make_event(adapter)
    rest = await event.process_buffer("【A】【B】tail", re.compile(r"【[^】]+】"))
    assert rest == "tail"
    assert len(adapter.sent) == 2
    assert getattr(adapter.sent[0][1].components[0], "text", "") == "【A】"

@pytest.mark.asyncio
async def test_event_process_buffer_stops_on_zero_width_match():
    """可匹配空串的正则不会发送空消息或进入死循环"""
    adapter = RecorderAdapter()
    event = _make_event(adapter)

    rest = await event.process_buffer("tail", re.compile(r".*?"))

    assert rest == "tail"
    assert adapter.sent == []
