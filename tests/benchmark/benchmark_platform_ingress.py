"""
平台入站与配置面性能基线: OneBot 原始 payload 到调度器判定的每消息成本, 自动参与窗口, 出站拆分, 配置重载与手动唤醒幂等检查

运行:
    python -B tests/benchmark/benchmark_platform_ingress.py --output tests/benchmark/results/platform/before.json --repeats 5

场景:
- ingress_explicit  : explicit 模式, 未唤醒群消息, 50 群白名单 + 50 条群覆盖 + 3 条时段规则, 2000 条/轮
- ingress_frequency : frequency 模式同上, 窗口预灌满 512 路由 × 32 条
- window_observe    : 预灌满 WakeWindow(512, 32) 后单次 observe
- outbound_split    : split_components 32000 字符含非文本组件
- config_reload     : 50 平台配置文件的 reload_platform_policies (后端未运行, 仅校验与指纹)
- manual_wake_check : 512 条记录下 ManualWakeRequests.check

监测指标:
- 每次操作耗时的均值与 p95 (µs)
- ingress 场景下 resolve_wake_settings / normalize_group_whitelist / normalize_wake_words 的每消息调用次数
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from pathlib import Path
import importlib.metadata
import statistics
import subprocess
import argparse
import tempfile
import hashlib
import platform
import asyncio
import logging
import json
import time
import sys
import os

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.disable(logging.CRITICAL)

from satrap.core.platform.onebot.adapter import OneBotAdapter  # noqa: E402
from satrap.core.platform.onebot.outbound import split_components  # noqa: E402
from satrap.core.pipeline.scheduler import PipelineScheduler  # noqa: E402
from satrap.core.pipeline.wake_window import WakeWindow  # noqa: E402
from satrap.core.pipeline.manual_wake import ManualWakeRequests, ManualWakeTicket  # noqa: E402
from satrap.core.platform import PlatformConfig  # noqa: E402
from satrap.core.components import Image, Plain  # noqa: E402
from satrap.core.config import platform_policy, wake_overrides  # noqa: E402
from satrap.core.pipeline import wake_policy  # noqa: E402

SOURCE_FILES = [
    "satrap/core/pipeline/scheduler.py", "satrap/core/pipeline/wake_policy.py", "satrap/core/pipeline/wake_window.py",
    "satrap/core/platform/event.py", "satrap/core/platform/onebot/adapter.py", "satrap/core/platform/onebot/outbound.py",
    "satrap/core/config/wake_overrides.py", "satrap/core/config/platform_policy.py", "satrap/core/backend/BackendManager.py",
    "satrap/core/pipeline/manual_wake.py",
]


def source_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def summarize(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "mean_us": statistics.fmean(ordered) * 1e6,
        "p50_us": ordered[len(ordered) // 2] * 1e6,
        "p95_us": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1e6,
        "count": len(ordered),
    }


class CallCounter:
    """包装模块函数计数, 用于观察每消息重复解析次数"""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self._originals: list[tuple[object, str, object]] = []

    def wrap(self, module: object, name: str) -> None:
        original = getattr(module, name)
        self._originals.append((module, name, original))

        def wrapped(*args: object, **kwargs: object) -> object:
            self.counts[name] = self.counts.get(name, 0) + 1
            return original(*args, **kwargs)

        setattr(module, name, wrapped)

    def restore(self) -> None:
        for module, name, original in self._originals:
            setattr(module, name, original)


def policy_settings(mode: str, whitelist: bool = True) -> dict[str, object]:
    return {
        "wake_mode": mode, "wake_words": ["小助手", "satrap"], "wake_aliases": ["小萨"],
        "group_whitelist": [str(100000 + i) for i in range(50)] if whitelist else [],
        "wake_group_overrides": {str(100000 + i): {"wake_message_threshold": 4 + i % 3} for i in range(50)},
        "wake_time_rules": [
            {"start": "00:00", "end": "06:00", "settings": {"wake_mode": "explicit"}},
            {"start": "06:00", "end": "18:00", "settings": {"wake_cooldown": 20}},
            {"start": "18:00", "end": "23:59", "settings": {"wake_cooldown": 40}},
        ],
        "wake_message_threshold": 32, "wake_cooldown": 30,
    }


def group_payload(index: int, group: str = "100007") -> dict[str, object]:
    return {"self_id": 10000, "user_id": 3000 + index % 40, "group_id": int(group), "message_id": index,
            "message_type": "group", "post_type": "message",
            "message": [{"type": "text", "data": {"text": f"普通聊天内容第 {index} 条, 没有唤醒词"}}]}


async def bench_ingress(mode: str, messages: int, prefill_window: bool) -> dict[str, object]:
    adapter = OneBotAdapter(PlatformConfig(id="bench", type="onebot", settings=policy_settings(mode, whitelist=not prefill_window)))
    adapter._bot = AsyncMock()
    manager = AsyncMock()
    scheduler = PipelineScheduler(manager)
    if prefill_window:
        window = scheduler.wake_window
        for route in range(window.max_routes):
            for item in range(window.max_messages):
                await adapter._handle_group_message(group_payload(route * 100 + item, group=str(200000 + route)))
                event = adapter._event_queue.get_nowait()
                window.observe(event)
    counter = CallCounter()
    counter.wrap(wake_overrides, "resolve_wake_settings")
    counter.wrap(platform_policy, "normalize_group_whitelist")
    counter.wrap(wake_policy, "normalize_wake_words")
    # 事件构造时也会调用解析函数, 通过 event 模块引用计数
    from satrap.core.platform import event as event_module
    counter.wrap(event_module, "resolve_wake_settings")
    from satrap.core.pipeline import scheduler as scheduler_module
    counter.wrap(scheduler_module, "resolve_wake_settings")
    counter.wrap(scheduler_module, "normalize_group_whitelist")
    samples: list[float] = []
    try:
        for index in range(messages):
            payload = group_payload(index)
            started = time.perf_counter()
            await adapter._handle_group_message(payload)
            event = adapter._event_queue.get_nowait()
            await scheduler.execute(event)
            samples.append(time.perf_counter() - started)
    finally:
        counter.restore()
        await scheduler.wake_timers.close()
    assert manager.handle_call_async.await_count == 0, "基线消息不应触发模型"
    return {**summarize(samples), "calls_per_message": {name: count / messages for name, count in counter.counts.items()}}


async def bench_window_observe(iterations: int) -> dict[str, object]:
    adapter = OneBotAdapter(PlatformConfig(id="bench", type="onebot", settings=policy_settings("frequency", whitelist=False)))
    adapter._bot = AsyncMock()
    window = WakeWindow()
    events = []
    for route in range(window.max_routes):
        for item in range(window.max_messages):
            await adapter._handle_group_message(group_payload(route * 100 + item, group=str(100000 + route)))
            event = adapter._event_queue.get_nowait()
            window.observe(event)
            events.append(event)
    samples: list[float] = []
    for index in range(iterations):
        event = events[(index * 7919) % len(events)]
        started = time.perf_counter()
        window.observe(event)
        samples.append(time.perf_counter() - started)
    return summarize(samples)


def bench_outbound_split(iterations: int) -> dict[str, object]:
    paragraphs = "\n\n".join("这是第 %d 段正文, 包含若干句子。" % i * 12 for i in range(60))
    components = [Plain(paragraphs[:16000]), Image(file="https://example.invalid/a.png"), Plain(paragraphs[:16000])]
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        chunks = split_components(components, 2000)
        samples.append(time.perf_counter() - started)
    return {**summarize(samples), "chunks": len(chunks)}


async def bench_config_reload(platforms: int, iterations: int) -> dict[str, object]:
    from satrap.core.backend.BackendManager import BackendManager
    from satrap.core.config.loader import ConfigLoader

    with tempfile.TemporaryDirectory(prefix="satrap-bench-") as temporary:
        path = Path(temporary) / "config.json"
        entries = [{"id": f"bot{i}", "type": "onebot", "settings": {**policy_settings("explicit"), "port": 9000 + i}} for i in range(platforms)]
        path.write_text(json.dumps({"data_root": str(Path(temporary) / "data"), "platforms": entries}), encoding="utf-8")
        backend = BackendManager(ConfigLoader.from_json(path))
        for entry in entries:
            backend._platform_active_configs[entry["id"]] = json.loads(json.dumps(entry))
        samples: list[float] = []
        for _ in range(iterations):
            started = time.perf_counter()
            await backend.reload_platform_policies()
            samples.append(time.perf_counter() - started)
    return {**summarize(samples), "platforms": platforms}


def bench_manual_wake_check(iterations: int) -> dict[str, object]:
    requests = ManualWakeRequests()
    for index in range(511):
        ticket = ManualWakeTicket(request_id=f"r{index}")
        ticket.status = "processed"
        requests.records[ticket.request_id] = ("fp", time.monotonic(), ticket)
    samples: list[float] = []
    for index in range(iterations):
        started = time.perf_counter()
        requests.check(f"probe{index}", "fp")
        samples.append(time.perf_counter() - started)
    return summarize(samples)


async def run_once(messages: int) -> dict[str, object]:
    return {
        "ingress_explicit": await bench_ingress("explicit", messages, prefill_window=False),
        "ingress_frequency": await bench_ingress("frequency", messages // 4, prefill_window=True),
        "window_observe": await bench_window_observe(2000),
        "outbound_split": bench_outbound_split(200),
        "config_reload": await bench_config_reload(50, 5),
        "manual_wake_check": bench_manual_wake_check(2000),
    }


def merge_repeats(runs: list[dict[str, object]]) -> dict[str, object]:
    """多轮取每指标中位数, 保留首轮的非数值字段"""
    merged: dict[str, object] = {}
    for scenario in runs[0]:
        first = runs[0][scenario]
        assert isinstance(first, dict)
        merged[scenario] = {
            key: statistics.median(float(run[scenario][key]) for run in runs) if isinstance(value, (int, float)) and key != "count" else value  # type: ignore[index]
            for key, value in first.items()
        }
    return merged


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "tests" / "benchmark" / "results" / "platform" / "local.json")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--messages", type=int, default=2000)
    args = parser.parse_args()
    before = source_hashes()
    await run_once(200)
    runs = [await run_once(args.messages) for _ in range(args.repeats)]
    if before != source_hashes():
        raise RuntimeError("基线期间源代码发生变化, 本次结果不应保存")
    result = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version, "platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count(),
            "dependencies": {name: importlib.metadata.version(name) for name in ("aiocqhttp", "pytest")},
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8").strip(),
            "source_sha256": before, "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "method": {"repeats": args.repeats, "warmup_messages": 200, "messages": args.messages, "aggregation": "median across repeats", "network": "none; OneBot bot mocked"},
        "scenarios": merge_repeats(runs),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, data in result["scenarios"].items():
        assert isinstance(data, dict)
        extra = f" calls/msg={ {k: round(v, 2) for k, v in data['calls_per_message'].items()} }" if "calls_per_message" in data else ""
        print(f"{name:20s} mean={data['mean_us']:9.1f}us p95={data['p95_us']:9.1f}us{extra}")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
