"""插件应用的服务范围, 旧引用移除及 Chat 并发锁契约"""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.backend import control_server
from satrap.core.framework.providers.edictum import EdictumProvider
from satrap.core.framework.SessionManager import SessionManager
from satrap.display.service import ChatService
from satrap.display import service as service_module
from satrap.edictum.plugin_spec import PluginSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,location,targets", [(None, None, ["Chat", "Edictum"]), ("chat", "chat", ["Chat"]), ("edictum", "agent-a", ["Edictum"])])
async def test_bridge_only_calls_selected_service(monkeypatch, kind, location, targets):
    """
    使用位置保存只联系对应服务, 全局参数保存保留两个服务

    参数:
    - monkeypatch: 内部请求替换夹具
    - kind: 使用位置类别或全局范围
    - location: 使用位置标识
    - targets: 应被请求的服务名称
    """
    calls = []

    def request(target, url, scope):
        """
        记录实际提交的服务及范围

        参数:
        - target: 服务名称
        - url: 内部接口
        - scope: 目标配置范围

        返回:
        - 模拟成功的应用结果
        """
        calls.append((target, url, dict(scope)))
        return {"target": target, "status": "applied", "sessions": []}

    monkeypatch.setattr(control_server, "_plugin_runtime_request", request)
    result = await control_server._apply_plugin_runtime(plugin_name="测试插件", kind=kind, location_id=location)
    assert [item["target"] for item in result] == targets
    assert sorted(item[0] for item in calls) == sorted(targets)
    expected = {"plugin_name": "测试插件", **({"config_name": location} if kind == "edictum" else {})}
    assert all(item[2] == expected for item in calls)


def test_scoped_internal_request_serializes_utf8(monkeypatch):
    """
    内部范围通过 JSON 请求体传递且保留认证

    参数:
    - monkeypatch: HTTP 请求替换夹具
    """
    def request(req, **kwargs):
        """
        核验内部请求并模拟离线

        参数:
        - req: 待发送的请求
        - kwargs: 网络截止参数
        """
        assert json.loads(req.data.decode("utf-8")) == {"plugin_name": "插件", "config_name": "助手"}
        assert req.get_header("Authorization").startswith("Bearer ")
        raise control_server.urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(control_server.urllib.request, "urlopen", request)
    result = control_server._plugin_runtime_request("Edictum", "http://127.0.0.1:1", {"plugin_name": "插件", "config_name": "助手"})
    assert result["status"] == "next_activation"


def test_provider_reference_filter_keeps_removed_runtime_and_new_reference():
    """旧运行时引用移除后仍需卸载, 新引用及无关配置区分"""
    provider = cast(Any, EdictumProvider.__new__(EdictumProvider))
    state = SimpleNamespace(config_name="agent", plugins=[SimpleNamespace(name="removed")])
    provider._get_runtime_state = lambda session: state
    provider.config_manager = SimpleNamespace(get_config=lambda name: {"plugins": ["new", {"name": "disabled", "enabled": False}]})
    assert provider.references_plugin(object(), "removed")
    assert provider.references_plugin(object(), "new")
    assert provider.references_plugin(object(), "disabled")
    assert not provider.references_plugin(object(), "unrelated")
    assert not provider.references_plugin(object(), "new", desired_plugins=[])


@pytest.mark.asyncio
async def test_session_manager_intersects_plugin_and_named_config_scope():
    """只协调同时满足插件引用与命名配置范围的实例"""
    manager = cast(Any, SessionManager.__new__(SessionManager))
    applied = []

    async def reconcile(session, *, desired_plugins):
        """
        记录被协调的实例

        参数:
        - session: 会话占位
        - desired_plugins: 指定目标覆盖

        返回:
        - 独立成功结果
        """
        applied.append(session.name)
        return {"ok": True}

    provider = SimpleNamespace(reconcile_session_plugins_async=reconcile, references_plugin=lambda session, name, **kwargs: session.name != "unrelated")
    manager.provider_registry = SimpleNamespace(get=lambda name: provider)
    entries = {name: SimpleNamespace(session=SimpleNamespace(name=name, _satrap_provider_name="edictum"), async_operation_lock=asyncio.Lock(),
                                    sync_operation_lock=threading.RLock()) for name in ("matched", "unrelated", "other-config")}
    manager.pool = SimpleNamespace(list_entries=lambda: entries)
    manager.store = SimpleNamespace(get=lambda name: SimpleNamespace(session_type_name="other" if name == "other-config" else "agent"))
    manager.platform_id = "test"
    result = await manager.reconcile_edictum_plugins_async(config_name="agent", plugin_name="target")
    assert applied == ["matched"]
    assert [item["session_id"] for item in result] == ["matched"]


