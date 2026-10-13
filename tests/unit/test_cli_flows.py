"""业务链路场景与集中边界契约"""
from __future__ import annotations

from argparse import Namespace
from email.message import Message
from typing import Any
import io
import urllib.error
import pytest
from satrap.cli import client as client_mod
from satrap.cli.client import DaemonClient, DaemonError, DaemonInfo, DaemonUnavailable
from satrap.cli import common
from pathlib import Path
from satrap.core.backend.BackendManager import BackendConfig
from satrap.cli import output
import json
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.main import _build_parser

def _client() -> DaemonClient:
    return DaemonClient(daemon=DaemonInfo(host="127.0.0.1", port=19999), token="t")

def test_request_raises_unavailable_on_connection_error(monkeypatch: pytest.MonkeyPatch):
    """连接失败抛 DaemonUnavailable"""

    def _raise(req: Any, timeout: float = 0):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise)
    with pytest.raises(DaemonUnavailable):
        _client().health()

def test_request_raises_daemon_error_with_server_message(monkeypatch: pytest.MonkeyPatch):
    """HTTP 错误解析服务端 error 字段"""

    def _raise(req: Any, timeout: float = 0):
        raise urllib.error.HTTPError(
            req.full_url, 409, "Conflict", hdrs=Message(), fp=io.BytesIO(b'{"error": "still referenced"}'),
        )

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise)
    with pytest.raises(DaemonError, match="still referenced"):
        _client().health()

def test_request_reports_stable_reason_code(monkeypatch: pytest.MonkeyPatch):
    """手动唤醒等接口的拒绝信封只有 status/reason, CLI 必须给出原因码而不是通用 HTTP 文案"""

    def _raise(req: Any, timeout: float = 0):
        raise urllib.error.HTTPError(
            req.full_url, 409, "Conflict", hdrs=Message(), fp=io.BytesIO(b'{"status": "rejected", "reason": "queue_full"}'),
        )

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise)
    with pytest.raises(DaemonError, match="queue_full"):
        _client().health()

def test_is_alive_never_raises(monkeypatch: pytest.MonkeyPatch):
    """is_alive 探测在不可达时返回 False"""

    def _raise(req: Any, timeout: float = 0):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise)
    assert _client().is_alive() is False

def test_new_routes_call_expected_paths(monkeypatch: pytest.MonkeyPatch):
    """会话实例 / edictum / 插件方法请求对应路由"""
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    client = _client()
    monkeypatch.setattr(client, "_request", lambda m, p, body=None: calls.append((m, p, body)) or {"ok": True})

    client.list_session_instances()
    client.delete_session_instance("s1", "main")
    client.bulk_delete_session_instances("empty")
    client.restart_session_instance("s1", "main")
    client.list_edictum_types()
    client.create_edictum_config({"name": "a", "edictum_type": "simple"})
    client.set_edictum_enabled("a", False)
    client.preview_edictum_runtime("a")
    client.set_chat_plugin_enabled("p1", True)
    client.set_chat_plugin_capability("p1", "tools", "shell", False)
    client.save_chat_plugin_config("p1", {"k": "v"})
    client.start_backend()

    paths = [p for _, p, _ in calls]
    assert paths == [
        "/api/sessions",
        "/api/sessions/s1?platform_id=main",
        "/api/sessions/bulk-delete",
        "/api/sessions/s1/restart?platform_id=main",
        "/api/config/edictum/types",
        "/api/config/edictum/sessions",
        "/api/config/edictum/sessions/a/disable",
        "/api/edictum/runtime/preview",
        "/api/chat/plugins/p1/enable",
        "/api/chat/plugins/p1/capability",
        "/api/chat/plugins/p1/config",
        "/start",
    ]
    assert calls[2][2] == {"mode": "empty"}
    assert calls[9][2] == {"kind": "tools", "cap": "shell", "enabled": False}

def test_control_and_chat_factories():
    """控制/聊天服务工厂使用各自端口与探测路径"""
    control = common.control_client_from_args(Namespace())
    chat = common.chat_client_from_args(Namespace())
    assert control.daemon.port == client_mod.DEFAULT_CONTROL_PORT
    assert control.health_path == "/status"
    assert chat.daemon.port == client_mod.DEFAULT_CHAT_PORT
    assert chat.health_path == "/api/chat/health"

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

