"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from types import SimpleNamespace
import time
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.config.platform_identity import platform_instance_id
from satrap.core.config.platform_messages import MessageScope, PlatformMessageStore, ArchiveMessage
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import EdictumProvider
from satrap.core.group_chat.reminders import ReminderStore
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry
from support.group_read import _FutureAdapter

class ReminderAdapter(_FutureAdapter):
    """支持非数字账号和成员 ID 的虚拟后台平台"""

    supports_scheduled_group_send = True

    def get_client(self):
        return self.client

    def group_chat_group_visible(self, group):
        return self.visible

def spec(enabled=True, **config):
    return {'name': 'group_chat', 'enabled': enabled, 'config': {'reminders_enabled': True, **config},
            'capabilities': {'tools': {'group_chat_create_reminder': True}}}

def setup(tmp_path):
    platform = {'id': 'future', 'type': 'future', 'session_provider': 'edictum', 'session_type': 'assistant'}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path / 'data'), platforms=[platform]))
    adapter = ReminderAdapter(PlatformConfig(**platform, instance_id=platform_instance_id(platform)))
    adapter.started = True
    adapter.client_self_id = 'robot:/一'
    adapter.client = object()
    adapter.visible = True
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters['future'] = adapter
    backend._running = True
    backend._reminder_scheduler = SimpleNamespace(_stopping=False, _clock_unstable=False)
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / 'edictum.json')
    configs.create('assistant', {'edictum_type': 'async_simple', 'model_name': 'base', 'plugins': [spec()]})
    configs.create('without-reminders', {'edictum_type': 'async_simple', 'model_name': 'base', 'plugins': []})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(backend.platform_db_path('future')))
    manager = SessionManager(db_path=backend.platform_db_path('future'), platform_id='future')
    manager.register_provider(provider)
    backend._platform_runtimes['future'] = (manager, None)
    scope = MessageScope('future', adapter.client_self_id, 'group', 'room:/群')
    adapter.message_archive = PlatformMessageStore(backend.platform_db_path('future'), 'future')
    adapter.message_archive.record(scope, ArchiveMessage('source', 'member:/甲', time.time(), '提醒请求'))
    reminder = ReminderStore(adapter.message_archive.database).create(scope, actor='member:/甲', text='检查结果', mentions=['member:/甲'],
        source_message_id='source', operation_id='create', time_spec={'after_seconds': 10},
        source_agent={'instance_id': adapter.config.instance_id})['reminder']
    return backend, adapter, manager, configs, scope, reminder
