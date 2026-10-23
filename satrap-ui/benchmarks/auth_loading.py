"""对照原提交与当前代码验证 Cookie 恢复及持久认证开销"""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import ModuleType
from unittest.mock import patch


def run() -> None:
    """在临时凭据目录中比较旧恢复行为和当前持久会话的热路径"""
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from satrap.core.auth_password import hash_dashboard_password
    from satrap.core.server_auth import ServerAuth

    revision = os.environ.get('SATRAP_BENCHMARK_BASE', 'HEAD')
    source = subprocess.check_output(['git', 'show', f'{revision}:satrap/core/server_auth.py'], cwd=root).decode('utf-8')
    module = ModuleType('benchmark_baseline_server_auth')
    sys.modules[module.__name__] = module
    exec(compile(source, '<baseline-server-auth>', 'exec'), module.__dict__)
    token = 'benchmark-api-token-with-at-least-32-characters'
    reports = {}
    with TemporaryDirectory(prefix='satrap-auth-benchmark-') as temporary:
        directory = Path(temporary)
        config = directory / 'config.json'
        config.write_text(json.dumps({'api': {'dashboard_password': hash_dashboard_password('benchmark-password')}}), encoding='utf-8')
        with patch.dict(os.environ, {'SATRAP_CREDENTIALS_ROOT': str(directory / 'credentials')}), patch('satrap.core.auth_password.dashboard_password_path', return_value=config):
            previous = module.ServerAuth.create('127.0.0.1', 0, token=token)
            old_cookie = previous.session_cookie().split(';', 1)[0]
            old_headers = {'cookie': old_cookie}
            old_samples = []
            for _ in range(100):
                started = time.perf_counter()
                accepted = previous.can_bootstrap('127.0.0.1', 'http://127.0.0.1:5173', old_headers)
                old_samples.append((time.perf_counter() - started) * 1000)
            reports['baseline'] = {'existing_cookie_valid': previous.cookie_authorized(old_headers), 'bootstrap_status': 200 if accepted else 401,
                                   'p50_ms': statistics.median(old_samples), 'p95_ms': sorted(old_samples)[94], 'samples_ms': old_samples}
            current = ServerAuth.create('127.0.0.1', 0, token=token)
            cookie = current.session_cookie(remember=True).split(';', 1)[0]
            restarted = ServerAuth.create('127.0.0.1', 0, token=token)
            database = directory / 'credentials/browser-sessions.db'
            fingerprint = (database.stat().st_mtime_ns, database.stat().st_size)
            samples = []
            for _ in range(100):
                started = time.perf_counter()
                restored = restarted.restore_session('127.0.0.1', 'http://127.0.0.1:5173', {'cookie': cookie})
                samples.append((time.perf_counter() - started) * 1000)
                assert restored == {}
            reports['after'] = {'bootstrap_status': 200, 'fresh_server_instance': True, 'same_day_reissues': 0,
                                'database_unchanged': fingerprint == (database.stat().st_mtime_ns, database.stat().st_size),
                                'p50_ms': statistics.median(samples), 'p95_ms': sorted(samples)[94], 'samples_ms': samples}
    results = root / 'satrap-ui/benchmarks/results/frontend-loading-2026-10-23'
    results.mkdir(parents=True, exist_ok=True)
    for name, report in reports.items():
        destination = results / f'auth-{name}.json'
        if name == 'baseline' and destination.exists():
            raise RuntimeError('认证 baseline 已存在, 不允许自动覆盖')
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({name: {key: value for key, value in report.items() if key != 'samples_ms'} for name, report in reports.items()}, ensure_ascii=False))


if __name__ == '__main__':
    run()
