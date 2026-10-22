"""业务链路场景与集中边界契约"""
from __future__ import annotations

import asyncio
from urllib.parse import urlencode
import json
import time
import pytest
from satrap.core.backend import control_server
from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.memory.scoped import ScopedMemories
from satrap.core.storage.layout import StorageLayout
from support.http import _request, _json_body, _use_config
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, NoReturn
import sqlite3
from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.memory.service import MemoryService
from satrap.core.memory.store import MemoryStore
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.type import LLMCallResponse
from support.group_reply import setup, restore_manager, SCOPE
from support.group_models import session as _memory_platform_session, Script, Invoker
from typing import Iterator, cast
from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallStreamEvent
from satrap.edictum import SimpleSession
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.edictum.memory_migration import migrate_memory_globals, migrate_memory_overrides
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.plugin_spec import parse_plugin_specs
from contextlib import closing
import threading
from satrap.core.memory.scoped import MemoryError
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.type import UserCall

@pytest.mark.asyncio
async def test_cold_memory_management_is_authenticated_and_revision_bound(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot:/a", "group", "群/中文")
    archive.record(scope, ArchiveMessage("source", "user:a", time.time(), "原始消息"))
    query = urlencode({"self_id": scope.self_id, "chat_id": scope.chat_id, "conversation_kind": "group"})
    root = "/api/platforms/future/memory/memories?" + query
    assert (await _request(root, authorized=False)).startswith(b"HTTP/1.1 401")
    payload = {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "idempotency_key": "create"}
    response = await _request(root, "POST", json.dumps(payload).encode())
    created = _json_body(response)
    assert created["status"] == "saved"
    memory_id = created["memory_id"]
    assert _json_body(await _request(root))["items"][0]["source_status"] == "operator"
    target = f"/api/platforms/future/memory/memories/{memory_id}?{query}"
    wrong = urlencode({"self_id": "other", "chat_id": scope.chat_id})
    assert (await _request(f"/api/platforms/future/memory/memories/{memory_id}?{wrong}")).startswith(b"HTTP/1.1 404")
    conflict = await _request(target, "PATCH", json.dumps({"content": "新内容", "expected_revision": 2, "idempotency_key": "update"}).encode())
    assert conflict.startswith(b"HTTP/1.1 409")
    delete = json.dumps({"expected_revision": 1, "idempotency_key": "delete"}).encode()
    assert _json_body(await _request(target, "DELETE", delete))["status"] == "deleted"
    assert _json_body(await _request(target, "DELETE", delete))["status"] == "deleted"
    assert archive.get(scope, "source")["text"] == "原始消息"

@pytest.mark.asyncio
async def test_approval_api_returns_applied_result(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    layout = StorageLayout(tmp_path / "data")
    layout.ensure_platform("future")
    monkeypatch.setattr(control_server, "_configured_storage_layout", lambda: layout)
    archive = PlatformMessageStore(layout.platform_db("future"), "future")
    scope = MessageScope("future", "bot", "group", "group")
    archive.record(scope, ArchiveMessage("source", "member", time.time(), "周五开会"))
    repository = ScopedMemories(archive, scope)
    proposal = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "source_message_ids": ["source"]},
                                 actor="member", current_message="source", operation_id="propose")
    query = urlencode({"self_id": "bot", "chat_id": "group"})
    root = f"/api/platforms/future/memory/proposals/{proposal['proposal_id']}/decision?{query}"
    result = _json_body(await _request(root, "POST", json.dumps({"approve": True, "expected_revision": 0}).encode()))
    assert result["status"] == "approved" and result["memory"]["content"] == "周五开会"

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_memory_plugin_works_without_group_chat_or_base_take(tmp_path, asynchronous):
    adapter, event, _ = setup(tmp_path)
    actor = event.call_origin.actor_id
    source = event.call_origin.source_message_id
    script = Script([
        LLMCallResponse("tools_call", "", tool_calls=[{"id": "remember", "name": "add_memory", "arguments": {
            "kind": "member_preference", "key": "preferred_name", "title": "称呼", "content": "称呼我为小明", "source_message_ids": [source]}}]),
        LLMCallResponse("message", "已记住你的称呼"),
        LLMCallResponse("message", "小明你好"),
    ])
    instance = await _memory_platform_session(tmp_path, script, asynchronous, False)
    if asynchronous:
        await instance.uninstall_plugin("group_chat")
    else:
        instance.uninstall_plugin("group_chat")
    instance.memory_db = str(adapter.message_archive.database)
    plugin_dir = str(Path(__file__).resolve().parents[2] / "satrap/expend/plugins/memory")
    if asynchronous:
        await instance.install_plugin(plugin_dir, config={"group_write_enabled": True})
    else:
        instance.install_plugin(plugin_dir, config={"group_write_enabled": True})
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(event)
    saved = ScopedMemories(adapter.message_archive, SCOPE).list()["items"]
    assert len(saved) == 1 and saved[0]["owner_user_id"] == actor
    assert saved[0]["source_message_ids"] == [source]
    assert {plugin.name for plugin in instance.list_plugins()} == {"memory"}
    _, next_event, _ = setup(tmp_path)
    await PipelineScheduler(Invoker(instance, asynchronous)).execute(next_event)
    assert "称呼我为小明" in str(script.requests[-1][0])
    assert "长期记忆资料" in str(script.requests[-1][0])


