"""隔离进程的真实控制/Chat/平台 HTTP 服务, 模型与插件等待由临时文件控制"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from satrap.core.backend import control_server
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.server_auth import ServerAuth
from satrap.core.utils.application_progress import ApplicationOperations
from satrap.display import service as chat_module
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.server import ChatHTTPServer
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.plugin_catalog import PluginCatalog
from tests.unit.test_display_service import _FakeAsyncLLM, _make_service, _must_conversation
from tests.support.platform_application import _platforms, _release


async def publish(path: Path, payload: dict[str, Any]) -> None:
    """
    原子发布验收状态, 允许 Windows 读取句柄短暂阻止替换

    参数:
    - path: 临时目录内的状态文件
    - payload: 不含凭据的实际状态
    """
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    deadline = time.monotonic() + 1
    while True:
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(.01)


async def main() -> None:
    """启动独立服务并保存脱敏的实际会话状态"""
    kind, directory, ready_file, *ports = sys.argv[1:]
    root = Path(directory).resolve()
    local = root / kind
    local.mkdir(parents=True, exist_ok=True)
    patches = pytest.MonkeyPatch()
    patches.setattr("satrap.edictum.plugin_catalog.USER_PLUGINS_DIR", root / "plugins")
    patches.setattr("satrap.display.plugins.USER_PLUGINS_DIR", root / "plugins")
    globals_ = PluginConfigManager(root / "globals")
    for module in ("satrap.edictum.plugin_apply", "satrap.edictum.plugin_settings", "satrap.display.service", "satrap.core.backend.control_server"):
        patches.setattr(module + ".PluginConfigManager", lambda: globals_)
    sessions: list[Any] = []
    held: Any = None
    providers: list[Any] = []
    chat: Any = None
    model_tasks: list[Any] = []
    counters = {"install": 0, "uninstall": 0, "model_build": 0}
    plugin = "application_probe"

    if kind == "chat":
        chat = _make_service(local, patches)

        class WaitingModel(_FakeAsyncLLM):
            """使用原模型调用面, 等待后返回固定离线结果"""

            async def stream_call(self, messages, **kwargs):
                """
                等待验收释放正在执行的模型请求

                参数:
                - messages: 实际会话投影
                - kwargs: 实际模型参数

                返回:
                - 原离线模型的流式事件
                """
                (local / "model_waiting").touch()
                while not (root / "release_locks").exists():
                    await asyncio.sleep(.02)
                async for event in super().stream_call(messages, **kwargs):
                    yield event

        def build(_config):
            """
            记录实际模型构建入口并提供离线模型

            参数:
            - _config: 原模型配置

            返回:
            - 等待模型
            """
            counters["model_build"] += 1
            return WaitingModel()

        patches.setattr(chat_module, "build_llm", build)
        chat._plugins.set_enabled(plugin, True)
        ids = [await chat.create_conversation(model="default") for _ in range(2)]
        convs = [_must_conversation(chat, cid) for cid in ids]
        sessions = [conv.session for conv in convs]
        server = ChatHTTPServer(chat, port=0)
    elif kind == "backend":
        runtime, configs, _globals, sessions, providers = await _platforms(local, patches, plugin_name=plugin, globals_manager=globals_)
        backend = BackendManager(BackendConfig(source_path=str(root / "main.json"), data_root=str(local / "data")))
        backend._edictum_cfg = configs
        backend._platform_runtimes = runtime._platform_runtimes
        backend._running = True
        held = backend._platform_runtimes["one"][0].pool.list_entries()["one-0"].async_operation_lock
        await held.acquire()
        server = BackendHTTPServer(backend, port=0, static_dir=local)
    else:
        patches.setattr(control_server, "CONFIG_PATH", root / "main.json")
        patches.setattr(control_server, "BACKEND_PID_FILE", local / "backend.pid")
        patches.setattr(control_server, "CONTROL_PID_FILE", local / "control.pid")
        patches.setattr(control_server, "_configured_backend_address", lambda: ("127.0.0.1", int(ports[1])))
        patches.setenv("SATRAP_CHAT_PORT", ports[0])
        patches.setattr(control_server, "ChatPluginRegistry", lambda: ChatPluginRegistry(root / "chat" / "plugins.json"))
        patches.setattr(control_server, "_PLUGIN_APPLICATIONS", ApplicationOperations())
        patches.setattr(control_server, "_CONTROL_AUTH", ServerAuth.create("127.0.0.1", 0))
        original_read = control_server._read_plugin_global_config
        original_load = PluginCatalog._load_entry
        loads: dict[str, int] = {}
        load_lock = threading.Lock()

        def counted_load(path):
            """
            统计真实元数据解析, 不改变目录加载结果

            参数:
            - path: 原插件目录

            返回:
            - 原目录条目
            """
            with load_lock:
                key = str(path)
                loads[key] = loads.get(key, 0) + 1
            return original_load(path)

        patches.setattr(PluginCatalog, "_load_entry", staticmethod(counted_load))

        def slow_read(*args, **kwargs):
            """
            在原读取前模拟慢存储, 不替换真实领域服务

            参数:
            - args: 原读取参数
            - kwargs: 原读取关键字参数

            返回:
            - 实际读取结果
            """
            if (root / "slow_reads").exists():
                time.sleep(.2)
            return original_read(*args, **kwargs)

        patches.setattr(control_server, "_read_plugin_global_config", slow_read)
        listener = await asyncio.start_server(control_server._handle_request, "127.0.0.1", 0)
        Path(ready_file).write_text(json.dumps({"port": listener.sockets[0].getsockname()[1], "pid": os.getpid()}), encoding="utf-8")
        try:
            while not (root / "stop").exists():
                with load_lock:
                    observed = dict(loads)
                await publish(local / "state.json", {"catalog_parses": observed})
                await asyncio.sleep(.02)
        finally:
            listener.close()
            await listener.wait_closed()
            patches.undo()
        return

    original_handles = [id(session.list_plugins()[0]) for session in sessions]
    for session in sessions:
        install, uninstall = session.install_plugin, session.uninstall_plugin

        async def installed(*args, original=install, **kwargs):
            """
            暂停实际安装并在释放后执行原生命周期

            参数:
            - args: 原安装参数
            - original: 当前会话原安装方法
            - kwargs: 原安装关键字参数

            返回:
            - 实际安装结果
            """
            counters["install"] += 1
            while not (root / "release_install").exists():
                await asyncio.sleep(.02)
            return await original(*args, **kwargs)

        async def uninstalled(*args, original=uninstall, **kwargs):
            """
            记录实际卸载次数, 不替换卸载流程

            参数:
            - args: 原卸载参数
            - original: 当前会话原卸载方法
            - kwargs: 原卸载关键字参数

            返回:
            - 实际卸载结果
            """
            counters["uninstall"] += 1
            return await original(*args, **kwargs)

        patches.setattr(session, "install_plugin", installed)
        patches.setattr(session, "uninstall_plugin", uninstalled)
    await server.start()
    if server._server is None:
        raise RuntimeError("隔离服务没有启动")
    if chat is not None:
        await chat.send(ids[0], "保留正在执行的模型请求")
        model_tasks = [convs[0].task]
    Path(ready_file).write_text(json.dumps({"port": server._server.sockets[0].getsockname()[1], "pid": os.getpid()}), encoding="utf-8")
    try:
        while not (root / "stop").exists():
            if held is not None and held.locked() and (root / "release_locks").exists():
                held.release()
                held = None
            state = {"pid": os.getpid(), "instances": len(sessions), **counters,
                "values": [(await session.cmd_process("/about"))[0] if session.list_plugins() else None for session in sessions],
                "same_handles": [bool(session.list_plugins()) and id(session.list_plugins()[0]) == old for session, old in zip(sessions, original_handles)],
                "model_tasks": [{"done": task.done(), "cancelled": task.cancelled()} for task in model_tasks if task is not None]}
            await publish(local / "state.json", state)
            await asyncio.sleep(.02)
    finally:
        if held is not None and held.locked():
            held.release()
        await server.stop()
        if chat is not None:
            await chat.close()
        else:
            await _release(sessions, providers)
        patches.undo()


if __name__ == "__main__":
    asyncio.run(main())
