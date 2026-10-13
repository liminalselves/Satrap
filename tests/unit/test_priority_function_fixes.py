"""ASR 删除引用锁与 CLI 自定义数据目录的功能回归"""
from concurrent.futures import ThreadPoolExecutor
from argparse import Namespace
from types import SimpleNamespace
import threading

import pytest

from satrap.core.framework.BackGroundManager import ConfigInUseError, ModelConfigManager
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK
from satrap.core.config.model_service import ModelConfigService
from satrap.core.storage import StorageLayout
from satrap.cli import cmd_model
import satrap.core.config.asr_references as asr_references


def test_asr_delete_waits_for_reference_writer_and_observes_committed_reference(tmp_path):
    attempted, checked = threading.Event(), threading.Event()
    references = []

    def checker(_):
        checked.set()
        return list(references)

    manager = ModelConfigManager(tmp_path / 'models.json', asr_in_use_checker=checker)
    ModelConfigService(manager).create('asr', 'speech', {'model': 'whisper-1'})

    def delete():
        attempted.set()
        return manager.remove_asr_config('speech')

    with ThreadPoolExecutor(max_workers=1) as worker:
        with REFERENCE_SCAN_LOCK:
            pending = worker.submit(delete)
            assert attempted.wait(2)
            assert not checked.wait(0.1)
            references.append({'kind': 'plugin_global', 'summary': '刚提交的引用'})
        with pytest.raises(ConfigInUseError):
            pending.result(timeout=3)
    assert manager.has_config('asr', 'speech')


@pytest.mark.parametrize('operation', ['delete', 'rename'])
def test_cli_asr_changes_scan_session_overrides_in_configured_data_root(tmp_path, monkeypatch, operation):
    layout = StorageLayout(tmp_path / 'custom-data')
    database = layout.platform_db('bot')
    database.parent.mkdir(parents=True, exist_ok=True)
    SessionOverrideStore(database).replace('session', 'plugins.echoasr', {'voice': 'speech'}, expected_revision=0)
    plugin = tmp_path / 'plugins' / 'echoasr'
    plugin.mkdir(parents=True)
    (plugin / 'meta.yaml').write_text('name: echoasr\nconfig_schema:\n  voice:\n    type: asr\n', encoding='utf-8')
    config = SimpleNamespace(data_root=layout.root, model_config_path=tmp_path / 'models.json',
        platforms=[{'id': 'bot', 'type': 'onebot', 'settings': {}}])
    monkeypatch.setattr(cmd_model, 'load_cli_config', lambda _: config)
    scan = asr_references.list_asr_config_references

    def isolated_scan(name, **kwargs):
        return scan(name, **kwargs, plugins_dir=tmp_path / 'plugins', plugin_config_dir=tmp_path / 'empty-global')

    monkeypatch.setattr(asr_references, 'list_asr_config_references', isolated_scan)
    manager = cmd_model._init_mgr(Namespace())
    service = ModelConfigService(manager)
    service.create('asr', 'speech', {'model': 'whisper-1'})
    with pytest.raises(ConfigInUseError) as error:
        if operation == 'delete':
            service.delete('asr', 'speech')
        else:
            service.update('asr', 'speech', {'name': 'renamed'})
    assert any(reference.get('kind') == 'session_override' and reference.get('session_id') == 'session'
               for reference in error.value.references)
    assert manager.has_config('asr', 'speech')
    assert not manager.has_config('asr', 'renamed')