async def _platform_memory_command_session(tmp_path, asynchronous, private=False, config=None):
    """
    构建离线平台会话, 通过真实命令分发调用记忆插件

    参数:
    - tmp_path: 测试独占的临时目录, 用于隔离会话数据库与消息档案
    - asynchronous: 为 True 时构建异步会话, 为 False 时构建同步会话
    - private: 是否使用私聊来源, 可选, 默认为 False, 即使用群聊来源
    - config: 插件配置, 可选, 默认为 None; None 或空字典使用开启群记忆写入的配置

    返回:
    - 适配器, 消息事件, 离线模型脚本及异步命令调用函数; 模型脚本为空, 意外模型调用会失败
    """
    # Step.1 隔离平台来源和存储, 使用离线模型防止命令意外调用外部服务
    adapter, event, _ = setup(tmp_path, private=private)
    adapter._loop = asyncio.get_running_loop()
    script = Script([])
    instance = await _memory_platform_session(tmp_path, script, asynchronous, False)
    instance.memory_db = str(adapter.message_archive.database)
    plugin_dir = str(Path(__file__).resolve().parents[2] / "satrap/expend/plugins/memory")
    # Step.2 为对应会话类型安装插件, 复用生产代码的命令注册路径
    if asynchronous:
        await instance.install_plugin(plugin_dir, config=config or {"group_write_enabled": True})
    else:
        instance.install_plugin(plugin_dir, config=config or {"group_write_enabled": True})

    # Step.3 提供统一的异步驱动入口, 保留同步会话在工作线程执行的生产约束
    async def invoke(message):
        """
        异步驱动平台命令分发, 同步会话通过工作线程执行

        参数:
        - message: 包含命令前缀的完整输入文字, 来源固定为测试消息事件

        返回:
        - 会话返回的命令结果文字; 分发异常由 SessionManager 隔离并返回空文字
        """
        call = UserCall(session_id=instance.session_id, message=message, origin=event.call_origin)
        if asynchronous:
            return await SessionManager._invoke_async_session(instance, call)
        return await asyncio.to_thread(SessionManager._invoke_sync_session, instance, call)

    return adapter, event, script, invoke


@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("private", [False, True])
async def test_memory_commands_through_platform_session(tmp_path, asynchronous, private, caplog):
    """
    验证同步与异步群聊和私聊命令的记忆生命周期及帮助说明

    参数:
    - tmp_path: 测试独占的临时目录
    - asynchronous: 是否通过异步会话执行命令
    - private: 是否使用私聊来源, 为 False 时使用群聊来源
    - caplog: 日志捕获夹具, 用于确认正常命令没有同步桥接失败
    """
    # Step.1 检查命令简介和空记忆提示, 覆盖真实注册与分发路径
    adapter, event, script, invoke = await _platform_memory_command_session(tmp_path, asynchronous, private)
    help_text = await invoke("/help")
    assert "/memory: 管理当前范围内的长期记忆" in help_text
    assert "/memory: None" not in help_text
    initial = await invoke("/memory")
    assert initial == ("当前没有长期记忆" if private else "当前没有可查看的长期记忆")
    # Step.2 添加并查询记忆, 核对群范围的所有者, 来源和修订号
    added = await invoke("/memory add 称呼 称呼我为小明")
    assert "记忆已添加: [称呼] 称呼我为小明" in added
    listed = await invoke("/memory list")
    if private:
        assert "称呼我为小明" in listed
        memory_id = listed.split("- ")[1].split(" ")[0]
    else:
        items = ScopedMemories(adapter.message_archive, SCOPE).list()["items"]
        assert len(items) == 1 and items[0]["owner_user_id"] == event.call_origin.actor_id
        assert items[0]["source_message_ids"] == [event.call_origin.source_message_id]
        memory_id = items[0]["id"]
        assert f"- {memory_id} [称呼] 称呼我为小明 (我的偏好)" in listed
        assert ScopedMemories(adapter.message_archive, SCOPE).get(memory_id)["memory"]["revision"] == 1
    # Step.3 删除刚保存的记录, 确认结果文字与实际存储一致
    deleted = await invoke(f"/memory del {memory_id}")
    assert deleted == f"记忆已删除: {memory_id}"
    final = await invoke("/memory")
    assert final == initial
    # Step.4 检查群命令限制及调用边界, 确认不触发模型或桥接失败
    if not private:
        assert ScopedMemories(adapter.message_archive, SCOPE).list()["items"] == []
        assert "群内不能" in await invoke("/memory clear")
        assert "群内不能" in await invoke("/memory mode full")
    assert not script.requests
    assert not [record for record in caplog.records if "同步桥接失败" in record.getMessage()]


@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(("config", "error_message"), [
    ({"group_write_enabled": False}, "群记忆写入未在记忆插件中开启"),
    ({"group_write_enabled": True, "memory_mode": "base"}, "群记忆写入未在记忆插件中开启"),
    ({"group_write_enabled": True, "memory_mode": "disabled"}, "记忆功能已禁用"),
])
async def test_group_memory_commands_preserve_write_restrictions(tmp_path, asynchronous, config, error_message):
    """
    验证群命令在写入关闭或记忆模式受限时拒绝保存且不改变存储

    参数:
    - tmp_path: 测试独占的临时目录
    - asynchronous: 是否通过异步会话执行命令
    - config: 本场景限制写入的插件配置
    - error_message: 预期的宿主拒绝原因, 命令应以中文失败提示展示
    """
    # Step.1 安装受限配置, 通过生产命令入口验证写入限制
    adapter, _, _, invoke = await _platform_memory_command_session(tmp_path, asynchronous, config=config)
    result = await invoke("/memory add 称呼 称呼我为小明")
    # Step.2 核对拒绝原因与存储结果, 避免失败提示掩盖实际写入
    assert result == f"添加失败: {error_message}"
    assert ScopedMemories(adapter.message_archive, SCOPE).list()["items"] == []


