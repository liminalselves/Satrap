"""display 层 ChatPluginRegistry / ChatService 单元测试"""
from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
from pathlib import Path
import pytest
from typing import Any, cast
from types import SimpleNamespace
import copy
import json
import yaml
import threading

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.model_snapshot import ModelConfigSnapshot
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.memory.store import MemoryStore
from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.display.recorder import DisplayRecorder, list_conversations, get_conversation_meta
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.service import ChatService
from satrap.display.server import ChatHTTPServer
from satrap.edictum.plugin import load_plugin_meta
from satrap.edictum.plugin_apply import PluginApplySnapshot
from satrap.core.storage import StorageLayout
from satrap.core.memory import store as ms_mod
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, LLMConfig, EmbeddingConfig, ReRankConfig, ASRConfig
from satrap.display import service as service_mod


def _must_conversation(service: ChatService, conversation_id: str):
    """取必须存在的会话运行时状态"""
    conv = service.get_conversation(conversation_id)
    if conv is None:
        raise AssertionError(f"会话不存在: {conversation_id}")
    return conv


async def _wait_conversation_task(service: ChatService, conversation_id: str) -> None:
    """等待会话当前任务结束 (会话与任务必须存在)"""
    task = _must_conversation(service, conversation_id).task
    if task is None:
        raise AssertionError(f"会话无进行中任务: {conversation_id}")
    await task


@pytest.mark.asyncio
async def test_actual_chat_application_reports_lock_and_partial_failure(tmp_path: Path, monkeypatch: Any) -> None:
    """实际 Chat 会话等待原锁, 来源撤销时任务显示失败并保留原部分失败结果"""
    manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: manager)
    monkeypatch.setenv("SATRAP_API_TOKEN", "test-chat-application-token-at-least-thirty-two-characters")
    _write_chat_commands(tmp_path, monkeypatch)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("chat_commands", True)
    server = ChatHTTPServer(svc, port=0)
    key = "actual-chat-operation-001"
    conv = None
    try:
        cid = await svc.create_conversation(model="default")
        conv = _must_conversation(svc, cid)
        old_handle = conv.session.list_plugins()[0]
        entry = svc._plugins.catalog.get("chat_commands")
        if entry is None:
            raise AssertionError("测试插件未加载")
        manager.save_global("chat_commands", entry.config_schema, {"about_text": "第一次快照"})
        await conv.plugin_lock.acquire()
        status, result = await server._route("POST", "/api/chat/plugins/reconcile", json.dumps({"operation_id": key, "plugin_name": "chat_commands"}).encode("utf-8"))
        assert status == 202
        for _attempt in range(100):
            status, result = await server._route("GET", f"/api/runtime-operations/{key}", b"")
            if result["instances"] and result["instances"][0]["phase"] == "waiting_lock":
                break
            await asyncio.sleep(0.01)
        assert status == 200 and result["instances"][0]["session_id"] == cid and result["instances"][0]["phase"] == "waiting_lock"
        assert conv.session.list_plugins()[0] is old_handle
        manager.save_global("chat_commands", entry.config_schema, {"about_text": "仅重试才能使用的新参数"})
        conv.plugin_lock.release()
        for _attempt in range(100):
            status, result = await server._route("GET", f"/api/runtime-operations/{key}", b"")
            if result["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert result["status"] == "failed" and result["result"]["ok"] and not result["result"]["applied"]
        assert result["instances"][0]["status"] == "failed" and result["result"]["failed"] == 1
        assert conv.session.list_plugins()[0] is old_handle
        assert "等待期间已变化" in result["result"]["sessions"][0]["error"]
        retried = await svc.reconcile_plugins(plugin_name="chat_commands")
        assert retried["applied"] and await conv.session.run("/about") == "仅重试才能使用的新参数"
    finally:
        if conv is not None and conv.plugin_lock.locked():
            conv.plugin_lock.release()
        operation = server.applications._operations.get(key)
        if operation is not None and operation.task is not None:
            await asyncio.wait_for(asyncio.shield(operation.task), 4)
        await svc.close()


@pytest.mark.asyncio
async def test_chat_data_edit_updates_live_context_without_rewriting_display(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    try:
        cid = await service.create_conversation()
        await service.send(cid, "原始输入")
        await _wait_conversation_task(service, cid)
        conversation = _must_conversation(service, cid)
        context_id = conversation.session.ctx.conversation_id
        server = ChatHTTPServer(service)
        request = {"conversation_id": context_id, "layer": "context"}
        status, current = await server._route("POST", "/api/chat/conversation-data", json.dumps(request).encode("utf-8"))
        assert status == 200
        assert current["source"] == "memory"
        edited_index = next(item["index"] for item in current["items"] if item["role"] == "user")
        status, saved = await server._route("POST", "/api/chat/conversation-data", json.dumps({**request, "action": "edit", "index": edited_index, "content": "模型上下文输入", "expected_revision": current["revision"]}).encode("utf-8"))
        assert status == 200 and saved["saved"]
        assert conversation.session.ctx.get_context()[edited_index]["content"] == "模型上下文输入"
        assert conversation.recorder.list_turns()[0]["user_input"] == "原始输入"
        history_request = {"conversation_id": cid, "layer": "history"}
        _, history = await server._route("POST", "/api/chat/conversation-data", json.dumps(history_request).encode("utf-8"))
        status, _ = await server._route("POST", "/api/chat/conversation-data", json.dumps({**history_request, "action": "edit", "index": 0, "user_input": "只修改展示", "answer": "展示答案", "expected_revision": history["revision"]}).encode("utf-8"))
        assert status == 200
        assert conversation.recorder.list_turns()[0]["answer"] == "展示答案"
        assert conversation.session.ctx.get_context()[edited_index]["content"] == "模型上下文输入"
        await service.send(cid, "后续输入")
        status, refused = await server._route("POST", "/api/chat/conversation-data", json.dumps({**request, "action": "clear", "expected_revision": saved["revision"]}).encode("utf-8"))
        assert status == 409 and "进行" in refused["error"]
        await _wait_conversation_task(service, cid)
        assert conversation.session.ctx.get_context()[edited_index]["content"] == "模型上下文输入"
    finally:
        await service.close()


# ---------- ChatPluginRegistry 测试 ----------


def _write_plugin(base: Path, name: str, tools: dict[str, str] | None = None) -> Path:
    """在指定目录写一个最小插件 (meta.yaml)"""
    pdir = base / name
    pdir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"name": name, "version": "0.1.0", "description": f"{name} 插件"}
    if tools:
        meta["tools"] = tools
    dumped = cast(Any, yaml).safe_dump(meta, allow_unicode=True)   # pyyaml 无完整类型声明
    (pdir / "meta.yaml").write_text(dumped if isinstance(dumped, str) else "", encoding="utf-8")
    return pdir


def test_registry_scan_enable_and_capability_persist(tmp_path: Path, monkeypatch: Any):
    """扫描默认不启用, 启用与能力开关写入 json 跨实例持久, 非法类别返回 False"""
    preset = tmp_path / "preset"
    _write_plugin(preset, "plug_a", tools={"shell": "执行命令", "read_file": "读文件"})
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", preset)
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "user")

    state = tmp_path / "state.json"
    reg = ChatPluginRegistry(state_path=state)
    plugins = reg.scan()
    assert len(plugins) == 1
    p = plugins[0]
    assert p["name"] == "plug_a" and p["enabled"] is False   # 默认不启用
    tool_names = [t["name"] for t in p["capabilities"]["tools"]]
    assert "shell" in tool_names and "read_file" in tool_names
    assert reg.is_enabled("plug_a") is False
    reg.set_enabled("plug_a", True)
    assert reg.is_enabled("plug_a") is True and state.is_file()
    assert reg.set_capability("plug_a", "tools", "shell", False) is True
    assert reg.capability_enabled("plug_a", "tools", "shell") is False
    assert reg.capability_enabled("plug_a", "tools", "other") is True   # 默认 True
    assert reg.set_capability("plug_a", "bad_kind", "x", True) is False
    reg2 = ChatPluginRegistry(state_path=state)
    assert reg2.is_enabled("plug_a") is True


