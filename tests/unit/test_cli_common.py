"""CLI 公共工具单元测试 (load_cli_config / coerce_value / parse_kv_pairs 等)

覆盖:
- load_cli_config: 自动探测 / yaml 加载 / api_host & api_port 覆盖
- offline_requested / force_offline 标志
- ensure_offline_allowed: 后端在线拒绝 / 强制离线警告 / 后端离线放行
- parse_kv_pairs / coerce_value 类型转换
- print_json 输出
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from satrap.cli import common
from satrap.core.backend.BackendManager import BackendConfig


class _AliveClient:
    """模拟后端在线的 daemon client"""

    def is_alive(self) -> bool:
        return True


class _DeadClient:
    """模拟后端离线的 daemon client"""

    def is_alive(self) -> bool:
        return False


def _alive_client_factory(args: Namespace, timeout: float = 2) -> _AliveClient:
    return _AliveClient()


def _dead_client_factory(args: Namespace, timeout: float = 2) -> _DeadClient:
    return _DeadClient()


def _passthrough(cfg: BackendConfig) -> BackendConfig:
    return cfg


def _autodetect_fixture(monkeypatch: pytest.MonkeyPatch):
    """屏蔽真实配置加载: autodetect 返回默认配置, merge_env 透传"""
    monkeypatch.setattr(common.ConfigLoader, "autodetect", staticmethod(lambda: BackendConfig()))
    monkeypatch.setattr(common.ConfigLoader, "merge_env", staticmethod(_passthrough))


# ================= load_cli_config =================


def test_load_cli_config_autodetect(monkeypatch: pytest.MonkeyPatch):
    """无 config 参数时使用自动探测结果"""
    _autodetect_fixture(monkeypatch)
    cfg = common.load_cli_config(Namespace(config=None, api_host=None, api_port=None))
    assert isinstance(cfg, BackendConfig)


def test_load_cli_config_from_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """config 指向 yaml 文件时从文件加载"""
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text("api_host: 1.2.3.4\napi_port: 9999\n", encoding="utf-8")
    _autodetect_fixture(monkeypatch)

    cfg = common.load_cli_config(Namespace(config=str(yaml_path), api_host=None, api_port=None))
    assert cfg.api_host == "1.2.3.4"
    assert cfg.api_port == 9999


def test_load_cli_config_from_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """config 指向 json 文件时从文件加载"""
    json_path = tmp_path / "cfg.json"
    json_path.write_text('{"api_host": "10.0.0.1"}', encoding="utf-8")
    _autodetect_fixture(monkeypatch)

    cfg = common.load_cli_config(Namespace(config=str(json_path), api_host=None, api_port=None))
    assert cfg.api_host == "10.0.0.1"


def test_load_cli_config_api_overrides(monkeypatch: pytest.MonkeyPatch):
    """args 中的 api_host / api_port 覆盖配置"""
    _autodetect_fixture(monkeypatch)
    cfg = common.load_cli_config(
        Namespace(config=None, api_host="192.168.1.1", api_port="8080")
    )
    assert cfg.api_host == "192.168.1.1"
    assert cfg.api_port == 8080


# ================= 离线标志 =================


def test_offline_flags():
    """offline / force_offline 标志读取"""
    assert common.offline_requested(Namespace(offline=True)) is True
    assert common.offline_requested(Namespace()) is False
    assert common.force_offline(Namespace(force_offline=True)) is True
    assert common.force_offline(Namespace()) is False


# ================= ensure_offline_allowed =================


def test_ensure_offline_allowed_rejects_when_backend_alive(monkeypatch: pytest.MonkeyPatch):
    """后端在线且未强制时拒绝并退出码 1"""
    monkeypatch.setattr(common, "daemon_client_from_args", _alive_client_factory)
    with pytest.raises(SystemExit) as exc:
        common.ensure_offline_allowed(Namespace())
    assert exc.value.code == 1


def test_ensure_offline_allowed_force_warns(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """后端在线但带 --force-offline 时仅警告"""
    monkeypatch.setattr(common, "daemon_client_from_args", _alive_client_factory)
    common.ensure_offline_allowed(Namespace(force_offline=True))
    out = capsys.readouterr().out
    assert "警告" in out


def test_ensure_offline_allowed_passes_when_backend_down(monkeypatch: pytest.MonkeyPatch):
    """后端离线时正常放行"""
    monkeypatch.setattr(common, "daemon_client_from_args", _dead_client_factory)
    common.ensure_offline_allowed(Namespace())


# ================= parse_kv_pairs / coerce_value =================


def test_parse_kv_pairs():
    """key=value 解析与类型转换"""
    updates = common.parse_kv_pairs([["a=1", "b=true"], ["c=null"]])
    assert updates == {"a": 1, "b": True, "c": None}
    assert common.parse_kv_pairs(None) == {}


def test_parse_kv_pairs_raises_without_equal():
    """缺少 = 的条目抛出 ValueError"""
    with pytest.raises(ValueError, match="key=value"):
        common.parse_kv_pairs([["no-equal"]])


def test_coerce_value():
    """字符串到标量的类型转换"""
    assert common.coerce_value("true") is True
    assert common.coerce_value("True") is True
    assert common.coerce_value("false") is False
    assert common.coerce_value("False") is False
    assert common.coerce_value("null") is None
    assert common.coerce_value("None") is None
    assert common.coerce_value("42") == 42
    assert common.coerce_value("[1,2]") == [1, 2]
    assert common.coerce_value("hello world") == "hello world"


def test_print_json(capsys: pytest.CaptureFixture[str]):
    """print_json 输出格式化 JSON"""
    common.print_json({"a": 1, "b": "中文"})
    out = capsys.readouterr().out
    assert '"a": 1' in out
    assert "中文" in out
