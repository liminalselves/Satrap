from __future__ import annotations

from pathlib import Path

from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.session_discovery import (
    SessionClassDiscoveryService,
    create_default_session_dir,
    discover_session_classes,
)
from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config.loader import ConfigLoader


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


def test_stdlib_named_session_file_loads_via_synthetic_module(tmp_path: Path, monkeypatch):
    """
    与标准库同名的会话文件 (如 json.py) 应经合成模块名从文件路径显式加载,
    既不遮蔽标准库也不会静默加载到标准库模块

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    import json as stdlib_json
    import sys

    scan_dir = tmp_path / ".satrap" / "session"
    _write_session_file(
        scan_dir,
        "json.py",
        """
from satrap.core.framework import Session

class JsonSession(Session):
    def __init__(self, session_id: str):
        super().__init__(session_id)
""".strip(),
    )
    monkeypatch.chdir(tmp_path)

    results = discover_session_classes([str(scan_dir)])
    found = [item for item in results if item.class_name == "JsonSession"]
    assert len(found) == 1 and not found[0].error
    assert found[0].module_name.startswith("satrap_user_sessions_json_")
    # 标准库 json 不被遮蔽, 合成模块与标准库同时可用
    assert sys.modules["json"] is stdlib_json
    assert stdlib_json.loads('{"ok": true}') == {"ok": True}


def test_cwd_named_session_does_not_replace_stdlib(tmp_path, monkeypatch):
    import json
    import sys
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    source = _write_session_file(tmp_path, "json.py", "from satrap.core.framework import Session\nclass LocalSession(Session): pass\n")
    results = discover_session_classes([str(tmp_path)])
    found = next(item for item in results if item.class_name == "LocalSession")
    assert found.module_name.startswith("satrap_user_sessions_")
    assert sys.modules["json"] is json
    assert json.loads("{}") == {}
    assert found.file_path == str(source)
    sys.modules.pop(found.module_name, None)


def test_failed_session_execution_once_and_registration_restored(tmp_path, monkeypatch):
    import sys
    import pytest
    from satrap.core.framework.session_discovery import load_session_module
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    marker = tmp_path / "count.txt"
    source = _write_session_file(tmp_path, "test.py", "value = 1\n")
    name = "satrap_user_sessions_once_test"
    original = load_session_module(name, source)
    source.write_text(
        f"from pathlib import Path\np = Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "raise ImportError('dependency unavailable')\n", encoding="utf-8",
    )
    with pytest.raises(ImportError):
        load_session_module(name, source)
    assert marker.read_text() == "1"
    assert sys.modules[name] is original and original.value == 1
    with pytest.raises(ImportError):
        load_session_module("satrap_user_sessions_first_failure", source)
    assert marker.read_text() == "2"
    assert "satrap_user_sessions_first_failure" not in sys.modules


def test_package_relative_import_survives_discovery_and_registration(tmp_path, monkeypatch):
    import sys
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    package = tmp_path / "relative_sessions"
    _write_session_file(package, "__init__.py", "")
    _write_session_file(package, "_helper.py", "VALUE = 'ok'\n")
    _write_session_file(package, "custom.py", "from ._helper import VALUE\nfrom satrap.core.framework import Session\nclass RelativeSession(Session): pass\n")
    found = next(item for item in discover_session_classes([str(package)]) if item.class_name == "RelativeSession")
    assert not found.error and found.module_name == "relative_sessions.custom"
    assert sys.modules[found.module_name].VALUE == "ok"
    manager = SessionClassConfigManager(storage_path=tmp_path / "classes.json", session_scan_paths=[str(package)])
    manager.register_by_class_path("relative", found.class_path)
    assert manager.get_config("relative")["class_path"] == found.class_path


def test_parent_import_does_not_execute_session_twice(tmp_path, monkeypatch):
    import sys
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    package = tmp_path / "eager_sessions"
    marker = tmp_path / "count.txt"
    _write_session_file(package, "__init__.py", "from .custom import EagerSession\n")
    _write_session_file(package, "custom.py",
        f"from pathlib import Path\np = Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "from satrap.core.framework import Session\nclass EagerSession(Session): pass\n")
    found = next(item for item in discover_session_classes([str(package)]) if item.class_name == "EagerSession")
    assert not found.error and marker.read_text() == "1"
    assert sys.modules["eager_sessions"].EagerSession is sys.modules[found.module_name].EagerSession


def test_manager_does_not_retry_user_import_error(tmp_path, monkeypatch):
    import sys
    import pytest
    from satrap.core.framework.session_discovery import build_session_module_catalog
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    marker = tmp_path / "count.txt"
    _write_session_file(tmp_path, "broken.py",
        f"from pathlib import Path\np = Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "raise ImportError('dependency unavailable')\n")
    module_name = next(iter(build_session_module_catalog([str(tmp_path)])))
    manager = SessionClassConfigManager(storage_path=tmp_path / "classes.json", session_scan_paths=[str(tmp_path)])
    with pytest.raises(ValueError, match="dependency unavailable"):
        manager.register_by_class_path("broken", module_name + ".BrokenSession")
    assert marker.read_text() == "1"
    assert module_name not in sys.modules
