"""ASR 命名配置删除/重命名的引用检查: 平台绑定, 插件全局配置, 会话覆盖与 409 路由"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast
import asyncio
import json

import pytest

from satrap.core.config.asr_references import list_asr_config_references
from satrap.core.config.model_service import ModelConfigService
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.framework.BackGroundManager import ConfigInUseError, ModelConfigManager
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

    def test_scan_failure_fails_closed(self, tmp_path: Path):
        def broken(config_name: str) -> list[dict[str, str]]:
            raise OSError("db locked")

        manager = ModelConfigManager(storage_path=tmp_path / "models.json", asr_in_use_checker=broken)
        service = ModelConfigService(manager)
        service.create("asr", "speech", {"model": "whisper-1"})
        with pytest.raises(ConfigInUseError) as exc:
            service.delete("asr", "speech")
        assert exc.value.references[0]["kind"] == "scan_error"
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
