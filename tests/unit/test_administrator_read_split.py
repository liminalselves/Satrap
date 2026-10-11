"""管理员配置先显示, 运行状态挂起时保持只读与原保存契约"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, cast

import pytest

from satrap.core.config.document import load_config_document, save_config_document
from satrap.core.utils.async_worker import WorkerBusyError
from tests.unit.test_administrator_settings_api import context, document


@pytest.mark.asyncio
async def test_config_snapshot_does_not_wait_for_runtime_and_never_applies(tmp_path, monkeypatch):
    """
    运行宿主挂起时设置仍可读, 所有新增 GET 不写入或应用

    参数:
    - tmp_path: 临时配置目录
    - monkeypatch: 读取与宿主等待观察
    """
    from satrap.core.backend import control_server as control

    path = tmp_path / "admin.json"
    saved = save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    original_load = control.load_config_document
    main_thread = threading.get_ident()
    reads = []
    calls = []
    entered, release = threading.Event(), threading.Event()

    def load(source):
        """
        检查文件读取不占用事件循环线程

        参数:
        - source: 原配置路径

        返回:
        - 原配置文档
        """
        reads.append(threading.get_ident())
        return original_load(source)

    def runtime(revision, *, apply=False):
        """
        挂起只读状态, 不模拟设置快照

        参数:
        - revision: 待确认修订
        - apply: 应始终为 False

        返回:
        - 原修订的已确认状态
        """
        calls.append(apply)
        entered.set()
        if not release.wait(2):
            raise RuntimeError("验收未释放运行状态查询")
        return {"status": "applied", "section_revision": revision}

    monkeypatch.setattr(control, "load_config_document", load)
    monkeypatch.setattr(control, "_administrator_runtime_request", runtime)
    root = "/config/administrator-groups"
    first = cast(Any, await control._route_administrator_settings(context(control, "GET", root + "/snapshot")))
    assert first and first[0] == 200 and first[1]["runtime"]["status"] == "unconfirmed"
    assert calls == []
    task = asyncio.create_task(control._route_administrator_settings(context(control, "GET", root + "/runtime")))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        snapshot = cast(Any, await asyncio.wait_for(control._route_administrator_settings(context(control, "GET", root + "/snapshot")), .5))
        assert snapshot and snapshot[1]["revision"] == first[1]["revision"]
        assert not task.done()
    finally:
        release.set()
        result = cast(Any, await task)
    assert result and result[1]["runtime"]["status"] == "applied"
    assert result[1]["section_revision"] == first[1]["section_revision"]
    legacy = cast(Any, await control._route_administrator_settings(context(control, "GET", root)))
    assert legacy and legacy[1]["runtime"]["status"] == "applied"
    assert all(thread != main_thread for thread in reads)
    assert calls == [False, False] and load_config_document(path) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/snapshot", "/runtime"])
async def test_administrator_read_busy_queue_returns_explicit_failure(tmp_path, monkeypatch, suffix):
    """
    读取队列满时明确失败并记录日志, 不进入保存事务

    参数:
    - tmp_path: 临时配置目录
    - monkeypatch: 工作队列与日志观察
    - suffix: 原同步读取或新增分离读取入口
    """
    from satrap.core.backend import control_server as control

    path = tmp_path / "admin.json"
    saved = save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    messages = []

    async def busy(*args, **kwargs):
        """
        模拟有界工作队列已满

        参数:
        - args: 原工作请求
        - kwargs: 原工作参数
        """
        raise WorkerBusyError("验收队列已满")

    monkeypatch.setattr(control.RAG_WORKERS, "run", busy)
    monkeypatch.setattr(control.logger, "warning", messages.append)
    result = cast(Any, await control._route_administrator_settings(context(control, "GET", "/config/administrator-groups" + suffix)))
    assert result[0] == 503 and "队列已满" in result[1]["error"]
    assert messages and "队列已满" in messages[0]
    assert load_config_document(path) == saved

