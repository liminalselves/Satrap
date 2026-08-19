"""display 层 ChatPluginRegistry / ChatService 单元测试"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.display import service as service_mod
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.recorder import list_conversations
from satrap.display.service import ChatService


# ---------------- ChatPluginRegistry ----------------


def _write_plugin(base: Path, name: str, tools: dict[str, str] | None = None) -> Path:
    """在指定目录写一个最小插件 (meta.yaml)"""
    pdir = base / name
    pdir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"name": name, "version": "0.1.0", "description": f"{name} 插件"}
    if tools:
        meta["tools"] = tools
    dumped = cast(Any, yaml).safe_dump(meta, allow_unicode=True)  # pyyaml 无完整类型声明
    (pdir / "meta.yaml").write_text(dumped if isinstance(dumped, str) else "", encoding="utf-8")
    return pdir


def test_registry_scan_default_not_installed(tmp_path: Path, monkeypatch: Any):
    """扫描到插件但默认不启用 (enabled=False)"""
    preset = tmp_path / "preset"
    _write_plugin(preset, "plug_a", tools={"shell": "执行命令", "read_file": "读文件"})
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", preset)
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "user")

    reg = ChatPluginRegistry(state_path=tmp_path / "state.json")
    plugins = reg.scan()
    assert len(plugins) == 1
    p = plugins[0]
    assert p["name"] == "plug_a"
    assert p["enabled"] is False  # 默认不启用
    tool_names = [t["name"] for t in p["capabilities"]["tools"]]
    assert "shell" in tool_names and "read_file" in tool_names


def test_registry_enable_persist(tmp_path: Path, monkeypatch: Any):
    """启用状态写入 json 且跨实例持久"""
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

    # 新实例读取同一 json
    reg2 = ChatPluginRegistry(state_path=state)
    assert reg2.is_enabled("plug_b") is True


def test_registry_capability_toggle(tmp_path: Path):
    """能力独立启停状态持久化, 非法类别返回 False"""
    reg = ChatPluginRegistry(state_path=tmp_path / "state.json")
    assert reg.set_capability("plug_c", "tools", "shell", False) is True
    assert reg.capability_enabled("plug_c", "tools", "shell") is False
    assert reg.capability_enabled("plug_c", "tools", "other") is True  # 默认 True
    assert reg.set_capability("plug_c", "bad_kind", "x", True) is False


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


# ---------------- ChatService ----------------


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
        return LLMConfig(name=name, model="m", api_key="k", base_url="http://x")


def _make_service(tmp_path: Path, monkeypatch: Any) -> ChatService:
    """构造 ChatService, build_llm 替换为 fake"""
    def _fake_build_llm(cfg: Any) -> AsyncLLM:
        return _FakeAsyncLLM()

    monkeypatch.setattr(service_mod, "build_llm", _fake_build_llm)
    reg = ChatPluginRegistry(state_path=tmp_path / "plugins.json")
    return ChatService(
        _FakeModelConfig(),  # type: ignore[arg-type]
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
    )


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

    # WS 事件: turn_start + thinking + content*2 + turn_done
    types = [e["type"] for e in events]
    assert "turn_start" in types
    assert "thinking_delta" in types
    assert types.count("content_delta") == 2
    assert "turn_done" in types

    # turns 落库
    turns = svc.list_turns(cid)
    assert len(turns) == 1
    assert turns[0]["user_input"] == "你好"
    assert turns[0]["answer"] == "答复"
    assert "思考中" in (turns[0]["thinking"] or "")

    # 会话列表
    convs = list_conversations(str(svc._display_db_path))
    assert any(c["conversation_id"] == cid for c in convs)


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


def test_service_plugin_enable_no_plugins(tmp_path: Path, monkeypatch: Any):
    """无插件目录时 enable 返回不存在"""
    monkeypatch.setattr("satrap.display.plugins.PLUGINS_PRESET_DIR", tmp_path / "none1")
    monkeypatch.setattr("satrap.display.plugins.USER_PLUGINS_DIR", tmp_path / "none2")
    svc = _make_service(tmp_path, monkeypatch)
    result = asyncio.run(svc.set_plugin_enabled("ghost", True))
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_service_send_default_think(tmp_path: Path, monkeypatch: Any):
    """send 未显式传 think 时使用会话默认 (create_conversation 持久化到 meta)"""
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
        _FakeModelConfig(),  # type: ignore[arg-type]
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
    )

    async def _run() -> tuple[str, str]:
        cid = await svc.create_conversation(model="default", think="high")
        conv = svc.get_conversation(cid)
        assert conv is not None and conv.default_think == "high"
        result = await svc.send(cid, "你好")  # 不传 think -> 用会话默认 high
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
    """retry 传入 think 时按指定强度重发, 不回落默认"""
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
        _FakeModelConfig(),  # type: ignore[arg-type]
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
    )

    async def _run() -> list[Any]:
        cid = await svc.create_conversation(model="default", think="off")
        conv = svc.get_conversation(cid)
        assert conv is not None
        assert await svc.send(cid, "你好", think="low") == {"ok": True, "conversation_id": cid}
        assert conv.task is not None
        await conv.task
        result = await svc.retry(cid, think="high")
        assert result["ok"] is True
        assert conv.task is not None
        await conv.task
        return list(recorded)

    thinks = asyncio.run(_run())
    assert thinks == ["low", "high"]


def test_service_memory_failure_propagation(tmp_path: Path, monkeypatch: Any):
    """记忆管理接口: 存储层失败必须传播为 ok=False (不再无条件假成功)"""
    from satrap.expend.tools import memory_store as ms_mod

    monkeypatch.setattr(ms_mod, "DEFAULT_MEMORY_DB", tmp_path / "memory.db")
    svc = _make_service(tmp_path, monkeypatch)

    # 空标题/空内容 -> 失败传播
    result = svc.add_memory("  ", "内容")
    assert result["ok"] is False and result["error"]

    # 正常添加
    added = svc.add_memory("标题", "内容", tags="a, b", importance=3)
    assert added["ok"] is True
    memory_id = added["memory"]["memory_id"]

    # 更新/删除不存在的 ID -> 失败传播
    assert svc.update_memory("不存在的id", content="x")["ok"] is False
    assert svc.delete_memory("不存在的id")["ok"] is False

    # 正常更新/删除仍成功
    assert svc.update_memory(memory_id, content="新内容")["ok"] is True
    assert svc.delete_memory(memory_id)["ok"] is True
