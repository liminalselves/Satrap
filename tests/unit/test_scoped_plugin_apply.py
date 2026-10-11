from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.backend.BackendManager import BackendManager, logger


@pytest.mark.asyncio
async def test_scoped_plugin_read_does_not_block_loop_or_reload_unrelated_models():
    """慢冷配置读取允许其它协程运行, 后续协调保留指定会话范围"""
    entered = threading.Event()
    release = threading.Event()
    read_threads: list[int] = []
    applied: list[dict[str, object]] = []

    def reload() -> None:
        """暂停读取以核验事件循环可用性"""
        read_threads.append(threading.get_ident())
        entered.set()
        if not release.wait(3):
            raise TimeoutError("测试未释放读取")

    async def reconcile(**scope: object) -> list[dict[str, object]]:
        """
        保存后端传入的定向协调范围

        参数:
        - scope: 配置名称和会话引用

        返回:
        - 成功协调的会话记录
        """
        applied.append(scope)
        return [{"ok": True, "session_id": "one"}]

    def unrelated_reload() -> None:
        """禁止插件协调调用无关模型重载"""
        raise AssertionError("不应重载模型")

    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._edictum_cfg = SimpleNamespace(reload=reload)
    backend._model_cfg = SimpleNamespace(reload=unrelated_reload)
    runtime = SimpleNamespace(reconcile_edictum_plugins_async=reconcile)
    backend._platform_runtimes = {"ob": (runtime, None), "other": (runtime, None)}
    task = asyncio.create_task(backend.reconcile_edictum_plugins_async(
        config_name="assistant", session_refs=[{"platform_id": "ob", "session_id": "one"}],
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        heartbeat = asyncio.create_task(asyncio.sleep(0, result="alive"))
        assert await asyncio.wait_for(heartbeat, 1) == "alive"
        assert not task.done()
    finally:
        release.set()
    assert await task == [{"ok": True, "session_id": "one"}]
    assert len(read_threads) == 1
    assert read_threads[0] != threading.get_ident()
    assert applied == [{"config_name": "assistant", "session_ids": {"one"}, "desired_plugins": None, "concurrency": 4}]


@pytest.mark.asyncio
async def test_read_failure_is_logged_and_does_not_apply_instances(monkeypatch):
    """配置读取失败记录堆栈, 不沿用旧配置安装插件"""
    def failed_read() -> None:
        """模拟文件读取失败"""
        raise OSError("source unavailable")

    messages: list[str] = []
    monkeypatch.setattr(logger, "error", messages.append)
    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._edictum_cfg = SimpleNamespace(reload=failed_read)
    backend._platform_runtimes = {}
    with pytest.raises(OSError, match="source unavailable"):
        await backend.reconcile_edictum_plugins_async(config_name="assistant")
    assert len(messages) == 1
    assert "Traceback" in messages[0] and "source unavailable" in messages[0]