def test_registry_official_overrides_user(tmp_path: Path, monkeypatch: Any):
    """同名插件官方目录优先"""
    preset = tmp_path / "preset"
    user = tmp_path / "user"
    _write_plugin(preset, "dup")
    _write_plugin(user, "dup")
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", preset)
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", user)

    reg = ChatPluginRegistry(state_path=tmp_path / "state.json")
    pdir = reg.get_plugin_dir("dup")
    assert pdir is not None
    assert pdir == preset / "dup"


# ---------- ChatService 测试 ----------


class _FakeAsyncLLM(AsyncLLM):
    """流式返回固定内容的 fake LLM"""

    def __init__(self) -> None:
        pass

    async def call(self, messages: list[dict[str, Any]], **kw: Any) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content="答复")

    async def stream_call(self, messages: list[dict[str, Any]], **kw: Any) -> AsyncIterator[LLMCallStreamEvent]:
        yield LLMCallStreamEvent(kind="thinking_delta", delta="思考中")
        yield LLMCallStreamEvent(kind="content_delta", delta="答")
        yield LLMCallStreamEvent(kind="content_delta", delta="复")
        yield LLMCallStreamEvent(kind="done", response=LLMCallResponse(type="answer", content="答复"))

    def set_parameters(self, **kwargs: Any) -> None:
        pass


class _FakeModelConfig:
    """最小 ModelConfigManager 替身"""

    def list_llm_configs(self, mask_api_key: bool = False) -> dict[str, Any]:
        return {"default": {}, "fast": {}}

    def get_llm_config(self, name: str = "default") -> Any:
        return LLMConfig(name=name, model="m", api_key="k", base_url="https://x")


def _as_model_config(fake: Any) -> ModelConfigManager:
    """ModelConfigManager 替身类型边界: 替身实现配置查询调用面, cast 集中在此工厂"""
    return cast(ModelConfigManager, fake)


def _make_service(tmp_path: Path, monkeypatch: Any) -> ChatService:
    """构造 ChatService, build_llm 替换为 fake"""
    def _fake_build_llm(cfg: Any) -> AsyncLLM:
        return _FakeAsyncLLM()

    monkeypatch.setattr(service_mod, "build_llm", _fake_build_llm)
    reg = ChatPluginRegistry(state_path=tmp_path / "plugins.json")
    return ChatService(
        _as_model_config(_FakeModelConfig()),
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )


@pytest.mark.asyncio
async def test_chat_run_pagination_preserves_python_all_results(tmp_path, monkeypatch):
    """HTTP 使用分页, Python 无参接口仍返回全部摘要"""
    from satrap.edictum.simple_session.recovery import store_for_session

    service = _make_service(tmp_path, monkeypatch)
    try:
        cid = await service.create_conversation()
        session = _must_conversation(service, cid).session
        store = store_for_session(session)
        for _ in range(23):
            run = store.create({}, "c", "f")
            store.update(run, status="completed")
        store.create({}, "c", "f")
        assert len(await session.list_runs()) == 24
        server = ChatHTTPServer(service)
        status, first = await server._route("GET", f"/api/chat/runs?conversation={cid}", b"")
        assert status == 200 and len(first["runs"]) == 20 and first["next_cursor"]
        status, second = await server._route("GET", f"/api/chat/runs?conversation={cid}&cursor={first['next_cursor']}", b"")
        assert status == 200 and len(second["runs"]) == 4 and second["next_cursor"] is None
        assert not ({row["id"] for row in first["runs"]} & {row["id"] for row in second["runs"]})
        status, pending = await server._route("GET", f"/api/chat/runs?conversation={cid}&unfinished=true", b"")
        assert status == 200 and len(pending["runs"]) == 1
        for query in ("limit=101", "limit=bad", "cursor=invalid"):
            status, _ = await server._route("GET", f"/api/chat/runs?conversation={cid}&{query}", b"")
            assert status == 400
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_chat_resume_after_service_restart_then_retry_and_fork(tmp_path, monkeypatch):
    """续跑保持原轮次, retry 创建新执行, fork 不继承旧任务执行权"""
    requests = []

    class RecoveringModel(_FakeAsyncLLM):
        async def stream_call(self, messages, **kwargs):
            requests.append(copy.deepcopy(messages))
            response = False if len(requests) == 1 else LLMCallResponse("answer", f"回答{len(requests)}")
            yield LLMCallStreamEvent(kind="done", response=response)

    svc = _make_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service_mod, "build_llm", lambda cfg: RecoveringModel())
    cid = await svc.create_conversation(model="default")
    await svc.send(cid, "问题")
    await _wait_conversation_task(svc, cid)
    runs = (await svc.list_runs(cid))["runs"]
    assert runs[0]["status"] == "failed"
    run_id = runs[0]["id"]
    await svc.close()

    resumed = _make_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service_mod, "build_llm", lambda cfg: RecoveringModel())
    try:
        result = await resumed.manage_run(cid, run_id, "resume")
        assert result["ok"], result
        await _wait_conversation_task(resumed, cid)
        assert requests[0] == requests[1]
        turns = resumed.list_turns(cid)
        assert len(turns) == 1
        assert turns[0]["answer"] == "回答2"
        assert turns[0]["variant_count"] == 1
        assert (await resumed.list_runs(cid))["runs"][0]["status"] == "completed"

        result = await resumed.retry(cid)
        assert result["ok"], result
        await _wait_conversation_task(resumed, cid)
        assert len((await resumed.list_runs(cid))["runs"]) == 2
        assert resumed.list_turns(cid)[0]["variant_count"] == 2
        forked = await resumed.fork(cid, 1)
        assert forked["ok"]
        assert (await resumed.list_runs(forked["conversation_id"]))["runs"] == []
        denied = await resumed.manage_run(forked["conversation_id"], run_id, "resume")
        assert denied["ok"] is False
    finally:
        await resumed.close()


@pytest.mark.asyncio
async def test_chat_override_api_defers_busy_session_and_fork_inherits_parameters(tmp_path, monkeypatch):
    """Chat HTTP 入口验证覆盖版本, 执行中延后应用, 分支继承独立覆盖"""
    svc = _make_service(tmp_path, monkeypatch)
    manager = ModelConfigManager(tmp_path / "models.json")
    manager.set_llm_config(LLMConfig(model="test", api_key="secret-test-key"), "default")
    svc._model_cfg = manager
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugin-config"))
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugin-config"))
    cid = await svc.create_conversation()
    conv = svc.get_conversation(cid)
    assert conv is not None
    server = ChatHTTPServer(svc)
    url = f"/api/chat/session-plugin-config?conversation_id={cid}&plugin=rag"
    try:
        status, result = await server._route("GET", url, b"")
        assert status == 200 and result["revision"] == 0
        assert "secret-test-key" not in json.dumps(result)
        conv.task = asyncio.create_task(asyncio.sleep(30))
        status, result = await server._route("PUT", url, json.dumps({"overrides": {"top_k": 2}, "expected_revision": 0}).encode())
        assert status == 200 and result["runtime"]["status"] == "next_turn"
        status, _ = await server._route("PUT", url, b'{"overrides":{},"expected_revision":0}')
        assert status == 409
        conv.task.cancel()
        await asyncio.gather(conv.task, return_exceptions=True)
        result = await svc.fork(cid, 0)
        copied = svc.session_plugin_config(result["conversation_id"], "rag")
        assert copied["overrides"] == {"top_k": 2}
        assert not svc._storage.session_root("chat", result["conversation_id"]).exists()
        store = SessionOverrideStore(svc._storage.platform_db("chat"))
        store.replace(result["conversation_id"], "plugins.rag", {"top_k": 3}, expected_revision=copied["revision"])
        assert store.read(cid, "plugins.rag")["overrides"] == {"top_k": 2}
    finally:
        if conv.task is not None:
            conv.task.cancel()
            await asyncio.gather(conv.task, return_exceptions=True)
        await svc.close()


