"""
语音附件处理性能基线: 魔数探测, PyAV 本地 amr 转码, 以及 resolve_attachments 语音路径 (ASR 与下载均为替身)

运行:
    python -B tests/benchmark/benchmark_audio_attachments.py --output tests/benchmark/results/platform/audio.json --repeats 3

场景:
- audio_probe       : probe_audio 对 16 KiB 语音头部的判定 (wav/silk/amr/未知 轮换)
- amr_convert       : 10 秒 8 kHz AMR-NB 经 convert_to_wav 转 16 kHz 单声道 wav (需 av)
- record_get_record : resolve_attachments, 实现服务端转码返回 wav (无下载, ASR 替身)
- record_local_amr  : resolve_attachments, 实现不支持 get_record, 直接下载 amr 后本地转码 (ASR 替身)

监测指标:
- 每次操作耗时的均值与 p95 (µs)
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from pathlib import Path
from typing import Any
import importlib.metadata
import importlib
import statistics
import subprocess
import argparse
import hashlib
import platform
import asyncio
import logging
import struct
import base64
import math
import json
import time
import sys
import os
import io

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.disable(logging.CRITICAL)

from aiocqhttp.exceptions import ActionFailed  # noqa: E402

from satrap.core.pipeline.audio_convert import convert_to_wav, converter_available, probe_audio  # noqa: E402
from satrap.core.platform.onebot.adapter import OneBotAdapter  # noqa: E402
from satrap.core.pipeline import attachments  # noqa: E402
from satrap.core.platform import PlatformConfig  # noqa: E402
from satrap.core.type import ASRConfig  # noqa: E402
from satrap.core.utils.outbound import OutboundHTTPResponse  # noqa: E402

SOURCE_FILES = [
    "satrap/core/pipeline/audio_convert.py", "satrap/core/pipeline/attachments.py", "satrap/core/platform/onebot/admin.py",
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


def wav_bytes(seconds: float, rate: int = 8000) -> bytes:
    frames = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames))
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


def amr_bytes(seconds: float) -> bytes:
    av: Any = importlib.import_module("av")
    output = io.BytesIO()
    with av.open(io.BytesIO(wav_bytes(seconds))) as source, av.open(output, "w", format="amr") as sink:
        stream = sink.add_stream("libopencore_amrnb", rate=8000, layout="mono")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
        for frame in source.decode(audio=0):
            for resampled in resampler.resample(frame):
                for packet in stream.encode(resampled):
                    sink.mux(packet)
        for packet in stream.encode(None):
            sink.mux(packet)
    return output.getvalue()


def bench_probe(iterations: int) -> dict[str, float]:
    heads = [b"RIFF" + bytes(16380), b"\x02#!SILK_V3" + bytes(16374), b"#!AMR\n" + bytes(16378), bytes(16384)]
    samples: list[float] = []
    for index in range(iterations):
        data = heads[index % 4]
        started = time.perf_counter()
        probe_audio(data, ".amr")
        samples.append(time.perf_counter() - started)
    return summarize(samples)


def bench_convert(iterations: int, seconds: float) -> dict[str, object]:
    if not converter_available():
        return {"skipped": "av 未安装"}
    data = amr_bytes(seconds)
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        out = convert_to_wav(data, max_seconds=600)
        samples.append(time.perf_counter() - started)
    return {**summarize(samples), "input_bytes": len(data), "output_bytes": len(out), "audio_seconds": seconds}


async def make_event(adapter: OneBotAdapter, message_id: int):
    await adapter._handle_group_message({"self_id": 10000, "user_id": 123, "group_id": 456, "message_id": message_id, "message_type": "group",
        "message": [{"type": "at", "data": {"qq": "10000"}}, {"type": "record", "data": {"file": "abc.amr", "url": "https://media.example.com/abc.amr"}}]})
    return adapter._event_queue.get_nowait()


async def bench_record_path(iterations: int, via_get_record: bool, seconds: float) -> dict[str, object]:
    if not converter_available() and not via_get_record:
        return {"skipped": "av 未安装"}
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={"asr_model": "speech"}))
    adapter._bot = AsyncMock()
    if via_get_record:
        adapter._bot.get_record.return_value = {"base64": base64.b64encode(wav_bytes(seconds, 16000)).decode()}
    else:
        adapter._bot.get_record.side_effect = ActionFailed({"retcode": 1404})
    payload = amr_bytes(seconds) if not via_get_record else b""

    async def download(url: str, **kwargs: object) -> OutboundHTTPResponse:
        return OutboundHTTPResponse(url=url, status_code=200, headers={}, content=payload)

    async def transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
        return "ok"

    original = (attachments.safe_async_get, attachments._transcribe)
    attachments.safe_async_get = download  # type: ignore[assignment]
    attachments._transcribe = transcribe  # type: ignore[assignment]
    config = ASRConfig(name="speech", model="m", api_key="k")
    samples: list[float] = []
    try:
        for index in range(iterations):
            event = await make_event(adapter, 1000 + index)
            started = time.perf_counter()
            results = await attachments.resolve_attachments(event, lambda name: config)
            samples.append(time.perf_counter() - started)
            assert results[0].status == "resolved", results
    finally:
        attachments.safe_async_get, attachments._transcribe = original  # type: ignore[assignment]
    return {**summarize(samples), "audio_seconds": seconds}


async def run_once(iterations: int) -> dict[str, object]:
    return {
        "audio_probe": bench_probe(iterations * 20),
        "amr_convert": bench_convert(iterations, 10.0),
        "record_get_record": await bench_record_path(iterations, True, 10.0),
        "record_local_amr": await bench_record_path(iterations, False, 10.0),
    }


def merge_repeats(runs: list[dict[str, object]]) -> dict[str, object]:
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
    parser.add_argument("--output", type=Path, default=ROOT / "tests" / "benchmark" / "results" / "platform" / "audio-local.json")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=50)
    args = parser.parse_args()
    before = source_hashes()
    await run_once(5)
    runs = [await run_once(args.iterations) for _ in range(args.repeats)]
    if before != source_hashes():
        raise RuntimeError("基线期间源代码发生变化, 本次结果不应保存")
    dependencies = {name: importlib.metadata.version(name) for name in ("aiocqhttp",)}
    if converter_available():
        dependencies["av"] = importlib.metadata.version("av")
    result = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version, "platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count(),
            "dependencies": dependencies,
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8").strip(),
            "source_sha256": before, "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "method": {"repeats": args.repeats, "iterations": args.iterations, "aggregation": "median across repeats", "network": "none; OneBot bot, download and ASR mocked"},
        "scenarios": merge_repeats(runs),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, data in result["scenarios"].items():
        assert isinstance(data, dict)
        if "skipped" in data:
            print(f"{name:20s} skipped: {data['skipped']}")
        else:
            print(f"{name:20s} mean={data['mean_us']:9.1f}us p95={data['p95_us']:9.1f}us")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
