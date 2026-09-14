"""离线测量同步与异步任务恢复开销, 每个样本使用独立数据库"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import closing
import hashlib
import inspect
import json
import logging
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.type import LLMCallResponse
from satrap.core.utils.TCBuilder import Tool, AsyncTool


class Probe(Tool):
    tool_name = "probe"
    description = "返回固定查询结果"
    params_dict = {"index": ("number", "索引")}
    recovery_policy = "retry"

    def execute(self, index=0):
        return "资料" * 128


class AsyncProbe(AsyncTool):
    tool_name = Probe.tool_name
    description = Probe.description
    params_dict = Probe.params_dict
    recovery_policy = "retry"

    async def execute(self, index=0):
        return "资料" * 128


class ModelCore:
    model = "recovery-benchmark"

    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = 0
        self.requests = []

    def response(self, messages, **kwargs):
        self.calls += 1
        self.requests.append(hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest())
        if self.calls <= self.rounds:
            return LLMCallResponse("tools_call", "", tool_calls=[
                {"id": f"call_{self.calls}_{i}", "name": "probe", "arguments": {"index": i}}
                for i in range(2)
            ])
        return LLMCallResponse("message", "完成")


class Model(ModelCore):
    def call(self, messages, **kwargs):
        return self.response(messages, **kwargs)


class AsyncModel(ModelCore):
    async def call(self, messages, **kwargs):
        return self.response(messages, **kwargs)


async def invoke(method, *args, **kwargs):
    value = method(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


async def sample(root, asynchronous, recoverable, history_messages, rounds):
    db = root / "platform.db"
    counts = Counter()
    measuring = False
    connect = sqlite3.connect

    def trace(statement):
        if measuring:
            verb = statement.lstrip().split(None, 1)[0].upper()
            if verb in {"INSERT", "UPDATE", "DELETE", "COMMIT", "SELECT"}:
                counts[verb] += 1

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(trace)
        return connection

    with patch.object(sqlite3, "connect", traced_connect):
        model = (AsyncModel if asynchronous else Model)(rounds)
        if asynchronous:
            wf = AsyncModelWorkflowFramework(cast(AsyncLLM, model), "benchmark", db_path=str(db), recoverable=recoverable)
            await wf.initialize()
            wf.tools_manager.register_tool(AsyncProbe())
        else:
            wf = ModelWorkflowFramework(cast(LLM, model), "benchmark", db_path=str(db), recoverable=recoverable)
            wf.tools_manager.register_tool(Probe())
        wf.ctx.max_context = 4_000_000  # 保持所有历史进入请求, 避免把截断算法开销混入恢复对比
        wf.ctx.history_budget = 2_800_000
        wf.ctx.trigger_tokens = 2_240_000
        wf.ctx.floor_tokens = 1_120_000
        wf.ctx.output_budget = 1_200_000
        await invoke(wf.ctx.add_turn_messages, [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i}:" + "历史" * 256}
            for i in range(history_messages)
        ])
        before = db.stat().st_size
        measuring = True
        start = time.perf_counter()
        try:
            result = await invoke(wf.full_agent, "执行测试", callback=False, max_iterations=10)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            measuring = False
            if isinstance(wf, ModelWorkflowFramework):
                wf.ctx.close()
        assert result == "完成"
        assert model.calls == rounds + 1
        with closing(connect(db)) as connection:
            history_rows = connection.execute("SELECT count(*) FROM chat_history").fetchone()[0]
            steps = connection.execute("SELECT count(*) FROM agent_steps").fetchone()[0] if recoverable else 0
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            free_bytes = connection.execute("PRAGMA freelist_count").fetchone()[0] * page_size
            payload_bytes = 0
            if recoverable:
                payload_bytes = connection.execute("SELECT coalesce(sum(length(cast(input AS BLOB))),0) FROM agent_steps").fetchone()[0]
                has_bodies = connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_step_inputs'").fetchone()
                if has_bodies:
                    payload_bytes += connection.execute("SELECT coalesce(sum(length(cast(body AS BLOB))),0) FROM agent_step_inputs").fetchone()[0]
        assert history_rows == history_messages + rounds * 3 + 2
        assert steps == (rounds * 3 + 1 if recoverable else 0)
        return {"elapsed_ms": elapsed, "sql": dict(counts), "db_growth_bytes": db.stat().st_size - before,
                "request_payload_bytes": payload_bytes, "db_free_bytes": free_bytes,
                "model_calls": model.calls, "request_hashes": model.requests,
                "history_rows_added": history_rows - history_messages, "steps": steps}


async def main(args):
    logging.disable(logging.CRITICAL)
    results = []
    with tempfile.TemporaryDirectory(prefix="satrap-recovery-benchmark-") as directory:
        root = Path(directory)
        for asynchronous in (False, True):
            for history in (0, 200, 2000):
                for rounds in (0, 3):
                    samples = {False: [], True: []}
                    for repeat in range(args.samples + 1):
                        for enabled in ((False, True) if repeat % 2 == 0 else (True, False)):
                            case = root / f"{asynchronous}-{history}-{rounds}-{repeat}-{enabled}"
                            case.mkdir()
                            result = await sample(case, asynchronous, enabled, history, rounds)
                            if repeat:
                                samples[enabled].append(result)
                    for enabled, rows in samples.items():
                        assert all(row["request_hashes"] == samples[False][0]["request_hashes"] for row in rows)
                        timings = sorted(row["elapsed_ms"] for row in rows)
                        results.append({"async": asynchronous, "recoverable": enabled, "history_messages": history,
                                        "tool_rounds": rounds, "median_ms": statistics.median(timings),
                                        "p95_ms": timings[math.ceil(len(timings) * .95) - 1], "samples": rows})
                    print(f"async={asynchronous} history={history} rounds={rounds}: "
                          f"{statistics.median(r['elapsed_ms'] for r in samples[False]):.1f} -> "
                          f"{statistics.median(r['elapsed_ms'] for r in samples[True]):.1f} ms", flush=True)
    report = {"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
              "samples_per_case": args.samples, "method": "离线固定模型, 每轮 2 工具; 每条历史 512 汉字; 上下文预算 400 万以避免触发截断, 不代表真实模型容量; 不含初始化与历史填充; 首个样本预热丢弃; SQL trace 计数包含计时开销; 文件增长不等于物理磁盘写入量",
              "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results/recovery.json")
    options = parser.parse_args()
    if options.samples < 1:
        parser.error("samples 必须大于 0")
    asyncio.run(main(options))