@pytest.mark.asyncio
async def test_chat_friend_instance_tools_api_preserves_revision_and_restores_inheritance(tmp_path, monkeypatch):
    """Chat 复用同一好友工具覆盖协议, 不允许旧版本覆盖新配置"""
    svc = _make_service(tmp_path, monkeypatch)
    models = ModelConfigManager(tmp_path / "models.json")
    models.set_llm_config(LLMConfig(model="test", api_key="secret-test-key"), "default")
    svc._model_cfg = models
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugin-config"))
    monkeypatch.setattr("satrap.display.plugins.PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugin-config"))
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugin-config"))
    cid = await svc.create_conversation()
    server = ChatHTTPServer(svc)
    url = f"/api/chat/session-plugin-config?conversation_id={cid}&plugin=friend_manager"
    try:
        status, initial = await server._route("GET", url, b"")
        assert status == 200 and len(initial["tool_descriptions"]) == 3
        payload = {"overrides": {}, "expected_revision": initial["revision"],
                   "tool_overrides": {"friend_manager_send_request": False}, "expected_tool_revision": initial["tool_revision"]}
        status, saved = await server._route("PUT", url, json.dumps(payload).encode("utf-8"))
        assert status == 200 and not saved["tool_overrides"]["friend_manager_send_request"]
        status, _ = await server._route("PUT", url, json.dumps(payload).encode("utf-8"))
        assert status == 409
        status, reset = await server._route("PUT", url, json.dumps({"overrides": {}, "expected_revision": saved["revision"],
                                                                  "tool_overrides": {}, "expected_tool_revision": saved["tool_revision"]}).encode("utf-8"))
        assert status == 200 and reset["tool_overrides"] == {}
    finally:
        await svc.close()


@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_retry_preparation_reserves_and_restores_context(tmp_path: Path, monkeypatch: Any, failure: str):
    svc = _make_service(tmp_path, monkeypatch)
    cid = await svc.create_conversation()
    await svc.send(cid, "原问题")
    conv = svc.get_conversation(cid)
    assert conv is not None and conv.task is not None
    await conv.task
    original = copy.deepcopy(conv.session.ctx.get_context())
    entered = asyncio.Event()
    release = asyncio.Event()
    original_delete = conv.session.ctx.del_last_chat

    async def blocked_delete(n: int):
        await original_delete(n)
        entered.set()
        await release.wait()
        raise ValueError("模拟准备失败")

    monkeypatch.setattr(conv.session.ctx, "del_last_chat", blocked_delete)
    task = asyncio.create_task(svc.retry(cid))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not (await svc.retry(cid))["ok"]
        assert not (await svc.send(cid, "并发消息"))["ok"]
        assert not (await svc.select_variant(cid, 0, 0))["ok"]
        with pytest.raises(ValueError, match="正在生成"):
            await svc.delete_conversation(cid)
        if failure == "cancel":
            assert (await svc.cancel(cid))["ok"]
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            assert not (await task)["ok"]
        assert conv.session.ctx.get_context() == original
        assert cid not in svc._operations
        last_turn = conv.recorder.last_turn()
        assert last_turn is not None and last_turn["variant_count"] == 1
        monkeypatch.setattr(conv.session.ctx, "del_last_chat", original_delete)
        assert (await svc.retry(cid))["ok"]
        await conv.task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await svc.close()


