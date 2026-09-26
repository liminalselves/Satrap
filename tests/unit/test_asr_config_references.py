"""ASR 命名配置删除/重命名的引用检查: 平台绑定, 插件全局配置, 会话覆盖与 409 路由"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
import asyncio
import json

import pytest

from satrap.core.config.asr_references import list_asr_config_references
from satrap.core.config.model_service import ModelConfigService
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.config.asr_references import AsrReferenceScanError
from satrap.core.framework.BackGroundManager import (
    ConfigInUseError,
    ConfigReferenceScanError,
    ModelConfigManager,
)
from satrap.core.storage import StorageLayout


def _checker_factory(
    tmp_path: Path, platforms: list[object], layout: StorageLayout | None = None,
) -> Callable[[str], list[dict[str, str]]]:
    """构造只扫描给定平台与空插件目录的检查器, 隔离机器上的真实插件"""

    def checker(config_name: str) -> list[dict[str, str]]:
        return list_asr_config_references(
            config_name,
            platforms=platforms,
            layout=layout,
            plugin_config_dir=tmp_path / "no-plugin-configs",
            plugins_dir=tmp_path / "no-plugins",
        )

    return checker


def _service(tmp_path: Path, platforms: list[object]) -> tuple[ModelConfigService, ModelConfigManager]:
    manager = ModelConfigManager(
        storage_path=tmp_path / "models.json",
        asr_in_use_checker=_checker_factory(tmp_path, platforms),
    )
    return ModelConfigService(manager), manager


class TestPlatformBinding:
    def test_bound_delete_rejected_until_unbound(self, tmp_path: Path):
        platforms: list[object] = [{"id": "bot", "type": "onebot", "settings": {"asr_model": "speech"}}]
        service, manager = _service(tmp_path, platforms)
        service.create("asr", "speech", {"model": "whisper-1", "api_key": "k"})
        with pytest.raises(ConfigInUseError) as exc:
            service.delete("asr", "speech")
        assert exc.value.references[0]["kind"] == "platform"
        assert exc.value.references[0]["platform_id"] == "bot"
        assert "speech" in str(exc.value)
        assert manager.has_config("asr", "speech")
        # 解绑后同一删除正常生效
        platforms.clear()
        assert service.delete("asr", "speech") is True
        assert not manager.has_config("asr", "speech")

    def test_last_item_reset_only_when_unbound(self, tmp_path: Path):
        platforms: list[object] = [{"id": "bot", "type": "onebot", "settings": {"asr_model": "default"}}]
        service, manager = _service(tmp_path, platforms)
        # 仅剩一项时被引用: 拒绝删除而非重置
        with pytest.raises(ConfigInUseError):
            service.delete("asr", "default")
        assert manager.has_config("asr", "default")
        # 无引用时最后一项删除退化为重置为空白配置
        platforms.clear()
        assert service.delete("asr", "default") is True
        assert manager.has_config("asr", "default")
        assert manager.get_asr_config("default").api_key in (None, "")

    def test_rename_bound_rejected_free_ok_and_collision(self, tmp_path: Path):
        platforms: list[object] = [{"id": "bot", "type": "onebot", "settings": {"asr_model": "speech"}}]
        service, manager = _service(tmp_path, platforms)
        service.create("asr", "speech", {"model": "whisper-1"})
        service.create("asr", "other", {"model": "whisper-2"})
        # 名称冲突先于引用检查
        with pytest.raises(ValueError, match="已存在"):
            manager.update_named_config("asr", "speech", {}, new_name="other")
        # 被引用时禁止重命名, 原配置保持可用
        with pytest.raises(ConfigInUseError) as exc:
            manager.update_named_config("asr", "speech", {}, new_name="voice")
        assert exc.value.references[0]["kind"] == "platform"
        assert manager.has_config("asr", "speech") and not manager.has_config("asr", "voice")
        # 解绑后重命名生效
        platforms.clear()
        manager.update_named_config("asr", "speech", {}, new_name="voice")
        assert not manager.has_config("asr", "speech") and manager.has_config("asr", "voice")


class TestReferenceScan:
    def test_plugin_global_and_session_override_references(self, tmp_path: Path):
        plugins_dir = tmp_path / "plugins"
        plugin = plugins_dir / "echoasr"
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(
            "name: echoasr\nversion: 0.1.0\nconfig_schema:\n  voice:\n    type: asr\n",
            encoding="utf-8",
        )
        config_dir = tmp_path / "plugin_config"
        config_dir.mkdir()
        (config_dir / "echoasr.json").write_text(json.dumps({"voice": "speech"}), encoding="utf-8")
        layout = StorageLayout(tmp_path / "data")
        database = layout.platform_db("bot")
        database.parent.mkdir(parents=True, exist_ok=True)
        SessionOverrideStore(database).replace("sess1", "plugins.echoasr", {"voice": "speech"}, expected_revision=0)
        platforms: list[object] = [{"id": "bot", "type": "onebot", "settings": {}}]
        references = list_asr_config_references(
            "speech", platforms=platforms, layout=layout, plugin_config_dir=config_dir, plugins_dir=plugins_dir,
        )
        kinds = {ref["kind"] for ref in references}
        assert kinds == {"plugin_global", "session_override"}
        session_ref = next(ref for ref in references if ref["kind"] == "session_override")
        assert session_ref["session_id"] == "sess1" and session_ref["plugin"] == "echoasr"
        assert all(ref["summary"] for ref in references)
        # 未被引用的同名扫描为空
        assert list_asr_config_references(
            "other", platforms=platforms, layout=layout, plugin_config_dir=config_dir, plugins_dir=plugins_dir,
        ) == []

    def test_scan_failure_is_reported_as_scan_error_not_in_use(self, tmp_path: Path):
        """反例: 扫描不完整必须独立报错, 不能伪称已经找到具体引用"""

        def broken(config_name: str) -> list[dict[str, str]]:
            raise OSError("db locked")

        manager = ModelConfigManager(storage_path=tmp_path / "models.json", asr_in_use_checker=broken)
        service = ModelConfigService(manager)
        service.create("asr", "speech", {"model": "whisper-1"})
        with pytest.raises(ConfigReferenceScanError) as exc:
            service.delete("asr", "speech")
        assert "db locked" in exc.value.reason and exc.value.target == "asr"
        assert manager.has_config("asr", "speech")


class TestControlRoute:
    @pytest.mark.asyncio
    async def test_delete_route_409_then_unbound_200(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from satrap.core.backend import control_server as control
        from satrap.core.config.document import save_config_document

        config_path = tmp_path / "config.json"
        models_path = tmp_path / "models.json"
        monkeypatch.setattr(control, "CONFIG_PATH", config_path)
        document: dict[str, object] = {
            "model_config_path": str(models_path),
            "data_root": str(tmp_path / "data"),
            "platforms": [{"id": "bot", "type": "onebot", "settings": {"asr_model": "speech"}}],
        }
        save_config_document(config_path, document)
        ModelConfigService(ModelConfigManager(storage_path=models_path)).create(
            "asr", "speech", {"model": "whisper-1", "api_key": "k"},
        )

        def delete_context() -> control._RouteContext:
            return control._RouteContext(
                "DELETE", "/config/models/asr/speech", "/config/models/asr/speech", asyncio.StreamReader(), b"",
            )

        response = await control._route_models(delete_context())
        assert response is not None
        status, body = response
        assert status == 409 and body["code"] == "config_in_use"
        references = cast(list[dict[str, str]], body["references"])
        assert any(ref.get("kind") == "platform" for ref in references)
        # 配置仍在, 未被误删
        assert ModelConfigManager(storage_path=models_path).has_config("asr", "speech")
        # 解绑后删除放行
        document["platforms"] = [{"id": "bot", "type": "onebot", "settings": {}}]
        save_config_document(config_path, document)
        response = await control._route_models(delete_context())
        assert response is not None
        assert response[0] == 200
        assert not ModelConfigManager(storage_path=models_path).has_config("asr", "speech")


class TestScanCompleteness:
    """B5 反例: 来源读不出, 解析失败或结构不符时不得返回"无引用"空结论"""

    @staticmethod
    def _asr_plugin(plugins_dir: Path, name: str = "echoasr", meta: str | None = None) -> Path:
        plugin = plugins_dir / name
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(
            meta if meta is not None else f"name: {name}\nversion: 0.1.0\nconfig_schema:\n  voice:\n    type: asr\n",
            encoding="utf-8",
        )
        return plugin

    @staticmethod
    def _scan(
        tmp_path: Path, layout: StorageLayout | None = None, plugins_dir: Path | None = None,
        config_dir: Path | None = None,
    ) -> list[dict[str, str]]:
        return list_asr_config_references(
            "speech", platforms=[{"id": "bot", "type": "onebot", "settings": {}}],
            layout=layout, plugin_config_dir=config_dir or tmp_path / "no-plugin-configs",
            plugins_dir=plugins_dir if plugins_dir is not None else tmp_path / "no-plugins",
        )

    def test_broken_plugin_metadata_is_scan_failure(self, tmp_path: Path):
        """反例: 元数据损坏的插件被静默跳过, 其可能的 asr 引用会漏检"""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        self._asr_plugin(plugins_dir, meta="name: echoasr\nconfig_schema: [未闭合\n")
        with pytest.raises(AsrReferenceScanError) as exc:
            self._scan(tmp_path, plugins_dir=plugins_dir)
        assert exc.value.reason == "plugin_meta" and "echoasr" in exc.value.origin

    def test_broken_plugin_global_json_is_scan_failure(self, tmp_path: Path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        self._asr_plugin(plugins_dir)
        config_dir = tmp_path / "plugin_config"
        config_dir.mkdir()
        (config_dir / "echoasr.json").write_text("{不是 JSON", encoding="utf-8")
        with pytest.raises(AsrReferenceScanError) as exc:
            self._scan(tmp_path, plugins_dir=plugins_dir, config_dir=config_dir)
        assert exc.value.reason == "plugin_config" and "echoasr" in exc.value.origin

    def test_locked_database_is_scan_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import sqlite3 as sqlite_module

        import satrap.core.config.asr_references as module

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        self._asr_plugin(plugins_dir)
        layout = StorageLayout(tmp_path / "data")
        database = layout.platform_db("bot")
        database.parent.mkdir(parents=True, exist_ok=True)
        SessionOverrideStore(database)

        def locked(*args: object, **kwargs: object) -> None:
            raise sqlite_module.OperationalError("database is locked")

        monkeypatch.setattr(module.sqlite3, "connect", locked)
        with pytest.raises(AsrReferenceScanError) as exc:
            self._scan(tmp_path, layout=layout, plugins_dir=plugins_dir)
        assert exc.value.reason == "override_db"

    def test_legacy_database_without_table_is_allowed(self, tmp_path: Path):
        """旧库 (user_version=0 且无覆盖表) 属于契约允许的空集合"""
        import sqlite3

        layout = StorageLayout(tmp_path / "data")
        database = layout.platform_db("bot")
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database))
        connection.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        assert self._scan(tmp_path, layout=layout) == []

    def test_legacy_database_without_table_and_declared_asr_field(self, tmp_path: Path):
        """反例: 已有插件声明 asr 字段时旧库仍按空覆盖集合处理, 缺表既不异常也不报扫描失败"""
        import sqlite3

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        self._asr_plugin(plugins_dir)
        layout = StorageLayout(tmp_path / "data")
        database = layout.platform_db("bot")
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database))
        connection.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        assert self._scan(tmp_path, layout=layout, plugins_dir=plugins_dir) == []

    def test_declared_schema_without_table_is_scan_failure(self, tmp_path: Path):
        """反例: 结构版本已声明覆盖表存在时, 缺表属于损坏而不是"没配置"空结论"""
        import sqlite3

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        self._asr_plugin(plugins_dir)
        layout = StorageLayout(tmp_path / "data")
        database = layout.platform_db("bot")
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database))
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()
        with pytest.raises(AsrReferenceScanError) as exc:
            self._scan(tmp_path, layout=layout, plugins_dir=plugins_dir)
        assert exc.value.reason == "override_schema"

    def test_invalid_override_json_is_scan_failure(self, tmp_path: Path):
        import sqlite3

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        self._asr_plugin(plugins_dir)
        layout = StorageLayout(tmp_path / "data")
        database = layout.platform_db("bot")
        database.parent.mkdir(parents=True, exist_ok=True)
        SessionOverrideStore(database)
        connection = sqlite3.connect(str(database))
        connection.execute(
            "INSERT INTO session_config_overrides "
            "(session_id, namespace, config_json, schema_version, revision, updated_at) "
            "VALUES ('sess1', 'plugins.echoasr', '{坏json', 1, 1, 0.0)",
        )
        connection.commit()
        connection.close()
        with pytest.raises(AsrReferenceScanError) as exc:
            self._scan(tmp_path, layout=layout, plugins_dir=plugins_dir)
        assert exc.value.reason == "override_json" and "sess1" in exc.value.origin

    def test_schema_marker_written_once_and_never_downgraded(self, tmp_path: Path):
        import sqlite3

        layout = StorageLayout(tmp_path / "data")
        database = layout.platform_db("bot")
        database.parent.mkdir(parents=True, exist_ok=True)
        SessionOverrideStore(database)
        connection = sqlite3.connect(str(database))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
        connection.close()
        # 更高版本不回退, 也不因再次初始化被改写
        SessionOverrideStore(database)
        connection = sqlite3.connect(str(database))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        connection.close()


class TestScanRouteAndCli:
    @pytest.mark.asyncio
    async def test_incomplete_scan_returns_503(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import sqlite3

        from satrap.core.backend import control_server as control
        from satrap.core.config.document import save_config_document

        config_path = tmp_path / "config.json"
        models_path = tmp_path / "models.json"
        monkeypatch.setattr(control, "CONFIG_PATH", config_path)
        document: dict[str, object] = {
            "model_config_path": str(models_path),
            "data_root": str(tmp_path / "data"),
            "platforms": [{"id": "bot", "type": "onebot", "settings": {}}],
        }
        save_config_document(config_path, document)
        # 用一个含 asr 字段的插件目录驱动覆盖扫描, 隔离机器上的真实插件
        plugins_dir = tmp_path / "plugins"
        plugin = plugins_dir / "echoasr"
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(
            "name: echoasr\nversion: 0.1.0\nconfig_schema:\n  voice:\n    type: asr\n", encoding="utf-8",
        )
        layout = control._configured_storage_layout()

        def checker(config_name: str) -> list[dict[str, str]]:
            return list_asr_config_references(
                config_name, platforms=cast(list[Any], document["platforms"]), layout=layout, plugins_dir=plugins_dir,
                plugin_config_dir=tmp_path / "no-plugin-configs",
            )

        monkeypatch.setattr(control, "_asr_in_use_checker", checker)
        ModelConfigService(ModelConfigManager(storage_path=models_path, asr_in_use_checker=control._asr_in_use_checker)).create(
            "asr", "speech", {"model": "whisper-1"},
        )
        # 平台库声明了覆盖表版本却缺表: 扫描不完整
        database = layout.platform_db("bot")
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database))
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()

        context = control._RouteContext(
            "DELETE", "/config/models/asr/speech", "/config/models/asr/speech", asyncio.StreamReader(), b"",
        )
        response = await control._route_models(context)
        assert response is not None
        assert response[0] == 503 and response[1]["code"] == "asr_reference_scan_failed"
        assert ModelConfigManager(storage_path=models_path).has_config("asr", "speech")

    def test_cli_exits_non_zero_on_incomplete_scan(self, capsys: pytest.CaptureFixture[str]):
        from argparse import Namespace

        from satrap.cli import output
        from satrap.core.framework.BackGroundManager import ConfigReferenceScanError

        def handler(args: Namespace) -> None:
            raise ConfigReferenceScanError("asr", "speech", "override_db: 平台 bot 覆盖库读取失败")

        with pytest.raises(SystemExit) as exc:
            output.dispatch_action({"x": handler}, Namespace(action="x"))
        assert exc.value.code == 1
        assert "引用扫描不完整" in capsys.readouterr().out
