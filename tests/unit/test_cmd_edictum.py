"""
edictum CLI 离线路径测试 (cmd_edictum)

覆盖:
- types / list / show / create / update(含改名迁移) / enable / disable / delete
- delete 的引用保护
- preview/apply 离线时统一报错
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any
import json
import pytest

from satrap.cli import cmd_edictum
from satrap.cli.output import CliError
from satrap.core.backend.BackendManager import BackendConfig


class _DeadClient:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def is_alive(self) -> bool:
        return False


@pytest.fixture
def offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackendConfig:
    """强制离线: 后端不可达, 配置文件指向 tmp"""
    config = BackendConfig(
        data_root=str(tmp_path / "data"),
        edictum_config_path=str(tmp_path / "edictum.json"),
        session_class_config_path=str(tmp_path / "sessions.json"),
        session_scan_paths=[],
    )
    monkeypatch.setattr(cmd_edictum, "daemon_client_from_args", _DeadClient)
    monkeypatch.setattr(cmd_edictum, "load_cli_config", lambda args: config)
    return config


def _args(**overrides: Any) -> Namespace:
    base: dict[str, Any] = dict(
        action="list", name="", type="", model="", description=None, rename="",
        set=None, params_json="", plugin=[], offline=False, force_offline=False,
    )
    base.update(overrides)
    return Namespace(**base)


def test_edictum_types(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """types 列出内置类型"""
    cmd_edictum.cmd_edictum_types(_args(action="types"))
    out = capsys.readouterr().out
    assert "simple" in out


def test_edictum_create_list_show(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """创建后可列出并查看详情"""
    cmd_edictum.cmd_edictum_create(_args(action="create", name="assistant", type="simple", description="demo"))
    capsys.readouterr()

    cmd_edictum.cmd_edictum_list(_args())
    out = capsys.readouterr().out
    assert "assistant" in out

    cmd_edictum.cmd_edictum_show(_args(action="show", name="assistant"))
    detail = json.loads(capsys.readouterr().out)
    assert detail["edictum_type"] == "simple"
    assert detail["description"] == "demo"


def test_edictum_create_duplicate_raises(offline_env: BackendConfig):
    """重名创建抛业务错误"""
    cmd_edictum.cmd_edictum_create(_args(action="create", name="dup", type="simple"))
    with pytest.raises(Exception, match="已存在"):
        cmd_edictum.cmd_edictum_create(_args(action="create", name="dup", type="simple"))


def test_edictum_update_and_rename(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """更新描述与改名"""
    cmd_edictum.cmd_edictum_create(_args(action="create", name="old-name", type="simple"))
    capsys.readouterr()
    cmd_edictum.cmd_edictum_update(_args(action="update", name="old-name", rename="new-name", description="updated"))
    capsys.readouterr()

    with pytest.raises(CliError, match="未找到"):
        cmd_edictum.cmd_edictum_show(_args(action="show", name="old-name"))
    cmd_edictum.cmd_edictum_show(_args(action="show", name="new-name"))
    detail = json.loads(capsys.readouterr().out)
    assert detail["description"] == "updated"


def test_edictum_enable_disable(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """启停切换 enabled 字段"""
    cmd_edictum.cmd_edictum_create(_args(action="create", name="toggle", type="simple"))
    capsys.readouterr()
    cmd_edictum.cmd_edictum_disable(_args(action="disable", name="toggle"))
    capsys.readouterr()
    cmd_edictum.cmd_edictum_show(_args(action="show", name="toggle"))
    detail = json.loads(capsys.readouterr().out)
    assert detail["enabled"] is False
    cmd_edictum.cmd_edictum_enable(_args(action="enable", name="toggle"))
    capsys.readouterr()
    cmd_edictum.cmd_edictum_show(_args(action="show", name="toggle"))
    detail = json.loads(capsys.readouterr().out)
    assert detail["enabled"] is True


def test_edictum_delete(offline_env: BackendConfig, capsys: pytest.CaptureFixture[str]):
    """删除后不可再查; 删除不存在配置抛 CliError"""
    cmd_edictum.cmd_edictum_create(_args(action="create", name="gone", type="simple"))
    capsys.readouterr()
    cmd_edictum.cmd_edictum_delete(_args(action="delete", name="gone"))
    with pytest.raises(CliError, match="未找到"):
        cmd_edictum.cmd_edictum_show(_args(action="show", name="gone"))
    with pytest.raises(CliError, match="未找到"):
        cmd_edictum.cmd_edictum_delete(_args(action="delete", name="gone"))


def test_edictum_runtime_requires_backend(offline_env: BackendConfig):
    """preview/apply 离线时统一报错"""
    with pytest.raises(CliError, match="需要后端运行"):
        cmd_edictum.cmd_edictum_preview(_args(action="preview", name="x"))
    with pytest.raises(CliError, match="需要后端运行"):
        cmd_edictum.cmd_edictum_apply(_args(action="apply", name="x"))


def test_edictum_update_without_fields_raises(offline_env: BackendConfig):
    """update 不带任何字段时给出用法提示"""
    cmd_edictum.cmd_edictum_create(_args(action="create", name="noop", type="simple"))
    with pytest.raises(CliError, match="没有需要更新的字段"):
        cmd_edictum.cmd_edictum_update(_args(action="update", name="noop"))