async def test_concurrent_resume_publishes_only_initialized_runtime(tmp_path: Path, monkeypatch: Any):
    svc = _make_service(tmp_path, monkeypatch)
    cid = await svc.create_conversation()
    await svc.close()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = svc._install_enabled_plugins
    installations = 0

    async def blocked_install(session):
        nonlocal installations
        installations += 1
        entered.set()
        await release.wait()
        return await original(session)

    monkeypatch.setattr(svc, "_install_enabled_plugins", blocked_install)
    tasks = [asyncio.create_task(svc._resume_conversation(cid)) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert svc.get_conversation(cid) is None
        release.set()
        first, second = await asyncio.gather(*tasks)
        assert first is second and installations == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await svc.close()


async def test_concurrent_preload_and_send_share_initialization(tmp_path: Path, monkeypatch: Any):
    svc = _make_service(tmp_path, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = svc._install_enabled_plugins
    installations = 0

    async def blocked_install(session):
        nonlocal installations
        installations += 1
        entered.set()
        await release.wait()
        return await original(session)

    monkeypatch.setattr(svc, "_install_enabled_plugins", blocked_install)
    first = asyncio.create_task(svc.preload_conversation(conversation_id="shared"))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert svc.get_conversation("shared") is None
        second = asyncio.create_task(svc.send("shared", "hello"))
        await asyncio.sleep(0)
        assert not (await svc.send("shared", "duplicate"))["ok"]
        release.set()
        assert await first == "shared"
        assert (await second)["ok"] and installations == 1
        conv = svc.get_conversation("shared")
        assert conv is not None and conv.task is not None
        await conv.task
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        await svc.close()


def test_service_list_models(tmp_path: Path, monkeypatch: Any):
    svc = _make_service(tmp_path, monkeypatch)
    assert set(svc.list_models()) == {"default", "fast"}


def test_service_send_and_turns(tmp_path: Path, monkeypatch: Any):
    """send 立即返回, 后台 run 完成后 turns 落库且 WS 收到事件"""
    svc = _make_service(tmp_path, monkeypatch)

    async def _run() -> tuple[str, list[dict[str, Any]]]:
        cid = await svc.create_conversation(model="default")
        queue = svc.subscribe(cid)
        result = await svc.send(cid, "你好", think="medium")
        assert result["ok"] is True
        # 等待后台 run 完成
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.task is not None
        await conv.task
        # 收集 WS 事件
        events: list[dict[str, Any]] = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return cid, events

    cid, events = asyncio.run(_run())

    types = [e["type"] for e in events]
    # WS 事件: turn_start + thinking + content*2 + turn_done
    assert "turn_start" in types
    assert "thinking_delta" in types
    assert types.count("content_delta") == 2
    assert "turn_done" in types
    done = next(event for event in events if event["type"] == "turn_done")
    assert done["context_stats"]["request_count"] == 1
    assert done["context_stats"]["last_request"]["strategy"] == "sliding"

    turns = svc.list_turns(cid)
    # turns 落库
    assert len(turns) == 1
    assert turns[0]["user_input"] == "你好"
    assert turns[0]["answer"] == "答复"
    assert "思考中" in (turns[0]["thinking"] or "")
    assert turns[0]["context_stats"]["request_count"] == 1
    assert turns[0]["variants"][0]["context_stats"]["request_count"] == 1

    convs = list_conversations(str(svc._display_db_path))
    # 会话列表
    assert any(c["conversation_id"] == cid for c in convs)


def test_service_model_policy_hot_update_defers_running_conversation(tmp_path: Path, monkeypatch: Any):
    """生成中的 Chat 会话应在本轮结束后安全应用最新上下文策略"""
    manager = ModelConfigManager(storage_path=tmp_path / "models.json")
    manager.set_llm_config(
        LLMConfig(name="default", model="m", api_key="k", base_url="https://x"),
        "default",
    )
    def build_fake_llm(_config: LLMConfig) -> _FakeAsyncLLM:
        """返回测试使用的异步模型替身"""
        return _FakeAsyncLLM()

    monkeypatch.setattr(service_mod, "build_llm", build_fake_llm)
    svc = ChatService(
        manager,
        ChatPluginRegistry(state_path=tmp_path / "plugins.json"),
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = svc.get_conversation(cid)
        assert conv is not None
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _slow_run(*args: Any, **kwargs: Any) -> str:
            entered.set()
            await release.wait()
            return "完成"

        monkeypatch.setattr(conv.session, "run", _slow_run)
        assert (await svc.send(cid, "开始"))["ok"] is True
        await entered.wait()
        result = svc.update_model(
            "default",
            {
                "context_window": 10000,
                "history_ratio": 0.6,
                "context_strategy": "summarize",
                "context_threshold": 0.75,
                "truncation_floor": 0.25,
                "summary_keep_recent_turns": 3,
            },
        )
        assert result["refreshed_conversations"] == 0
        assert result["deferred_conversations"] == 1
        assert conv.session.ctx.exceed_process == "sliding"

        release.set()
        assert conv.task is not None
        await conv.task
        assert conv.pending_model_config is None
        assert conv.session.ctx.exceed_process == "summarize"
        assert conv.session.ctx.history_budget == 6000
        assert conv.session.ctx.trigger_tokens == 4500
        assert conv.session.ctx.floor_tokens == 1500
        assert conv.session.ctx.summary_keep_recent_turns == 3
        await svc.close()

    asyncio.run(_run())


def test_service_ask_user_round_trip(tmp_path: Path, monkeypatch: Any):
    """ask_user 经 WS 发出问题并由 HTTP 服务层回填回答"""
    svc = _make_service(tmp_path, monkeypatch)

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = svc.get_conversation(cid)
        assert conv is not None
        provider = conv.session.user_input_provider
        assert provider is not None
        queue = svc.subscribe(cid)

        async def _run_with_question(
            user_input: str,
            img_urls: list[str] | None = None,
            *,
            video_urls: list[str] | None = None,
            thinking: str = "off",
        ) -> str:
            answer = provider("请选择", ["继续", "取消"])
            if isinstance(answer, str):
                return answer
            return await answer

        monkeypatch.setattr(conv.session, "run", _run_with_question)
        assert (await svc.send(cid, "开始询问"))["ok"] is True
        turn_start = await asyncio.wait_for(queue.get(), timeout=1)
        assert turn_start["type"] == "turn_start"
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event["type"] == "ask_user"
        assert event["question"] == "请选择"
        assert event["options"] == ["继续", "取消"]
        request_id = event["request_id"]

        replay_queue = svc.subscribe(cid)
        replay = svc.runtime_snapshot(cid)["pending_user_inputs"][0]
        assert replay_queue.empty()
        assert replay["request_id"] == request_id
        assert replay["options"] == ["继续", "取消"]

        assert svc.answer_user_input(cid, request_id, "1") == {"ok": True}
        assert conv.task is not None
        await asyncio.wait_for(conv.task, timeout=1)
        assert svc.answer_user_input(cid, request_id, "重复回答")["ok"] is False

        end = await asyncio.wait_for(queue.get(), timeout=1)
        assert end["type"] == "ask_user_end"
        assert end["status"] == "answered"
        done = await asyncio.wait_for(queue.get(), timeout=1)
        assert done["type"] == "turn_done"
        assert done["answer"] == "1"
        assert not conv.pending_user_inputs
        await svc.close()

    asyncio.run(_run())


def test_service_cancel_pending_ask_user(tmp_path: Path, monkeypatch: Any):
    """取消生成会结束等待中的 ask_user 请求"""
    svc = _make_service(tmp_path, monkeypatch)

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = svc.get_conversation(cid)
        assert conv is not None
        provider = conv.session.user_input_provider
        assert provider is not None

        async def _run_with_question(
            user_input: str,
            img_urls: list[str] | None = None,
            *,
            video_urls: list[str] | None = None,
            thinking: str = "off",
        ) -> str:
            answer = provider("是否取消?")
            if isinstance(answer, str):
                return answer
            return await answer

        monkeypatch.setattr(conv.session, "run", _run_with_question)
        queue = svc.subscribe(cid)
        assert (await svc.send(cid, "开始询问"))["ok"] is True
        assert (await asyncio.wait_for(queue.get(), timeout=1))["type"] == "turn_start"
        assert (await asyncio.wait_for(queue.get(), timeout=1))["type"] == "ask_user"

        assert await svc.cancel(cid) == {"ok": True}
        events = [queue.get_nowait(), queue.get_nowait()]
        assert [event["type"] for event in events] == ["ask_user_end", "turn_done"]
        assert events[0]["status"] == "cancelled"
        assert not conv.pending_user_inputs
        await svc.close()

    asyncio.run(_run())


def test_service_preload_is_invisible_until_first_send(tmp_path: Path, monkeypatch: Any):
    """预加载只创建内存运行时, 首次发送时才写入会话列表"""
    svc = _make_service(tmp_path, monkeypatch)

    async def _run() -> str:
        cid = await svc.preload_conversation(model="default", think="off")
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.persisted is False
        assert list_conversations(str(svc._display_db_path)) == []

        result = await svc.send(
            cid,
            "你好",
            think="high",
            preload_settings={
                "model": "default",
                "temperature": 0.3,
                "system_prompt": "测试提示词",
                "project_id": None,
            },
        )
        assert result["ok"] is True
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.persisted is True
        assert conv.default_think == "high"
        assert conv.temperature == 0.3
        assert conv.system_prompt == "测试提示词"
        assert conv.task is not None
        await conv.task
        await svc.close()
        return cid

    cid = asyncio.run(_run())
    conversations = list_conversations(str(svc._display_db_path))
    assert [item["conversation_id"] for item in conversations] == [cid]
    assert conversations[0]["turn_count"] == 1


def test_service_preload_rebuilds_on_model_or_config_change(tmp_path: Path, monkeypatch: Any):
    """模型切换与同名模型参数变化都使预加载指纹失效, 并在同一 ID 下重建"""
    built: list[tuple[str | None, float | None]] = []

    class _MutableModelConfig(_FakeModelConfig):
        temperature = 0.1

        def get_llm_config(self, name: str = "default") -> LLMConfig:
            return LLMConfig(
                name=name,
                model="m",
                api_key="k",
                base_url="https://x",
                temperature=self.temperature,
            )

    def _fake_build_llm(cfg: Any) -> AsyncLLM:
        built.append((cfg.name, cfg.temperature))
        return _FakeAsyncLLM()

    model_config = _MutableModelConfig()
    monkeypatch.setattr(service_mod, "build_llm", _fake_build_llm)
    svc = ChatService(
        _as_model_config(model_config),
        ChatPluginRegistry(state_path=tmp_path / "plugins.json"),
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    async def _run() -> None:
        # 模型切换
        cid = await svc.preload_conversation(model="default")
        result = await svc.send(
            cid, "切换模型",
            preload_settings={"model": "fast", "system_prompt": "", "project_id": None},
        )
        assert result["ok"] is True
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.task is not None
        await conv.task
        assert conv.model == "fast"
        meta = get_conversation_meta(cid, db_path=str(svc._display_db_path))
        assert meta is not None and meta["model"] == "fast"
        # 同名模型参数变化
        cid2 = await svc.preload_conversation(model="default")
        model_config.temperature = 0.9
        result2 = await svc.send(cid2, "参数已修改", preload_settings={"model": "default"})
        assert result2["ok"] is True
        conv2 = svc.get_conversation(cid2)
        assert conv2 is not None and conv2.task is not None
        await conv2.task
        await svc.close()

    asyncio.run(_run())
    assert [name for name, _ in built] == ["default", "fast", "default", "default"]
    assert built[0][1] == 0.1 and built[-1][1] == 0.9


def test_service_runtime_fingerprint_tracks_plugin_capability_and_config(
    tmp_path: Path,
    monkeypatch: Any,
):
    """插件能力开关和全局配置变化都应改变会话运行时指纹"""

    preset = tmp_path / "preset"
    plugin_dir = _write_plugin(preset, "plug", tools={"shell": "执行命令"})
    meta = cast(
        dict[str, Any],
        cast(Any, yaml).safe_load((plugin_dir / "meta.yaml").read_text(encoding="utf-8")),
    )
    meta["config_schema"] = {
        "mode": {"type": "string", "default": "safe", "description": "运行模式"},
    }
    dumped = cast(Any, yaml).safe_dump(meta, allow_unicode=True)
    (plugin_dir / "meta.yaml").write_text(str(dumped), encoding="utf-8")
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", preset)
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "user")

    config_manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: config_manager)
    registry = ChatPluginRegistry(state_path=tmp_path / "plugins.json")
    registry.set_enabled("plug", True)
    svc = ChatService(
        _as_model_config(_FakeModelConfig()),
        registry,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    first = svc._runtime_fingerprint("default", system_prompt=None, project_id=None, temperature=None)
    registry.set_capability("plug", "tools", "shell", False)
    second = svc._runtime_fingerprint("default", system_prompt=None, project_id=None, temperature=None)
    assert second != first

    schema = parse_config_schema(load_plugin_meta(plugin_dir))
    config_manager.save_global("plug", schema, {"mode": "fast"})
    third = svc._runtime_fingerprint("default", system_prompt=None, project_id=None, temperature=None)
    assert third != second


def _write_chat_commands(tmp_path: Path, monkeypatch: Any) -> None:
    """使用专属 Chat 测试插件验证命令配置和热更新"""
    root = tmp_path / "chat_plugins"
    plugin = root / "chat_commands"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text(
        "name: chat_commands\nversion: 0.1.0\napplicability:\n  session_types: [chat]\n"
        "commands:\n  about: 说明\nconfig_schema:\n  about_text:\n    type: string\n    default: ''\n",
        encoding="utf-8",
    )
    (plugin / "commands.py").write_text(
        "def build_commands(session, config=None):\n"
        "    async def about():\n        return (config or {}).get('about_text', '')\n"
        "    return {}, {'about': about}\n", encoding="utf-8",
    )
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", root)
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "empty")


def test_chat_plugin_capability_updates_active_session(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Chat 子能力开关应通过插件命名空间即时更新活跃会话"""
    config_manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: config_manager)
    _write_chat_commands(tmp_path, monkeypatch)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("chat_commands", True)
    svc._plugins.set_capability("chat_commands", "commands", "about", False)

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = svc.get_conversation(cid)
        assert conv is not None
        plugin = next(item for item in conv.session.list_plugins() if item.name == "chat_commands")
        assert plugin.commands["about"] is False

        result = await svc.set_plugin_capability(
            "chat_commands",
            "commands",
            "about",
            True,
        )

        assert result["ok"] is True
        assert result["sessions"][0]["status"] == "applied"
        assert plugin.commands["about"] is True
        await svc.close()

    asyncio.run(_run())


def test_chat_plugin_config_hot_reinstalls_active_session(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Chat 插件配置保存后应通过共享协调器热重装活跃会话"""
    config_manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: config_manager)
    _write_chat_commands(tmp_path, monkeypatch)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("chat_commands", True)

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = svc.get_conversation(cid)
        assert conv is not None

        result = await svc.save_plugin_config(
            "chat_commands",
            {"about_text": "Chat 热重装说明"},
        )

        assert result["ok"] is True
        assert result["applied"] is True
        assert result["sessions"][0]["plugins"][0]["action"] == "reinstall"
        assert await conv.session.run("/about") == "Chat 热重装说明"
        await svc.close()

    asyncio.run(_run())


def test_chat_plugin_reconcile_reads_global_cold_writes(tmp_path: Path, monkeypatch: Any) -> None:
    """独立管理页写入全局参数后, Chat 协调接口更新实际命令"""
    from satrap.core.config.plugin_service import PluginManagementService

    config_manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: config_manager)
    _write_chat_commands(tmp_path, monkeypatch)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("chat_commands", True)
    cold = PluginManagementService(svc._plugins.catalog, {}, svc._plugins, manager=config_manager)

    async def run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = _must_conversation(svc, cid)
        assert await conv.session.run("/about") == ""
        before = cold.get_config("chat_commands")
        cold.save_config("chat_commands", {"about_text": "来自管理页"}, before["revision"])
        status, result = await ChatHTTPServer(svc)._route("POST", "/api/chat/plugins/reconcile", b'{"plugin_name":"chat_commands"}')
        assert status == 200 and result["applied"]
        assert await conv.session.run("/about") == "来自管理页"
        snapshot_status, snapshot = await ChatHTTPServer(svc)._route("GET", "/api/chat/plugins/runtime", b"")
        assert snapshot_status == 200
        actual = snapshot["sessions"][0]["plugins"][0]
        assert actual["capabilities"]["loaded"]["commands"] == {"about": True}
        assert "config" not in actual
        external = ChatPluginRegistry(svc._plugins.state_path)
        external.catalog = svc._plugins.catalog
        external.set_enabled("chat_commands", False)
        await ChatHTTPServer(svc)._route("POST", "/api/chat/plugins/reconcile", b"{}")
        assert not svc.plugin_runtime_snapshot()["sessions"][0]["plugins"][0]["enabled"]
        await svc.close()

    asyncio.run(run())


def test_chat_apply_snapshot_shared_by_instances_and_keeps_unchanged_handle(tmp_path: Path, monkeypatch: Any) -> None:
    """三个真实 Chat 实例复用一次全局解析, 独立覆盖未变化时不重装"""
    manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: manager)
    _write_chat_commands(tmp_path, monkeypatch)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("chat_commands", True)

    async def run() -> None:
        try:
            ids = [await svc.create_conversation(model="default") for _ in range(3)]
            convs = [_must_conversation(svc, cid) for cid in ids]
            local = convs[1].session
            store = local.plugin_override_store
            if store is None:
                raise AssertionError("测试实例缺少覆盖存储")
            store.replace(local.session_id, "plugins.chat_commands", {"about_text": "本实例覆盖"}, expected_revision=0)
            await svc.reconcile_plugins(plugin_name="chat_commands")
            handles = [conv.session.list_plugins()[0] for conv in convs]
            entry = svc._plugins.catalog.get("chat_commands")
            if entry is None:
                raise AssertionError("测试插件未加载")
            schema = entry.config_schema
            manager.save_global("chat_commands", schema, {"about_text": "新全局参数"})
            load = manager.load_global_explicit
            reads = []

            def counted(name, schema):
                """记录单次应用的正文解析次数"""
                reads.append(name)
                return load(name, schema)

            monkeypatch.setattr(manager, "load_global_explicit", counted)
            result = await svc.reconcile_plugins(plugin_name="chat_commands")
            assert result["ok"] and result["applied"] and reads == ["chat_commands"]
            assert len(result["sessions"]) == 3
            assert [await conv.session.run("/about") for conv in convs] == ["新全局参数", "本实例覆盖", "新全局参数"]
            assert convs[1].session.list_plugins()[0] is handles[1]
            assert result["sessions"][1]["plugins"] == []
            assert convs[0].session.list_plugins()[0] is not handles[0]
            assert convs[2].session.list_plugins()[0] is not handles[2]
        finally:
            await svc.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_chat_model_resources_share_capture_reject_stale_source_and_rollback(tmp_path: Path, monkeypatch: Any) -> None:
    """三个真实 Chat 实例共享四类模型来源, 排队变化拒绝且失败回滚旧资源"""
    globals_ = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: globals_)
    monkeypatch.setattr("satrap.edictum.plugin_settings.PluginConfigManager", lambda: globals_)
    _write_chat_commands(tmp_path, monkeypatch)
    plugin = tmp_path / "chat_plugins" / "model_probe"
    plugin.mkdir()
    (plugin / "meta.yaml").write_text(
        "name: model_probe\nversion: 0.1.0\nconfig_schema:\n"
        "  fail:\n    type: bool\n    default: false\n"
        "  llm:\n    type: llm\n    default: default\n"
        "  embed:\n    type: embed\n    default: resource\n"
        "  rerank:\n    type: rerank\n    default: resource\n"
        "  asr:\n    type: asr\n    default: resource\n", encoding="utf-8",
    )
    (plugin / "tools.py").write_text(
        "def get_tools(session, config, *, resources):\n"
        "    session._model_probe = resources\n"
        "    if config['fail']:\n        raise ValueError('model probe failed')\n"
        "    return []\n", encoding="utf-8",
    )
    svc = _make_service(tmp_path, monkeypatch)
    models = ModelConfigManager(tmp_path / "models.json")
    models.update_llm_config("default", model="old", api_key="test")
    models.set_embedding_config(EmbeddingConfig(model="embedding-old", api_key="test"), "resource")
    models.set_rerank_config(ReRankConfig(model="rerank-old", api_key="test", base_url="http://localhost:1"), "resource")
    models.set_asr_config(ASRConfig(model="asr-old", api_key="test"), "resource")
    svc._model_cfg = models
    svc._plugins.set_enabled("model_probe", True)
    writer = ModelConfigManager(models.storage_path)
    reads = []
    read = ModelConfigSnapshot.read
    captured = threading.Event()
    capture = PluginApplySnapshot.capture

    def counted(source):
        """记录实际模型文件解析, 不刷新运行期管理器"""
        reads.append(source.storage_path)
        return read(source)

    def record(*args, **kwargs):
        """通知真实来源捕获完成, 不改变快照内容"""
        snapshot = capture(*args, **kwargs)
        captured.set()
        return snapshot

    try:
        ids = [await svc.create_conversation(model="default") for _ in range(3)]
        convs = [_must_conversation(svc, cid) for cid in ids]
        sessions = [cast(Any, conv.session) for conv in convs]
        main_models = [session.llm for session in sessions]
        writer.set_embedding_config(EmbeddingConfig(model="embedding-new", api_key="test", dimensions=7), "resource")
        writer.set_rerank_config(ReRankConfig(model="rerank-new", api_key="test", base_url="http://localhost:2"), "resource")
        writer.set_asr_config(ASRConfig(model="asr-new", api_key="test"), "resource")
        monkeypatch.setattr(ModelConfigSnapshot, "read", counted)
        applied = await svc.reconcile_plugins(plugin_name="model_probe")
        assert applied["applied"] and reads == [models.storage_path]
        old_handles = [session.list_plugins()[0] for session in sessions]
        for session in sessions:
            assert {key: value[1].model for key, value in session._model_probe._configs.items()} == {
                "llm": "old", "embed": "embedding-new", "rerank": "rerank-new", "asr": "asr-new",
            }
            assert session._model_probe._clients == {} and session.plugin_model_manager is models
        assert models.get_embedding_config("resource").model == "embedding-old"
        repeated = await svc.reconcile_plugins(plugin_name="model_probe")
        assert repeated["applied"] and all(not item["plugins"] for item in repeated["sessions"])
        assert [session.list_plugins()[0] for session in sessions] == old_handles
        monkeypatch.setattr(PluginApplySnapshot, "capture", record)
        ready = asyncio.Event()
        reconcile = service_mod.reconcile_plugin_states_async

        async def observed_reconcile(*args, **kwargs):
            """其它目标进入真实安装阶段后再改写, 避免将捕获重试误当排队变化"""
            ready.set()
            return await reconcile(*args, **kwargs)

        monkeypatch.setattr(service_mod, "reconcile_plugin_states_async", observed_reconcile)
        await convs[0].plugin_lock.acquire()
        task = asyncio.create_task(svc.reconcile_plugins(plugin_name="model_probe"))
        try:
            assert await asyncio.to_thread(captured.wait, 2)
            await asyncio.wait_for(ready.wait(), 2)
            writer.set_embedding_config(EmbeddingConfig(model="embedding-latest", api_key="test", dimensions=9), "resource")
        finally:
            convs[0].plugin_lock.release()
        stale = await asyncio.wait_for(task, 2)
        assert not stale["sessions"][0]["ok"] and "等待期间已变化" in stale["sessions"][0]["error"]
        assert sessions[0].list_plugins()[0] is old_handles[0]
        entry = svc._plugins.catalog.get("model_probe")
        if entry is None:
            raise AssertionError("测试插件不存在")
        globals_.save_global("model_probe", entry.config_schema, {"fail": True})
        failed = await svc.reconcile_plugins(plugin_name="model_probe")
        assert not failed["applied"] and failed["failed"] == 3
        assert all(item["plugins"][0]["status"] == "rolled_back" for item in failed["sessions"])
        assert all(session._model_probe._configs["embed"][1].model == "embedding-new" for session in sessions)
        assert [conv.session for conv in convs] == sessions and [session.llm for session in sessions] == main_models
        assert all(session.plugin_model_manager is models for session in sessions)
        models.storage_path.write_text("broken", encoding="utf-8")
        handles = [session.list_plugins()[0] for session in sessions]
        with pytest.raises(ValueError):
            await svc.reconcile_plugins(plugin_name="model_probe")
        assert [session.list_plugins()[0] for session in sessions] == handles
        svc._plugins.set_enabled("model_probe", False)
        disabled = await svc.reconcile_plugins(plugin_name="model_probe")
        assert disabled["applied"] and all(not session.list_plugins() for session in sessions)
        assert models.storage_path.read_text(encoding="utf-8") == "broken"
    finally:
        await svc.close()


