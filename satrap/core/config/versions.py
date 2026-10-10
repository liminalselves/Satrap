"""
配置来源的轻量版本探测

只返回不透明变化标识与来源可读状态, 不构造模型或会话管理器,
不参与保存冲突判断或调用授权; 常规探测仅 stat, 变化或低频核验时读取内容
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal
import threading
import hashlib
import time
import json
import os
import yaml

from satrap.core.config._yaml import safe_yaml_load
from satrap.core.config_paths import get_config_dir
from satrap.edictum.plugin_config import CONFIG_DIR
from satrap.edictum.plugin import PLUGINS_PRESET_DIR, USER_PLUGINS_DIR
from satrap.core.utils.documents import DEFAULT_MAX_FILE_SIZE

from satrap.core.log import logger

RESOURCES = ('main_config', 'models', 'session_classes', 'edictum_configs', 'chat_plugins', 'plugin_catalog', 'plugin_defaults')
_PATH_KEYS = ('model_config_path', 'session_class_config_path', 'edictum_config_path')
_State = Literal['available', 'missing', 'damaged', 'unreadable']


def _token(value: object) -> str:
    """
    为内部来源信息生成不透明标识

    参数:
    - value: 文件指纹或目录集合

    返回:
    - SHA256 摘要, 不是保存 revision 或授权凭据
    """
    return hashlib.sha256(repr(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class _Probe:
    """内部已核验来源, 主文档只保留路径字段"""

    signature: tuple[object, ...]
    change_token: str
    state: _State
    checked_at: float
    paths: dict[str, str] = field(default_factory=dict)

    def payload(self) -> dict[str, str]:
        """
        转换为不包含正文和路径的响应

        返回:
        - 变化标识与可用状态
        """
        return {'change_token': self.change_token, 'state': self.state}


class ConfigVersionService:
    """单进程来源探测器, 由控制服务在有界工作线程中调用"""

    def __init__(self, main_path: Path, project_root: Path, *, verify_interval: float = 60,
                 on_content_change: Callable[[], None] | None = None) -> None:
        """
        初始化来源缓存

        参数:
        - main_path: 当前主配置路径
        - project_root: 相对配置路径的解析根目录
        - verify_interval: stat 未变化时核验内容的间隔秒数
        - on_content_change: stat 未变化但内容变化时使下游读取快照失效
        """
        self.main_path, self.project_root = main_path, project_root
        self.verify_interval = verify_interval
        self._on_content_change = on_content_change
        self._probes: OrderedDict[tuple[str, str], _Probe] = OrderedDict()
        self._lock = threading.RLock()

    def _file(self, path: Path, kind: str, force: bool) -> _Probe:
        """
        探测文件并按指纹复用语法核验结果

        参数:
        - path: 内部来源路径
        - kind: main, json 或 opaque, opaque 不解释元数据语义
        - force: 是否强制读取内容

        返回:
        - 不包含配置正文的内部来源结果
        """
        identity = os.path.normcase(os.path.abspath(path))
        key = (identity, kind)
        now = time.monotonic()
        state: _State = 'available'
        paths: dict[str, str] = {}
        try:
            info = path.stat()
            signature: tuple[object, ...] = (identity, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)
        except FileNotFoundError:
            signature, state = (identity, 'missing'), 'missing'
        except OSError as error:
            signature, state = (identity, type(error).__name__), 'unreadable'
        previous = self._probes.get(key)
        if not force and previous and previous.signature == signature and now - previous.checked_at < self.verify_interval:
            self._probes.move_to_end(key)
            return previous
        digest = ''
        if state == 'available':
            try:
                with path.open('rb') as file:
                    raw = file.read(DEFAULT_MAX_FILE_SIZE + 1)
                if len(raw) > DEFAULT_MAX_FILE_SIZE:
                    raise ValueError('配置来源超过读取上限')
                digest = hashlib.sha256(raw).hexdigest()
                if kind != 'opaque':
                    text = raw.decode('utf-8')
                    if kind == 'main':
                        try:
                            data = json.loads(text) if path.suffix.lower() == '.json' else safe_yaml_load(text)
                        except json.JSONDecodeError:
                            data = safe_yaml_load(text)
                        data = {} if data is None else data
                    else:
                        data = json.loads(text)
                    if not isinstance(data, dict):
                        raise ValueError('配置根节点必须是对象')
                    if kind == 'main':
                        paths = {name: str(data[name]) for name in _PATH_KEYS if data.get(name)}
            except (ValueError, UnicodeError, yaml.YAMLError) as error:
                state = 'damaged'
                logger.warning(f'[配置版本] 来源损坏, 文件={path.name}, 类型={type(error).__name__}')
            except OSError as error:
                state = 'unreadable'
                logger.warning(f'[配置版本] 来源不可读, 文件={path.name}, 类型={type(error).__name__}')
            try:
                after = path.stat()
                if signature != (identity, after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_ino):
                    raise RuntimeError('配置来源在版本探测期间变化, 请重试')
            except FileNotFoundError as error:
                raise RuntimeError('配置来源在版本探测期间删除, 请重试') from error
        elif state == 'unreadable':
            logger.warning(f'[配置版本] 来源无法探测, 文件={path.name}')
        probe = _Probe(signature, _token((signature, digest, state)), state, now, paths)
        if previous and previous.signature == signature and previous.change_token != probe.change_token:
            if self._on_content_change:
                self._on_content_change()
        self._probes[key] = probe
        self._probes.move_to_end(key)
        while len(self._probes) > 128:
            self._probes.popitem(last=False)
        return probe

    def _default_path(self, name: str, legacy_name: str | None = None) -> Path:
        """
        沿用默认目录优先级但不执行迁移或创建文件

        参数:
        - name: 新目录文件名
        - legacy_name: 旧目录名称

        返回:
        - 当前可读取的新位置或旧位置
        """
        target = get_config_dir() / name
        legacy = self.project_root / '.satrap' / (legacy_name or name)
        return target if os.getenv('SATRAP_CONFIG_ROOT') or target.exists() or not legacy.exists() else legacy

    def _directory(self, roots: Iterable[Path], kind: str, force: bool) -> dict[str, str]:
        """
        探测目录集合, 不解析插件 YAML 或扫描会话实例

        参数:
        - roots: 插件根目录或全局参数目录
        - kind: catalog 或 defaults
        - force: 是否强制核验文件内容

        返回:
        - 集合变化标识及读取状态
        """
        records: list[object] = []
        states: list[_State] = []
        present = False
        try:
            for root in roots:
                records.append(os.path.normcase(os.path.abspath(root)))
                if not root.exists():
                    records.append('missing')
                    continue
                present = True
                for child in sorted(root.iterdir()):
                    path = child / 'meta.yaml' if kind == 'catalog' and child.is_dir() else child
                    if (kind == 'catalog' and not child.is_dir()) or (kind == 'defaults' and child.suffix != '.json'):
                        continue
                    probe = self._file(path, 'opaque' if kind == 'catalog' else 'json', force)
                    records.append((child.name, probe.change_token))
                    states.append(probe.state)
        except OSError as error:
            logger.warning(f'[配置版本] 目录不可读, 类型={type(error).__name__}')
            return {'change_token': _token((records, type(error).__name__)), 'state': 'unreadable'}
        state = 'unreadable' if 'unreadable' in states else 'damaged' if 'damaged' in states else 'available' if present else 'missing'
        return {'change_token': _token((records, state)), 'state': state}

    def get(self, resources: Iterable[str] = RESOURCES, *, force: bool = False) -> dict[str, object]:
        """
        读取指定资源的版本, 主文档只用于选择独立配置来源

        参数:
        - resources: 已知资源名集合
        - force: 强制内容核验, 用于人工刷新

        返回:
        - 不含配置正文, 凭据或存储路径的版本响应
        """
        names = tuple(dict.fromkeys(resources))
        if not names or any(name not in RESOURCES for name in names):
            raise ValueError('未知或空配置版本资源')
        with self._lock:
            main = self._file(self.main_path, 'main', force)
            result: dict[str, dict[str, str]] = {}
            definitions = {'models': ('model_config_path', 'SATRAP_MODEL_CONFIG_PATH', 'model_config.json'),
                           'session_classes': ('session_class_config_path', 'SATRAP_SESSION_CLASS_CONFIG_PATH', 'session_class_config.json'),
                           'edictum_configs': ('edictum_config_path', 'SATRAP_EDICTUM_CONFIG_PATH', 'edictum_session_config.json')}
            for name in names:
                if name == 'main_config':
                    result[name] = main.payload()
                elif name in definitions:
                    if main.state not in {'available', 'missing'}:
                        result[name] = {'change_token': _token((name, main.change_token)), 'state': 'unreadable'}
                        continue
                    config_key, env_key, filename = definitions[name]
                    if config_key in main.paths:
                        path = Path(main.paths[config_key])
                        if not path.is_absolute():
                            path = self.project_root / path
                    else:
                        path = Path(os.environ[env_key]) if os.getenv(env_key) else self._default_path(filename)
                    result[name] = self._file(path, 'json', force).payload()
                elif name == 'chat_plugins':
                    result[name] = self._file(self._default_path('chat_plugins.json'), 'json', force).payload()
                elif name == 'plugin_catalog':
                    result[name] = self._directory((PLUGINS_PRESET_DIR, USER_PLUGINS_DIR), 'catalog', force)
                else:
                    result[name] = self._directory((CONFIG_DIR,), 'defaults', force)
            if self._file(self.main_path, 'main', False).change_token != main.change_token:
                raise RuntimeError('配置来源路径在探测期间变化, 请重试')
            return {'ok': True, 'resources': result}
