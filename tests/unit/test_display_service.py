"""display 层 ChatPluginRegistry / ChatService 单元测试"""
from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
from pathlib import Path
import pytest
from typing import Any, cast
from types import SimpleNamespace
import copy
import yaml

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.display.recorder import DisplayRecorder, list_conversations
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.service import ChatService
from satrap.core.storage import StorageLayout
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, LLMConfig
from satrap.display import service as service_mod


# ---------- ChatPluginRegistry 测试 ----------


def _write_plugin(base: Path, name: str, tools: dict[str, str] | None = None) -> Path:
    """
    在指定目录写一个最小插件 (meta.yaml)

    参数:
    - base: 基础
    - name: 名称
    - tools: 工具列表

    返回:
    - Path: 在指定目录写一个最小插件 (meta.yaml)
    """
    pdir = base / name
    pdir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"name": name, "version": "0.1.0", "description": f"{name} 插件"}
    if tools:
        meta["tools"] = tools
    dumped = cast(Any, yaml).safe_dump(meta, allow_unicode=True)   # pyyaml 无完整类型声明
    (pdir / "meta.yaml").write_text(dumped if isinstance(dumped, str) else "", encoding="utf-8")
    return pdir


def test_registry_scan_default_not_installed(tmp_path: Path, monkeypatch: Any):
    """
    扫描到插件但默认不启用 (enabled=False)

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    preset = tmp_path / "preset"
    _write_plugin(preset, "plug_a", tools={"shell": "执行命令", "read_file": "读文件"})
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", preset)
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "user")

    reg = ChatPluginRegistry(state_path=tmp_path / "state.json")
    plugins = reg.scan()
    assert len(plugins) == 1
    p = plugins[0]
    assert p["name"] == "plug_a"
    assert p["enabled"] is False   # 默认不启用
    tool_names = [t["name"] for t in p["capabilities"]["tools"]]
    assert "shell" in tool_names and "read_file" in tool_names


def test_registry_enable_persist(tmp_path: Path, monkeypatch: Any):
    """
    启用状态写入 json 且跨实例持久

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    preset = tmp_path / "preset"
    _write_plugin(preset, "plug_b")
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", preset)
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "user")

    state = tmp_path / "state.json"
    reg = ChatPluginRegistry(state_path=state)
    assert reg.is_enabled("plug_b") is False
    reg.set_enabled("plug_b", True)
    assert reg.is_enabled("plug_b") is True
    assert state.is_file()

    reg2 = ChatPluginRegistry(state_path=state)
    # 新实例读取同一 json
    assert reg2.is_enabled("plug_b") is True


def test_registry_capability_toggle(tmp_path: Path):
    """
    能力独立启停状态持久化, 非法类别返回 False

    参数:
    - tmp_path: tmp路径
    """
    reg = ChatPluginRegistry(state_path=tmp_path / "state.json")
    assert reg.set_capability("plug_c", "tools", "shell", False) is True
    assert reg.capability_enabled("plug_c", "tools", "shell") is False
    assert reg.capability_enabled("plug_c", "tools", "other") is True   # 默认 True
    assert reg.set_capability("plug_c", "bad_kind", "x", True) is False


def test_registry_official_overrides_user(tmp_path: Path, monkeypatch: Any):
    """
    同名插件官方目录优先

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
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
        from satrap.core.type import LLMConfig
        return LLMConfig(name=name, model="m", api_key="k", base_url="https://x")


def _as_model_config(fake: Any) -> ModelConfigManager:
    """ModelConfigManager 替身类型边界: 替身实现配置查询调用面, cast 集中在此工厂"""
    return cast(ModelConfigManager, fake)


def _make_service(tmp_path: Path, monkeypatch: Any) -> ChatService:
    """
    构造 ChatService, build_llm 替换为 fake

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具

    返回:
    - ChatService: 构造 ChatService, build_llm 替换为 fake
    """
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
        assert conv.recorder.last_turn()["variant_count"] == 1
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
        await svc.get_conversation("shared").task
    finally:
        release.set()
        await asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True)
        await svc.close()


def test_service_list_models(tmp_path: Path, monkeypatch: Any):
    svc = _make_service(tmp_path, monkeypatch)
    assert set(svc.list_models()) == {"default", "fast"}


def test_service_send_and_turns(tmp_path: Path, monkeypatch: Any):
    """
    send 立即返回, 后台 run 完成后 turns 落库且 WS 收到事件

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    生成中的 Chat 会话应在本轮结束后安全应用最新上下文策略

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    ask_user 经 WS 发出问题并由 HTTP 服务层回填回答

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    取消生成会结束等待中的 ask_user 请求

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    预加载只创建内存运行时, 首次发送时才写入会话列表

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
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