@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_group_memory_commands_cannot_delete_other_members_preferences(tmp_path, asynchronous):
    """
    验证群命令删除他人偏好时保留原记录和修订号

    参数:
    - tmp_path: 测试独占的临时目录
    - asynchronous: 是否通过异步会话执行命令
    """
    # Step.1 通过可信管理入口准备其他成员的偏好, 与命令调用者身份区分
    adapter, _, _, invoke = await _platform_memory_command_session(tmp_path, asynchronous)
    repository = ScopedMemories(adapter.message_archive, SCOPE)
    saved = repository.mutate("create", {"kind": "member_preference", "owner_user_id": "789", "key": "name", "content": "乙的称呼"},
                              actor="operator", operator=True, operation_id="seed")
    # Step.2 检查越权删除的拒绝文字及存储结果
    result = await invoke(f"/memory del {saved['memory_id']}")
    assert result == "删除失败: 只能修改你本人在当前群的偏好"
    memory = repository.get(saved["memory_id"])["memory"]
    assert memory["content"] == "乙的称呼" and memory["revision"] == 1


@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_async_group_memory_command_cancels_before_writing(tmp_path, monkeypatch, failure):
    """
    验证异步群命令超时或被取消时终止宿主等待, 不产生延迟写入

    参数:
    - tmp_path: 测试独占的临时目录
    - monkeypatch: 替换宿主等待点和超时时间的夹具, 测试结束后自动恢复
    - failure: timeout 表示等待超时, cancel 表示命令任务被外部取消
    """
    from satrap.expend.plugins.memory import runtime

    # Step.1 在写入前阻塞来源核验, 观察宿主等待是否被取消
    adapter, _, _, invoke = await _platform_memory_command_session(tmp_path, True)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_scope = adapter.group_chat_scope

    async def delayed_scope(origin):
        """
        异步阻塞来源核验, 在等待结束或取消时标记退出

        参数:
        - origin: 当前命令绑定的可信来源, 放行后交给原核验逻辑

        返回:
        - 放行后返回原群范围; 等待期间被取消时继续抛出 CancelledError
        """
        entered.set()
        try:
            await release.wait()
            return await original_scope(origin)
        finally:
            cancelled.set()

    # Step.2 触发超时或外部取消, 核对失败提示及取消信号
    monkeypatch.setattr(adapter, "group_chat_scope", delayed_scope)
    monkeypatch.setattr(runtime, "HOST_TIMEOUT_SECONDS", 0.05 if failure == "timeout" else 30)
    task = asyncio.create_task(invoke("/memory add 称呼 称呼我为小明"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    if failure == "timeout":
        result = await asyncio.wait_for(task, timeout=2)
        assert result == "记忆操作超时, 已取消等待"
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # Step.3 放行原等待点, 确认已结束的命令没有继续写入
    assert cancelled.is_set()
    release.set()
    await asyncio.sleep(0)
    assert ScopedMemories(adapter.message_archive, SCOPE).list()["items"] == []

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_group_write_switch_and_main_workflow_are_checked(tmp_path):
    adapter, event, _ = setup(tmp_path)
    workflow = object()
    instance: Any = SimpleNamespace(_wf=workflow)
    service = MemoryService(MemoryStore(db_path=adapter.message_archive.database, scope="session:test"), session=instance)
    values = {"kind": "member_preference", "key": "name", "title": "称呼", "content": "小明", "source_message_ids": [event.call_origin.source_message_id]}
    with bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        disabled = await service.group_operation("create", values, access=lambda: {"memory_mode": "full", "group_write_enabled": False})
        assert disabled["error_type"] == "write_disabled" and disabled["ok"] is False
        with bind_tool_workflow(object()):
            child = await service.group_operation("create", values, access=lambda: {"memory_mode": "full", "group_write_enabled": True})
        assert child["error_type"] == "read_only_workflow"
        spoof = await service.group_operation("create", {**values, "owner_user_id": "other"}, access=lambda: {"memory_mode": "full", "group_write_enabled": True})
        assert spoof["error_type"] == "invalid_argument"
    assert ScopedMemories(adapter.message_archive, SCOPE).list()["items"] == []

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.parametrize('change', ['route', 'account', 'disabled'])
def test_context_injection_rechecks_identity_after_database_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 caplog: pytest.LogCaptureFixture, change: str) -> None:
    adapter, event, _ = setup(tmp_path)
    origin = replace(event.call_origin, conversation_kind='group', conversation_id=SCOPE.chat_id)
    repository = ScopedMemories(adapter.message_archive, SCOPE)
    repository.mutate('create', {'kind': 'group_rule', 'key': 'rule', 'content': '旧范围记忆正文'}, actor='operator', operator=True, operation_id='create')
    original = ScopedMemories.list

    def changed_after_read(self: ScopedMemories, *, kind: str = "", user_id: str = "", keyword: str = "",
                           limit: int = 20, cursor: str = "", viewer: str = "") -> dict[str, Any]:
        result = original(self, kind=kind, user_id=user_id, keyword=keyword, limit=limit, cursor=cursor, viewer=viewer)
        if kind == 'member_preference':
            if change == 'route':
                adapter._agent_route_memory[(SCOPE.self_id, 'group', SCOPE.chat_id)] = ((), 1)
            elif change == 'account':
                adapter.bot_self_id = adapter.client_self_id = 'different-account'
            else:
                adapter.config.enable = False
        return result

    monkeypatch.setattr(ScopedMemories, 'list', changed_after_read)
    service = MemoryService(MemoryStore(db_path=adapter.message_archive.database, scope='unused'))
    with bind_call_origin(origin):
        assert service.group_context_sync(access=lambda: {'memory_mode': 'base'}) == ''
    records = [record for record in caplog.records if '注入来源核验失败' in record.getMessage()]
    assert [record.levelname for record in records] == ['WARNING']

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.parametrize('failure', ['storage', 'unexpected'])
def test_context_injection_degrades_with_matching_log_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           caplog: pytest.LogCaptureFixture, failure: str) -> None:
    """存储不可用只记录警告, 非预期异常记录完整堆栈, 两者都降级为不注入"""
    adapter, event, _ = setup(tmp_path)
    origin = replace(event.call_origin, conversation_kind='group', conversation_id=SCOPE.chat_id)
    archive = adapter.message_archive
    assert archive is not None

    def broken(self: ScopedMemories, **values: object) -> NoReturn:
        if failure == 'storage':
            raise sqlite3.OperationalError('database is locked')
        raise RuntimeError('模拟未预期损坏')

    monkeypatch.setattr(ScopedMemories, 'list', broken)
    service = MemoryService(MemoryStore(db_path=archive.database, scope='unused'))
    with bind_call_origin(origin):
        assert service.group_context_sync(access=lambda: {'memory_mode': 'base'}) == ''
    records = {record.levelname: record.getMessage() for record in caplog.records if '群记忆' in record.getMessage()}
    if failure == 'storage':
        assert 'WARNING' in records and '记忆存储不可用' in records['WARNING'] and 'Traceback' not in records['WARNING']
    else:
        assert 'ERROR' in records and '本轮记忆注入失败' in records['ERROR'] and 'Traceback' in records['ERROR']

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "memory"

