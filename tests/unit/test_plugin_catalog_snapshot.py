from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import asyncio
import time

import pytest

from satrap.core.utils.async_worker import BoundedAsyncWorker, WorkerBusyError
from satrap.core.utils.snapshot_cache import SnapshotCache
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.core.utils import snapshot_cache


def write_meta(base: Path, name: str, version: str = '1') -> Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'meta.yaml'
    path.write_text(f'name: {name}\nversion: "{version}"\nconfig_schema:\n  note:\n    type: string\n    default: original\n', encoding='utf-8')
    return path


def test_warm_catalog_index_copies_and_external_invalidation(tmp_path, monkeypatch):
    builtin, user = tmp_path / 'builtin', tmp_path / 'user'
    path = write_meta(builtin, 'probe')
    write_meta(user, 'probe', 'shadow')
    calls = []
    load = PluginCatalog._load_entry
    monkeypatch.setattr(PluginCatalog, '_load_entry', staticmethod(lambda path: (calls.append(path), load(path))[1]))
    catalog = PluginCatalog(builtin, user)
    assert catalog.get('probe').version == '1'
    fixed = catalog.snapshot()
    changed = catalog.get('probe')
    changed.config_schema['note'].default = 'changed'
    changed.capabilities['tools']['fake'] = 'changed'
    assert PluginCatalog(builtin, user).get('probe').config_schema['note'].default == 'original'
    assert 'fake' not in catalog.get('probe').capabilities['tools']
    assert len(calls) == 2
    replacement = path.with_suffix('.new')
    replacement.write_text('name: probe\nversion: "2"\n', encoding='utf-8')
    replacement.replace(path)
    assert catalog.get('probe').version == '2'
    assert fixed.get('probe').version == '1'
    write_meta(user, 'added')
    assert catalog.get('added') is not None
    path.unlink()
    assert catalog.get('probe').version == 'shadow'


def test_broken_metadata_recovers_and_preserves_errors(tmp_path):
    path = write_meta(tmp_path, 'probe')
    catalog = PluginCatalog(tmp_path, tmp_path / 'missing')
    path.write_text('name: [invalid', encoding='utf-8')
    assert catalog.scan() == []
    assert catalog.scan_errors[0]['name'] == 'probe'
    catalog.scan_errors.clear()
    catalog.scan()
    assert catalog.scan_errors
    write_meta(tmp_path, 'probe', 'fixed')
    assert catalog.get('probe').version == 'fixed'
    assert catalog.scan_errors == []


def test_concurrent_catalog_builds_once(tmp_path, monkeypatch):
    write_meta(tmp_path, 'probe')
    calls = []
    load = PluginCatalog._load_entry

    def slow(path):
        calls.append(path)
        time.sleep(.03)
        return load(path)

    monkeypatch.setattr(PluginCatalog, '_load_entry', staticmethod(slow))
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: PluginCatalog(tmp_path, tmp_path / 'missing').get('probe'), range(8)))
    assert len(calls) == 1
    assert all(entry.version == '1' for entry in results)


def test_late_snapshot_cannot_replace_new_generation():
    cache = SnapshotCache()
    entered, release = threading.Event(), threading.Event()
    revision = [1]

    def load():
        value = revision[0]
        if value == 1:
            entered.set()
            assert release.wait(2)
        return value

    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(cache.get, 'resource', lambda: revision[0], load)
        assert entered.wait(2)
        revision[0] = 2
        assert cache.get('resource', lambda: revision[0], load) == 2
        release.set()
        assert old.result(timeout=2) == 2
        assert cache.get('resource', lambda: revision[0], load) == 2


@pytest.mark.asyncio
async def test_async_waiters_do_not_fill_pool_and_cancellation_isolated():
    cache = SnapshotCache()
    worker = BoundedAsyncWorker('test-snapshot', workers=1, capacity=1)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return 42

    try:
        first = asyncio.create_task(cache.get_async('key', lambda: 1, load, worker))
        await asyncio.to_thread(entered.wait, 2)
        followers = [asyncio.create_task(cache.get_async('key', lambda: 1, load, worker)) for _ in range(12)]
        await asyncio.sleep(.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await asyncio.gather(*followers) == [42] * 12
        assert calls == [1]
    finally:
        release.set()
        worker.close()


@pytest.mark.asyncio
async def test_queue_full_and_parse_failure_allow_retry():
    cache = SnapshotCache()
    worker = BoundedAsyncWorker('test-snapshot', workers=1, capacity=1)
    release, entered = threading.Event(), threading.Event()
    occupied = asyncio.create_task(worker.run(lambda: (entered.set(), release.wait(2))))
    await asyncio.to_thread(entered.wait, 2)
    try:
        with pytest.raises(WorkerBusyError):
            await cache.get_async('key', lambda: 1, lambda: 42, worker)
        release.set()
        await occupied
        with pytest.raises(ValueError):
            await cache.get_async('key', lambda: 1, lambda: int('bad'), worker)
        assert await cache.get_async('key', lambda: 1, lambda: 42, worker) == 42
    finally:
        release.set()
        await occupied
        worker.close()


@pytest.mark.asyncio
async def test_failure_notifications_follow_cleanup_and_worker_release(monkeypatch):
    cache = SnapshotCache()
    worker = BoundedAsyncWorker('test-snapshot', workers=1, capacity=1)
    original = snapshot_cache.Future

    class CheckedFuture(original):
        def set_exception(self, error):
            assert not cache._pending
            assert worker._slots.acquire(blocking=False)
            worker._slots.release()
            super().set_exception(error)

    monkeypatch.setattr(snapshot_cache, 'Future', CheckedFuture)
    try:
        with pytest.raises(ValueError):
            await cache.get_async('resource', lambda: 1, lambda: int('bad'), worker)
        assert await cache.get_async('resource', lambda: 1, lambda: 7, worker) == 7
    finally:
        worker.close()