def test_service_preload_rebuilds_same_id_after_model_change(tmp_path: Path, monkeypatch: Any):
    """
    首次发送时模型设置变化应在同一 ID 下重建预加载运行时

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    built_models: list[str | None] = []

    def _fake_build_llm(cfg: Any) -> AsyncLLM:
        built_models.append(cfg.name)
        return _FakeAsyncLLM()

    monkeypatch.setattr(service_mod, "build_llm", _fake_build_llm)
    svc = ChatService(
        _as_model_config(_FakeModelConfig()),
        ChatPluginRegistry(state_path=tmp_path / "plugins.json"),
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    async def _run() -> tuple[str, str]:
        cid = await svc.preload_conversation(model="default")
        result = await svc.send(
            cid,
            "切换模型",
            preload_settings={"model": "fast", "system_prompt": "", "project_id": None},
        )
        assert result["ok"] is True
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.task is not None
        await conv.task
        model = conv.model
        await svc.close()
        return cid, model

    cid, model = asyncio.run(_run())
    assert model == "fast"
    assert built_models == ["default", "fast"]
    from satrap.display.recorder import get_conversation_meta
    meta = get_conversation_meta(cid, db_path=str(svc._display_db_path))
    assert meta is not None and meta["model"] == "fast"


def test_service_preload_rebuilds_after_selected_model_config_change(tmp_path: Path, monkeypatch: Any):
    """
    同名模型的参数变化也应使预加载指纹失效

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    from satrap.core.type import LLMConfig

    temperatures: list[float | None] = []

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
        temperatures.append(cfg.temperature)
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
        cid = await svc.preload_conversation(model="default")
        model_config.temperature = 0.9
        result = await svc.send(cid, "参数已修改", preload_settings={"model": "default"})
        assert result["ok"] is True
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.task is not None
        await conv.task
        await svc.close()

    asyncio.run(_run())
    assert temperatures == [0.1, 0.9]