CODING_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "satrap_coding"

class _FakeLLM(LLM):
    """最小同步 fake LLM"""

    def __init__(self) -> None:
        pass

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content="回复")

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        yield LLMCallStreamEvent(kind="content_delta", delta="流")

@pytest.fixture
def _memory_plugin_session(tmp_path: Any, monkeypatch: Any) -> SimpleSession:
    """隔离数据目录的会话 + 安装 memory 插件"""
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    from satrap.expend.plugins.memory import state as state_mod
    from satrap.core.memory import store as ms_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))
    # 隔离: 插件配置目录 + 记忆 db + 沙箱根
    monkeypatch.setattr(ms_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    monkeypatch.setattr(state_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")

    s = SimpleSession("conv-1", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(PLUGIN_DIR))
    return s

def test_memory_mode_config_readonly(tmp_path: Any, monkeypatch: Any):
    """memory_mode=base: 写工具被拒绝; disabled: 注入块为空"""
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    from satrap.expend.plugins.memory import state as state_mod
    from satrap.core.memory import store as ms_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))
    monkeypatch.setattr(ms_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    monkeypatch.setattr(state_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")

    s = SimpleSession("conv-mode", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(PLUGIN_DIR), config={
        "memory_mode": "base",
    })

    from satrap.expend.plugins.memory.state import get_plugin_state
    store = get_plugin_state(s)["store"]
    assert store.mode == "base"
    assert store.can_write() is False

    tm = s._wf.tools_manager
    # base 只读: 写工具返回扁平失败与"只读"提示
    add = tm.tools["add_memory"]
    readonly = add.execute(title="偏好", content="x")
    assert readonly["ok"] is False and readonly["error_type"] == "read_only" and "只读" in readonly["error"]

    store.set_mode("disabled")
    # disabled 模式: 不注入 (注入块为空), 写拒绝文案按实际模式生成
    assert store.to_context_block() == ""
    offline = add.execute(title="偏好", content="x")
    assert offline["ok"] is False and offline["error_type"] == "memory_disabled"
    assert "已禁用" in offline["error"] and "disabled" in offline["error"]

def test_memory_tools_lifecycle(_memory_plugin_session: SimpleSession):
    """memory 工具: 添加 -> 列表 -> 更新 -> 删除"""
    tm = _memory_plugin_session._wf.tools_manager
    add = tm.tools["add_memory"]
    result = add.execute(title="偏好", content="喜欢简洁回答", tags=["style"], importance=3)
    assert "已添加" in result

    list_tool = tm.tools["list_memories"]
    listed = list_tool.execute()
    assert "偏好" in listed and "喜欢简洁回答" in listed

    from satrap.expend.plugins.memory.state import get_plugin_state
    # 取记忆 ID 前缀
    store = get_plugin_state(_memory_plugin_session)["store"]
    mem_id = store.list_all()[0]["id"]

    update = tm.tools["update_memory"]
    assert "已更新" in update.execute(memory_id=mem_id, content="喜欢极简回答")

    delete = tm.tools["delete_memory"]
    assert "已删除" in delete.execute(memory_id=mem_id)
    assert "没有" in list_tool.execute()

def test_memory_command_lifecycle(_memory_plugin_session: SimpleSession):
    """/memory 命令: 注册 -> list/add/del/mode 全流程"""
    cmd = _memory_plugin_session.cmd_handler.commands["memory"]

    assert "没有长期记忆" in cmd()
    assert "已添加" in cmd("add", "偏好", "喜欢简洁回答")
    listed = cmd()
    assert "偏好" in listed and "喜欢简洁回答" in listed

    mem_id = listed.split("- ")[1].split(" ")[0]
    assert "已删除" in cmd("del", mem_id[:8])   # 支持 ID 前缀

    assert "已切换: base" in cmd("mode", "base")
    assert "只读模式" in cmd("add", "x", "y")
    assert "只读模式" in cmd("clear")
    assert "已切换: full" in cmd("mode", "full")
    assert "用法" in cmd("unknown")

def test_memory_inject_handler(_memory_plugin_session: SimpleSession):
    """记忆注入 handler: 添加记忆后注入到用户输入"""
    from satrap.expend.plugins.memory.state import get_plugin_state
    state = get_plugin_state(_memory_plugin_session)
    store = state["store"]
    store.add("项目约定", "回复用中文", importance=5)

    _memory_plugin_session.run("你好")
    injected = next(message["content"] for message in reversed(_memory_plugin_session.ctx.get_context()) if message["role"] == "user")
    assert "长期记忆" in injected
    assert "项目约定" in injected
    assert "回复用中文" in injected

    store.add("新增约定", "回答附上示例", importance=5)
    _memory_plugin_session.run("再问一次")
    updated = next(message["content"] for message in reversed(_memory_plugin_session.ctx.get_context()) if message["role"] == "user")
    assert "回答附上示例" in updated

    store.clear()
    _memory_plugin_session.run("清空后")
    cleared = next(message["content"] for message in reversed(_memory_plugin_session.ctx.get_context()) if message["role"] == "user")
    assert cleared == "清空后"

def test_memory_writes_not_blocked_by_plan_mode(tmp_path: Any, monkeypatch: Any):
    """计划模式限制工作区文件写入和 Shell; 记忆是元信息, 增删改不受拦截"""
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    from satrap.expend.plugins.memory import state as state_mod
    from satrap.core.memory import store as ms_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))
    monkeypatch.setattr(ms_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    monkeypatch.setattr(state_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    (tmp_path / "workspace").mkdir()

    s = SimpleSession("conv-plan", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(CODING_PLUGIN_DIR), config={
        "workspace_root": str(tmp_path / "workspace"),
        "data_root": str(tmp_path / "coding"),
    })
    s.install_plugin(str(PLUGIN_DIR))

    assert "已进入计划模式" in s.cmd_handler.commands["plan"]("on")

    add = s._wf.tools_manager.tools["add_memory"]
    # 计划模式下记忆写不被拦截: 工具与命令均可写
    assert "已添加" in add.execute(title="决策", content="用 SQLite", tags=[], importance=1)
    assert "已添加" in s.cmd_handler.commands["memory"]("add", "偏好", "中文回复")

def test_memory_tool_failures_use_flat_envelope(_memory_plugin_session: SimpleSession):
    """记忆工具失败统一为框架扁平结果, 宿主失败补上工具名"""
    tm = _memory_plugin_session._wf.tools_manager
    missing = tm.tools["get_memory"].execute(memory_id="不存在的记忆")
    assert missing == {"ok": False, "error": "记忆不存在: 不存在的记忆", "error_type": "not_found", "tool_name": "get_memory"}
    blank = tm.tools["add_memory"].execute(title="  ", content="  ")
    assert blank["ok"] is False and blank["error_type"] == "invalid_argument" and blank["tool_name"] == "add_memory"
    # 群聊宿主结果只补工具名与统一键名, 成功结果与文本原样返回
    from satrap.expend.plugins.memory.tools.sync import ListMemoriesTool
    host = cast(ListMemoriesTool, tm.tools["list_memories"])
    assert host._visible({"ok": False, "error": "记忆操作超时, 已取消等待", "error_type": "unavailable"}) == {
        "ok": False, "error": "记忆操作超时, 已取消等待", "error_type": "unavailable", "tool_name": "list_memories"}
    assert host._visible({"ok": True, "items": []}) == {"ok": True, "items": []}
    assert host._visible("当前没有长期记忆") == "当前没有长期记忆"

@pytest.fixture
def catalog(tmp_path):
    return PluginCatalog(preset_dir=Path(__file__).resolve().parents[2] / "satrap/expend/plugins", user_dir=tmp_path / "plugins")

def test_split_preserves_closed_capabilities_and_is_idempotent(catalog):
    original = [{"name": "base_take", "config": {"memory_mode": "base", "search_timeout": 12}, "capabilities": {
        "tools": {"add_memory": False, "list_memories": True},
        "handlers": {"base_take.memory_inject": False}, "commands": {"memory": False}}}]
    specs = parse_plugin_specs(original, catalog, require_available=True)
    base, memory = specs
    assert base.name == "base_take" and memory.name == "memory"
    assert base.config == {"search_timeout": 12}
    assert set(base.capabilities["tools"]) == {"search", "fetch_page", "code_sandbox", "read_document", "read_media", "send_file"}
    assert memory.config == {"memory_mode": "base"}
    assert memory.capabilities["tools"]["add_memory"] is False
    assert memory.capabilities["tools"]["get_memory"] is False
    assert memory.capabilities["handlers"]["memory.memory_inject"] is False
    assert memory.capabilities["commands"]["memory"] is False
    assert not any(memory.capabilities["skills"].values())
    serialized = [spec.to_config() for spec in specs]
    assert [spec.to_config() for spec in parse_plugin_specs(serialized, catalog)] == serialized
    assert original[0]["config"]["memory_mode"] == "base"
    assert len(parse_plugin_specs([base.to_config()], catalog)) == 1

def test_disabled_legacy_parent_cannot_enable_memory(catalog):
    specs = parse_plugin_specs([{"name": "base_take", "enabled": False, "config": {"memory_mode": "full"}}], catalog)
    memory = specs[1]
    assert memory.enabled is False
    assert not any(memory.capabilities["tools"].values())
    assert not any(memory.capabilities["handlers"].values())

def test_existing_memory_config_wins_but_closed_capabilities_stay_closed(catalog):
    specs = parse_plugin_specs([
        {"name": "base_take", "config": {"memory_mode": "full"}, "capabilities": {"tools": {"delete_memory": False}}},
        {"name": "memory", "config": {"memory_mode": "disabled"}, "capabilities": {"tools": {"add_memory": False}}},
    ], catalog)
    memory = specs[1]
    assert memory.config["memory_mode"] == "disabled"
    assert memory.capabilities["tools"]["delete_memory"] is False
    assert memory.capabilities["tools"]["add_memory"] is False

def test_global_migration_preserves_unknown_base_fields_and_new_explicit_values(tmp_path, catalog):
    manager = PluginConfigManager(tmp_path)
    source = tmp_path / "base_take.json"
    target = tmp_path / "memory.json"
    source.write_text(json.dumps({"memory_mode": "base", "memory_scope": "isolated", "search_timeout": 17}), encoding="utf-8")
    target.write_text(json.dumps({"memory_mode": "disabled"}), encoding="utf-8")
    entry = catalog.get("memory")
    manager.load_global("memory", entry.config_schema)
    assert json.loads(source.read_text(encoding="utf-8")) == {"search_timeout": 17}
    expected = {"memory_mode": "disabled", "memory_scope": "isolated"}
    assert json.loads(target.read_text(encoding="utf-8")) == expected
    manager.load_global("memory", entry.config_schema)
    assert json.loads(target.read_text(encoding="utf-8")) == expected

def test_bad_new_global_file_does_not_remove_old_fields(tmp_path, catalog):
    source = tmp_path / "base_take.json"
    source.write_text('{"memory_mode":"base"}', encoding="utf-8")
    (tmp_path / "memory.json").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        PluginConfigManager(tmp_path).load_global("memory", catalog.get("memory").config_schema)
    assert json.loads(source.read_text(encoding="utf-8"))["memory_mode"] == "base"

@pytest.mark.parametrize("failed_name", ["memory.json", "base_take.json"])
def test_global_migration_replace_failure_keeps_source_and_can_resume(tmp_path, monkeypatch, failed_name):
    """原子写入失败不丢旧字段, 目标已写入时再次迁移仍收敛到同一结果"""
    from satrap.core.storage import persist

    manager = PluginConfigManager(tmp_path)
    source, target = tmp_path / "base_take.json", tmp_path / "memory.json"
    old = {"memory_mode": "base", "memory_scope": "isolated", "search_timeout": 17, "description": "保留字段"}
    source.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    target.write_text('{"memory_mode":"disabled"}', encoding="utf-8")
    replace = persist.os.replace

    def fail_replace(temporary, destination):
        if Path(destination).name == failed_name:
            raise OSError("模拟原子替换失败")
        return replace(temporary, destination)

    with monkeypatch.context() as patch:
        patch.setattr(persist.os, "replace", fail_replace)
        with pytest.raises(OSError, match="模拟原子替换失败"):
            migrate_memory_globals(manager)
    assert json.loads(source.read_text(encoding="utf-8")) == old
    expected = {"memory_mode": "disabled"}
    if failed_name == "base_take.json":
        expected["memory_scope"] = "isolated"
    assert json.loads(target.read_text(encoding="utf-8")) == expected
    assert not list(tmp_path.glob("*.tmp"))
    migrate_memory_globals(manager)
    assert json.loads(source.read_text(encoding="utf-8")) == {"search_timeout": 17, "description": "保留字段"}
    assert json.loads(target.read_text(encoding="utf-8")) == {"memory_mode": "disabled", "memory_scope": "isolated"}

def test_session_override_migration_is_transactional_and_preserves_revision(tmp_path):
    store = SessionOverrideStore(tmp_path / "p.db")
    store.replace("s", "plugins.base_take", {"memory_mode": "full", "memory_scope": "isolated", "search_timeout": 8}, expected_revision=0)
    store.replace("s", "plugins.memory", {"memory_mode": "disabled"}, expected_revision=0)
    assert migrate_memory_overrides(store, "s") is True
    assert store.read("s", "plugins.base_take")["overrides"] == {"search_timeout": 8}
    record = store.read("s", "plugins.memory")
    assert record["overrides"] == {"memory_mode": "disabled", "memory_scope": "isolated"}
    assert record["revision"] == 2
    assert migrate_memory_overrides(store, "s") is False
    assert store.read("s", "plugins.memory")["revision"] == 2

def test_existing_memory_records_keep_their_identifiers(tmp_path):
    database = tmp_path / "p.db"
    record = MemoryStore(db_path=database, scope="session:s").add("偏好", "简洁回答")
    reopened = MemoryStore(db_path=database, scope="session:s")
    assert reopened.get(record["memory_id"])["content"] == "简洁回答"
    assert MemoryStore(db_path=database, scope="session:another").list_all() == []

@pytest.fixture
def memories(tmp_path: Path) -> tuple[ScopedMemories, list[float]]:
    clock = [1_800_000_000.0]
    archive = PlatformMessageStore(tmp_path / "platform.db", "platform:custom", clock=lambda: clock[0])
    scope = MessageScope("platform:custom", "account:bot", "group", "group/特殊")
    for identity, sender in (("m1", "user:a"), ("m2", "user:a"), ("m3", "user:b")):
        archive.record(scope, ArchiveMessage(identity, sender, clock[0], "明确要求记住"))
    return ScopedMemories(archive, scope), clock

def add_preference(repository, *, owner="user:a", source="m1", key="preferred_name", operation="first"):
    return repository.mutate("create", {"kind": "member_preference", "key": key, "title": "称呼", "content": "称呼我为小明", "source_message_ids": [source]},
                             actor=owner, current_message=source, operation_id=operation)

def test_owner_identity_revision_and_listing_are_enforced(memories):
    """所有者由宿主注入: 模块不能冒充, 更新需本人且版本最新, 列表按查看者收窄"""
    repository, _ = memories
    record = add_preference(repository)["memory"]
    assert record["owner_user_id"] == "user:a" and record["revision"] == 1
    spoof = repository.mutate("create", {"kind": "member_preference", "key": "name", "title": "称呼", "content": "小明", "owner_user_id": "user:b", "source_message_ids": ["m1"]},
                              actor="user:a", current_message="m1", operation_id="owner-spoof")
    assert spoof["memory"]["owner_user_id"] == "user:a"
    values = {"memory_id": record["memory_id"], "content": "简短回答", "source_message_ids": ["m3"], "expected_revision": 1}
    with pytest.raises(MemoryError, match="本人"):
        repository.mutate("update", values, actor="user:b", current_message="m3", operation_id="bad-owner")
    values["source_message_ids"] = ["m2"]
    changed = repository.mutate("update", values, actor="user:a", current_message="m2", operation_id="good-update")
    assert changed["memory"]["revision"] == 2
    with pytest.raises(MemoryError, match="已更新"):
        repository.mutate("update", values, actor="user:a", current_message="m2", operation_id="stale-update")
    add_preference(repository, owner="user:b", source="m3", operation="other")
    assert {item["owner_user_id"] for item in repository.list(viewer="user:a")["items"]} == {"user:a"}

def test_source_and_delete_must_bind_to_current_request_and_owner(memories):
    """来源必须属于本人且包含本轮; 删除只作用于本轮请求且不删归档历史"""
    repository, _ = memories
    values = {"kind": "member_preference", "key": "style", "title": "风格", "content": "简洁", "source_message_ids": ["m1"]}
    with pytest.raises(MemoryError, match="本轮"):
        repository.mutate("create", values, actor="user:a", current_message="m2", operation_id="old-source")
    with pytest.raises(MemoryError, match="发言"):
        repository.mutate("create", values, actor="user:b", current_message="m1", operation_id="wrong-source")
    memory = add_preference(repository)["memory"]
    delete_values = {"memory_id": memory["memory_id"], "expected_revision": 1, "request_message_id": "m1"}
    with pytest.raises(MemoryError, match="本轮"):
        repository.mutate("delete", delete_values, actor="user:a", current_message="m2", operation_id="old-delete")
    delete_values["request_message_id"] = "m2"
    assert repository.mutate("delete", delete_values, actor="user:a", current_message="m2", operation_id="delete")["status"] == "deleted"
    assert repository.archive.get(repository.scope, "m1")["text"] == "明确要求记住"
    assert repository.list()["items"] == []

def test_idempotency_does_not_duplicate_or_overwrite(memories):
    repository, _ = memories
    first = add_preference(repository)
    again = add_preference(repository)
    assert first["memory"]["memory_id"] == again["memory"]["memory_id"]
    existing = add_preference(repository, operation="another-request")
    assert existing["status"] == "already_exists"
    assert len(repository.list()["items"]) == 1
    with pytest.raises(MemoryError, match="不同内容"):
        add_preference(repository, key="changed", operation="first")

def test_group_rule_is_pending_until_transactionally_approved(memories):
    repository, _ = memories
    proposal = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "source_message_ids": ["m1"]},
                                 actor="user:a", current_message="m1", operation_id="rule")
    assert proposal["status"] == "pending"
    assert repository.list()["items"] == []
    result = repository.decide(proposal["proposal_id"], True, 0)
    assert result["status"] == "approved"
    assert repository.list()["items"][0]["content"] == "周五开会"
    assert repository.decide(proposal["proposal_id"], True, 0)["status"] == "approved"

