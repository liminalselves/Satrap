"""会话实例路由的只读查询, 接口一致性和批量查询约束"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import asyncio
import hashlib
import json
import sqlite3
import threading

import pytest

from satrap.core.config import session_routes
from satrap.core.config.session_routes import read_session_routes
from satrap.core.conversation import ConversationRoute
from satrap.core.framework.SessionManager import SessionConfigStore
from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.platform import PlatformAdapter
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.storage import StorageLayout


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """使用真实路由表和索引, 不接入平台网络"""
    path = StorageLayout(tmp_path / "data").platform_db("qq")
    UserInfoStore(path)
    SessionConfigStore(path)
    return path


def _key(scope: str = "group", *, group: str = "123456", user: str = "", account: str = "bot", version: int = 1) -> str:
    parts: list[object] = ["qq", "edictum", "agent", scope, account, group, user]
    if version == 2:
        parts.extend([0, "private" if scope == "private" else "group", user if scope == "private" else group, 1])
    return f"scoped:v{version}:" + json.dumps(parts, separators=(",", ":"))


def _reference(database: Path, session: str, key: str, *, current: bool = True, used: float = 10, created: float = 1, user: str = "") -> None:
    """写入测试引用, 历史记录可独立于当前路由存在"""
    with closing(sqlite3.connect(database)) as connection, connection:
        if current:
            connection.execute("INSERT INTO context_sessions VALUES(?,?,?,?,?,?,?,?)",
                               (key, user, "qq", "edictum", "agent", session, created, used))
        else:
            connection.execute("INSERT INTO context_session_history VALUES(?,?,?,?)", (key, session, created, used))


def test_session_routes_v1_group_and_v2_private(database: Path) -> None:
    _reference(database, "group", _key())
    private = ConversationRoute(platform="qq", provider="edictum", session_type="agent", scope="private",
                                self_id="bot", user_id="654321", conversation_kind="private", conversation_id="654321", binding_generation=1)
    assert private.key is not None
    _reference(database, "private", private.key)
    result = read_session_routes(database, {"type": "onebot"}, ["group", "private", "unbound"])
    assert result["group"] == {
        "scope": "group", "self_id": "bot", "group_id": "123456", "user_id": "",
        "conversation_kind": "", "conversation_id": "", "target_label": None, "kind_label": None, "current": True,
    }
    route = result["private"]
    assert route is not None
    assert (route["scope"], route["user_id"], route["self_id"], route["conversation_kind"], route["conversation_id"], route["kind_label"]) == (
        "private", "654321", "bot", "private", "654321", "私聊")
    assert result["unbound"] is None


def test_session_routes_current_precedence_and_latest_history(database: Path) -> None:
    _reference(database, "current", _key(group="1"), used=3)
    _reference(database, "current", _key(group="2"), used=4)
    _reference(database, "current", _key(group="9"), current=False, used=100)
    _reference(database, "history", _key(group="3"), current=False, used=3)
    _reference(database, "history", _key(group="4"), current=False, used=4)
    _reference(database, "fallback", _key(group="5"), current=False, used=0, created=5)
    _reference(database, "fallback", _key(group="6"), current=False, used=0, created=6)
    _reference(database, "tie", _key(group="7"), used=8, created=7)
    _reference(database, "tie", _key(group="8"), used=8, created=7)
    result = read_session_routes(database, None, ["current", "history", "fallback", "tie"])
    assert [(result[name]["group_id"], result[name]["current"]) for name in ("current", "history", "fallback")] == [
        ("2", True), ("4", False), ("6", False)]
    assert result["tie"]["group_id"] == "8"


def test_session_routes_legacy_and_history_without_current_table(database: Path) -> None:
    _reference(database, "legacy", "agent:qq:member", user="member")
    assert read_session_routes(database, None, ["legacy"])["legacy"]["user_id"] == "member"
    assert read_session_routes(database, None, ["legacy"])["legacy"]["scope"] == "legacy_user"
    _reference(database, "legacy-history", "agent:qq:member", current=False)
    old = read_session_routes(database, None, ["legacy-history"])["legacy-history"]
    assert old is not None and old["user_id"] == "member" and old["current"] is False
    _reference(database, "orphan", _key(group="42"), current=False)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE context_sessions")
    route = read_session_routes(database, None, ["orphan"])["orphan"]
    assert route is not None and route["group_id"] == "42" and route["current"] is False


@pytest.mark.parametrize("key", ['scoped:v1:{bad', 'scoped:v2:[1]', 'scoped:v9:[]'])
def test_session_routes_corrupt_keys_do_not_fail_list(database: Path, key: str, caplog: pytest.LogCaptureFixture) -> None:
    _reference(database, "broken", key)
    _reference(database, "valid", _key())
    result = read_session_routes(database, None, ["broken", "valid"])
    assert result["broken"]["scope"] == "unknown"
    assert result["valid"]["scope"] == "group"
    assert "路由" in caplog.text


@pytest.mark.parametrize("exists", [False, True])
def test_session_routes_missing_storage_does_not_create_tables(tmp_path: Path, exists: bool) -> None:
    database = tmp_path / "missing" / "platform.db"
    if exists:
        database.parent.mkdir()
        sqlite3.connect(database).close()
        before = database.read_bytes()
    assert read_session_routes(database, None, ["unbound"]) == {"unbound": None}
    if exists:
        assert database.read_bytes() == before
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    else:
        assert not database.parent.exists()


def test_session_routes_adapter_failure_is_isolated(database: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class Adapter(PlatformAdapter):
        @classmethod
        def conversation_catalog_metadata(cls, connection: sqlite3.Connection, route: dict[str, str]) -> dict[str, str]:
            if route["session_id"] == "bad":
                raise ValueError("标签读取异常")
            return {"target": "有效标签"}

    monkeypatch.setattr(session_routes.registry, "get", lambda _: Adapter)
    _reference(database, "bad", _key(group="1"))
    _reference(database, "good", _key(group="2"))
    result = read_session_routes(database, {"type": "custom"}, ["bad", "good"])
    assert result["bad"]["target_label"] is None
    assert result["good"]["target_label"] == "有效标签"
    assert "[会话实例]" in caplog.text and "标签读取异常" in caplog.text


def test_session_routes_batch_queries_are_bounded_and_readonly(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    count = 1201
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE group_directory(self_id TEXT,group_id TEXT,group_name TEXT,PRIMARY KEY(self_id,group_id))")
        connection.executemany("INSERT INTO context_sessions VALUES(?,?,?,?,?,?,?,?)",
                               ((_key(group=str(i)), "", "qq", "edictum", "agent", f"s{i}", 1, 2) for i in range(count)))
        connection.executemany("INSERT INTO context_session_history VALUES(?,?,?,?)", ((_key(group=str(i)), f"h{i}", 1, 2) for i in range(count)))
        connection.executemany("INSERT INTO group_directory VALUES(?,?,?)", (("bot", str(i), f"群{i}") for i in range(count)))
        connection.execute("INSERT INTO group_directory VALUES('other','1','其它账号的群')")
        connection.execute("UPDATE group_directory SET group_name=NULL WHERE self_id='bot' AND group_id='2'")
    before = hashlib.sha256(database.read_bytes()).digest()
    statements: list[tuple[str, int]] = []
    connections: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    class TracedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            statements.append((sql, len(parameters)))
            assert len(parameters) <= 500
            return super().execute(sql, parameters)

        def close(self) -> None:
            closed.append(self)
            super().close()

    def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        assert kwargs.get("uri") and "mode=ro" in args[0]
        connection = connect(*args, **kwargs, factory=TracedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(session_routes.sqlite3, "connect", traced)
    result = read_session_routes(database, {"type": "onebot"}, [f"{prefix}{i}" for prefix in ("s", "h") for i in range(count)])
    assert len(result) == count * 2
    assert result["s1"]["target_label"] == "群1"
    assert result["s2"]["target_label"] is None
    assert result["s1200"]["target_label"] == "群1200"
    assert result["h1200"]["target_label"] == "群1200" and result["h1200"]["current"] is False
    assert len(connections) == len(closed) == 1
    assert len(statements) <= 17
    assert [size for sql, size in statements if sql.startswith("SELECT * FROM context_sessions")] == [500, 500, 500, 500, 402]
    assert [size for sql, size in statements if "FROM context_session_history h" in sql] == [500, 500, 201]
    assert hashlib.sha256(database.read_bytes()).digest() == before


@pytest.mark.asyncio
async def test_session_routes_hot_and_cold_list_contracts(database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from satrap.core.backend import control_server, http_api
    from satrap.core.backend.BackendManager import BackendConfig, BackendManager
    from satrap.core.config import session_instance_service
    from satrap.core.type import SessionConfig

    store = SessionConfigStore(database)
    for identity in ("current", "history", "unbound", "broken"):
        store.upsert(SessionConfig(session_id=identity, session_type_name="agent", created_at=1, last_used_at=2, message_count=0, session_config={}))
    _reference(database, "current", _key())
    _reference(database, "history", _key(group="old"), current=False)
    _reference(database, "broken", "scoped:v1:invalid")
    platform = {"id": "qq", "type": "onebot"}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path / "data"), platforms=[platform], session_scan_paths=[]))
    manager = SimpleNamespace(store=store, list_sessions=lambda: [], list_session_configs=store.list,
                              get_session_runtime_metadata=lambda _: {})
    user = SimpleNamespace(store=UserInfoStore(database, readonly=True))
    monkeypatch.setattr(backend, "list_platform_runtimes", lambda: {"qq": (manager, user)})
    config = tmp_path / "config.yaml"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "platforms": [platform], "session_scan_paths": [],
                                  "session_class_config_path": str(tmp_path / "sessions.json"), "edictum_config_path": str(tmp_path / "edictum.json")}), encoding="utf-8")
    monkeypatch.setattr(control_server, "CONFIG_PATH", config)
    threads: list[int] = []

    def observed(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return read_session_routes(*args, **kwargs)

    monkeypatch.setattr(http_api, "read_session_routes", observed)
    monkeypatch.setattr(session_instance_service, "read_session_routes", observed)
    before = database.read_bytes()
    status, hot = await http_api.BackendHTTPServer(backend)._route("GET", "/api/sessions", b"")
    assert status == 200
    path = "/config/session-instances"
    cold_response = await control_server._route_session_instances(control_server._RouteContext("GET", path, path, asyncio.StreamReader(), b""))
    assert cold_response is not None and cold_response[0] == 200
    hot_routes = {item["session_id"]: item["route"] for item in hot["sessions"]}
    cold_routes = {item["session_id"]: item["route"] for item in cold_response[1]["sessions"]}
    assert hot_routes == cold_routes
    assert hot_routes["current"]["current"] is True
    assert hot_routes["history"]["current"] is False
    assert hot_routes["unbound"] is None
    assert hot_routes["broken"]["scope"] == "unknown"
    assert threads and all(identity != threading.get_ident() for identity in threads)
    assert database.read_bytes() == before


def test_session_routes_readonly_instance_store_preserves_old_schema(tmp_path: Path) -> None:
    database = tmp_path / "old.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE session_configs(session_id TEXT,session_type_name TEXT,created_at REAL,last_used_at REAL,message_count INTEGER,session_config TEXT)")
        connection.execute("INSERT INTO session_configs VALUES('old','agent',1,2,0,'{}')")
    before = database.read_bytes()
    store = SessionConfigStore(database, readonly=True)
    users = UserInfoStore(database, readonly=True)
    assert store.list()[0].session_id == "old"
    assert read_session_routes(users.db_path, None, ["old"]) == {"old": None}
    assert database.read_bytes() == before
    missing = tmp_path / "new" / "platform.db"
    assert SessionConfigStore(missing, readonly=True).list() == []
    assert not missing.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_session_routes_cold_get_never_initializes_missing_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool) -> None:
    from satrap.core.backend import control_server

    layout = StorageLayout(tmp_path / "data")
    database = layout.platform_db("local")
    before: bytes | None = None
    if existing:
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE session_configs(session_id TEXT,session_type_name TEXT,created_at REAL,last_used_at REAL,message_count INTEGER,session_config TEXT)")
            connection.execute("INSERT INTO session_configs VALUES('old','agent',1,2,0,'{}')")
        before = database.read_bytes()
    config = tmp_path / "config.yaml"
    config.write_text(json.dumps({"data_root": str(layout.root), "platforms": None, "session_scan_paths": [],
                                  "session_class_config_path": str(tmp_path / "sessions.json"), "edictum_config_path": str(tmp_path / "edictum.json")}), encoding="utf-8")
    monkeypatch.setattr(control_server, "CONFIG_PATH", config)
    path = "/config/session-instances"
    response = await control_server._route_session_instances(control_server._RouteContext("GET", path, path, asyncio.StreamReader(), b""))
    assert response is not None and response[0] == 200
    if existing:
        assert response[1]["sessions"][0]["route"] is None
        assert database.read_bytes() == before
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("session_configs",)]
    else:
        assert response[1]["sessions"] == []
        assert not database.parent.exists()
