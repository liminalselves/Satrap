"""
CLI 统一输出层测试 (output.py)

覆盖:
- ok / info / warn / error 前缀约定
- --json 模式下的结构化输出
- dispatch_action: 未知操作退出码 2, CliError 按自带退出码, ValueError 退出码 1
- run_cli_action: DaemonUnavailable / DaemonError 统一渲染
- format_table 对齐
"""
from __future__ import annotations

from argparse import Namespace
import json
import pytest

from satrap.cli import output
from satrap.cli.client import DaemonError, DaemonUnavailable


@pytest.fixture(autouse=True)
def _reset_json_mode():
    """每个测试后恢复人类可读模式"""
    yield
    output.set_json_mode(False)


def test_prefixes(capsys: pytest.CaptureFixture[str]):
    """提示/警告/错误前缀固定"""
    output.info("m1")
    output.warn("m2")
    output.error("m3", hint="h1")
    out = capsys.readouterr().out
    assert "提示: m1" in out
    assert "警告: m2" in out
    assert "错误: m3" in out
    assert "提示: h1" in out


def test_json_mode_error_is_structured(capsys: pytest.CaptureFixture[str]):
    """JSON 模式下错误输出为结构化对象"""
    output.set_json_mode(True)
    output.error("boom", hint="try this")
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "error": "boom", "hint": "try this"}


def test_json_mode_render_data(capsys: pytest.CaptureFixture[str]):
    """JSON 模式下 render_data 输出数据而非人类文本"""
    output.set_json_mode(True)
    output.render_data({"a": 1}, lambda: print("human"))
    out = capsys.readouterr().out
    assert '"a": 1' in out
    assert "human" not in out


def test_dispatch_unknown_action_exits_2(capsys: pytest.CaptureFixture[str]):
    """未知操作退出码 2 且提示统一"""
    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({"list": lambda args: None}, Namespace(action="nope"))
    assert exc.value.code == 2
    assert "未知操作" in capsys.readouterr().out


def test_dispatch_cli_error_uses_own_exit_code(capsys: pytest.CaptureFixture[str]):
    """CliError 按自带退出码退出"""

    def _handler(args: Namespace):
        raise output.CliError("bad", hint="fix it", exit_code=1)

    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({"x": _handler}, Namespace(action="x"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "错误: bad" in out
    assert "提示: fix it" in out


def test_dispatch_value_error_exits_1(capsys: pytest.CaptureFixture[str]):
    """ValueError 统一按业务错误退出码 1"""

    def _handler(args: Namespace):
        raise ValueError("invalid")

    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({"x": _handler}, Namespace(action="x"))
    assert exc.value.code == 1
    assert "错误: invalid" in capsys.readouterr().out


def test_dispatch_daemon_unavailable_exits_1_with_hint(capsys: pytest.CaptureFixture[str]):
    """服务不可达时错误话术带下一步建议"""

    def _handler(args: Namespace):
        raise DaemonUnavailable("后端未响应")

    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({"x": _handler}, Namespace(action="x"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "错误: 后端未响应" in out
    assert "提示:" in out


def test_dispatch_daemon_error_exits_1(capsys: pytest.CaptureFixture[str]):
    """服务端错误响应退出码 1"""

    def _handler(args: Namespace):
        raise DaemonError("服务端错误")

    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({"x": _handler}, Namespace(action="x"))
    assert exc.value.code == 1
    assert "错误: 服务端错误" in capsys.readouterr().out


def test_dispatch_key_attribute(capsys: pytest.CaptureFixture[str]):
    """dispatch_action 支持按 command 属性分发 (顶层命令)"""
    seen: list[str] = []
    output.dispatch_action({"status": lambda args: seen.append("ok")}, Namespace(command="status"), key="command")
    assert seen == ["ok"]
    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({}, Namespace(command="nope"), key="command")
    assert exc.value.code == 2


def test_format_table_aligns_columns():
    """表格按列宽对齐且空表显示占位"""
    text = output.format_table([["a", "长列值"], ["bb", "c"]], ["H1", "H2"])
    lines = text.splitlines()
    assert len(lines) == 4
    assert lines[1].count("-+-") == 1
    assert output.format_table([], ["H"]) == "(空)"
