from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, cast
from pathlib import Path

import pytest

from satrap.core.backend.BackendManager import BackendManager, logger
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_apply import EdictumPluginApplySnapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plugins", "runtime"])
async def test_scoped_plugin_read_does_not_block_loop_or_reload_unrelated_models(kind: str, tmp_path: Path):
    """
    慢冷配置读取允许其它协程运行, 后续协调保留指定会话范围

    参数:
    - kind: 插件或完整配置协调路径
    - tmp_path: 隔离配置来源
    """
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
    backend._edictum_cfg = SimpleNamespace(reload=reload, storage_path=tmp_path / "named.json", list_configs=lambda: {},
                                         plugin_catalog=PluginCatalog(tmp_path / "builtin", tmp_path / "user"))
    backend._model_cfg = SimpleNamespace(reload=unrelated_reload)
    runtime = SimpleNamespace(reconcile_edictum_plugins_async=reconcile, reconcile_edictum_runtime_async=reconcile,
                              model_config_manager=None,
                              edictum_plugin_apply_scope=lambda **kwargs: {"assistant": False},
                              edictum_runtime_apply_targets=lambda **kwargs: [("one", None, "assistant", "generation")])
    backend._platform_runtimes = {"ob": (runtime, None), "other": (runtime, None)}
    apply = getattr(backend, f"reconcile_edictum_{kind}_async")
    task = asyncio.create_task(apply(
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
    desired_key = "desired_plugins" if kind == "plugins" else "desired_config"
    expected: dict[str, object] = {"config_name": "assistant", "session_ids": {"one"}, desired_key: None, "concurrency": 4}
    snapshot = applied[0].get("apply_snapshot")
    assert isinstance(snapshot, EdictumPluginApplySnapshot)
    expected["apply_snapshot"] = snapshot
    expected["apply_targets"] = [("one", None, "assistant", "generation")]
    assert applied == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plugins", "runtime"])
async def test_read_failure_is_logged_and_does_not_apply_instances(monkeypatch, kind: str, tmp_path: Path):
    """
    配置读取失败记录堆栈, 不沿用旧配置安装插件

    参数:
    - monkeypatch: 日志替换夹具
    - kind: 插件或完整配置协调路径
    - tmp_path: 隔离配置来源
    """
    def failed_read() -> None:
        """模拟文件读取失败"""
        raise OSError("source unavailable")

    messages: list[str] = []
    monkeypatch.setattr(logger, "error", messages.append)
    backend = cast(Any, BackendManager.__new__(BackendManager))
    backend._edictum_cfg = SimpleNamespace(reload=failed_read, storage_path=tmp_path / "named.json")
    backend._platform_runtimes = {}
    with pytest.raises(OSError, match="source unavailable"):
        await getattr(backend, f"reconcile_edictum_{kind}_async")(config_name="assistant")
    assert len(messages) == 1
    assert "Traceback" in messages[0] and "source unavailable" in messages[0]
