"""隔离监听端口复测真实冷配置读取, 禁止 Edictum 配置迁移写入"""
from __future__ import annotations

from unittest.mock import patch
from collections import Counter
import statistics
import asyncio
import time
import json

from satrap.core.backend import control_server
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.config import EdictumConfigManager


async def run() -> None:
    """在临时端口运行当前读取路由并输出脱敏耗时与解析次数"""
    counts: Counter[str] = Counter()
    load = PluginCatalog._load_entry

    def counted(path):
        counts['meta_loads'] += 1
        return load(path)

    def reject_write(self):
        raise RuntimeError('只读基准禁止配置迁移或创建文件')

    async with await asyncio.start_server(control_server._handle_request, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]

        async def request(path: str) -> dict[str, object]:
            started = time.perf_counter()
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            try:
                writer.write((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {control_server._CONTROL_AUTH.token}\r\nConnection: close\r\n\r\n').encode('utf-8'))
                await writer.drain()
                response = await reader.read()
                status = int(response.split(b' ', 2)[1])
                if status != 200:
                    raise RuntimeError(f'{path} 返回 HTTP {status}')
                return {'path': path, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 2), 'bytes': len(response)}
            finally:
                writer.close()
                await writer.wait_closed()

        with patch.object(PluginCatalog, '_load_entry', staticmethod(counted)), patch.object(EdictumConfigManager, '_save_locked', reject_write):
            PluginCatalog().invalidate()
            paths = ['/config/plugins', '/config/edictum/plugins', '/config/edictum/types', '/config/edictum/sessions', '/config/plugins/friend_manager/config', '/config/administrator-groups']
            cold = [await request(path) for path in paths]
            warm = [await request(path) for path in paths]
            before_parallel = counts['meta_loads']
            pending = [asyncio.create_task(request(f'/config/plugins/{name}/config')) for name in ['friend_manager', 'group_chat', 'memory']]
            await asyncio.sleep(.01)
            probes = [await request('/status') for _ in range(10)]
            parallel = await asyncio.gather(*pending)
            print(json.dumps({'cold': cold, 'warm': warm, 'parallel': parallel, 'status_probes': probes,
                              'status_p95_ms': statistics.quantiles([float(item['elapsed_ms']) for item in probes], n=20)[18],
                              'meta_loads': counts['meta_loads'], 'parallel_meta_loads': counts['meta_loads'] - before_parallel}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(run())
