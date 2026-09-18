"""
plugin CLI 离线路径测试 (cmd_plugin)

覆盖:
- list / show 基于真实插件目录扫描
- enable / disable / capability 写入独立状态文件
- config 查看与修改 (schema 校验)
- 未知插件/非法能力类别/非法状态的统一错误
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any
import json
import pytest

from satrap.cli import cmd_plugin
from satrap.cli.output import CliError
from satrap.display.plugins import ChatPluginRegistry


class _DeadClient:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def is_alive(self) -> bool:
        return False


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChatPluginRegistry:
    """强制离线: 状态文件放入 tmp, Chat 服务视为不可达"""
    reg = ChatPluginRegistry(state_path=tmp_path / "chat_plugins.json")
    monkeypatch.setattr(cmd_plugin, "_registry", lambda: reg)
    monkeypatch.setattr(cmd_plugin, "chat_client_from_args", _DeadClient)
    return reg


def _args(**overrides: Any) -> Namespace:
    base: dict[str, Any] = dict(action="list", name="", kind="", cap="", state="on", set=None, from_json=None)
    base.update(overrides)
    return Namespace(**base)


def _first_available(registry: ChatPluginRegistry) -> dict[str, Any]:
    plugins = [p for p in registry.scan() if p.get("availability", {}).get("allowed", False)]
    assert plugins, "测试环境至少需要一个可用插件"
    return plugins[0]


def test_plugin_list_and_show(registry: ChatPluginRegistry, capsys: pytest.CaptureFixture[str]):
    """list 输出表格, show 输出详情 JSON"""
    cmd_plugin.cmd_plugin_list(_args())
    out = capsys.readouterr().out
    assert "名称" in out and "版本" in out

    name = str(_first_available(registry)["name"])
    cmd_plugin.cmd_plugin_show(_args(action="show", name=name))
    detail = json.loads(capsys.readouterr().out)
    assert detail["name"] == name
    assert "capabilities" in detail


def test_plugin_show_unknown_raises(registry: ChatPluginRegistry):
    """查看不存在的插件抛 CliError"""
    with pytest.raises(CliError, match="插件不存在"):
        cmd_plugin.cmd_plugin_show(_args(action="show", name="no-such-plugin"))


def test_plugin_enable_disable_persists_state(registry: ChatPluginRegistry):
    """启停写入状态文件并可读回"""
    name = str(_first_available(registry)["name"])
    cmd_plugin.cmd_plugin_enable(_args(action="enable", name=name))
    assert registry.is_enabled(name) is True
    cmd_plugin.cmd_plugin_disable(_args(action="disable", name=name))
    assert registry.is_enabled(name) is False


def test_plugin_capability_toggle(registry: ChatPluginRegistry):
    """能力独立启停持久化"""
    plugin = _first_available(registry)
    name = str(plugin["name"])
    caps = plugin["capabilities"]
    kind, items = next((k, v) for k, v in caps.items() if v)
    cap = str(items[0]["name"])

    cmd_plugin.cmd_plugin_capability(_args(action="capability", name=name, kind=kind, cap=cap, state="off"))
    assert registry.capability_enabled(name, kind, cap) is False
    cmd_plugin.cmd_plugin_capability(_args(action="capability", name=name, kind=kind, cap=cap, state="on"))
    assert registry.capability_enabled(name, kind, cap) is True


def test_plugin_capability_rejects_bad_kind(registry: ChatPluginRegistry):
    """非法能力类别抛 CliError"""
    name = str(_first_available(registry)["name"])
    with pytest.raises(CliError, match="非法能力类别"):
        cmd_plugin.cmd_plugin_capability(_args(action="capability", name=name, kind="bogus", cap="x"))


def test_plugin_capability_rejects_bad_state(registry: ChatPluginRegistry):
    """非法状态抛 CliError"""
    name = str(_first_available(registry)["name"])
    with pytest.raises(CliError, match="非法状态"):
        cmd_plugin.cmd_plugin_capability(_args(action="capability", name=name, kind="tools", cap="x", state="maybe"))


def test_plugin_config_view_and_set(registry: ChatPluginRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """配置查看返回 schema + 当前值, --set 经 schema 校验后落盘"""
    name = str(_first_available(registry)["name"])
    entry = registry.catalog.get(name)
    assert entry is not None

    real_manager_cls = cmd_plugin.PluginConfigManager
    monkeypatch.setattr(cmd_plugin, "PluginConfigManager", lambda: real_manager_cls(tmp_path / "plugin_config"))
    cmd_plugin.cmd_plugin_config(_args(action="config", name=name))
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
    cmd_plugin.cmd_plugin_config(_args(action="config", name=name, set=set_expr))
    out = capsys.readouterr().out
    assert "已保存插件配置" in out


def test_plugin_config_rejects_undeclared_key(registry: ChatPluginRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """未声明配置键抛 CliError"""
    name = str(_first_available(registry)["name"])
    real_manager_cls = cmd_plugin.PluginConfigManager
    monkeypatch.setattr(cmd_plugin, "PluginConfigManager", lambda: real_manager_cls(tmp_path / "plugin_config"))
    with pytest.raises(CliError, match="配置校验失败"):
        cmd_plugin.cmd_plugin_config(_args(action="config", name=name, set=[["not_declared_key=1"]]))
