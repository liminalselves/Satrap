"""用真实目录路由和临时 SQLite 测量大查询期间的事件循环响应"""
from __future__ import annotations

import ast
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import time
from types import MethodType, SimpleNamespace


async def run() -> None:
    """对原提交与当前路由各执行两次预热和十次正式并发查询"""
    from tempfile import TemporaryDirectory

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    import satrap.display.recorder as recorder
    import satrap.display.server as server_module

    results = root / 'satrap-ui/benchmarks/results/frontend-loading-2026-10-23'
    if (results / 'concurrent-baseline.json').exists():
        raise RuntimeError('并发 baseline 已存在, 不允许自动覆盖')
    reports: dict[str, dict[str, object]] = {}
    with TemporaryDirectory(prefix='satrap-concurrent-benchmark-') as temporary:
        database = Path(temporary) / 'synthetic.db'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript('CREATE TABLE conversation_meta(conversation_id TEXT PRIMARY KEY, project_id TEXT, model TEXT, think TEXT, created_at REAL); CREATE TABLE display_turns(id INTEGER PRIMARY KEY, conversation_id TEXT, turn_index INTEGER, user_input TEXT, created_at REAL); CREATE INDEX idx_conv_turn ON display_turns(conversation_id, turn_index);')
            connection.executemany('INSERT INTO conversation_meta VALUES(?, NULL, ?, ?, ?)', ((str(index), 'model', 'off', index) for index in range(5000)))
            connection.executemany('INSERT INTO display_turns(conversation_id, turn_index, user_input, created_at) VALUES(?, ?, ?, ?)', ((str(index), turn, 'synthetic', index + turn) for index in range(5000) for turn in range(10)))
        for mode in ('baseline', 'after'):
            namespace = dict(vars(server_module))
            if mode == 'baseline':
                source = subprocess.check_output(['git', 'show', 'HEAD:satrap/display/recorder.py'], cwd=root).decode('utf-8')
                old_recorder = dict(vars(recorder))
                nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in {'query_conversations', 'list_conversations', '_conversation_rows'}]
                exec(compile('from __future__ import annotations\n' + '\n\n'.join(ast.get_source_segment(source, node) or '' for node in nodes), '<baseline-queries>', 'exec'), old_recorder)
                namespace['list_conversations'] = old_recorder['list_conversations']
                source = subprocess.check_output(['git', 'show', 'HEAD:satrap/display/server.py'], cwd=root).decode('utf-8')
                declaration = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == 'ChatHTTPServer')
                route = next(node for node in declaration.body if isinstance(node, ast.AsyncFunctionDef) and node.name == '_route')
                exec(compile('from __future__ import annotations\n' + ast.unparse(route), '<baseline-route>', 'exec'), namespace)
                handler = namespace['_route']
            else:
                handler = server_module.ChatHTTPServer._route
            server = object.__new__(server_module.ChatHTTPServer)
            server.service = SimpleNamespace(_display_db_path=database, _conversations={})
            server.application_status = lambda *_: None
            route_call = MethodType(handler, server)
            samples: list[dict[str, float]] = []
            for iteration in range(12):
                started = time.perf_counter()

                async def health() -> float:
                    """
                    在查询开始五毫秒后调度健康检查, 包含事件循环排队时间

                    返回:
                    - 超过计划执行时间的毫秒数
                    """
                    await asyncio.sleep(0.005)
                    status, _ = await route_call('GET', '/api/chat/health', b'')
                    assert status == 200
                    return max(0, (time.perf_counter() - started - 0.005) * 1000)

                pending = asyncio.create_task(health())
                await asyncio.sleep(0)
                status, body = await route_call('GET', '/api/chat/conversations', b'')
                query_ms = (time.perf_counter() - started) * 1000
                assert status == 200 and len(body['conversations']) == 5000
                delay_ms = await pending
                if iteration >= 2:
                    samples.append({'query_ms': query_ms, 'scheduled_health_delay_ms': delay_ms})
            reports[mode] = {'conversations': 5000, 'turns': 50000, 'transport': 'actual route dispatch, no HTTP socket',
                             'p50_health_delay_ms': statistics.median(item['scheduled_health_delay_ms'] for item in samples),
                             'p95_health_delay_ms': sorted(item['scheduled_health_delay_ms'] for item in samples)[9], 'samples': samples}
    for mode, report in reports.items():
        (results / f'concurrent-{mode}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({mode: {key: value for key, value in report.items() if key != 'samples'} for mode, report in reports.items()}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(run())
