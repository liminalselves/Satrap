"""插件配置机制测试: config_schema 解析 / 全局读写 / 两级合成 / install_plugin 注入"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import SimpleSession
from satrap.edictum.plugin_config import (
    ConfigField,
    PluginConfigManager,
    parse_config_schema,
    schema_to_payload,
)


class _FakeLLM(LLM):
    """最小同步 fake LLM"""

    def __init__(self) -> None:
        pass

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content="回复")

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        yield LLMCallStreamEvent(kind="content_delta", delta="流")


# ---------------- schema 解析 ----------------

def test_parse_config_schema_full():
    """完整 config_schema 解析为 ConfigField"""
    meta: dict[str, Any] = {
        "config_schema": {
            "sandbox_root": {"type": "path", "default": "", "description": "沙箱根"},
            "search_timeout": {"type": "number", "default": 10},
            "mode": {"type": "select", "default": "a", "options": ["a", "b"]},
            "verbose": {"type": "bool", "default": False},
        }
    }
    schema = parse_config_schema(meta)
    assert set(schema) == {"sandbox_root", "search_timeout", "mode", "verbose"}
    assert schema["sandbox_root"].type == "path"
    assert schema["search_timeout"].default == 10
    assert schema["mode"].options == ["a", "b"]
    assert schema["verbose"].type == "bool"


def test_parse_config_schema_shorthand_and_invalid():
    """简写形式 + 非法类型回退 string + 非字典整体跳过"""
    meta: dict[str, Any] = {"config_schema": {"plain": "默认值", "bad": {"type": "weird"}}}
    schema = parse_config_schema(meta)
    assert schema["plain"].type == "string"
    assert schema["plain"].default == "默认值"
    assert schema["bad"].type == "string"
    assert parse_config_schema({"config_schema": "not-dict"}) == {}
    assert parse_config_schema({}) == {}


def test_config_field_validate():
    """各类型校验与归一化"""
    assert ConfigField("n", "number", 10).validate("15") == 15.0
    assert ConfigField("n", "number", 10).validate(3) == 3
    assert ConfigField("b", "bool", False).validate(1) is True
    assert ConfigField("s", "select", "a", options=["a", "b"]).validate("b") == "b"
    # 不在可选值回退默认
    assert ConfigField("s", "select", "a", options=["a", "b"]).validate("z") == "a"
    # None 回退默认
    assert ConfigField("p", "path", "/x").validate(None) == "/x"


def test_schema_to_payload():
    """schema 转可序列化 payload"""
    schema = {"root": ConfigField("root", "path", "", "根目录", [])}
    payload = schema_to_payload(schema)
    assert payload["root"]["type"] == "path"
    assert payload["root"]["description"] == "根目录"
    json.dumps(payload)  # 必须可序列化


# ---------------- 全局读写 + 合成 ----------------

def _schema() -> dict[str, ConfigField]:
    return {
        "sandbox_root": ConfigField("sandbox_root", "path", ""),
        "timeout": ConfigField("timeout", "number", 10),
    }


def test_global_roundtrip(tmp_path: Path):
    """写全局配置后读回, 未设置键用默认"""
    mgr = PluginConfigManager(tmp_path)
    mgr.save_global("p1", _schema(), {"sandbox_root": "/custom"})
    loaded = mgr.load_global("p1", _schema())
    assert loaded["sandbox_root"] == "/custom"
    assert loaded["timeout"] == 10  # 默认


def test_global_ignores_undeclared_key(tmp_path: Path):
    """未在 schema 声明的键被忽略"""
    mgr = PluginConfigManager(tmp_path)
    mgr.save_global("p1", _schema(), {"ghost": 1, "timeout": 20})
    loaded = mgr.load_global("p1", _schema())
    assert "ghost" not in loaded
    assert loaded["timeout"] == 20


def test_resolve_session_override(tmp_path: Path):
    """会话覆盖优先于全局"""
    mgr = PluginConfigManager(tmp_path)
    mgr.save_global("p1", _schema(), {"timeout": 20})
    resolved = mgr.resolve("p1", _schema(), {"timeout": 99})
    assert resolved["timeout"] == 99
    # 无覆盖用全局
    assert mgr.resolve("p1", _schema())["timeout"] == 20


def test_load_global_missing_file(tmp_path: Path):
    """无配置文件时全部默认"""
    mgr = PluginConfigManager(tmp_path)
    assert mgr.load_global("ghost", _schema()) == {"sandbox_root": "", "timeout": 10}


# ---------------- install_plugin 注入 ----------------

def _make_plugin(tmp_path: Path) -> Path:
    """造一个声明 config_schema + get_tools(session, config) 的插件"""
    plugin_dir = tmp_path / "cfg_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "meta.yaml").write_text(
        "name: cfg_plugin\n"
        "version: 0.1.0\n"
        "config_schema:\n"
        "  root:\n"
        "    type: path\n"
        "    default: ''\n"
        "  timeout:\n"
        "    type: number\n"
        "    default: 10\n",
        encoding="utf-8",
    )
    (plugin_dir / "tools.py").write_text(
        "from satrap.core.utils.TCBuilder import Tool\n"
        "\n"
        "CAPTURED = {}\n"
        "\n"
        "class CfgTool(Tool):\n"
        "    tool_name = 'cfg_tool'\n"
        "    description = 'cfg'\n"
        "    params_dict = {}\n"
        "\n"
        "    def __init__(self, config):\n"
        "        super().__init__()\n"
        "        self._config = config\n"
        "\n"
        "    def execute(self):\n"
        "        return str(self._config)\n"
        "\n"
        "def get_tools(session, config):\n"
        "    CAPTURED.update(config)\n"
        "    return [CfgTool(config)]\n",
        encoding="utf-8",
    )
    return plugin_dir


def test_install_plugin_injects_config(tmp_path: Path, monkeypatch: Any):
    """install_plugin 把合成配置注入 get_tools 工厂 (默认 + 会话覆盖)"""
    plugin_dir = _make_plugin(tmp_path)
    # 全局配置目录隔离
    from satrap.edictum import simple_session as ss_mod
    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))

    s = SimpleSession("c1", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(plugin_dir), config={"timeout": 42})

    # 从注册的工具实例读注入的配置 (避免依赖模块名/缓存)
    tool = s._wf.tools_manager.tools["cfg_tool"]
    captured = getattr(tool, "_config")
    assert captured["timeout"] == 42  # 会话覆盖
    assert captured["root"] == ""     # schema 默认


def test_install_plugin_config_schema_on_plugin(tmp_path: Path, monkeypatch: Any):
    """安装后 plugin.config_schema 携带序列化声明供前端渲染"""
    plugin_dir = _make_plugin(tmp_path)
    from satrap.edictum import simple_session as ss_mod
    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))

    s = SimpleSession("c1", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    plugin = s.install_plugin(str(plugin_dir))
    assert plugin.config_schema["root"]["type"] == "path"
    assert plugin.config_schema["timeout"]["default"] == 10
    json.dumps(plugin.config_schema)