@pytest.mark.parametrize("source", ["global", "chat_state"])
def test_chat_queued_apply_rejects_changed_source_and_retries_latest(tmp_path: Path, monkeypatch: Any, source: str) -> None:
    """等待会话锁期间来源变化不应用旧快照, 显式重试再加载新参数"""
    manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: manager)
    _write_chat_commands(tmp_path, monkeypatch)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("chat_commands", True)
    captured = threading.Event()
    to_thread = asyncio.to_thread

    async def prepared(func, *args, **kwargs):
        """在完整准备线程返回后通知, 不把捕获中途的可重试快照误认作最终批次"""
        result = await to_thread(func, *args, **kwargs)
        if getattr(func, "__name__", "") == "prepare":
            captured.set()
        return result

    async def run() -> None:
        try:
            cid = await svc.create_conversation(model="default")
            conv = _must_conversation(svc, cid)
            old_handle = conv.session.list_plugins()[0]
            entry = svc._plugins.catalog.get("chat_commands")
            if entry is None:
                raise AssertionError("测试插件未加载")
            schema = entry.config_schema
            manager.save_global("chat_commands", schema, {"about_text": "不应应用的旧参数"})
            monkeypatch.setattr(service_mod.asyncio, "to_thread", prepared)
            await conv.plugin_lock.acquire()
            task = asyncio.create_task(svc.reconcile_plugins(plugin_name="chat_commands"))
            try:
                assert await asyncio.to_thread(captured.wait, 2)
                if source == "global":
                    manager.save_global("chat_commands", schema, {"about_text": "最新参数"})
                else:
                    external = ChatPluginRegistry(svc._plugins.state_path)
                    external.catalog = svc._plugins.catalog
                    external.set_capability("chat_commands", "commands", "about", False)
            finally:
                conv.plugin_lock.release()
            rejected = await asyncio.wait_for(task, 2)
            assert rejected["ok"] and not rejected["applied"] and rejected["failed"] == 1
            assert "等待期间已变化" in rejected["sessions"][0]["error"]
            assert rejected["sessions"][0]["timings_ms"]["config_read_ms"] >= 0
            assert conv.session.list_plugins()[0] is old_handle
            assert await conv.session.run("/about") == ""
            result = await svc.reconcile_plugins(plugin_name="chat_commands")
            assert result["ok"] and result["applied"]
            if source == "global":
                assert await conv.session.run("/about") == "最新参数"
            else:
                assert conv.session.list_plugins()[0].commands["about"] is False
        finally:
            await svc.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["session", "entry", "new_conversation"])