def test_offline_flags():
    """offline / force_offline 标志读取"""
    assert common.offline_requested(Namespace(offline=True)) is True
    assert common.offline_requested(Namespace()) is False
    assert common.force_offline(Namespace(force_offline=True)) is True
    assert common.force_offline(Namespace()) is False

def test_ensure_offline_allowed_rejects_when_backend_alive(monkeypatch: pytest.MonkeyPatch):
    """后端在线且未强制时抛 CliError (由分发层渲染为退出码 1)"""
    monkeypatch.setattr(common, "daemon_client_from_args", _alive_client_factory)
    with pytest.raises(output.CliError) as exc:
        common.ensure_offline_allowed(Namespace())
    assert exc.value.exit_code == output.EXIT_ERROR

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

class _FakeClient:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive

def test_parser_accepts_mode_flags_after_subcommands():
    """写命令应支持在子命令后使用离线 flags"""
    parser = _build_parser()

    args = parser.parse_args(["session", "register", "demo", "--class-path", "x.Y", "--offline"])
    assert args.command == "session"
    assert args.action == "register"
    assert args.offline is True

    args = parser.parse_args(["platform", "add", "mk", "--type", "misskey", "--force-offline"])
    assert args.command == "platform"
    assert args.force_offline is True

def test_parser_accepts_api_flags_for_control_commands():
    """控制命令应支持命令后的 API 地址覆盖"""
    parser = _build_parser()
    args = parser.parse_args(["status", "--api-host", "127.0.0.2", "--api-port", "19871"])

    assert args.command == "status"
    assert args.api_host == "127.0.0.2"
    assert args.api_port == 19871

def test_parser_run_accepts_api_flags():
    """run 命令应与其他命令一致地接受 API 地址覆盖"""
    parser = _build_parser()
    args = parser.parse_args(["run", "--api-host", "127.0.0.3", "--api-port", "19880"])
    assert args.command == "run"
    assert args.api_host == "127.0.0.3"
    assert args.api_port == 19880

def test_parser_new_command_groups():
    """plugin / edictum / session instance / start / config validate 解析"""
    parser = _build_parser()

    args = parser.parse_args(["start"])
    assert args.command == "start"

    args = parser.parse_args(["config", "validate"])
    assert args.action == "validate"

    args = parser.parse_args(["plugin", "capability", "demo", "tools", "shell", "off"])
    assert args.command == "plugin"
    assert (args.kind, args.cap, args.state) == ("tools", "shell", "off")

    args = parser.parse_args(["edictum", "create", "assistant", "--type", "simple", "--plugin", "base_take"])
    assert args.command == "edictum"
    assert args.plugin == ["base_take"]

    args = parser.parse_args(["session", "instance", "bulk-delete", "--mode", "empty"])
    assert args.action == "instance"
    assert args.instance_action == "bulk-delete"
    assert args.mode == "empty"

    args = parser.parse_args(["session", "instance", "restart", "s1", "--platform-id", "main"])
    assert args.instance_action == "restart"
    assert args.platform_id == "main"

def test_platform_parser_accepts_session_type_binding():
    """平台命令应支持显式选择入站消息使用的会话类"""
    parser = _build_parser()
    args = parser.parse_args(
        ["platform", "add", "mk", "--type", "misskey", "--session-type", "assistant"]
    )

    assert args.session_type == "assistant"

def test_global_flags_survive_subparser_defaults():
    """全局 flags 写在子命令前也不能被子命令默认值覆盖"""
    parser = _build_parser()

    args = parser.parse_args(["--offline", "session", "register", "demo", "--class-path", "x.Y"])
    assert args.offline is True

    args = parser.parse_args(["--api-host", "127.0.0.9", "status"])
    assert args.api_host == "127.0.0.9"

    args = parser.parse_args(["--config", "demo.yaml", "session", "list"])
    assert args.config == "demo.yaml"

