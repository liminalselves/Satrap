"""使用合成平台数据库比较目录聚合前后的时间和 Python 内存"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import time
import tracemalloc
import traceback
from types import SimpleNamespace


def run() -> None:
    """以相同只读连接和数据分别执行原提交与当前目录实现"""
    root = Path(__file__).resolve().parents[2]
    sources = {'baseline': subprocess.check_output(['git', 'show', 'HEAD:satrap/core/config/conversation_catalog.py'], cwd=root).decode('utf-8'),
               'after': (root / 'satrap/core/config/conversation_catalog.py').read_text(encoding='utf-8')}
    reports = {'baseline': [], 'after': []}
    for count in (1000, 5000):
        with sqlite3.connect(':memory:') as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript('CREATE TABLE conversation_meta(conversation_id TEXT PRIMARY KEY, project_id TEXT, model TEXT, think TEXT, created_at REAL); CREATE TABLE display_turns(conversation_id TEXT, turn_index INTEGER, user_input TEXT, created_at REAL); CREATE INDEX idx_conv_turn ON display_turns(conversation_id, turn_index);')
            connection.executemany('INSERT INTO conversation_meta VALUES(?, NULL, ?, ?, ?)', ((str(index), 'model', 'off', index) for index in range(count)))
            connection.executemany('INSERT INTO display_turns VALUES(?, ?, ?, ?)', ((str(index), turn, 'synthetic-' + 'x' * 512, index + turn) for index in range(count) for turn in range(10)))
            for mode, source in sources.items():
                functions = '\n\n'.join(ast.get_source_segment(source, node) or '' for node in ast.parse(source).body
                                        if isinstance(node, ast.FunctionDef) and node.name in {'_rows', '_route_metadata', 'conversation_records'})
                namespace = {'sqlite3': sqlite3, 'json': json, 'traceback': traceback,
                             'registry': SimpleNamespace(get=lambda name: None),
                             'logger': SimpleNamespace(warning=lambda *args: None, error=lambda *args: None)}
                exec(compile('from __future__ import annotations\n' + functions, '<catalog-loading>', 'exec'), namespace)
                samples = []
                for iteration in range(32):
                    started = time.perf_counter()
                    rows = namespace['conversation_records'](connection, {'id': 'benchmark', 'type': 'benchmark'})
                    if iteration >= 2:
                        samples.append((time.perf_counter() - started) * 1000)
                    assert len(rows) == count and rows[0]['history_count'] == 10
                tracemalloc.start()
                namespace['conversation_records'](connection, {'id': 'benchmark', 'type': 'benchmark'})
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
                reports[mode].append({'conversations': count, 'turns': count * 10, 'p50_ms': statistics.median(samples), 'p95_ms': sorted(samples)[28], 'python_peak_bytes': peak, 'samples_ms': samples})
    results = root / 'satrap-ui/benchmarks/results/frontend-loading-2026-10-23'
    results.mkdir(parents=True, exist_ok=True)
    for mode, report in reports.items():
        destination = results / f'catalog-query-{mode}.json'
        if mode == 'baseline' and destination.exists():
            raise RuntimeError('目录 baseline 已存在, 不允许自动覆盖')
        destination.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({mode: [{key: value for key, value in row.items() if key != 'samples_ms'} for row in report] for mode, report in reports.items()}))


if __name__ == '__main__':
    run()