def test_pending_update_cannot_overwrite_newer_operator_decision(memories):
    repository, _ = memories
    created = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会"}, actor="operator", operator=True, operation_id="operator-create")
    memory_id = created["memory_id"]
    proposal = repository.mutate("update", {"memory_id": memory_id, "expected_revision": 1, "content": "周六开会", "source_message_ids": ["m1"]},
                                 actor="user:a", current_message="m1", operation_id="rule-update")
    repository.mutate("update", {"memory_id": memory_id, "expected_revision": 1, "content": "周日开会"}, actor="operator", operator=True, operation_id="operator-update")
    assert repository.decide(proposal["proposal_id"], True, 1)["status"] == "conflicted"
    assert repository.get(memory_id)["memory"]["content"] == "周日开会"

def test_source_deleted_keeps_saved_memory_but_blocks_pending_approval(memories):
    repository, _ = memories
    preference = add_preference(repository)["memory"]
    pending = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "source_message_ids": ["m1"]},
                                actor="user:a", current_message="m1", operation_id="rule")
    repository.archive.recall(repository.scope, "m1")
    assert repository.get(preference["memory_id"])["memory"]["source_status"] == "unavailable"
    with pytest.raises(MemoryError, match="来源不可用"):
        repository.decide(pending["proposal_id"], True, 0)
    assert repository.proposals()["items"][0]["state"] == "pending"

