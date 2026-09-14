"""对比旧逐任务查询与新分页摘要, 不执行模型或工具"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from satrap.core.framework.Base.execution.store import RunStore


def measure(operation):
    times = []
    result = []
    for _ in range(6):
        start = time.perf_counter()
        result = operation()
        times.append((time.perf_counter() - start) * 1000)
    return {"median_ms": statistics.median(times[1:]), "rows": len(result)}


def legacy(store):
    result = []
    for run in store.list():
        with store.connect() as db:
            rows = db.execute("SELECT step_key,kind,status,recovery_policy FROM agent_steps WHERE run_id=? ORDER BY rowid", (run["id"],)).fetchall()
        result.append({"id": run["id"], "steps": [dict(row) for row in rows]})
    return result


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix="satrap-recovery-list-") as folder:
        for count in (100, 1000, 10000):
            store = RunStore(Path(folder) / f"{count}.db", "a")
            with closing(sqlite3.connect(store.database)) as db:
                db.executemany("INSERT INTO agent_runs VALUES (?,?,?,?,'c','f',NULL,NULL,?,?)", [(str(i), "a", "completed", json.dumps({"user_input": "x" * 4096}), i, i) for i in range(count)])
                db.executemany("INSERT INTO agent_steps VALUES (?,?,'model','completed','{}','{}','retry')", [(str(i), f"model:{j}") for i in range(count) for j in range(5)])
                db.commit()
            before = measure(lambda: legacy(store))
            after = measure(lambda: store.summaries(20)["runs"])
            compatible = measure(lambda: store.summaries()["runs"])
            result = {"tasks": count, "legacy_all": before, "paged_20": after, "compatible_all": compatible}
            results.append(result)
            print(json.dumps(result), flush=True)
    path = Path(__file__).parent / "results/recovery-list.json"
    path.write_text(json.dumps({"method": "每任务 4 KiB 输入和 5 步骤, 预热一次, 正式测量 5 次; legacy 复现旧列表查询路径", "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