def test_service_runtime_fingerprint_tracks_plugin_capability_and_config(
    tmp_path: Path,
    monkeypatch: Any,
):
    """
    插件能力开关和全局配置变化都应改变会话运行时指纹

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    from satrap.edictum.plugin import load_plugin_meta
    from satrap.edictum.plugin_config import PluginConfigManager, parse_config_schema

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


def test_chat_plugin_capability_updates_active_session(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """
    Chat 子能力开关应通过插件命名空间即时更新活跃会话

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    config_manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: config_manager)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("session_commands", True)
    svc._plugins.set_capability("session_commands", "commands", "about", False)

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = svc.get_conversation(cid)
        assert conv is not None
        plugin = next(item for item in conv.session.list_plugins() if item.name == "session_commands")
        assert plugin.commands["about"] is False

        result = await svc.set_plugin_capability(
            "session_commands",
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
    """
    Chat 插件配置保存后应通过共享协调器热重装活跃会话

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    config_manager = PluginConfigManager(tmp_path / "plugin_config")
    monkeypatch.setattr(service_mod, "PluginConfigManager", lambda: config_manager)
    svc = _make_service(tmp_path, monkeypatch)
    svc._plugins.set_enabled("session_commands", True)

    async def _run() -> None:
        cid = await svc.create_conversation(model="default")
        conv = svc.get_conversation(cid)
        assert conv is not None

        result = await svc.save_plugin_config(
            "session_commands",
            {"about_text": "Chat 热重装说明"},
        )

        assert result["ok"] is True
        assert result["applied"] is True
        assert result["sessions"][0]["plugins"][0]["action"] == "reinstall"
        assert await conv.session.run("/about") == "Chat 热重装说明"
        await svc.close()

    asyncio.run(_run())


def test_service_preload_timeout_purges_empty_runtime(tmp_path: Path, monkeypatch: Any):
    """
    未发送的预加载会话超时后应释放内存并清除私有目录

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    svc = _make_service(tmp_path, monkeypatch)
    svc._preload_ttl_seconds = 0.01

    async def _run() -> tuple[str, Path]:
        cid = await svc.preload_conversation()
        session_root = svc._storage.session_root("chat", cid)
        assert session_root.is_dir()
        await asyncio.sleep(0.05)
        assert svc.get_conversation(cid) is None
        await svc.close()
        return cid, session_root

    cid, session_root = asyncio.run(_run())
    assert not session_root.exists()
    assert all(item["conversation_id"] != cid for item in list_conversations(str(svc._display_db_path)))


def test_service_send_concurrent_rejected(tmp_path: Path, monkeypatch: Any):
    """
    同一会话上一个 run 未完成时拒绝并发 send

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    删除 Chat 会话应清理上下文和会话记忆, 并把全部私有文件移入回收区

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    from satrap.expend.tools.memory_store import MemoryStore

    svc = _make_service(tmp_path, monkeypatch)
    conversation_id = asyncio.run(svc.create_conversation())
    session_root = svc._storage.session_root("chat", conversation_id)
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
    assert memory.count() == 0
    assert svc.list_turns(conversation_id) == []


def test_service_plugin_enable_no_plugins(tmp_path: Path, monkeypatch: Any):
    """
    无插件目录时 enable 返回不存在

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", tmp_path / "none1")
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "none2")
    svc = _make_service(tmp_path, monkeypatch)
    result = asyncio.run(svc.set_plugin_enabled("ghost", True))
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_service_send_default_think(tmp_path: Path, monkeypatch: Any):
    """
    send 未显式传 think 时使用会话默认 (create_conversation 持久化到 meta)

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    recorded: list[Any] = []

    class _RecLLM(_FakeAsyncLLM):
        async def stream_call(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
            recorded.append(kw.get("thinking"))
            async for ev in super().stream_call(messages, **kw):
                yield ev

    def _fake_build_llm(cfg: Any) -> AsyncLLM:
        return _RecLLM()

    monkeypatch.setattr(service_mod, "build_llm", _fake_build_llm)
    reg = ChatPluginRegistry(state_path=tmp_path / "plugins.json")
    svc = ChatService(
        _as_model_config(_FakeModelConfig()),
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    async def _run() -> tuple[str, str]:
        cid = await svc.create_conversation(model="default", think="high")
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.default_think == "high"
        result = await svc.send(cid, "你好")   # 不传 think -> 用会话默认 high
        assert result["ok"] is True
        assert conv.task is not None
        await conv.task
        from satrap.display.recorder import get_conversation_meta
        meta = get_conversation_meta(cid, db_path=str(svc._display_db_path))
        assert meta is not None and meta["think"] == "high"
        return cid, str(recorded[0] if recorded else None)

    cid, first_think = asyncio.run(_run())
    assert first_think == "high"


def test_service_retry_with_think(tmp_path: Path, monkeypatch: Any):
    """
    retry 传入 think 时按指定强度重发, 不回落默认

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    recorded: list[Any] = []

    class _RecLLM(_FakeAsyncLLM):
        async def stream_call(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
            recorded.append(kw.get("thinking"))
            async for ev in super().stream_call(messages, **kw):
                yield ev

    def _fake_build_llm(cfg: Any) -> AsyncLLM:
        return _RecLLM()

    monkeypatch.setattr(service_mod, "build_llm", _fake_build_llm)
    reg = ChatPluginRegistry(state_path=tmp_path / "plugins.json")
    svc = ChatService(
        _as_model_config(_FakeModelConfig()),
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
    )

    async def _run() -> list[Any]:
        cid = await svc.create_conversation(model="default", think="off")
        conv = svc.get_conversation(cid)
        assert conv is not None
        sent = await svc.send(cid, "你好", think="low")
        assert sent["ok"] is True and sent["conversation_id"] == cid
        assert sent["turn_index"] == 0 and sent["variant_index"] == 0
        assert conv.task is not None
        await conv.task
        result = await svc.retry(cid, think="high")
        assert result["ok"] is True
        assert conv.task is not None
        await conv.task
        return list(recorded)

    thinks = asyncio.run(_run())
    assert thinks == ["low", "high"]


def test_service_retry_preserves_variants_and_switches_context(tmp_path: Path, monkeypatch: Any):
    """
    Retry 保留旧回复, 新生成不携带旧回答, 左右切换同步模型上下文

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    Fork 同时复制展示轮次和模型实际使用的上下文

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
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
    """
    记忆管理接口: 存储层失败必须传播为 ok=False (不再无条件假成功)

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    from satrap.expend.tools import memory_store as ms_mod

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
    service._conversations = {"audit": conv}
    service._operations = {}
    service._broadcast = lambda *args: None
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