def test_daemon_client_new_routes_call_expected_paths(monkeypatch: pytest.MonkeyPatch):
    """DaemonClient 新增方法应请求对应 HTTP 路由"""
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(method: str, path: str, body: dict[str, Any] | None = None):
        calls.append((method, path, body))
        return {"ok": True}

    client = DaemonClient()
    monkeypatch.setattr(client, "_request", fake_request)

    client.register_session_class("demo", "mod.Demo")
    client.unregister_session_class("demo")
    client.set_model("llm", "default", {"model": "x"})
    client.update_model("llm", "default", {"temperature": 0.2})
    client.remove_model("llm", "default")

    assert calls == [
        ("POST", "/api/config/session-classes", {"name": "demo", "class_path": "mod.Demo", "description": "", "context_key": "", "model_key": ""}),
        ("DELETE", "/api/config/session-classes/demo", None),
        ("POST", "/api/config/models/llm/default", {"model": "x"}),
        ("PATCH", "/api/config/models/llm/default", {"temperature": 0.2}),
        ("DELETE", "/api/config/models/llm/default", None),
    ]

@pytest.mark.asyncio
async def test_http_session_class_write_routes(tmp_path: Path):
    """后端 HTTP API 应支持 session class 注册和删除"""
    backend = BackendManager()
    backend._session_cls_cfg = SessionClassConfigManager(storage_path=tmp_path / "sessions.json")
    server = BackendHTTPServer(backend)
    payload = b'{"name":"dummy","class_path":"satrap.core.framework.Base.Session"}'

    status, data = await server._route("POST", "/api/config/session-classes", payload)
    assert status == 200
    assert data == {"ok": True}
    assert backend.session_class_mgr is not None
    assert backend.session_class_mgr.has_config("dummy")

    status, data = await server._route("DELETE", "/api/config/session-classes/dummy", b"")
    assert status == 200
    assert data == {"ok": True}

@pytest.mark.asyncio
async def test_http_edictum_cold_config_routes(tmp_path: Path):
    """后端 HTTP API 应暴露 Edictum 类型与命名冷配置完整 CRUD"""
    backend = BackendManager()
    backend._edictum_types = create_default_edictum_type_registry()
    backend._edictum_cfg = EdictumConfigManager(
        backend._edictum_types,
        tmp_path / "edictum.json",
    )
    server = BackendHTTPServer(backend)

    status, data = await server._route("GET", "/api/config/edictum/types", b"")
    assert status == 200
    assert {item["name"] for item in data["types"]} == {"simple", "async_simple"}

    status, data = await server._route(
        "POST",
        "/api/config/edictum/sessions",
        b'{"name":"assistant","edictum_type":"simple","model_name":"default"}',
    )
    assert status == 200
    assert data["config"]["provider"] == "edictum"

    status, data = await server._route(
        "PUT",
        "/api/config/edictum/sessions/assistant",
        b'{"name":"renamed","description":"updated"}',
    )
    assert status == 200
    assert data["name"] == "renamed"

    status, data = await server._route(
        "POST",
        "/api/config/edictum/sessions/renamed/disable",
        b"",
    )
    assert status == 200
    assert data["config"]["enabled"] is False

    status, data = await server._route(
        "DELETE",
        "/api/config/edictum/sessions/renamed",
        b"",
    )
    assert status == 200
    assert data == {"ok": True}

