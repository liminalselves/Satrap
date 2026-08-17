"""插件加载基准测试: 串行 vs 并行插件安装耗时对比

运行:
    python tests/benchmark/benchmark_plugin_load.py

场景:
- 创建 N 个 mock 插件 (临时目录, 含 meta.yaml + tools.py)
- 分别测量串行和并行安装总耗时
- 断言并行耗时 < 串行耗时
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from satrap.edictum.simple_session import AsyncSimpleSession
from satrap.core.APICall.LLMCall import AsyncLLM

PLUGIN_COUNT = 5
"""mock 插件数量"""

META_YAML = """\
name: {name}
version: "0.1.0"
author: benchmark
description: "benchmark mock plugin"
"""

TOOLS_PY = """\
from satrap.core.utils.TCBuilder import AsyncTool


class {class_name}(AsyncTool):
    tool_name = "{tool_name}"
    description = "benchmark mock tool"
    params_dict = {{}}

    async def execute(self, **kwargs):
        return "ok"
"""


def _create_mock_plugins(base_dir: Path, count: int) -> list[Path]:
    """在临时目录创建 N 个 mock 插件"""
    dirs: list[Path] = []
    for i in range(count):
        name = f"bench_plugin_{i}"
        pdir = base_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "meta.yaml").write_text(META_YAML.format(name=name), encoding="utf-8")
        class_name = f"BenchTool{i}"
        tool_name = f"bench_tool_{i}"
        (pdir / "tools.py").write_text(
            TOOLS_PY.format(class_name=class_name, tool_name=tool_name),
            encoding="utf-8",
        )
        dirs.append(pdir)
    return dirs


def _make_session() -> AsyncSimpleSession:
    llm = AsyncLLM(api_key="benchmark-key", model="benchmark-model")
    return AsyncSimpleSession("benchmark-session", llm)


async def _install_sequential(session: AsyncSimpleSession, plugin_dirs: list[Path]) -> float:
    """串行安装, 返回总耗时 (秒)"""
    start = time.perf_counter()
    for pdir in plugin_dirs:
        await session.install_plugin(str(pdir))
    return time.perf_counter() - start


async def _install_parallel(session: AsyncSimpleSession, plugin_dirs: list[Path]) -> float:
    """并行安装, 返回总耗时 (秒)"""
    async def _install_one(pdir: Path) -> None:
        await session.install_plugin(str(pdir))

    start = time.perf_counter()
    await asyncio.gather(*[_install_one(p) for p in plugin_dirs])
    return time.perf_counter() - start


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bench_plugin_"))
    try:
        plugin_dirs = _create_mock_plugins(tmp, PLUGIN_COUNT)
        print(f"插件数量: {PLUGIN_COUNT}")

        # 串行
        session1 = _make_session()
        seq_time = await _install_sequential(session1, plugin_dirs)
        print(f"串行安装总耗时: {seq_time:.3f}s")

        # 并行
        session2 = _make_session()
        par_time = await _install_parallel(session2, plugin_dirs)
        print(f"并行安装总耗时: {par_time:.3f}s")

        speedup = seq_time / par_time if par_time > 0 else float("inf")
        print(f"加速比: {speedup:.2f}x")

        assert par_time < seq_time, (
            f"并行耗时 ({par_time:.3f}s) 应小于串行耗时 ({seq_time:.3f}s)"
        )
        print("PASS: 并行安装快于串行安装")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
