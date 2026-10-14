"""
插件热路径时间与内存基线, 用于验证授权缓存, 声明缓存与记忆/提醒分页优化

运行:
    python -B tests/benchmark/benchmark_plugin_hotpath.py --output tests/benchmark/results/plugin_hotpath/baseline.json --repeats 7

场景:
- tool_defs_step    : 每个模型步经真实工具管理器过滤 group_admin 全部工具, 管理组 1/20/200
- tool_execute_auth : 异步只读工具一次完整执行 (直接调用与真实管理器调用, 平台调用为替身)
- factory_bind      : 直接工厂构造并绑定声明 (meta.yaml 声明按路径缓存)
- memory_inject_turn: 会话记忆注入块, 记忆 100/1000/10000 条
- memory_handle_leak: 连续 1000 次注入后的句柄与 RSS 增量
- scoped_list_page  : 群记忆首页与末页分页, 记忆 10000 条
- reminder_page     : 群提醒首页与末页分页, 提醒 10000 条
- loop_blocking     : 控制服务健康检查与上传读盘的阻塞操作, 事件循环延迟与离屏判定

每个场景记录耗时中位数/p95, tracemalloc 峰值, RSS 与句柄增量, 关键函数调用次数, SQL 语句与行数,
以及规范化输出哈希; 优化前后哈希不同即视为语义变化
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
import threading
import tempfile
import argparse
import aiohttp
import asyncio
import logging
import sqlite3
import json
import sys
import time
import gc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.disable(logging.CRITICAL)

from satrap.core.platform import PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig, current_adapter_manager, set_current_adapter_manager  # noqa: E402
from satrap.core.platform.misskey.client import MisskeyAPI  # noqa: E402
from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore  # noqa: E402
from satrap.core.config.administrator_groups import AdministratorService  # noqa: E402
from satrap.core.config.platform_identity import platform_instance_id  # noqa: E402
from satrap.core.utils.TCBuilder.async_manager import AsyncToolsManager  # noqa: E402
from satrap.core.utils.TCBuilder.manager import ToolsManager  # noqa: E402
from satrap.core.platform.onebot.adapter import OneBotAdapter  # noqa: E402
from satrap.core.call_context import CallOrigin, bind_call_origin  # noqa: E402
from satrap.core.backend import control_server  # noqa: E402
from satrap.expend.plugins.group_admin.tools import get_tools  # noqa: E402
from satrap.core.group_chat.reminders import ReminderStore  # noqa: E402
from satrap.core.platform.misskey import client as misskey_client  # noqa: E402
from satrap.core.memory.scoped import ScopedMemories  # noqa: E402
from satrap.core.memory.service import MemoryService  # noqa: E402
from satrap.core.memory.store import MemoryStore  # noqa: E402
from satrap.core.framework.Base import Session  # noqa: E402
from satrap.edictum import AsyncSimpleSession  # noqa: E402

from _harness import CallCounter, count_sql, environment, loop_lag, measure, measure_async, result_hash, source_hashes, summarize, write_result, _handles  # noqa: E402

SOURCE_FILES = [
    "satrap/core/plugin_authorization.py", "satrap/core/model_tool_authorization.py", "satrap/core/config/administrator_groups.py",
    "satrap/core/utils/TCBuilder/manager_base.py", "satrap/expend/plugins/group_admin/tools.py", "satrap/core/memory/store.py",
    "satrap/core/memory/scoped.py", "satrap/core/memory/service.py", "satrap/core/group_chat/reminders.py",
    "satrap/core/backend/control_server.py", "satrap/core/platform/misskey/client.py",
]

COUNTED = [
    "satrap.core.config.administrator_groups:AdministratorService.snapshot",
    "satrap.core.config.administrator_groups:AdministratorService.resolve",
    "satrap.core.plugin_authorization:evaluate_plugin_permissions",
    "satrap.edictum.plugin:load_plugin_meta",
]

NOW = 1_800_000_000.0
PLATFORM = {"id": "ob", "instance_id": "original"}
ORIGIN = CallOrigin(adapter_id="ob", self_id="10000", chat_type="GroupMessage", chat_id="456",
                    actor_id="123", source_message_id="77", request_id="r1")
SCOPE = MessageScope("ob", "10000", "group", "456")


def _setup_adapter() -> OneBotAdapter:
    """装配真实适配器管理器, 平台调用使用替身"""
    registry = PlatformAdapterRegistry()
    registry.register("onebot", OneBotAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="ob", type="onebot", settings={}))
    assert isinstance(adapter, OneBotAdapter)
    bot = AsyncMock()
    bot.get_group_honor_info.return_value = {}
    adapter._bot = bot
    adapter.bot_self_id = "10000"
    set_current_adapter_manager(manager)
    return adapter


def _admin_groups(count: int) -> list[dict[str, Any]]:
    """
    生成管理组, 仅最后一组包含当前调用者

    参数:
    - count: 管理组数量

    返回:
    - 管理组配置列表
    """
    instance = platform_instance_id(PLATFORM)
    return [{"id": f"g{i}", "name": f"组{i}", "enabled": True,
             "members": [{"platform_id": "ob", "platform_instance_id": instance, "user_id": "123" if i == count - 1 else f"9{i}"}],
             "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": []}} for i in range(count)]


class _FakeAsync(AsyncSimpleSession):
    """仅用于选择异步工具类型的会话替身"""

    def __init__(self) -> None:
        pass


def _observe(call: Callable[[], Any], counter: CallCounter) -> tuple[dict[str, int | str], dict[str, int]]:
    """
    额外执行一次并统计函数调用与 SQL

    参数:
    - call: 被测调用
    - counter: 已激活的计数器

    返回:
    - 调用计数与 SQL 计数
    """
    counter.snapshot()
    with count_sql() as stats:
        call()
    return counter.snapshot(), dict(stats)


def tool_defs_step(repeats: int, counter: CallCounter) -> dict[str, Any]:
    """模拟每个模型步经真实工具管理器的定义过滤"""
    results: dict[str, Any] = {}
    for count in (1, 20, 200):
        _setup_adapter()
        manager = current_adapter_manager()
        assert manager is not None
        manager.administrator_service = AdministratorService(lambda: [PLATFORM], _admin_groups(count))
        registry = ToolsManager()
        for tool in get_tools(cast(Session, object()), {}):
            registry.register_tool(tool)

        def step() -> list[str]:
            with bind_call_origin(ORIGIN):
                return [item["function"]["name"] for item in registry.get_tools_definitions()]
        metrics, names = measure(step, repeats)
        calls, sql = _observe(step, counter)
        results[f"admin_groups_{count}"] = {**metrics, "calls": calls, "sql": sql, "result_hash": result_hash(names)}
    return results


async def tool_execute_auth(repeats: int, counter: CallCounter) -> dict[str, Any]:
    """异步只读工具一次完整执行, 分别经直接调用与真实管理器"""
    _setup_adapter()
    manager = current_adapter_manager()
    assert manager is not None
    manager.administrator_service = AdministratorService(lambda: [PLATFORM], _admin_groups(20))
    tool = next(item for item in get_tools(_FakeAsync(), {}) if item.get_tool_name() == "group_admin_get_honors")
    registry: AsyncToolsManager = AsyncToolsManager()
    registry.register_tool(tool)

    async def run() -> Any:
        with bind_call_origin(ORIGIN):
            return await cast(Any, tool).execute()

    async def run_managed() -> Any:
        with bind_call_origin(ORIGIN):
            return await registry.execute_tool("group_admin_get_honors", {})

    metrics, output = await measure_async(run, repeats)
    counter.snapshot()
    await run()
    calls = counter.snapshot()
    managed_metrics, managed_output = await measure_async(run_managed, repeats)
    counter.snapshot()
    await run_managed()
    managed_calls = counter.snapshot()
    return {"admin_groups_20": {**metrics, "calls": calls, "result_hash": result_hash(output)},
            "admin_groups_20_managed": {**managed_metrics, "calls": managed_calls, "result_hash": result_hash(managed_output)}}


def factory_bind(repeats: int, counter: CallCounter) -> dict[str, Any]:
    """直接工厂构造并绑定声明"""
    _setup_adapter()

    def build() -> list[str]:
        return [tool.get_tool_name() for tool in get_tools(cast(Session, object()), {})]
    metrics, names = measure(build, repeats)
    calls, sql = _observe(build, counter)
    return {"group_admin": {**metrics, "calls": calls, "sql": sql, "result_hash": result_hash(names)}}


def _seed_memories(db: Path, scope: str, count: int, kind: str = "conversation") -> None:
    """
    直接写入记忆行, 避免逐条事务拖慢准备阶段

    参数:
    - db: 数据库路径, 表结构需已建立
    - scope: 记忆作用域键
    - count: 条数
    - kind: 记忆类型
    """
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO memories (id, scope, title, content, tags, importance, created_at, updated_at, kind, owner_user_id, purpose_key, revision, origin)"
            " VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?, '', '', 1, 'legacy')",
            [(f"m{i:06d}", scope, f"标题{i}", f"内容{i} " * 8, i % 5, f"2026-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}",
              f"2026-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}", kind) for i in range(count)])
    connection.close()


def memory_inject_turn(repeats: int, counter: CallCounter, workdir: Path) -> dict[str, Any]:
    """会话记忆注入块"""
    results: dict[str, Any] = {}
    for count in (100, 1000, 10000):
        db = workdir / f"memory-{count}.db"
        store = MemoryStore(db, "full", scope="conv-1")
        _seed_memories(db, "conv-1", count)
        service = MemoryService(store, session=None)
        metrics, block = measure(service.context_block, repeats)
        calls, sql = _observe(service.context_block, counter)
        results[f"memories_{count}"] = {**metrics, "calls": calls, "sql": sql, "result_hash": result_hash(block)}
    return results


def memory_handle_leak(workdir: Path) -> dict[str, Any]:
    """连续注入后的句柄与连接回收"""
    db = workdir / "memory-leak.db"
    store = MemoryStore(db, "full", scope="conv-1")
    _seed_memories(db, "conv-1", 100)
    service = MemoryService(store, session=None)
    gc.collect()
    before = _handles()
    gc.disable()
    try:
        for _ in range(1000):
            service.context_block()
        without_gc = _handles() - before
    finally:
        gc.enable()
    gc.collect()
    return {"calls_1000": {"handle_delta_without_gc": without_gc, "handle_delta_after_gc": _handles() - before}}


def _page_pair(first: Callable[[], dict[str, Any]], follow: Callable[[str], dict[str, Any]]) -> Callable[[], list[list[str]]]:
    """
    构造首页加翻页到末页的调用

    参数:
    - first: 读取首页
    - follow: 按游标读取下一页

    返回:
    - 返回首页与末页条目 ID 的无参调用
    """
    def run() -> list[list[str]]:
        page = first()
        head = page
        while page["has_more"]:
            page = follow(page["next_cursor"])
        key = "memory_id" if head["items"] and "memory_id" in head["items"][0] else "reminder_id" if head["items"] and "reminder_id" in head["items"][0] else "id"
        return [[str(item.get(key)) for item in head["items"]], [str(item.get(key)) for item in page["items"]]]
    return run


def scoped_list_page(repeats: int, counter: CallCounter, workdir: Path) -> dict[str, Any]:
    """群记忆首页, 以及从首页翻到末页 (共 200 页)"""
    db = workdir / "scoped.db"
    archive = PlatformMessageStore(db, "ob", clock=lambda: NOW)
    memories = ScopedMemories(archive, SCOPE)
    _seed_memories(db, SCOPE.key, 10000, "group_rule")
    first = lambda: memories.list(limit=50, viewer="123")  # noqa: E731
    results: dict[str, Any] = {}
    metrics, output = measure(first, repeats)
    calls, sql = _observe(first, counter)
    results["first_page_10000"] = {**metrics, "calls": calls, "sql": sql, "result_hash": result_hash(output["items"])}
    walk = _page_pair(first, lambda cursor: memories.list(limit=50, viewer="123", cursor=cursor))
    metrics, pages = measure(walk, max(3, repeats // 2))
    calls, sql = _observe(walk, counter)
    results["walk_all_pages_10000"] = {**metrics, "calls": calls, "sql": sql, "result_hash": result_hash(pages)}
    return results


def reminder_page(repeats: int, counter: CallCounter, workdir: Path) -> dict[str, Any]:
    """群提醒首页, 以及从首页翻到末页 (共 200 页)"""
    db = workdir / "reminders.db"
    store = ReminderStore(db, clock=lambda: NOW)
    scope_json = json.dumps(asdict(SCOPE), ensure_ascii=False)
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO group_chat_reminders (reminder_id, scope_key, scope_json, creator_id, creator_kind, source_message_id, text, "
            "mentions_json, due_at, created_at) VALUES (?, ?, ?, '123', 'model', '77', ?, '[]', ?, ?)",
            [(f"r{i:06d}", SCOPE.key, scope_json, f"提醒{i}", NOW + 60 + i, NOW) for i in range(10000)])
    connection.close()
    first = lambda: store.list(SCOPE, actor="123", limit=50)  # noqa: E731
    results: dict[str, Any] = {}
    metrics, output = measure(first, repeats)
    calls, sql = _observe(first, counter)
    results["first_page_10000"] = {**metrics, "calls": calls, "sql": sql, "result_hash": result_hash(output["items"])}
    walk = _page_pair(first, lambda cursor: store.list(SCOPE, actor="123", limit=50, cursor=cursor))
    metrics, pages = measure(walk, max(3, repeats // 2))
    calls, sql = _observe(walk, counter)
    results["walk_all_pages_10000"] = {**metrics, "calls": calls, "sql": sql, "result_hash": result_hash(pages)}
    return results


_BLOCKING_SECONDS = 0.2
"""loop_blocking 场景中每次阻塞操作的模拟时长"""

_UPLOAD_BYTES = 256 * 1024
"""loop_blocking 场景上传的本地文件大小"""


class _UploadResponse:
    """上传响应替身: 只实现 _process_response 用到的调用面"""

    status = 200

    async def json(self) -> dict[str, Any]:
        """
        返回固定文件 ID

        返回:
        - 平台响应体
        """
        return {"id": "file-1"}


class _UploadContext:
    """上传请求的异步上下文替身"""

    async def __aenter__(self) -> _UploadResponse:
        return _UploadResponse()

    async def __aexit__(self, *_: object) -> bool:
        return False


class _UploadSession:
    """上传会话替身, 不产生真实网络请求"""

    def __init__(self) -> None:
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> _UploadContext:
        """
        接收上传请求

        参数:
        - url: 请求地址
        - kwargs: 请求参数

        返回:
        - 上传请求上下文
        """
        return _UploadContext()

    async def close(self) -> None:
        """记录会话关闭"""
        self.closed = True


def _max_lag(lags: list[dict[str, float]]) -> dict[str, float]:
    """
    汇总各轮事件循环延迟

    参数:
    - lags: 每次 loop_lag 返回的最大与 p95 延迟

    返回:
    - 最差的最大延迟与 p95 延迟毫秒
    """
    return {"max_ms": max(item["max_ms"] for item in lags), "p95_ms": max(item["p95_ms"] for item in lags)}


def _offloaded(threads: list[threading.Thread]) -> bool:
    """
    判定阻塞函数是否全部在工作线程执行

    参数:
    - threads: 本次采样记录到的调用线程

    返回:
    - 有调用且都不在事件循环线程时返回 True
    """
    return bool(threads) and all(thread is not threading.main_thread() for thread in threads)


async def _control_status_blocking(repeats: int) -> dict[str, Any]:
    """
    GET /status: 健康检查阻塞 200 毫秒时的事件循环延迟

    参数:
    - repeats: 采样次数

    返回:
    - 耗时, 循环延迟与包含离屏判定的结果哈希
    """
    original = control_server._check_backend_health
    threads: list[threading.Thread] = []

    def slow_health(*_: Any, **__: Any) -> dict[str, Any]:
        """模拟阻塞的健康检查"""
        threads.append(threading.current_thread())
        time.sleep(_BLOCKING_SECONDS)
        return {"running": False}

    async def request_once() -> control_server.ControlResponse:
        context = control_server._RouteContext("GET", "/status", "/status", asyncio.StreamReader(), b"")
        response = await control_server._route_ui_config_status(context)
        if response is None:
            raise RuntimeError("控制路由未命中 /status")
        return response

    setattr(control_server, "_check_backend_health", slow_health)
    try:
        await request_once()
        # 预热一次, 排除线程池与会话首次创建的影响
        times: list[float] = []
        lags: list[dict[str, float]] = []
        offloaded: list[bool] = []
        for _ in range(repeats):
            threads.clear()
            start = time.perf_counter()
            lags.append(await loop_lag(request_once))
            times.append((time.perf_counter() - start) * 1000)
            offloaded.append(_offloaded(threads))
        response = await request_once()
    finally:
        setattr(control_server, "_check_backend_health", original)
    return {"elapsed_ms": summarize(times), "loop_lag_ms": _max_lag(lags),
            "result_hash": result_hash({"offloaded": all(offloaded), "response": response})}


async def _misskey_upload_blocking(repeats: int, workdir: Path) -> dict[str, Any]:
    """
    本地文件上传: 读盘阻塞 200 毫秒时的事件循环延迟

    参数:
    - repeats: 采样次数
    - workdir: 临时目录

    返回:
    - 耗时, 循环延迟与包含离屏判定的结果哈希
    """
    path = workdir / "upload.bin"
    path.write_bytes(b"x" * _UPLOAD_BYTES)
    api = MisskeyAPI("https://misskey.example", "token")
    api._session = cast("aiohttp.ClientSession", _UploadSession())
    original = misskey_client._read_file_bytes
    threads: list[threading.Thread] = []

    def slow_read(file_path: str) -> bytes:
        """模拟阻塞的读盘并保留真实内容"""
        threads.append(threading.current_thread())
        time.sleep(_BLOCKING_SECONDS)
        return original(file_path)

    async def upload_once() -> dict[str, Any]:
        return await api.upload_file(str(path))

    setattr(misskey_client, "_read_file_bytes", slow_read)
    try:
        await upload_once()
        # 预热一次, 排除线程池与会话首次创建的影响
        times: list[float] = []
        lags: list[dict[str, float]] = []
        offloaded: list[bool] = []
        for _ in range(repeats):
            threads.clear()
            start = time.perf_counter()
            lags.append(await loop_lag(upload_once))
            times.append((time.perf_counter() - start) * 1000)
            offloaded.append(_offloaded(threads))
        result = await upload_once()
    finally:
        setattr(misskey_client, "_read_file_bytes", original)
    return {"elapsed_ms": summarize(times), "loop_lag_ms": _max_lag(lags),
            "result_hash": result_hash({"offloaded": all(offloaded), "file_id": result.get("id")})}


async def loop_blocking(repeats: int, counter: CallCounter, workdir: Path) -> dict[str, Any]:
    """事件循环停顿: 阻塞操作由工作线程执行, 循环延迟保持在毫秒级"""
    control = await _control_status_blocking(repeats)
    upload = await _misskey_upload_blocking(repeats, workdir)
    return {"control_status": {**control, "calls": counter.snapshot()}, "misskey_upload": {**upload, "calls": counter.snapshot()}}


async def main() -> None:
    """解析参数, 依次运行全部场景并写出结果"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("--repeats 至少为 3")
    if args.output.exists():
        parser.error(f"输出已存在: {args.output}")
    sources = source_hashes(SOURCE_FILES)
    scenarios: dict[str, Any] = {}
    counter = CallCounter(COUNTED)
    with tempfile.TemporaryDirectory() as temp, counter.active():
        workdir = Path(temp)
        scenarios["tool_defs_step"] = tool_defs_step(args.repeats, counter)
        scenarios["tool_execute_auth"] = await tool_execute_auth(args.repeats, counter)
        scenarios["factory_bind"] = factory_bind(args.repeats, counter)
        scenarios["memory_inject_turn"] = memory_inject_turn(args.repeats, counter, workdir)
        scenarios["memory_handle_leak"] = memory_handle_leak(workdir)
        scenarios["scoped_list_page"] = scoped_list_page(args.repeats, counter, workdir)
        scenarios["reminder_page"] = reminder_page(args.repeats, counter, workdir)
        scenarios["loop_blocking"] = await loop_blocking(args.repeats, counter, workdir)
        set_current_adapter_manager(None)
        gc.collect()
    if source_hashes(SOURCE_FILES) != sources:
        raise SystemExit("运行期间被测源码发生变化, 结果作废")
    write_result(args.output, {"benchmark": "plugin_hotpath", "repeats": args.repeats,
                               "environment": environment(Path(__file__), sources), "scenarios": scenarios})
    print(f"已写出 {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