def test_expired_proposal_and_foreign_scope_cursor(memories):
    repository, clock = memories
    add_preference(repository)
    add_preference(repository, key="style", operation="second")
    page = repository.list(limit=1)
    other_scope = MessageScope(repository.scope.adapter_id, repository.scope.self_id, "group", "other")
    other = ScopedMemories(repository.archive, other_scope)
    with pytest.raises(MemoryError, match="重新查询"):
        other.list(limit=1, cursor=page["next_cursor"])
    with pytest.raises(MemoryError, match="不属于"):
        other.get(page["items"][0]["memory_id"])
    proposal = repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会", "source_message_ids": ["m1"]},
                                 actor="user:a", current_message="m1", operation_id="rule")
    clock[0] += 86401
    assert repository.decide(proposal["proposal_id"], True, 0)["status"] == "expired"

def reference_rows(repository: ScopedMemories, *, kind: str = "", user_id: str = "", keyword: str = "", viewer: str = "") -> list[sqlite3.Row]:
    """改动前的逐行筛选参照实现, 仅用于对照 SQL 侧筛选"""
    with closing(repository.store._connect()) as connection:
        rows = connection.execute("SELECT * FROM memories WHERE scope=? ORDER BY importance DESC, updated_at DESC, id",
                                  (repository.scope.key,)).fetchall()
    return [row for row in rows if (not viewer or row["kind"] == "group_rule" or row["owner_user_id"] == viewer)
            and (not kind or row["kind"] == kind)
            and (not user_id or row["kind"] == "member_preference" and row["owner_user_id"] == user_id)
            and (not keyword or keyword.casefold() in (row["title"] + "\n" + row["content"]).casefold())]

