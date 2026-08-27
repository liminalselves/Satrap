from __future__ import annotations

from pathlib import Path

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config.loader import ConfigLoader
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.session_discovery import (
    SessionClassDiscoveryService,
    create_default_session_dir,
    discover_session_classes,
)


def _write_session_file(root: Path, name: str, content: str) -> Path:
    """
    写入测试用 session 文件

    参数:
    - root: 根目录
    - name: 名称
    - content: 内容

    返回:
    - Path: 写入测试用 session 文件
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(content, encoding="utf-8")
    return path


def test_discover_multiple_session_classes_in_one_file(tmp_path: Path):
    """
    扫描应发现同一文件中的多个 Session/AsyncSession 子类

    参数:
    - tmp_path: tmp路径
    """
    scan_dir = tmp_path / "session_src"
    _write_session_file(
        scan_dir,
        "demo_sessions.py",
        """
from satrap.core.framework import AsyncSession, Session

class PlainObject:
    pass

class SyncDemo(Session):
    def __init__(self, session_id: str, topic: str, count: int = 1):
        super().__init__(session_id)

class AsyncDemo(AsyncSession):
    def __init__(self, session_id: str, enabled: bool = True):
        super().__init__(session_id)
""".strip(),
    )

    results = discover_session_classes([str(scan_dir)])
    classes = {item.class_name: item for item in results if not item.error}

    assert sorted(classes) == ["AsyncDemo", "SyncDemo"]
    assert classes["AsyncDemo"].is_async is True
    assert classes["SyncDemo"].is_async is False
    assert classes["SyncDemo"].init_params == {"topic": "", "count": 0}


def test_discovery_service_matches_compatibility_function(tmp_path: Path):
    """
    发现服务与原兼容函数应返回相同结果

    参数:
    - tmp_path: 临时目录
    """
    scan_dir = tmp_path / "service_src"
    _write_session_file(
        scan_dir,
        "service_demo.py",
        """
from satrap.core.framework import Session

class ServiceDemo(Session):
    pass
""".strip(),
    )

    service_results = SessionClassDiscoveryService([str(scan_dir)]).discover()
    compatibility_results = discover_session_classes([str(scan_dir)])

    assert [item.to_dict() for item in service_results] == [
        item.to_dict() for item in compatibility_results
    ]


def test_discover_reports_import_errors_without_stopping(tmp_path: Path):
    """
    单个文件导入错误不应影响其它文件扫描

    参数:
    - tmp_path: tmp路径
    """
    scan_dir = tmp_path / "broken_src"
    _write_session_file(scan_dir, "broken.py", "raise RuntimeError('boom')\n")
    _write_session_file(
        scan_dir,
        "ok.py",
        """
from satrap.core.framework import Session

class OkSession(Session):
    pass
""".strip(),
    )

    results = discover_session_classes([str(scan_dir)])

    assert any(item.error and "boom" in item.error for item in results)
    assert any(item.class_name == "OkSession" for item in results)


def test_register_by_discovered_class_path(tmp_path: Path):
    """
    扫描得到的 class_path 可直接注册并生成参数模板

    参数:
    - tmp_path: tmp路径
    """
    scan_dir = tmp_path / "register_src"
    _write_session_file(
        scan_dir,
        "custom.py",
        """
from satrap.core.framework import Session

class CustomSession(Session):
    def __init__(self, session_id: str, label: str):
        super().__init__(session_id)
""".strip(),
    )
    discovered = [item for item in discover_session_classes([str(scan_dir)]) if item.class_name == "CustomSession"][0]
    manager = SessionClassConfigManager(
        storage_path=tmp_path / "session_classes.json",
        session_scan_paths=[str(scan_dir)],
    )

    manager.register_by_class_path("custom", discovered.class_path)

    config = manager.get_config("custom")
    assert config is not None
    assert config["class_path"] == discovered.class_path
    assert manager.get_params("custom") == {"label": ""}


def test_session_scan_paths_config_defaults_and_loading():
    """BackendConfig 应包含默认 session_scan_paths 并可从配置覆盖"""
    default_config = BackendConfig()
    loaded = ConfigLoader.from_dict({"session_scan_paths": ["custom_sessions"], "platforms": []})

    assert default_config.session_scan_paths == [".satrap/session"]
    assert loaded.session_scan_paths == ["custom_sessions"]
    assert ConfigLoader.default_config_document(default_config)["session_scan_paths"] == [".satrap/session"]


def test_create_default_session_dir(tmp_path: Path):
    """
    创建默认 Session 目录时应补齐 __init__.py

    参数:
    - tmp_path: tmp路径
    """
    target = create_default_session_dir([str(tmp_path / "sessions")])

    assert target.exists()
    assert (target / "__init__.py").exists()