@pytest.mark.asyncio
async def test_http_session_config_and_runtime_routes(tmp_path: Path):
    """React 会话页依赖的配置编辑和实例创建路由应完整工作"""
    backend = BackendManager()
    backend._session_cls_cfg = SessionClassConfigManager(storage_path=tmp_path / "sessions.json")
    session_manager = SessionManager(db_path=tmp_path / "session-config.db")
    session_manager.class_cfg_mgr = backend._session_cls_cfg
    user_manager = UserManager(session_manager, db_path=tmp_path / "users.db")
    session_manager.user_manager = user_manager
    backend._session_mgr = session_manager
    backend._user_mgr = user_manager
    backend._platform_runtimes = {"main": (session_manager, user_manager)}
    backend._session_cls_cfg.register_by_class_path("dummy", "satrap.core.framework.Base.Session")
    server = BackendHTTPServer(backend)

    status, data = await server._route(
        "PUT",
        "/api/config/session-classes/dummy",
        b'{"name":"renamed","description":"demo","context_key":"room_id","model_key":"model_name","params":{"model_name":"default"}}',
    )
    assert status == 200
    assert data["config"]["description"] == "demo"
    assert data["config"]["context_key"] == "room_id"
    session_class_mgr = backend.session_class_mgr
    assert session_class_mgr is not None
    assert session_class_mgr.has_config("dummy") is False
    assert session_class_mgr.has_config("renamed") is True

    status, data = await server._route(
        "POST",
        "/api/config/session-classes/renamed/disable",
        b"",
    )
    assert status == 200
    assert data["config"]["enabled"] is False

    status, data = await server._route(
        "POST",
        "/api/config/session-classes/renamed/enable",
        b"",
    )
    assert status == 200
    assert data["config"]["enabled"] is True

    status, data = await server._route(
        "POST",
        "/api/sessions",
        b'{"class_name":"renamed","session_id":"runtime-1","adapter_id":"main","llm_name":"default"}',
    )
    assert status == 200
    assert data["session"]["session_id"] == "runtime-1"
    assert data["session"]["session_config"]["adapter_id"] == "main"

    status, data = await server._route("GET", "/api/sessions", b"")
    assert status == 200
    assert data["sessions"][0]["session_id"] == "runtime-1"
    assert data["sessions"][0]["active"] is False

    chat_manager = SessionManager(db_path=tmp_path / "chat-platform.db", platform_id="chat")
    chat_manager.class_cfg_mgr = backend._session_cls_cfg
    chat_user_manager = UserManager(chat_manager, db_path=tmp_path / "chat-users.db")
    chat_manager.user_manager = chat_user_manager
    chat_manager.register_session_from_provider_config(
        "session_class",
        "renamed",
        session_id="chat-history",
    )
    backend._platform_runtimes["chat"] = (chat_manager, chat_user_manager)

    status, data = await server._route("GET", "/api/sessions", b"")
    assert status == 200
    assert [item["session_id"] for item in data["sessions"]] == ["runtime-1"]

    session_manager.store.update_runtime_fields("runtime-1", 1.0, 2)
    empty = session_manager.register_session_from_provider_config(
        "session_class",
        "renamed",
        session_id="runtime-empty",
    )
    single = session_manager.register_session_from_provider_config(
        "session_class",
        "renamed",
        session_id="runtime-single",
    )
    session_manager.store.update_runtime_fields(single.session_id or "", 1.0, 1)
    user_manager.get_or_create_user("user-1", "onebot")
    user_manager.bind_session("user-1", empty.session_id or "")
    user_manager.store.upsert_context_session(
        "user-1",
        "onebot",
        "renamed",
        empty.session_id or "",
    )

    status, data = await server._route(
        "POST",
        "/api/sessions/bulk-delete",
        b'{"mode":"empty"}',
    )
    assert status == 200
    assert data["deleted_ids"] == ["runtime-empty"]
    assert user_manager.get_user_session_ids("user-1") == []
    assert user_manager.store.get_context_session("user-1", "onebot", "renamed") is None

    status, data = await server._route(
        "POST",
        "/api/sessions/bulk-delete",
        b'{"mode":"single"}',
    )
    assert status == 200
    assert data["deleted_ids"] == ["runtime-single"]

    selected = session_manager.register_session_from_provider_config(
        "session_class",
        "renamed",
        session_id="runtime-selected",
    )
    status, data = await server._route(
        "POST",
        "/api/sessions/bulk-delete",
        json.dumps({
            "mode": "selected",
            "session_refs": [{"platform_id": "main", "session_id": selected.session_id}],
        }).encode(),
    )
    assert status == 200
    assert data["deleted_ids"] == ["runtime-selected"]

    status, data = await server._route(
        "DELETE",
        "/api/sessions/runtime-1?platform_id=main",
        b"",
    )
    assert status == 200
    assert data["deleted_ids"] == ["runtime-1"]

@pytest.mark.asyncio
async def test_http_session_discovery_directory_is_limited_to_config(tmp_path: Path):
    """Session 目录 API 只能操作配置声明的扫描目录"""
    configured_path = tmp_path / "sessions"
    backend = BackendManager()
    backend.config.session_scan_paths = [str(configured_path)]
    server = BackendHTTPServer(backend)

    status, data = await server._route(
        "POST",
        "/api/session/discovery/directories",
        json.dumps({"path": str(configured_path)}).encode(),
    )
    assert status == 200
    assert Path(data["path"]).exists()

    status, data = await server._route(
        "POST",
        "/api/session/discovery/directories",
        b'{"path":"outside"}',
    )
    assert status == 400
    assert "只能创建" in data["error"]