def fill_mixed_memories(repository: ScopedMemories) -> None:
    """铺两位成员的偏好与一条已应用群记忆, 覆盖全部筛选维度"""
    add_preference(repository)
    add_preference(repository, owner="user:b", source="m3", operation="other")
    repository.mutate("create", {"kind": "group_rule", "key": "meeting", "title": "会议", "content": "周五开会 Meeting",
                                 "source_message_ids": ["m1"]}, actor="user:a", current_message="m1", operator=True, operation_id="rule")

@pytest.mark.parametrize("filters", [{}, {"kind": "group_rule"}, {"kind": "member_preference"}, {"user_id": "user:a"},
                                     {"viewer": "user:b"}, {"keyword": "称呼"}, {"keyword": "meeting"},
                                     {"user_id": "user:a", "keyword": "小明"}])
def test_list_filters_and_pagination_match_reference(memories: tuple[ScopedMemories, list[float]], filters: dict[str, str]) -> None:
    repository, _ = memories
    fill_mixed_memories(repository)
    collected: list[str] = []
    cursor = ""
    while True:
        response = repository.list(limit=1, cursor=cursor, **filters)
        collected.extend(item["memory_id"] for item in response["items"])
        cursor = response["next_cursor"] or ""
        if not cursor:
            break
    assert collected == [row["id"] for row in reference_rows(repository, **filters)]