@pytest.mark.asyncio
async def test_chat_bound_concurrency_preserves_lock_and_isolates_failure(monkeypatch):
    """
    锁被占用的会话不阻塞其他会话, 并发有界且单项失败不取消同批

    参数:
    - monkeypatch: 规格解析和生命周期替换夹具
    """
    service = cast(Any, ChatService.__new__(ChatService))
    spec = PluginSpec(name="target")
    service._plugins = SimpleNamespace(resolve_specs=lambda manager: [spec], catalog=object())
    service._coordinate_sandbox = lambda session: None
    sessions = []
    for index in range(5):
        sessions.append(SimpleNamespace(conversation_id=str(index), persisted=True, plugin_states=[], plugin_lock=asyncio.Lock(),
                                        session=SimpleNamespace(install_plugin=str(index), uninstall_plugin=object())))
    service._conversations = {conv.conversation_id: conv for conv in sessions}
    await sessions[0].plugin_lock.acquire()
    started = []
    running = 0
    peak = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def reconcile(states, specs, installer, uninstaller):
        """
        暂停生命周期操作以核验并发和锁

        参数:
        - states: 旧插件状态
        - specs: 目标规格
        - installer: 含会话标识的安装占位
        - uninstaller: 卸载占位

        返回:
        - 独立成功结果, 指定会话模拟失败
        """
        nonlocal running, peak
        started.append(installer)
        running += 1
        peak = max(peak, running)
        if len(started) >= 2:
            two_started.set()
        try:
            await release.wait()
            if installer == "3":
                raise RuntimeError("单个会话安装失败")
            return {"ok": True, "plugins": []}
        finally:
            running -= 1

    monkeypatch.setattr(service_module, "resolve_runtime_specs", lambda session, specs, catalog: specs)
    monkeypatch.setattr(service_module, "reconcile_plugin_states_async", reconcile)
    task = asyncio.create_task(service._reconcile_chat_plugins(plugin_name="target", concurrency=3))
    try:
        await asyncio.wait_for(two_started.wait(), 2)
        assert "0" not in started
        assert peak == 2
        release.set()
        await asyncio.sleep(0)
    finally:
        release.set()
        sessions[0].plugin_lock.release()
    result = await asyncio.wait_for(task, 2)
    assert [item["conversation_id"] for item in result] == [str(index) for index in range(5)]
    assert [item["ok"] for item in result] == [True, True, True, False, True]
    assert peak <= 3
    assert all(not conv.plugin_lock.locked() for conv in sessions)


@pytest.mark.asyncio
async def test_chat_filter_skips_unrelated_and_keeps_old_enabled_reference(monkeypatch):
    """
    关闭或移除旧插件仍协调, 无关预加载实例不失效

    参数:
    - monkeypatch: 生命周期替换夹具
    """
    service = cast(Any, ChatService.__new__(ChatService))
    service._plugins = SimpleNamespace(resolve_specs=lambda manager: [PluginSpec(name="target", enabled=False)], catalog=object())
    service._coordinate_sandbox = lambda session: None
    old = SimpleNamespace(conversation_id="old", persisted=True, plugin_states=[SimpleNamespace(name="target", enabled=True, handle=object())],
                          plugin_lock=asyncio.Lock(), session=SimpleNamespace(install_plugin="old", uninstall_plugin=None))
    unrelated = SimpleNamespace(conversation_id="preload", persisted=False, plugin_states=[], build_fingerprint="unchanged")
    service._conversations = {"old": old, "preload": unrelated}
    applied = []

    async def reconcile(states, specs, installer, uninstaller):
        """
        记录仍需卸载的旧实例

        参数:
        - states: 旧插件状态
        - specs: 目标规格
        - installer: 安装占位
        - uninstaller: 卸载占位

        返回:
        - 成功结果
        """
        applied.append(installer)
        return {"ok": True}

    monkeypatch.setattr(service_module, "resolve_runtime_specs", lambda session, specs, catalog: specs)
    monkeypatch.setattr(service_module, "reconcile_plugin_states_async", reconcile)
    result = await service._reconcile_chat_plugins(plugin_name="target")
    assert applied == ["old"]
    assert len(result) == 1
    assert unrelated.build_fingerprint == "unchanged"
