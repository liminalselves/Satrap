"""会话类配置共享领域服务测试"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast
import pytest

from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.config.session_class_service import SessionClassConfigService


class _SysPathMonkeyPatch(Protocol):
    """声明测试依赖的模块搜索路径修改接口"""

    def syspath_prepend(self, path: str) -> None:
        """
        将目录加入模块搜索路径开头

        参数:
        - path: 待加入的目录
        """
        ...


def test_session_class_config_service_cold_crud_and_reload(tmp_path: Path):
    """
    共享服务应在不导入会话类时完成冷配置增删改查

    参数:
    - tmp_path: 临时目录
    """
    storage_path = tmp_path / "session-classes.json"
    scan_path = tmp_path / "trusted_sessions"
    scan_path.mkdir()
    (scan_path / "future.py").write_text("raise RuntimeError('不应在冷配置阶段导入')\n", encoding="utf-8")
    manager = SessionClassConfigManager(
        storage_path=storage_path,
        session_scan_paths=[str(scan_path)],
    )
    service = SessionClassConfigService(manager)

    created = service.create(
        {
            "name": "cold",
            "class_path": "trusted_sessions.future.FutureSession",
            "is_async": True,
            "description": "冷创建",
            "params": {"model_name": "default"},
        }
    )

    assert created["class_path"] == "trusted_sessions.future.FutureSession"
    assert created["is_async"] is True
    assert service.list_configs()["cold"]["params"] == {"model_name": "default"}

    updated = service.update(
        "cold",
        {
            "name": "renamed",
            "class_path": "trusted_sessions.future.ProviderSession",
            "description": "已重命名",
            "context_key": "room_id",
        },
    )
    service.set_enabled("renamed", False)

    assert updated["description"] == "已重命名"
    assert service.get("cold") is None
    assert service.get("renamed") is not None
    assert service.get("renamed")["enabled"] is False   # type: ignore[index]

    reloaded = SessionClassConfigService(
        SessionClassConfigManager(
            storage_path=storage_path,
            session_scan_paths=[str(scan_path)],
        )
    )
    assert reloaded.get("renamed") is not None
    assert reloaded.get("renamed")["class_path"] == "trusted_sessions.future.ProviderSession"   # type: ignore[index]
    assert reloaded.delete("renamed") is True
    assert reloaded.list_configs() == {}


def test_session_class_config_service_rejects_invalid_and_conflicting_input(tmp_path: Path):
    """
    共享服务应拒绝非法字段和名称冲突

    参数:
    - tmp_path: 临时目录
    """
    service = SessionClassConfigService(
        SessionClassConfigManager(storage_path=tmp_path / "session-classes.json")
    )
    service.create({"name": "first", "class_path": "satrap.core.framework.Base.Session"})
    service.create({"name": "second", "class_path": "satrap.core.framework.Base.AsyncSession"})

    with pytest.raises(ValueError, match="名称已存在"):
        service.update("first", {"name": "second"})
    with pytest.raises(ValueError, match="未知会话类配置字段"):
        service.update("first", {"unknown": True})
    with pytest.raises(ValueError, match="params 必须是对象"):
        service.create({"name": "bad", "class_path": "example.Bad", "params": []})

    assert service.get("first")["class_path"] == "satrap.core.framework.Base.Session"   # type: ignore[index]
    assert service.get("second")["class_path"] == "satrap.core.framework.Base.AsyncSession"   # type: ignore[index]


def test_session_class_config_rejects_untrusted_module_without_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    管理接口应在导入前拒绝扫描目录外的模块

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    marker = tmp_path / "imported.txt"
    module_path = tmp_path / "evil_session.py"
    module_path.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )
    cast(_SysPathMonkeyPatch, monkeypatch).syspath_prepend(str(tmp_path))
    service = SessionClassConfigService(
        SessionClassConfigManager(
            storage_path=tmp_path / "session-classes.json",
            session_scan_paths=[str(tmp_path / "trusted")],
        )
    )

    with pytest.raises(ValueError, match="不在可信代码根"):
        service.create({"name": "evil", "class_path": "evil_session.EvilSession"})

    assert not marker.exists()


def test_register_config_entry_returns_independent_params_copy(tmp_path: Path):
    """
    冷注册应返回独立副本, 入参与返回值的后续修改均不影响存储

    参数:
    - tmp_path: 临时目录
    """
    storage_path = tmp_path / "session-classes.json"
    scan_path = tmp_path / "trusted_sessions"
    scan_path.mkdir()
    (scan_path / "future.py").write_text("raise RuntimeError('不应在冷配置阶段导入')\n", encoding="utf-8")
    manager = SessionClassConfigManager(
        storage_path=storage_path,
        session_scan_paths=[str(scan_path)],
    )

    source_params = {"model_name": "default"}
    created = manager.register_config_entry(
        "copy-check",
        "trusted_sessions.future.FutureSession",
        params=source_params,
    )

    source_params["model_name"] = "mutated"
    created["params"]["model_name"] = "mutated"

    stored = manager.get_config("copy-check")
    assert stored is not None
    assert stored["params"] == {"model_name": "default"}

    stored["params"]["model_name"] = "mutated"
    reloaded = manager.get_config("copy-check")
    assert reloaded is not None
    assert reloaded["params"] == {"model_name": "default"}
