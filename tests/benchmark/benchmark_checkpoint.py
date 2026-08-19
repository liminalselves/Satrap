"""检查点存储基准测试: 多轮模拟写入 / fork / retry / rollback, 监测速度 / 内存 / 存储

运行:
    python tests/benchmark/benchmark_checkpoint.py

场景:
- A 写入基线   : ContextManager 无检查点, 200 条消息写入 (每条 100 汉字)
- B 自动 stable : ContextManager 开启检查点, 200 条消息写入 (指针式自动存档)
- C 会话聚合    : Session 3 上下文, 50/100/300 轮多档对比,
                  每轮 4 条消息 (每条 100 汉字), 每 5 轮聚合检查点,
                  中途 fork / retry / rollback / 撤销

监测指标:
- 速度: 各操作总耗时与平均耗时 (ms/op)
- 内存: 进程 RSS 峰值与增量 + Python 堆 (tracemalloc) 峰值
- 存储: DB 文件大小增量 + state_checkpoints / state_snapshots / chat_history 行数
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from typing import Any, cast
from pathlib import Path
from typing import Any

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from satrap.core.framework.Base import Session
from satrap.core.utils.context import ContextManager

MESSAGES_PER_ROUND = 4   # 每轮写入消息数
STABLE_BASELINE_MSGS = 200   # 场景 A/B 消息数
ROUND_LADDER = (50, 100, 300)   # 场景 C 轮次档位

# 100 汉字消息体: 模拟真实对话长内容 (千字文片段)
_MSG_TEMPLATE = (
    "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳"
    "云腾致雨露结为霜金生丽水玉出昆冈剑号巨阙珠称夜光果珍李柰菜重芥姜"
    "海咸河淡鳞潜羽翔龙师火帝鸟官人皇始制文字乃服衣裳推位让国有虞陶唐"
)
MSG_BODY = (_MSG_TEMPLATE * 4)[:100]
"""每条消息 100 个汉字"""


def _rss_mb() -> float:
    """当前进程 RSS (MB)"""
    mem = cast(Any, psutil.Process().memory_info())  # psutil 无类型声明
    return float(mem.rss) / (1024 * 1024)


def _db_stats(db_path: str) -> dict[str, int | float]:
    """收集 DB 存储统计: 文件大小 (KB) 与三表行数"""
    size_kb = os.path.getsize(db_path) / 1024.0
    stats: dict[str, int | float] = {"size_kb": size_kb}
    conn = sqlite3.connect(db_path)
    try:
        for table in ("state_checkpoints", "state_snapshots", "chat_history"):
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if row and row[0]:
                stats[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            else:
                stats[table] = 0
    finally:
        conn.close()
    return stats


def _fmt_ms(seconds: float) -> str:
    """格式化毫秒"""
    return f"{seconds * 1000:.1f} ms"


def _fmt_avg_ms(seconds: float, count: int) -> str:
    """格式化平均毫秒/op"""
    if count <= 0:
        return "-"
    return f"{seconds * 1000 / count:.3f} ms/op"


def _print_stats(
    title: str,
    elapsed: float,
    ops: dict[str, tuple[float, int]],
    mem_before: float,
    db_path: str,
    heap_peak: float | None = None,
) -> None:
    """打印单个场景的基准报告"""
    print(f"\n[{title}]")
    print(f"  总耗时        : {_fmt_ms(elapsed)}")
    for op_name, (op_sec, op_count) in ops.items():
        print(f"  {op_name:<12}: 合计 {_fmt_ms(op_sec):>10}  平均 {_fmt_avg_ms(op_sec, op_count)}  ({op_count} 次)")
    peak = _rss_mb()
    print(f"  进程内存      : 峰值 {peak:.1f} MB (增量 {peak - mem_before:+.1f} MB)")
    if heap_peak is not None:
        print(f"  Python 堆峰值 : {heap_peak / 1048576:.2f} MB")
    stats = _db_stats(db_path)
    print(
        f"  DB 存储       : {stats['size_kb']:.1f} KB  "
        f"(检查点 {stats['state_checkpoints']} 行, 快照 {stats['state_snapshots']} 行, "
        f"消息 {stats['chat_history']} 行)"
    )


# ── 场景 A/B: 写入基线 vs 自动 stable ──


def _run_write_scenario(enable_checkpoint: bool, tmp_dir: str) -> dict[str, Any]:
    """写 STABLE_BASELINE_MSGS 条 100 汉字消息, 返回 (耗时, ops, db_path)"""
    db_path = os.path.join(tmp_dir, f"write_{enable_checkpoint}.db")
    ctx = ContextManager("conv-w", db_path=db_path, enable_checkpoint=enable_checkpoint)
    write_sec = 0.0
    for i in range(STABLE_BASELINE_MSGS):
        t0 = time.perf_counter()
        if i % 2 == 0:
            ctx.add_user_message(f"用户消息 {i}: {MSG_BODY}")
        else:
            ctx.add_bot_message(f"回复消息 {i}: {MSG_BODY}")
        write_sec += time.perf_counter() - t0
    return {
        "elapsed": write_sec,
        "ops": {"消息写入": (write_sec, STABLE_BASELINE_MSGS)},
        "db_path": db_path,
    }


# ── 场景 C: 会话聚合 + fork / retry / rollback ──


class _BenchSession(Session):
    """基准会话: 会话共享 + 两个工作流上下文, 关闭自动检查点 (只测聚合操作)"""

    def __init__(self, session_id: str, db_path: str):
        super().__init__(session_id, db_path=db_path, enable_checkpoint=True)
        self.session_ctx = ContextManager(session_id, db_path=db_path, auto_checkpoint=False)
        self.session_ctx.state_store = self._state_store
        self.wf_main = ContextManager(
            self.workflow_id_assign("main"), db_path=db_path, auto_checkpoint=False
        )
        self._track_workflow_context("main", self.wf_main)
        self.wf_sub = ContextManager(
            self.workflow_id_assign("sub"), db_path=db_path, auto_checkpoint=False
        )
        self._track_workflow_context("sub", self.wf_sub)


def _run_session_scenario(tmp_dir: str, rounds: int) -> dict[str, Any]:
    """多轮模拟: 每轮 4 条 100 汉字消息, 每 5 轮聚合检查点, 中途 fork/retry/rollback/撤销"""
    db_path = os.path.join(tmp_dir, f"session_{rounds}.db")
    session = _BenchSession("bench-session", db_path)
    op_names = ("消息写入", "聚合检查点", "fork", "retry", "rollback", "撤销回滚")
    op_elapsed: dict[str, float] = {name: 0.0 for name in op_names}
    op_counts: dict[str, int] = {name: 0 for name in op_names}

    def _accumulate(op_name: str, t0: float, count: int) -> None:
        op_elapsed[op_name] += time.perf_counter() - t0
        op_counts[op_name] += count

    batch_ids: list[str] = []
    t_start = time.perf_counter()
    for i in range(1, rounds + 1):
        for _j in range(MESSAGES_PER_ROUND):
            t0 = time.perf_counter()
            session.session_ctx.add_user_message(f"会话 {i}-{_j}: {MSG_BODY}")
            session.wf_main.add_bot_message(f"主工作流 {i}-{_j}: {MSG_BODY}")
            session.wf_sub.add_tool_message(f"tool-{i}-{_j}", {"result": i, "payload": MSG_BODY})
            session.session_ctx.add_bot_message(f"回复 {i}-{_j}: {MSG_BODY}")
            _accumulate("消息写入", t0, 4)

        if i % 5 == 0:
            t0 = time.perf_counter()
            batch_ids.append(session.create_checkpoint(name=f"轮次{i}"))
            _accumulate("聚合检查点", t0, 1)

        if i == 8:
            t0 = time.perf_counter()
            session.fork("bad_end")
            _accumulate("fork", t0, 1)

        if i == 12 and len(batch_ids) >= 2:
            t0 = time.perf_counter()
            session.retry(batch_ids[0])
            _accumulate("retry", t0, 1)

        if i == 16 and len(batch_ids) >= 3:
            t0 = time.perf_counter()
            session.rollback(batch_ids[1])
            _accumulate("rollback", t0, 1)

        if i == 20:
            # 撤销第 16 轮的 rollback: 回滚到它留下的保护检查点
            protects = [
                cp
                for cp in session.list_checkpoints()
                if cp.source == "rollback_snapshot" and cp.scope_id == session.session_id
            ]
            if protects:
                t0 = time.perf_counter()
                session.rollback(protects[-1].checkpoint_id)
                _accumulate("撤销回滚", t0, 1)

        if i == 24 and len(batch_ids) >= 4:
            t0 = time.perf_counter()
            session.rollback(batch_ids[0])
            _accumulate("rollback", t0, 1)

    ops: dict[str, tuple[float, int]] = {
        name: (op_elapsed[name], op_counts[name]) for name in op_names
    }
    return {"elapsed": time.perf_counter() - t_start, "ops": ops, "db_path": db_path}


def _run_ladder_scenario(tmp_dir: str) -> list[dict[str, Any]]:
    """按轮次档位运行场景 C, 返回各档结果 (含内存增量与堆峰值)"""
    results: list[dict[str, Any]] = []
    for rounds in ROUND_LADDER:
        mem_before = _rss_mb()
        tracemalloc.start()
        result = _run_session_scenario(tmp_dir, rounds)
        heap_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        result["mem_before"] = mem_before
        result["mem_peak"] = _rss_mb()
        result["heap_peak"] = heap_peak
        result["rounds"] = rounds
        _print_stats(
            f"场景 C] 会话聚合生命周期 ({rounds} 轮, 每轮 {MESSAGES_PER_ROUND} 条 x 100 汉字)",
            result["elapsed"],
            result["ops"],
            mem_before,
            result["db_path"],
            heap_peak,
        )
        results.append(result)
    return results


def _print_ladder_comparison(results: list[dict[str, Any]]) -> None:
    """输出场景 C 多档轮次对比表"""
    print("\n[场景 C] 轮次档位对比 (每轮 4 条 x 100 汉字)")
    header = (
        f"{'轮次':>6} | {'总耗时':>10} | {'写入 ms/op':>11} | "
        f"{'检查点 ms/op':>12} | {'DB 大小':>9} | {'消息行':>6} | {'检查点行':>7} | {'快照行':>6} | {'内存增量':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        stats = _db_stats(r["db_path"])
        write_sec, write_cnt = r["ops"]["消息写入"]
        ckpt_sec, ckpt_cnt = r["ops"]["聚合检查点"]
        print(
            f"{r['rounds']:>6} | {_fmt_ms(r['elapsed']):>10} | "
            f"{_fmt_avg_ms(write_sec, write_cnt):>11} | {_fmt_avg_ms(ckpt_sec, ckpt_cnt):>12} | "
            f"{stats['size_kb']:>8.1f}K | {stats['chat_history']:>6} | "
            f"{stats['state_checkpoints']:>7} | {stats['state_snapshots']:>6} | "
            f"{r['mem_peak'] - r['mem_before']:>+8.1f}M"
        )


def main() -> None:
    """运行全部基准场景并输出报告"""
    print("=" * 72)
    print("Satrap 检查点存储基准 (写入 / fork / retry / rollback)")
    print(
        f"Python {sys.version.split()[0]} | psutil {psutil.__version__} | "
        f"消息 {len(MSG_BODY)} 汉字 | 档位 {ROUND_LADDER}"
    )
    print("=" * 72)

    tmp_dir = tempfile.mkdtemp(prefix="satrap_bench_")
    try:
        # 场景 A: 写入基线
        mem_before = _rss_mb()
        tracemalloc.start()
        result_a = _run_write_scenario(False, tmp_dir)
        heap_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        _print_stats(
            "场景 A] 写入基线 (无检查点)",
            result_a["elapsed"],
            result_a["ops"],
            mem_before,
            result_a["db_path"],
            heap_peak,
        )

        # 场景 B: 自动 stable
        mem_before = _rss_mb()
        tracemalloc.start()
        result_b = _run_write_scenario(True, tmp_dir)
        heap_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        _print_stats(
            "场景 B] 自动 stable (指针式)",
            result_b["elapsed"],
            result_b["ops"],
            mem_before,
            result_b["db_path"],
            heap_peak,
        )

        # 场景 A/B 对比
        stats_a = _db_stats(result_a["db_path"])
        stats_b = _db_stats(result_b["db_path"])
        ratio = (
            stats_b["size_kb"] / stats_a["size_kb"] if stats_a["size_kb"] > 0 else 0.0
        )
        print("\n[A vs B] 自动 stable 开销对比")
        print(f"  写入耗时        : {_fmt_avg_ms(result_a['elapsed'], 200)} -> {_fmt_avg_ms(result_b['elapsed'], 200)}")
        print(
            f"  DB 大小         : {stats_a['size_kb']:.1f} KB -> {stats_b['size_kb']:.1f} KB "
            f"(x{ratio:.2f})"
        )
        print(
            f"  检查点行        : {stats_a['state_checkpoints']} -> {stats_b['state_checkpoints']} "
            f"(均为指针, 快照表 {stats_b['state_snapshots']} 行)"
        )

        # 场景 C: 会话聚合生命周期 (多档轮次)
        results_c = _run_ladder_scenario(tmp_dir)
        _print_ladder_comparison(results_c)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