def test_chat_application_freezes_instances_before_capture_and_rechecks_after_source(tmp_path: Path, monkeypatch: Any, change: str) -> None:
    """Chat 在捕获前固定实例, 同对象替换及来源核验期间变化均保留旧插件"""
    manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: manager)
    _write_chat_commands(tmp_path, monkeypatch)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("chat_commands", True)
    captured = threading.Event()
    capture = PluginApplySnapshot.capture

    async def run() -> None:
        conv = None
        original = None
        cid = ""
        try:
            cid = await svc.create_conversation(model="default")
            conv = _must_conversation(svc, cid)
            other_id = await svc.create_conversation(model="default")
            other = _must_conversation(svc, other_id)
            original = conv.session
            handle = original.list_plugins()[0]
            other_handle = other.session.list_plugins()[0]
            entry = svc._plugins.catalog.get("chat_commands")
            if entry is None:
                raise AssertionError("测试插件未加载")
            manager.save_global("chat_commands", entry.config_schema, {"about_text": "changed"})

            def record(catalog, manager, **kwargs):
                """捕获真实来源并通知测试可替换实例"""
                snapshot = capture(catalog, manager, **kwargs)
                captured.set()
                return snapshot

            monkeypatch.setattr(PluginApplySnapshot, "capture", record)
            await conv.plugin_lock.acquire()
            task = asyncio.create_task(svc.reconcile_plugins(plugin_name="chat_commands"))
            try:
                assert await asyncio.to_thread(captured.wait, 2)
                if change == "session":
                    conv.session = other.session
                elif change == "entry":
                    svc._conversations[cid] = other
                elif change == "new_conversation":
                    await svc.create_conversation(model="default")
            finally:
                conv.plugin_lock.release()
            result = await asyncio.wait_for(task, 5)
            assert len(result["sessions"]) == 2
            item = next(item for item in result["sessions"] if item["conversation_id"] == cid)
            if change == "new_conversation":
                assert result["applied"] and item["ok"] and result["failed"] == 0
            else:
                assert not item["ok"] and item["error"] == "会话运行时已变化" and result["failed"] == 1
                assert original.list_plugins()[0] is handle
            assert other.session.list_plugins()[0] is not other_handle
        finally:
            if conv is not None and original is not None:
                conv.session = original
                svc._conversations[cid] = conv
            await svc.close()

    asyncio.run(run())


