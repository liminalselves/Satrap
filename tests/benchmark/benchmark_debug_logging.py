"""合成输入对照高级诊断的关闭开销, 共享存储增长和懒读取, 不读取用户数据库"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import platform
import sqlite3
import statistics
import time
import tracemalloc
from unittest.mock import patch
import psutil

from satrap.core.log.debug import capture
from satrap.core.log.debug.contracts import DebugData, DebugEvent, DebugObject
from satrap.core.log.debug.policy import DebugPolicy
from satrap.core.log.debug.model import complete_sync
from satrap.core.log.debug.store import DebugStore


def timing(samples: list[float]) -> dict[str, float]:
    """
    汇总微秒级对照样本

    参数:
    - samples: 相同操作的独立秒数样本

    返回:
    - P50, P95 与最大值, 单位微秒
    """
    ordered = sorted(samples)
    return {"p50_us": statistics.median(samples) * 1e6, "p95_us": ordered[int((len(ordered)-1)*.95)] * 1e6, "max_us": max(samples)*1e6}


def corpus(count: int) -> list[DebugEvent]:
    """
    构造实际事件协议的合成请求, 相同模型历史只更换末条用户输入

    参数:
    - count: 请求数量, 每请求两个事件

    返回:
    - 不含真实身份或凭据的有限语料
    """
    obj = DebugObject("benchmark", "bot", "group", "room")
    history: list[DebugData] = [{"role": "system", "content": "稳定系统提示与工具说明" * 100},
                              *[{"role": "user" if index % 2 == 0 else "assistant", "content": f"共享历史{index}:" + "上下文片段" * 40} for index in range(8)]]
    result = []
    for index in range(count):
        event = DebugEvent(f"request{index:06}", obj, f"context{index // 100}", "合成上下文", f"start{index}", 0, "request", "start", "", "", 1.0 + index, "", {},
                           {"input": {"model": "synthetic", "messages": [*history, {"role": "user", "content": f"新问题{index}"}]}}, "benchmark")
        result.extend((event, replace(event, event_id=f"end{index}", sequence=1, phase="end", status="success", bodies={})))
    return result


def storage(root: Path, count: int) -> dict[str, object]:
    """
    同语料对照原样 JSON 存储与生产诊断编码

    参数:
    - root: 本次隔离输出目录
    - count: 合成请求数量

    返回:
    - 实际占用, 编码峰值和目录/详情读取耗时
    """
    events = corpus(count)
    baseline = root / f"plain-{count}.sqlite"
    tracemalloc.start()
    started = time.perf_counter()
    with closing(sqlite3.connect(baseline)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE events(request_id TEXT,sequence INTEGER,body TEXT,PRIMARY KEY(request_id,sequence))")
        for offset in range(0, len(events), 64):
            with db:
                db.executemany("INSERT INTO events VALUES(?,?,?)", ((event.request_id, event.sequence, json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":"))) for event in events[offset:offset+64]))
    plain_seconds = time.perf_counter() - started
    _, plain_peak = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    store = DebugStore(root / f"shared-{count}.sqlite", "backend")
    rss_before = psutil.Process().memory_info().rss
    started = time.perf_counter()
    for offset in range(0, len(events), 64):
        store.append(events[offset:offset+64])
    append_seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = psutil.Process().memory_info().rss
    reads: dict[str, dict[str, float]] = {}
    for name, operation in (("contexts", lambda: store.contexts(limit=50)), ("requests", lambda: store.requests(limit=50)),
                            ("events", lambda: store.events("request000000")), ("body", lambda: store.body("request000000", "start0", "input"))):
        samples = []
        for _ in range(20):
            began = time.perf_counter(); operation(); samples.append(time.perf_counter()-began)
        reads[name] = timing(samples)
    for index in (0, count // 2, count-1):
        assert store.body(f"request{index:06}", f"start{index}", "input") == events[index*2].bodies["input"]
    stats = store.stats()
    return {"requests": count, "events": len(events), "plain_bytes": baseline.stat().st_size, "shared_bytes": stats["database_bytes"], "wal_bytes": stats["wal_bytes"],
            "plain_append_seconds": plain_seconds, "shared_append_seconds": append_seconds, "capture_peak_bytes": peak,
            "plain_peak_bytes": plain_peak, "rss_delta_bytes": rss_after-rss_before, "reads": reads}


def request_cost(root: Path) -> dict[str, object]:
    """
    在同一模型返回替身上比较原调用, 开关关闭和开启的真实采集边界

    参数:
    - root: 隔离配置与诊断库目录

    返回:
    - 前台耗时, 后台排空耗时及不完整数量, 不包含真实网络延迟
    """
    obj = DebugObject("benchmark", "bot", "group", "room")
    response = {"choices": [{"message": {"content": "合成回答"}}]}
    request = {"model": "synthetic", "messages": [{"role": "user", "content": "合成输入"}]}
    def create(**kwargs: object) -> object:
        """
        返回固定合成响应

        参数:
        - kwargs: 与 SDK 相同的调用参数

        返回:
        - 固定结果对象
        """
        return response
    results = {}
    writer = capture.DebugWriter(DebugStore(root / "capture.sqlite", "backend"), capacity=4096)
    try:
        with patch.object(capture, "debug_writer", return_value=writer):
            for mode in ("baseline", "off", "on"):
                settings = capture.policy_store()
                settings.save(DebugPolicy(default_enabled=mode == "on"), settings.read().revision)
                samples = []
                for index in range(1000):
                    start = time.perf_counter()
                    if mode == "baseline":
                        actual = create(**request)
                    else:
                        with capture.debug_request(obj, f"{mode}-{index}", context_id="context", input_data="合成输入"):
                            actual = complete_sync(create, request, 60)
                    assert actual is response
                    samples.append(time.perf_counter()-start)
                    if mode == "on" and index % 20 == 19:
                        assert writer.flush(20)   # 使用有限并发批次, 不把无等待的模型替身当作真实吞吐
                drain = time.perf_counter(); assert writer.flush(20)
                results[mode] = {**timing(samples), "final_drain_seconds": time.perf_counter()-drain}
    finally:
        writer.close()
    return results


async def session_cost(root: Path) -> dict[str, object]:
    """
    对照真实异步会话的两次模型调用与一次工具调用, 网络由固定 SDK 返回替身隔离

    参数:
    - root: 本轮隔离配置, 业务数据库和诊断库目录

    返回:
    - 三组相同会话工作量的请求耗时, 事件循环延迟与进程内存采样
    """
    from contextlib import nullcontext
    from dataclasses import replace as update
    from openai.types.chat import ChatCompletion
    from satrap import AsyncLLM
    from satrap.edictum import AsyncSimpleSession
    from satrap.core.utils.TCBuilder import AsyncTool
    from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot

    class Echo(AsyncTool):
        tool_name = "benchmark_echo"
        description = "返回固定合成资料"
        params_dict = {}

        async def execute(self) -> dict[str, str]:
            """
            返回无外部副作用的固定工具结果

            返回:
            - 合成工具资料
            """
            await asyncio.sleep(0)
            return {"result": "合成工具结果"}

    async def create(**kwargs: object) -> ChatCompletion:
        """
        按实际工具回合返回固定 SDK 资料, 不访问网络

        参数:
        - kwargs: 实际 SDK 请求, 以末条工具结果决定回合

        返回:
        - 请求工具或最终回答的正式 SDK 响应
        """
        await asyncio.sleep(0)
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        done = messages[-1].get("role") == "tool"
        message = {"role": "assistant", "content": "合成完成" if done else None}
        if not done:
            message["tool_calls"] = [{"id": "benchmark-call", "type": "function", "function": {"name": "benchmark_echo", "arguments": "{}"}}]
        return ChatCompletion.model_validate({"id": "synthetic", "created": 1, "model": "synthetic", "object": "chat.completion",
                    "choices": [{"index": 0, "finish_reason": "stop" if done else "tool_calls", "message": message}]})

    async def direct(create_call, parameters, timeout):
        """
        绕过 SDK 诊断包装, 保留相同会话与工具执行

        参数:
        - create_call: 实际 SDK 返回替身
        - parameters: 已准备的请求参数
        - timeout: 基线保持同一超时形参, 不额外包裹调用

        返回:
        - 原始替身响应
        """
        return await create_call(**parameters)

    writer = capture.DebugWriter(DebugStore(root / "session-debug.sqlite", "backend"), capacity=4096)
    results = {}
    try:
        with patch.object(capture, "debug_writer", return_value=writer):
            for mode in ("baseline", "off", "on"):
                settings = capture.policy_store()
                settings.save(DebugPolicy(default_enabled=mode == "on"), settings.read().revision)
                llm = AsyncLLM(api_key="synthetic", base_url="http://127.0.0.1:9/v1", model="synthetic", supports_visual_input=False)
                session = AsyncSimpleSession(f"benchmark-{mode}", llm, db_path=str(root / f"business-{mode}.sqlite"))
                bind_environment(session, update(environment_snapshot(session), session_root=root / f"session-{mode}"))
                samples, lag, rss = [], [], []
                async def heartbeat() -> None:
                    """按有限频率采样事件循环延迟和真实进程 RSS"""
                    process = psutil.Process()
                    while True:
                        began = time.perf_counter()
                        await asyncio.sleep(.005)
                        lag.append(max(0, time.perf_counter() - began - .005))
                        rss.append(process.memory_info().rss)
                try:
                    await session.initialize()
                    session.tools_manager.register_tool(Echo())
                    monitor = asyncio.create_task(heartbeat())
                    with patch.object(llm.client.chat.completions, "create", create), \
                         patch("satrap.core.log.debug.model.complete_async", direct) if mode == "baseline" else nullcontext():
                        obj = DebugObject("benchmark", "bot", "group", f"room-{mode}")
                        for index in range(30):
                            began = time.perf_counter()
                            scope = nullcontext() if mode == "baseline" else capture.debug_request(obj, f"session-{mode}-{index}", context_id=session.session_id, input_data="合成输入")
                            with scope:
                                assert await session.run("调用一次合成工具") == "合成完成"
                            samples.append(time.perf_counter() - began)
                            if mode == "on":
                                assert await asyncio.to_thread(writer.flush, 20)
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                    results[mode] = {"requests": len(samples), "model_calls_per_request": 2, "tools_per_request": 1,
                                     "request": timing(samples), "loop_lag": timing(lag) if lag else {}, "sampled_peak_rss_bytes": max(rss) if rss else None}
                finally:
                    await llm.client.close()
    finally:
        writer.close()
    results["scope"] = "同版本真实 AsyncSimpleSession.run, 基线绕过 SDK 诊断包装和请求作用域; 无真实网络, RSS 为 5ms 采样峰值"
    return results


def main() -> None:
    """运行隔离基准并保存不含用户数据的 JSON 报告"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="debug-benchmark-", dir=output) as temporary:
        root = Path(temporary).resolve()
        assert root.is_relative_to(output)
        os.environ["SATRAP_CONFIG_ROOT"] = str(root / "config")
        result = {"python": platform.python_version(), "platform": platform.platform(), "synthetic": True,
                  "scope": "实际会话合成 SDK 对照与 SQLite 微基准, 不替代远端服务或真实网络验收", "request_cost": request_cost(root),
                  "session_cost": asyncio.run(session_cost(root)),
                  "storage": [storage(root, count) for count in (100, 1000, 10000)]}
        (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
