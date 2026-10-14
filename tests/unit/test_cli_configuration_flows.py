"""业务链路场景与集中边界契约"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any
import json
import pytest
from satrap.cli import cmd_edictum
from satrap.cli.output import CliError
from satrap.core.backend.BackendManager import BackendConfig
from satrap.cli import cmd_plugin
from satrap.display.plugins import ChatPluginRegistry
from satrap.cli import cmd_session_instance
from satrap.core.config.session_instance_service import SessionInstanceConfigService
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.storage import StorageLayout
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager

class _cmd_edictum_DeadClient:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def is_alive(self) -> bool:
        return False

@pytest.fixture
def _cmd_edictum_offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackendConfig:
    """强制离线: 后端不可达, 配置文件指向 tmp"""
    config = BackendConfig(
        data_root=str(tmp_path / "data"),
        edictum_config_path=str(tmp_path / "edictum.json"),
        session_class_config_path=str(tmp_path / "sessions.json"),
        session_scan_paths=[],
    )
    monkeypatch.setattr(cmd_edictum, "daemon_client_from_args", _cmd_edictum_DeadClient)
    monkeypatch.setattr(cmd_edictum, "load_cli_config", lambda args: config)
    return config

def _cmd_edictum_args(**overrides: Any) -> Namespace:
    base: dict[str, Any] = dict(
        action="list", name="", type="", model="", description=None, rename="",
        set=None, params_json="", plugin=[], offline=False, force_offline=False,
    )
    base.update(overrides)
    return Namespace(**base)

def test_edictum_types(_cmd_edictum_offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """types 列出内置类型"""
    cmd_edictum.cmd_edictum_types(_cmd_edictum_args(action="types"))
    out = capsys.readouterr().out
    assert "simple" in out

def test_edictum_cli_crud_smoke(_cmd_edictum_offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """CLI 全链路冒烟: 创建/列出/查看/改名/启停/删除与错误路径经真实服务"""
    cmd_edictum.cmd_edictum_create(_cmd_edictum_args(action="create", name="assistant", type="simple", description="demo"))
    capsys.readouterr()

    cmd_edictum.cmd_edictum_list(_cmd_edictum_args())
    assert "assistant" in capsys.readouterr().out

    # 重名创建经服务拒绝
    with pytest.raises(Exception, match="已存在"):
        cmd_edictum.cmd_edictum_create(_cmd_edictum_args(action="create", name="assistant", type="simple"))

    # 改名后旧名不可查, 新名保留更新后的描述
    cmd_edictum.cmd_edictum_update(
        _cmd_edictum_args(action="update", name="assistant", rename="platform-assistant", description="updated"),
    )
    capsys.readouterr()
    with pytest.raises(CliError, match="未找到"):
        cmd_edictum.cmd_edictum_show(_cmd_edictum_args(action="show", name="assistant"))
    cmd_edictum.cmd_edictum_disable(_cmd_edictum_args(action="disable", name="platform-assistant"))
    capsys.readouterr()
    cmd_edictum.cmd_edictum_show(_cmd_edictum_args(action="show", name="platform-assistant"))
    detail = json.loads(capsys.readouterr().out)
    assert detail["edictum_type"] == "simple" and detail["description"] == "updated"
    assert detail["enabled"] is False
    cmd_edictum.cmd_edictum_enable(_cmd_edictum_args(action="enable", name="platform-assistant"))
    capsys.readouterr()
    cmd_edictum.cmd_edictum_show(_cmd_edictum_args(action="show", name="platform-assistant"))
    assert json.loads(capsys.readouterr().out)["enabled"] is True

    # 删除后不可再查, 重复删除同样报错
    cmd_edictum.cmd_edictum_delete(_cmd_edictum_args(action="delete", name="platform-assistant"))
    with pytest.raises(CliError, match="未找到"):
        cmd_edictum.cmd_edictum_show(_cmd_edictum_args(action="show", name="platform-assistant"))
    with pytest.raises(CliError, match="未找到"):
        cmd_edictum.cmd_edictum_delete(_cmd_edictum_args(action="delete", name="platform-assistant"))

def test_edictum_runtime_requires_backend(_cmd_edictum_offline_env: BackendConfig):
    """preview/apply 离线时统一报错"""
    with pytest.raises(CliError, match="需要后端运行"):
        cmd_edictum.cmd_edictum_preview(_cmd_edictum_args(action="preview", name="x"))
    with pytest.raises(CliError, match="需要后端运行"):
        cmd_edictum.cmd_edictum_apply(_cmd_edictum_args(action="apply", name="x"))

def test_edictum_update_without_fields_raises(_cmd_edictum_offline_env: BackendConfig):
    """update 不带任何字段时给出用法提示"""
    cmd_edictum.cmd_edictum_create(_cmd_edictum_args(action="create", name="noop", type="simple"))
    with pytest.raises(CliError, match="没有需要更新的字段"):
        cmd_edictum.cmd_edictum_update(_cmd_edictum_args(action="update", name="noop"))

class _cmd_plugin_DeadClient:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def is_alive(self) -> bool:
        return False

@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChatPluginRegistry:
    """强制离线: 状态文件放入 tmp, Chat 服务视为不可达"""
    reg = ChatPluginRegistry(state_path=tmp_path / "chat_plugins.json")
    monkeypatch.setattr(cmd_plugin, "_registry", lambda: reg)
    monkeypatch.setattr(cmd_plugin, "chat_client_from_args", _cmd_plugin_DeadClient)
    return reg

def _cmd_plugin_args(**overrides: Any) -> Namespace:
    base: dict[str, Any] = dict(action="list", name="", kind="", cap="", state="on", set=None, from_json=None)
    base.update(overrides)
    return Namespace(**base)

def _first_available(registry: ChatPluginRegistry) -> dict[str, Any]:
    plugins = [p for p in registry.scan() if p.get("availability", {}).get("allowed", False)]
    assert plugins, "测试环境至少需要一个可用插件"
    return plugins[0]

def test_plugin_list_and_show(registry: ChatPluginRegistry, capsys: pytest.CaptureFixture[str]):
    """list 输出表格, show 输出详情 JSON"""
    cmd_plugin.cmd_plugin_list(_cmd_plugin_args())
    out = capsys.readouterr().out
    assert "名称" in out and "版本" in out

    name = str(_first_available(registry)["name"])
    cmd_plugin.cmd_plugin_show(_cmd_plugin_args(action="show", name=name))
    detail = json.loads(capsys.readouterr().out)
    assert detail["name"] == name
    assert "capabilities" in detail

def test_plugin_show_unknown_raises(registry: ChatPluginRegistry):
    """查看不存在的插件抛 CliError"""
    with pytest.raises(CliError, match="插件不存在"):
        cmd_plugin.cmd_plugin_show(_cmd_plugin_args(action="show", name="no-such-plugin"))

def test_plugin_enable_disable_persists_state(registry: ChatPluginRegistry):
    """启停写入状态文件并可读回"""
    name = str(_first_available(registry)["name"])
    cmd_plugin.cmd_plugin_enable(_cmd_plugin_args(action="enable", name=name))
    assert registry.is_enabled(name) is True
    cmd_plugin.cmd_plugin_disable(_cmd_plugin_args(action="disable", name=name))
    assert registry.is_enabled(name) is False

def test_plugin_capability_toggle(registry: ChatPluginRegistry):
    """能力独立启停持久化"""
    plugin = _first_available(registry)
    name = str(plugin["name"])
    caps = plugin["capabilities"]
    kind, items = next((k, v) for k, v in caps.items() if v)
    cap = str(items[0]["name"])

    cmd_plugin.cmd_plugin_capability(_cmd_plugin_args(action="capability", name=name, kind=kind, cap=cap, state="off"))
    assert registry.capability_enabled(name, kind, cap) is False
    cmd_plugin.cmd_plugin_capability(_cmd_plugin_args(action="capability", name=name, kind=kind, cap=cap, state="on"))
    assert registry.capability_enabled(name, kind, cap) is True

@pytest.mark.parametrize("overrides, match", [
    ({"kind": "bogus", "cap": "x"}, "非法能力类别"),
    ({"kind": "tools", "cap": "x", "state": "maybe"}, "非法状态"),
])
def test_plugin_capability_rejects_invalid(registry: ChatPluginRegistry, overrides: dict[str, Any], match: str):
    """非法能力类别或状态抛 CliError"""
    name = str(_first_available(registry)["name"])
    with pytest.raises(CliError, match=match):
        cmd_plugin.cmd_plugin_capability(_cmd_plugin_args(action="capability", name=name, **overrides))

def test_plugin_config_view_and_set(registry: ChatPluginRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """配置查看返回 schema + 当前值, --set 经 schema 校验后落盘"""
    name = str(_first_available(registry)["name"])
    entry = registry.catalog.get(name)
    assert entry is not None

    real_manager_cls = cmd_plugin.PluginConfigManager
    monkeypatch.setattr(cmd_plugin, "PluginConfigManager", lambda: real_manager_cls(tmp_path / "plugin_config"))
    cmd_plugin.cmd_plugin_config(_cmd_plugin_args(action="config", name=name))
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "schema" in payload and "config" in payload

    if not entry.config_schema:
        pytest.skip("该插件无可配字段")
    field = next(iter(entry.config_schema.values()))
    if field.type == "bool":
        set_expr = [[f"{field.name}=true"]]
    elif field.type == "number":
        set_expr = [[f"{field.name}=1"]]
    else:
        set_expr = [[f"{field.name}=cli-value"]]
    cmd_plugin.cmd_plugin_config(_cmd_plugin_args(action="config", name=name, set=set_expr))
    out = capsys.readouterr().out
    assert "已保存插件配置" in out

def test_plugin_config_rejects_undeclared_key(registry: ChatPluginRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """未声明配置键抛 CliError"""
    name = str(_first_available(registry)["name"])
    real_manager_cls = cmd_plugin.PluginConfigManager
    monkeypatch.setattr(cmd_plugin, "PluginConfigManager", lambda: real_manager_cls(tmp_path / "plugin_config"))
    with pytest.raises(CliError, match="配置校验失败"):
        cmd_plugin.cmd_plugin_config(_cmd_plugin_args(action="config", name=name, set=[["not_declared_key=1"]]))

class _cmd_session_instance_DeadClient:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def is_alive(self) -> bool:
        return False

@pytest.fixture
def _cmd_session_instance_offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackendConfig:
    """强制离线并预置一个会话类与两个实例"""
    config = BackendConfig(
        data_root=str(tmp_path / "data"),
        session_class_config_path=str(tmp_path / "sessions.json"),
        edictum_config_path=str(tmp_path / "edictum.json"),
        session_scan_paths=[],
    )
    monkeypatch.setattr(cmd_session_instance, "daemon_client_from_args", _cmd_session_instance_DeadClient)
    monkeypatch.setattr(cmd_session_instance, "load_cli_config", lambda args: config)

    scm = SessionClassConfigManager(storage_path=config.session_class_config_path, session_scan_paths=[])
    scm.register_by_class_path("dummy", "satrap.core.framework.Base.Session")
    layout = StorageLayout(config.data_root)
    database = layout.platform_db("local")
    service = SessionInstanceConfigService(
        SessionConfigStore(database),
        UserInfoStore(database),
        scm,
        EdictumConfigManager(create_default_edictum_type_registry(), storage_path=config.edictum_config_path),
        "local",
        layout,
    )
    service.create_instance("session_class", "dummy", session_id="inst-1")
    service.create_instance("session_class", "dummy", session_id="inst-2")
    return config

def _cmd_session_instance_args(**overrides: Any) -> Namespace:
    base: dict[str, Any] = dict(
        instance_action="list", session_id="", session_ids=[], mode="selected",
        platform_id="", offline=False, force_offline=False,
    )
    base.update(overrides)
    return Namespace(**base)

def test_instance_list(_cmd_session_instance_offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """列出持久化实例"""
    cmd_session_instance.cmd_instance_list(_cmd_session_instance_args())
    out = capsys.readouterr().out
    assert "inst-1" in out and "inst-2" in out

def test_instance_delete(_cmd_session_instance_offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """删除单个实例"""
    cmd_session_instance.cmd_instance_delete(_cmd_session_instance_args(instance_action="delete", session_id="inst-1"))
    capsys.readouterr()
    cmd_session_instance.cmd_instance_list(_cmd_session_instance_args())
    out = capsys.readouterr().out
    assert "inst-1" not in out
    assert "inst-2" in out

def test_instance_delete_missing_raises(_cmd_session_instance_offline_env: BackendConfig):
    """删除不存在实例抛 CliError"""
    with pytest.raises(CliError, match="不存在"):
        cmd_session_instance.cmd_instance_delete(_cmd_session_instance_args(instance_action="delete", session_id="nope"))

def test_instance_bulk_delete_empty(_cmd_session_instance_offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """empty 模式删除无消息实例"""
    cmd_session_instance.cmd_instance_bulk_delete(_cmd_session_instance_args(instance_action="bulk-delete", mode="empty"))
    out = capsys.readouterr().out
    assert "inst-1" in out and "inst-2" in out
    cmd_session_instance.cmd_instance_list(_cmd_session_instance_args())
    out = capsys.readouterr().out
    assert "没有持久化会话实例" in out

def test_instance_bulk_delete_selected_requires_ids(_cmd_session_instance_offline_env: BackendConfig):
    """selected 模式不带 ID 时给出用法提示"""
    with pytest.raises(CliError, match="至少选择一个"):
        cmd_session_instance.cmd_instance_bulk_delete(_cmd_session_instance_args(instance_action="bulk-delete", mode="selected", session_ids=[]))

def test_instance_bulk_delete_selected(_cmd_session_instance_offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """selected 模式删除指定实例"""
    cmd_session_instance.cmd_instance_bulk_delete(
        _cmd_session_instance_args(instance_action="bulk-delete", mode="selected", session_ids=["inst-2"])
    )
    capsys.readouterr()
    cmd_session_instance.cmd_instance_list(_cmd_session_instance_args())
    out = capsys.readouterr().out
    assert "inst-1" in out and "inst-2" not in out

def test_instance_restart_requires_backend(_cmd_session_instance_offline_env: BackendConfig):
    """restart 离线时统一报错"""
    with pytest.raises(CliError, match="需要后端运行"):
        cmd_session_instance.cmd_instance_restart(_cmd_session_instance_args(instance_action="restart", session_id="inst-1"))

def test_unknown_platform_raises(_cmd_session_instance_offline_env: BackendConfig):
    """未知平台实例 ID 统一报错"""
    with pytest.raises(CliError, match="未知平台实例"):
        cmd_session_instance.cmd_instance_delete(
            _cmd_session_instance_args(instance_action="delete", session_id="inst-1", platform_id="no-such-platform")
        )