def test_chat_corrupt_state_does_not_disable_existing_runtime(tmp_path: Path, monkeypatch: Any) -> None:
    """损坏启用状态使应用明确失败, 不把空状态当成应卸载全部插件"""
    manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: manager)
    _write_chat_commands(tmp_path, monkeypatch)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("chat_commands", True)

    async def run() -> None:
        try:
            cid = await svc.create_conversation(model="default")
            conv = _must_conversation(svc, cid)
            old_handle = conv.session.list_plugins()[0]
            svc._plugins.state_path.write_text("broken", encoding="utf-8")
            with pytest.raises(ValueError):
                await svc.reconcile_plugins(plugin_name="chat_commands")
            assert conv.session.list_plugins()[0] is old_handle
            assert svc._plugins.is_enabled("chat_commands")
            assert await conv.session.run("/about") == ""
        finally:
            await svc.close()

    asyncio.run(run())


def test_service_preload_timeout_purges_empty_runtime(tmp_path: Path, monkeypatch: Any):
    """未发送的预加载会话超时后应释放内存并清除私有目录"""
    svc = _make_service(tmp_path, monkeypatch)
    svc._preload_ttl_seconds = 0.01

    async def _run() -> tuple[str, Path]:
        cid = await svc.preload_conversation()
        session_root = svc._storage.session_root("chat", cid)
        assert not session_root.exists()
        (session_root / "uploads").mkdir(parents=True)
        (session_root / "uploads" / "draft.txt").write_text("draft", encoding="utf-8")
        await asyncio.sleep(0.05)
        assert svc.get_conversation(cid) is None
        await svc.close()
        return cid, session_root

    cid, session_root = asyncio.run(_run())
    assert not session_root.exists()
    assert all(item["conversation_id"] != cid for item in list_conversations(str(svc._display_db_path)))


def test_service_send_concurrent_rejected(tmp_path: Path, monkeypatch: Any):
    """同一会话上一个 run 未完成时拒绝并发 send"""
    svc = _make_service(tmp_path, monkeypatch)

    async def _run() -> dict[str, Any]:
        cid = await svc.create_conversation()
        # 手动放一个未完成的 task 占位
        conv = svc.get_conversation(cid)
        assert conv is not None
        conv.task = asyncio.ensure_future(asyncio.sleep(10))
        try:
            return await svc.send(cid, "x")
        finally:
            conv.task.cancel()

    result = asyncio.run(_run())
    assert result["ok"] is False
    assert "仍在进行" in result["error"]


def test_service_send_missing_conversation(tmp_path: Path, monkeypatch: Any):
    svc = _make_service(tmp_path, monkeypatch)
    result = asyncio.run(svc.send("not-exist", "x"))
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_service_delete_conversation_cascades_data_and_trashes_files(
    tmp_path: Path,
    monkeypatch: Any,
):
    """删除 Chat 会话应清理上下文和会话记忆, 并把全部私有文件移入回收区"""

    svc = _make_service(tmp_path, monkeypatch)
    conversation_id = asyncio.run(svc.create_conversation())
    session_root = svc._storage.session_root("chat", conversation_id)
    (session_root / "sandbox").mkdir(parents=True)
    (session_root / "sandbox" / "result.txt").write_text("data", encoding="utf-8")
    memory = MemoryStore(
        db_path=svc._chat_db_path,
        scope=f"session:{conversation_id}",
    )
    memory.add("私有记忆", "仅属于当前会话")

    result = asyncio.run(svc.delete_conversation(conversation_id))

    assert result["ok"] is True
    assert not session_root.exists()
    trashed = list((svc._storage.trash_root("chat") / "sessions").iterdir())
    assert len(trashed) == 1
    assert (trashed[0] / "files" / "sandbox" / "result.txt").read_text(encoding="utf-8") == "data"
    assert (trashed[0] / "manifest.json").exists()
    assert (trashed[0] / "records.json").exists()
    assert memory.list_all() == []
    assert svc.list_turns(conversation_id) == []