@pytest.mark.asyncio
async def test_http_model_write_routes(tmp_path: Path):
    """后端 HTTP API 应支持模型配置写接口"""
    backend = BackendManager()
    backend._model_cfg = ModelConfigManager(storage_path=tmp_path / "models.json")
    server = BackendHTTPServer(backend)

    status, data = await server._route("POST", "/api/config/models/llm/test", b'{"model":"gpt-test"}')
    assert status == 200
    assert data == {"ok": True}
    assert backend.model_config_manager is not None
    assert backend.model_config_manager.get_llm_config("test").model == "gpt-test"

    status, data = await server._route("PATCH", "/api/config/models/llm/test", b'{"temperature":0.2}')
    assert status == 200
    assert data == {"ok": True}
    assert backend.model_config_manager.get_llm_config("test").temperature == 0.2

    status, data = await server._route("DELETE", "/api/config/models/llm/test", b"")
    assert status == 200
    assert data == {"ok": True}

def test_platform_parser_accepts_wake_command():
    """平台 wake 子命令提供群/成员必填项与互斥可选正文"""
    parser = _build_parser()
    args = parser.parse_args(["platform", "wake", "ob", "--group", "20000", "--user", "30000", "--prompt", "处理"])
    assert (args.action, args.id, args.group, args.user, args.prompt) == ("wake", "ob", "20000", "30000", "处理")
    assert args.message_id == "" and args.request_id == ""

def test_platform_wake_command_posts_payload_and_reports_status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """wake 命令只发送允许字段, 拒绝转为 CliError, 不从参数读取操作者"""
    from argparse import Namespace
    from satrap.cli import cmd_platform
    from satrap.cli.output import CliError

    sent: list[dict[str, object]] = []

    class _Client:
        def require_alive(self) -> None:
            pass

        def wake_platform(self, payload: dict[str, object]) -> dict[str, object]:
            sent.append(payload)
            return {"status": "accepted", "request_id": payload["request_id"]}

    def _make_client(args: Namespace, timeout: float = 2) -> _Client:
        return _Client()

    monkeypatch.setattr(cmd_platform, "daemon_client_from_args", _make_client)
    ns = Namespace(id="ob", group="20000", user="30000", prompt="处理", message_id="", reason="", request_id="r1")
    cmd_platform.cmd_platform_wake(ns)
    assert sent == [{"adapter_id": "ob", "group_id": "20000", "user_id": "30000", "request_id": "r1", "prompt": "处理"}]
    human = capsys.readouterr().out
    assert "已接受手动唤醒" in human and '"status"' not in human
    from satrap.cli.output import set_json_mode
    set_json_mode(True)
    try:
        cmd_platform.cmd_platform_wake(ns)
        machine = capsys.readouterr().out
        assert '"status": "accepted"' in machine and "已接受" not in machine
    finally:
        set_json_mode(False)

    ns.request_id = ""
    cmd_platform.cmd_platform_wake(ns)
    assert sent[2]["request_id"] != sent[0]["request_id"]

    with pytest.raises(CliError, match="只能指定一个"):
        cmd_platform.cmd_platform_wake(Namespace(id="ob", group="1", user="2", prompt="a", message_id="3", reason="", request_id=""))

    class _Rejecting(_Client):
        def wake_platform(self, payload: dict[str, object]) -> dict[str, object]:
            from satrap.cli.client import DaemonError
            raise DaemonError("HTTP 409: Conflict")

    def _make_rejecting(args: Namespace, timeout: float = 2) -> _Rejecting:
        return _Rejecting()

    monkeypatch.setattr(cmd_platform, "daemon_client_from_args", _make_rejecting)
    with pytest.raises(CliError, match="被拒绝"):
        cmd_platform.cmd_platform_wake(Namespace(id="ob", group="1", user="2", prompt="", message_id="", reason="", request_id=""))

def test_model_parser_accepts_asr_type():
    """模型命令的四类 choices 均包含 asr"""
    parser = _build_parser()
    for argv in (["model", "list", "asr"], ["model", "show", "asr", "speech"], ["model", "set", "asr", "speech", "--set", "model=whisper-1"], ["model", "remove", "asr", "speech"]):
        assert parser.parse_args(argv).type == "asr"