@pytest.mark.parametrize("write", ["update", "delete"])
def test_list_page_and_cursor_come_from_one_read_snapshot(memories: tuple[ScopedMemories, list[float]], write: str,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """签名与当页正文出自同一读事务: 两次查询之间的并发写入不改变本页, 提交后旧游标失效"""
    repository, _ = memories
    add_preference(repository)
    add_preference(repository, owner="user:b", source="m3", operation="other")
    before = repository.list(limit=1)
    target_id = before["items"][0]["memory_id"]

    real_offset = ScopedMemories._offset
    writer: list[threading.Thread] = []
    errors: list[Exception] = []

    def concurrent_write() -> None:
        try:
            values: dict[str, object] = {"memory_id": target_id, "expected_revision": 1}
            if write == "update":
                values["content"] = "并发改写的新正文"
            repository.mutate(write, values, actor="operator", operator=True, operation_id=f"concurrent-{write}")
        except Exception as exc:  # 写入线程的异常必须显式带出
            errors.append(exc)

    def hooked_offset(cursor: str, signature: str) -> int:
        offset = real_offset(cursor, signature)
        if not writer:
            thread = threading.Thread(target=concurrent_write)
            writer.append(thread)
            thread.start()
            thread.join(0.5)
        return offset

    monkeypatch.setattr(ScopedMemories, "_offset", staticmethod(hooked_offset))
    raced = repository.list(limit=1)
    writer[0].join()
    assert not errors
    # 有判别力的是条目: 修复前并发写入的新正文或删除造成的错位会进入本页; 游标签名在写入前已算定, 两种实现下一致
    assert json.dumps(raced["items"], ensure_ascii=False, sort_keys=True) == json.dumps(before["items"], ensure_ascii=False, sort_keys=True)
    assert raced["next_cursor"] == before["next_cursor"] and raced["has_more"] == before["has_more"]
    with pytest.raises(MemoryError, match="重新查询"):
        repository.list(limit=1, cursor=before["next_cursor"])
