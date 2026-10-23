"""在临时数据库测量统一动作列表, 摘要与查询期间的健康检查调度"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


async def run() -> None:
    """使用合成动作执行规模矩阵, 保留每组的原始采样"""
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    reports = []
    with tempfile.TemporaryDirectory(prefix='satrap-action-benchmark-') as temporary:
        directory = Path(temporary)
        for key, value in {'SATRAP_DATA_ROOT': directory, 'SATRAP_LOG_ROOT': directory / 'logs',
                           'SATRAP_CONFIG_ROOT': directory / 'config', 'SATRAP_CREDENTIALS_ROOT': directory / 'credentials',
                           'SATRAP_RUNTIME_ROOT': directory / 'runtime'}.items():
            os.environ[key] = str(value)
        from satrap.core.config.platform_schema import ensure_platform_tables
        from satrap.core.platform.action_catalog import ActionScope, ActionCatalog, platform_action_catalog
        from satrap.core.backend.http_api import BackendHTTPServer
        from satrap.core.log import logger

        async def health() -> dict[str, bool]:
            """
            提供独立于模型和平台网络的健康检查宿主

            返回:
            - 固定健康状态
            """
            return {'healthy': True}

        server = object.__new__(BackendHTTPServer)
        server.backend = SimpleNamespace(health=health)
        original_connect = sqlite3.connect
        counter = [0]
        lock = threading.Lock()

        def connect(database: str, *, uri: bool = False, timeout: float = 5) -> sqlite3.Connection:
            """
            为测量连接安装 SQL 计数器, 不改变查询或事务

            参数:
            - database: 查询数据库 URI
            - uri: 是否按 URI 解析
            - timeout: 锁等待超时秒数

            返回:
            - 带计数探针的连接
            """
            connection = original_connect(database, uri=uri, timeout=timeout)
            def trace(statement: str) -> None:
                """
                统计实际执行的 SQL

                参数:
                - statement: SQLite 已执行的语句
                """
                with lock:
                    counter[0] += 1
            connection.set_trace_callback(trace)
            return connection

        try:
            for count in (1000, 10000):
                scopes = []
                now = time.time()
                for platform in range(5):
                    database = directory / f'{count}-{platform}.db'
                    with closing(original_connect(database)) as connection, connection:
                        ensure_platform_tables(connection)
                        for index in range(count):
                            state = 'succeeded' if index % 5 == 0 else 'pending'
                            expires = now - 1 if index % 11 == 0 else now + 600
                            params = json.dumps({'user_id': str(index % 100), 'duration': 3600})
                            if index % 3 == 0:
                                connection.execute('INSERT INTO friend_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                                   ('100', str(index), 'delete_friend', params, 'model', state, now - index, expires, None, None, None, None, 'author'))
                            else:
                                connection.execute('INSERT INTO group_actions(action_id,self_id,group_id,action_type,params_json,fingerprint,actor_kind,policy_revision,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?,0,?,?,?)',
                                                   (str(index), '100', str(index % 200), 'ban_group_member', params, 'fp', 'model', state, now - index, expires))
                    scopes.append(ActionScope(f'bot-{platform}', database, '100', True))
                for platforms in (1, 5):
                    for summary in (False, True):
                        samples = []
                        for iteration in range(32):
                            counter[0] = 0
                            started = time.perf_counter()
                            with patch('satrap.core.platform.action_catalog.sqlite3.connect', connect):
                                async def timed_query() -> tuple[ActionCatalog, float]:
                                    """
                                    独立计时查询, 不把五毫秒健康探针等待计入查询耗时

                                    返回:
                                    - 查询结果和实际查询毫秒数
                                    """
                                    query_start = time.perf_counter()
                                    result = await platform_action_catalog(scopes[:platforms], summary=summary)
                                    return result, (time.perf_counter() - query_start) * 1000
                                pending = asyncio.create_task(timed_query())
                                await asyncio.sleep(0.005)
                                status, _ = await server._route_ui_health_reload('GET', '/api/health', b'')
                                health_delay = max(0, (time.perf_counter() - started - 0.005) * 1000)
                                assert status == 200
                                result, query_ms = await pending
                            assert result['complete'] and result['pending_count'] > 0
                            assert len(result['items']) == (0 if summary else 50)
                            if iteration >= 2:
                                samples.append({'ms': query_ms, 'sql': counter[0], 'health_delay_ms': health_delay})
                        reports.append({'platforms': platforms, 'actions_per_platform': count, 'summary': summary,
                                        'p50_ms': statistics.median(item['ms'] for item in samples),
                                        'p95_ms': sorted(item['ms'] for item in samples)[28],
                                        'sql_statements': samples[0]['sql'],
                                        'health_p95_ms': sorted(item['health_delay_ms'] for item in samples)[28], 'samples': samples})
        finally:
            logger.close()
    output = root / 'satrap-ui/benchmarks/results/platform-approvals-2026-10-23'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'queries.json').write_text(json.dumps({'transport': 'production read functions and health route, no HTTP socket', 'warmups': 2, 'iterations': 30, 'results': reports}, indent=2) + '\n', encoding='utf-8')
    print(json.dumps([{key: value for key, value in report.items() if key != 'samples'} for report in reports]))


if __name__ == '__main__':
    asyncio.run(run())