def test_platform_remove_wraps_save_errors_as_cli_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """删除平台时锁超时/权限错误转为 CliError, 与 upsert 一致"""
    from argparse import Namespace
    from satrap.cli import cmd_platform
    from satrap.cli.output import CliError
    path = tmp_path / "config.json"
    path.write_text('{"platforms": [{"id": "ob", "type": "onebot", "settings": {}}]}', encoding="utf-8")
    monkeypatch.setattr(cmd_platform, "find_config_path", lambda: path)
    def _no_warn(args: object) -> None:
        return None

    monkeypatch.setattr(cmd_platform, "_warn_if_backend_running", _no_warn)

    def broken_save(*args: object, **kwargs: object) -> None:
        raise PermissionError("locked by another process")

    monkeypatch.setattr(cmd_platform, "save_config_document", broken_save)
    with pytest.raises(CliError, match="删除失败"):
        cmd_platform.cmd_platform_remove(Namespace(id="ob"))

@pytest.fixture()
def _reset_json_mode():
    """每个测试后恢复人类可读模式"""
    yield
    output.set_json_mode(False)

@pytest.mark.usefixtures('_reset_json_mode')
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

@pytest.mark.usefixtures('_reset_json_mode')
def test_json_mode_error_is_structured(capsys: pytest.CaptureFixture[str]):
    """JSON 模式下错误输出为结构化对象"""
    output.set_json_mode(True)
    output.error("boom", hint="try this")
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "error": "boom", "hint": "try this"}

@pytest.mark.usefixtures('_reset_json_mode')
def test_json_mode_render_data(capsys: pytest.CaptureFixture[str]):
    """JSON 模式下 render_data 输出数据而非人类文本"""
    output.set_json_mode(True)
    output.render_data({"a": 1}, lambda: print("human"))
    out = capsys.readouterr().out
    assert '"a": 1' in out
    assert "human" not in out

@pytest.mark.usefixtures('_reset_json_mode')
def test_dispatch_unknown_action_exits_2(capsys: pytest.CaptureFixture[str]):
    """未知操作退出码 2 且提示统一"""
    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({"list": lambda args: None}, Namespace(action="nope"))
    assert exc.value.code == 2
    assert "未知操作" in capsys.readouterr().out

@pytest.mark.usefixtures('_reset_json_mode')
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

@pytest.mark.usefixtures('_reset_json_mode')
def test_dispatch_value_error_exits_1(capsys: pytest.CaptureFixture[str]):
    """ValueError 统一按业务错误退出码 1"""

    def _handler(args: Namespace):
        raise ValueError("invalid")

    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({"x": _handler}, Namespace(action="x"))
    assert exc.value.code == 1
    assert "错误: invalid" in capsys.readouterr().out

@pytest.mark.usefixtures('_reset_json_mode')
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

@pytest.mark.usefixtures('_reset_json_mode')
def test_dispatch_daemon_error_exits_1(capsys: pytest.CaptureFixture[str]):
    """服务端错误响应退出码 1"""

    def _handler(args: Namespace):
        raise DaemonError("服务端错误")

    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({"x": _handler}, Namespace(action="x"))
    assert exc.value.code == 1
    assert "错误: 服务端错误" in capsys.readouterr().out

@pytest.mark.usefixtures('_reset_json_mode')
def test_dispatch_key_attribute(capsys: pytest.CaptureFixture[str]):
    """dispatch_action 支持按 command 属性分发 (顶层命令)"""
    seen: list[str] = []
    output.dispatch_action({"status": lambda args: seen.append("ok")}, Namespace(command="status"), key="command")
    assert seen == ["ok"]
    with pytest.raises(SystemExit) as exc:
        output.dispatch_action({}, Namespace(command="nope"), key="command")
    assert exc.value.code == 2

@pytest.mark.usefixtures('_reset_json_mode')
def test_format_table_aligns_columns():
    """表格按列宽对齐且空表显示占位"""
    text = output.format_table([["a", "长列值"], ["bb", "c"]], ["H1", "H2"])
    lines = text.splitlines()
    assert len(lines) == 4
    assert lines[1].count("-+-") == 1
    assert output.format_table([], ["H"]) == "(空)"
