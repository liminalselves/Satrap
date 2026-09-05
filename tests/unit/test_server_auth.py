"""HTTP 与 WebSocket 共享鉴权策略测试"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from pathlib import Path
import pytest

from satrap.core.server_auth import BrowserSessionStore, ServerAuth, load_or_create_api_token


def test_token_is_generated_once_and_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    未配置环境令牌时生成高熵令牌并稳定复用

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.delenv("SATRAP_API_TOKEN", raising=False)
    token_path = tmp_path / "api-token"
    first = load_or_create_api_token(token_path)
    second = load_or_create_api_token(token_path)
    assert first == second
    assert len(first) >= 32
    assert token_path.read_text(encoding="utf-8") == first


def test_concurrent_token_creation_returns_same_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    并发启动多个服务时只能有一个进程创建共享令牌

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.delenv("SATRAP_API_TOKEN", raising=False)
    token_path = tmp_path / "api-token"

    def load_token(_: int) -> str:
        """
        读取或创建共享测试令牌

        参数:
        - _: 未使用的并发任务序号

        返回:
        - 共享 API 令牌
        """
        return load_or_create_api_token(token_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(load_token, range(16)))
    assert len(set(tokens)) == 1


def test_non_loopback_binding_requires_explicit_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    非回环监听不能静默使用自动生成令牌

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.delenv("SATRAP_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="非回环地址"):
        ServerAuth.create("0.0.0.0", 19870, token_path=tmp_path / "api-token")


def test_bearer_random_cookie_and_exact_origin_validation() -> None:
    """Bearer, Cookie 和精确 Origin 白名单按预期校验"""
    token = "secure-test-token-that-is-longer-than-thirty-two"
    auth = ServerAuth.create("127.0.0.1", 19870, token=token)
    assert auth.authorized({"authorization": f"Bearer {token}"})
    cookie_header = auth.session_cookie()
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    session_id = cookie[auth.cookie_name].value
    assert session_id != token
    assert token not in cookie_header
    assert auth.authorized({"cookie": f"{auth.cookie_name}={session_id}"})
    assert not auth.authorized({"authorization": "Bearer wrong"})
    assert auth.origin_allowed("http://localhost:5173")
    assert not auth.origin_allowed("https://evil.example")
    assert auth.cors_headers("http://localhost:5173")["Access-Control-Allow-Origin"] == "http://localhost:5173"


def test_browser_session_expiry_capacity_and_revocation() -> None:
    """浏览器会话支持过期, 容量淘汰和显式撤销"""
    clock = {"now": 10.0}
    store = BrowserSessionStore(
        ttl_seconds=5,
        max_sessions=2,
        now_provider=lambda: clock["now"],
    )
    first = store.issue()
    clock["now"] = 11.0
    second = store.issue()
    assert store.validate(first) and store.validate(second)

    clock["now"] = 12.0
    third = store.issue()
    assert not store.validate(first)
    assert store.validate(second) and store.validate(third)
    assert store.revoke(second) is True
    assert store.validate(second) is False

    clock["now"] = 18.0
    assert store.validate(third) is False


def test_strict_mode_disables_loopback_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    严格模式下回环请求也必须提供 Bearer token

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    monkeypatch.setenv("SATRAP_LOOPBACK_BOOTSTRAP", "false")
    auth = ServerAuth.create(
        "127.0.0.1",
        19870,
        token="secure-test-token-that-is-longer-than-thirty-two",
    )
    assert not auth.can_bootstrap("127.0.0.1", "http://localhost:5173", {})