def test_service_plugin_enable_no_plugins(tmp_path: Path, monkeypatch: Any):
    """无插件目录时 enable 返回不存在"""
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", tmp_path / "none1")
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "none2")
    svc = _make_service(tmp_path, monkeypatch)
    result = asyncio.run(svc.set_plugin_enabled("ghost", True))
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_service_think_default_and_retry_override(tmp_path: Path, monkeypatch: Any):
    """send 未显式传 think 用会话默认; retry 传入 think 时按指定强度重发不回落默认"""
    recorded: list[Any] = []

    class _RecLLM(_FakeAsyncLLM):
        async def stream_call(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
            recorded.append(kw.get("thinking"))
            async for ev in super().stream_call(messages, **kw):
                yield ev

    monkeypatch.setattr(service_mod, "build_llm", lambda cfg: _RecLLM())
    reg = ChatPluginRegistry(state_path=tmp_path / "plugins.json")
    svc = ChatService(
        _as_model_config(_FakeModelConfig()),
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    async def _run() -> None:
        cid = await svc.create_conversation(model="default", think="high")
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.default_think == "high"
        result = await svc.send(cid, "你好")   # 不传 think -> 用会话默认 high
        assert result["ok"] is True and conv.task is not None
        await conv.task
        meta = get_conversation_meta(cid, db_path=str(svc._display_db_path))
        assert meta is not None and meta["think"] == "high"
        # retry 显式传入 think 覆盖默认
        cid2 = await svc.create_conversation(model="default", think="off")
        conv2 = svc.get_conversation(cid2)
        assert conv2 is not None
        sent = await svc.send(cid2, "你好", think="low")
        assert sent["ok"] is True and sent["conversation_id"] == cid2
        assert sent["turn_index"] == 0 and sent["variant_index"] == 0
        assert conv2.task is not None
        await conv2.task
        retried = await svc.retry(cid2, think="high")
        assert retried["ok"] is True and conv2.task is not None
        await conv2.task
        await svc.close()

    asyncio.run(_run())
    assert recorded == ["high", "low", "high"]


def test_service_retry_preserves_variants_and_switches_context(tmp_path: Path, monkeypatch: Any):
    """Retry 保留旧回复, 新生成不携带旧回答, 左右切换同步模型上下文"""
    model_inputs: list[list[dict[str, Any]]] = []

    class _VariantLLM(_FakeAsyncLLM):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_call(
            self,
            messages: list[dict[str, Any]],
            **kw: Any,
        ) -> AsyncIterator[LLMCallStreamEvent]:
            self.calls += 1
            model_inputs.append([dict(message) for message in messages])
            answer = f"版本{self.calls}"
            yield LLMCallStreamEvent(kind="content_delta", delta=answer)
            yield LLMCallStreamEvent(
                kind="done",
                response=LLMCallResponse(type="answer", content=answer),
            )

    def build_variant_llm(_config: LLMConfig) -> _VariantLLM:
        """返回支持回复版本测试的模型替身"""
        return _VariantLLM()

    monkeypatch.setattr(service_mod, "build_llm", build_variant_llm)
    svc = ChatService(
        _as_model_config(_FakeModelConfig()),
        ChatPluginRegistry(state_path=tmp_path / "plugins.json"),
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = svc.get_conversation(cid)
        assert conv is not None
        first = await svc.send(cid, "问题")
        assert first["turn_index"] == 0
        assert conv.task is not None
        await conv.task

        retried = await svc.retry(cid)
        assert retried["turn_index"] == 0 and retried["variant_index"] == 1
        assert conv.task is not None
        await conv.task
        turns = svc.list_turns(cid)
        assert len(turns) == 1
        assert turns[0]["variant_count"] == 2 and turns[0]["active_variant"] == 1
        assert [item["answer"] for item in turns[0]["variants"]] == ["版本1", "版本2"]
        assert not any(message.get("content") == "版本1" for message in model_inputs[1])

        selected = await svc.select_variant(cid, 0, 0)
        assert selected["ok"] is True and selected["turn"]["answer"] == "版本1"
        assert conv.session.ctx.get_context()[-1]["content"] == "版本1"
        selected = await svc.select_variant(cid, 0, 1)
        assert selected["ok"] is True and selected["turn"]["answer"] == "版本2"
        assert conv.session.ctx.get_context()[-1]["content"] == "版本2"

    asyncio.run(_run())


def test_service_fork_copies_display_and_model_context(tmp_path: Path, monkeypatch: Any):
    """Fork 同时复制展示轮次和模型实际使用的上下文"""
    def build_fake_llm(_config: LLMConfig) -> _FakeAsyncLLM:
        """返回 Fork 测试使用的异步模型替身"""
        return _FakeAsyncLLM()

    monkeypatch.setattr(service_mod, "build_llm", build_fake_llm)
    svc = ChatService(
        _as_model_config(_FakeModelConfig()),
        ChatPluginRegistry(state_path=tmp_path / "plugins.json"),
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        source = svc.get_conversation(cid)
        assert source is not None
        await svc.send(cid, "第一问")
        assert source.task is not None
        await source.task
        await svc.send(cid, "第二问")
        assert source.task is not None
        await source.task

        result = await svc.fork(cid, 1)
        forked = svc.get_conversation(result["conversation_id"])
        assert forked is not None and result["copied_turns"] == 1
        assert len(forked.recorder.list_turns()) == 1
        context = forked.session.ctx.get_context()
        assert any(message.get("content") == "第一问" for message in context)
        assert any(message.get("content") == "答复" for message in context)
        assert not any(message.get("content") == "第二问" for message in context)

    asyncio.run(_run())


def test_service_memory_failure_propagation(tmp_path: Path, monkeypatch: Any):
    """记忆管理接口: 存储层失败必须传播为 ok=False (不再无条件假成功)"""

    monkeypatch.setattr(ms_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    svc = _make_service(tmp_path, monkeypatch)

    scope = "session:test-memory"
    result = svc.add_memory("  ", "内容", scope=scope)
    # 空标题/空内容 -> 失败传播
    assert result["ok"] is False and result["error"]

    added = svc.add_memory("标题", "内容", tags="a, b", importance=3, scope=scope)
    # 正常添加
    assert added["ok"] is True
    memory_id = added["memory"]["memory_id"]

    assert svc.update_memory("不存在的id", content="x", scope=scope)["ok"] is False
    # 更新/删除不存在的 ID -> 失败传播
    assert svc.delete_memory("不存在的id", scope=scope)["ok"] is False

    assert svc.update_memory(memory_id, content="新内容", scope=scope)["ok"] is True
    # 正常更新/删除仍成功
    assert svc.delete_memory(memory_id, scope=scope)["ok"] is True


async def test_concurrent_retry_must_reserve_conversation(tmp_path):
    recorder = DisplayRecorder(str(tmp_path / "display.db"), "audit")
    recorder.start_turn("original")
    recorder.end_turn("answer", context_messages=[{"role": "user", "content": "original"}])
    entered = asyncio.Event()
    release = asyncio.Event()
    counts = {"deletions": 0, "runs": 0}

    async def delete_turn(n):
        counts["deletions"] += 1
        entered.set()
        await release.wait()   # 固定在生产重试流程的上下文回退 await 处制造交错

    async def run_turn(*args, **kwargs):
        counts["runs"] += 1

    conv = SimpleNamespace(task=None, recorder=recorder, default_think="off", plugin_lock=asyncio.Lock(), session=SimpleNamespace(
        ctx=SimpleNamespace(del_last_chat=delete_turn, static_message=lambda: 0, get_context=lambda: []),
    ))
    service = ChatService.__new__(ChatService)
    service._conversations = {"audit": cast(service_mod._Conversation, conv)}
    service._operations = {}
    service._broadcast = lambda conv, msg: None
    service._run_turn = run_turn
    tasks = []
    try:
        tasks.append(asyncio.create_task(service.retry("audit")))
        await asyncio.wait_for(entered.wait(), 2)
        tasks.append(asyncio.create_task(service.retry("audit")))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        await asyncio.sleep(0)
        assert counts["deletions"] == 1 and sum(bool(r["ok"]) for r in results) == 1, (
            f"counts={counts}, results={results}"
        )
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if conv.task is not None:
            await conv.task
        recorder.close()


@pytest.mark.asyncio
async def test_visual_attachments_rejected_before_turn_creation(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    try:
        cid = await service.create_conversation()
        conv = service.get_conversation(cid)
        assert conv is not None
        upload = service.save_upload(cid, "image.png", b"image")
        attachment = {"name": "image.png", "url": upload["file_url"], "type": upload["file_type"]}
        rejected = await service.send(cid, "看图", attachments=[attachment])
        assert rejected["ok"] is False
        assert "未启用" in rejected["error"]
        assert conv.recorder.list_turns() == []
        assert conv.task is None
        conv.session.llm.supports_visual_input = True
        images, videos = service._attachment_media(conv, [attachment])
        assert images == [str(Path(upload["file_url"]).resolve())]
        assert videos == []
        preview = service.preview_media(cid, upload["file_url"])
        assert preview["data_url"] == "data:image/png;base64,aW1hZ2U="
        server = ChatHTTPServer(service)
        from urllib.parse import urlencode
        status, body = await server._route("GET", "/api/chat/media?" + urlencode({
            "conversation": cid, "source": upload["file_url"],
        }), b"")
        assert status == 200 and body == preview
        foreign = tmp_path / "private.png"
        foreign.write_bytes(b"private")
        with pytest.raises(ValueError, match="当前会话已上传"):
            service.preview_media(cid, str(foreign))
        rejected = await service.send(cid, "看图", attachments=[{**attachment, "url": str(foreign)}])
        assert rejected["ok"] is False
        assert "当前会话已上传" in rejected["error"]
        assert conv.recorder.list_turns() == []
        other = await service.create_conversation()
        other_upload = service.save_upload(other, "other.mp4", b"video")
        with pytest.raises(ValueError, match="当前会话已上传"):
            service._attachment_media(conv, [{"url": other_upload["file_url"], "type": "video/mp4"}])
        video = service.save_upload(cid, "video.mp4", b"video")
        assert service._attachment_media(conv, [{"url": video["file_url"], "type": "video/mp4"}]) == (
            [], [str(Path(video["file_url"]).resolve())],
        )
    finally:
        await service.close()
