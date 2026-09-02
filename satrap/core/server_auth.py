"""本地管理服务共享鉴权, 来源校验与令牌持久化"""
from __future__ import annotations

import hmac
import hashlib
import ipaddress
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from satrap.core.utils.paths import get_project_root


TOKEN_ENV_NAME = "SATRAP_API_TOKEN"
ALLOWED_ORIGINS_ENV_NAME = "SATRAP_ALLOWED_ORIGINS"
SESSION_COOKIE_NAME = "satrap_session"
DEFAULT_TOKEN_PATH = get_project_root() / ".satrap" / "api-token"
LOOPBACK_BOOTSTRAP_ENV_NAME = "SATRAP_LOOPBACK_BOOTSTRAP"
DEFAULT_BROWSER_SESSION_TTL = 8 * 3600
DEFAULT_MAX_BROWSER_SESSIONS = 1024


class BrowserSessionStore:
    """有容量和有效期限制的进程内浏览器会话存储"""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_BROWSER_SESSION_TTL,
        max_sessions: int = DEFAULT_MAX_BROWSER_SESSIONS,
        now_provider: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        初始化浏览器会话存储

        参数:
        - ttl_seconds: 会话有效期秒数
        - max_sessions: 最大活跃会话数量
        - now_provider: 单调时间提供函数
        """
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_sessions = max(1, int(max_sessions))
        self._now = now_provider
        self._sessions: dict[str, float] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _digest(session_id: str) -> str:
        """
        计算会话 ID 摘要, 避免服务端保存原始凭据

        参数:
        - session_id: 原始随机会话 ID

        返回:
        - 会话 ID 的 SHA-256 十六进制摘要
        """
        return hashlib.sha256(session_id.encode()).hexdigest()

    def _prune(self, now: float) -> None:
        """
        清除过期会话并将存储限制在容量上限内

        参数:
        - now: 当前单调时间
        """
        expired = [digest for digest, deadline in self._sessions.items() if deadline <= now]
        for digest in expired:
            self._sessions.pop(digest, None)
        while len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions, key=self._sessions.__getitem__)
            self._sessions.pop(oldest, None)

    def issue(self) -> str:
        """
        创建随机浏览器会话

        返回:
        - 可写入 HttpOnly Cookie 的随机会话 ID
        """
        session_id = secrets.token_urlsafe(32)
        now = self._now()
        with self._lock:
            self._prune(now)
            self._sessions[self._digest(session_id)] = now + self.ttl_seconds
        return session_id

    def validate(self, session_id: str) -> bool:
        """
        校验浏览器会话并清理过期记录

        参数:
        - session_id: Cookie 中的随机会话 ID

        返回:
        - 会话存在且未过期时返回 True
        """
        if not session_id:
            return False
        now = self._now()
        digest = self._digest(session_id)
        with self._lock:
            deadline = self._sessions.get(digest)
            if deadline is None:
                return False
            if deadline <= now:
                self._sessions.pop(digest, None)
                return False
            return True

    def revoke(self, session_id: str) -> bool:
        """
        撤销浏览器会话

        参数:
        - session_id: Cookie 中的随机会话 ID

        返回:
        - 会话原本存在时返回 True
        """
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(self._digest(session_id), None) is not None


def is_loopback_host(host: str) -> bool:
    """
    判断监听地址或客户端地址是否为回环地址

    参数:
    - host: 主机名或 IP 地址

    返回:
    - 地址为 localhost 或回环 IP 时返回 True
    """
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_token(token: str) -> str:
    """
    校验 API 令牌具备足够熵和长度

    参数:
    - token: 待校验的 API 令牌

    返回:
    - 去除首尾空白后的有效令牌
    """
    normalized = token.strip()
    if len(normalized) < 32:
        raise ValueError(f"{TOKEN_ENV_NAME} 长度至少为 32 个字符")
    return normalized


def load_or_create_api_token(token_path: str | Path = DEFAULT_TOKEN_PATH) -> str:
    """
    优先读取环境变量, 否则从受限运行时文件加载或生成令牌

    参数:
    - token_path: 令牌持久化路径, 默认使用项目数据目录

    返回:
    - 已校验的共享 API 令牌
    """
    env_token = os.getenv(TOKEN_ENV_NAME, "").strip()
    if env_token:
        return _validate_token(env_token)

    path = Path(token_path)
    if path.is_file():
        for _ in range(50):
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return _validate_token(value)
            time.sleep(0.01)
        raise RuntimeError(f"API 令牌文件读取超时: {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        for _ in range(50):
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return _validate_token(value)
            time.sleep(0.01)
        raise RuntimeError(f"API 令牌文件初始化超时: {path}")
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as token_file:
        token_file.write(token)
        token_file.flush()
        os.fsync(token_file.fileno())
    return token


def _normalize_origin(origin: str) -> str:
    """
    规范化 HTTP Origin, 拒绝携带路径, 查询或凭据的值

    参数:
    - origin: 原始 HTTP Origin

    返回:
    - 去除尾部斜杠并转换为小写的有效 Origin
    """
    value = origin.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"无效 Origin: {origin}")
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError(f"无效 Origin: {origin}")
    return value.lower()


def default_allowed_origins(host: str, port: int) -> frozenset[str]:
    """
    构造管理界面和本地开发服务器的精确 Origin 白名单

    参数:
    - host: 当前服务监听主机
    - port: 当前服务监听端口

    返回:
    - 规范化后的精确 Origin 白名单
    """
    origins = {
        f"http://127.0.0.1:{value}"
        for value in (19870, 19871, 19872, 4173, 5173)
    }
    origins.update(
        f"http://localhost:{value}"
        for value in (19870, 19871, 19872, 4173, 5173)
    )
    if is_loopback_host(host):
        origins.add(f"http://{host}:{port}")
    configured = os.getenv(ALLOWED_ORIGINS_ENV_NAME, "")
    for item in configured.split(","):
        if item.strip():
            origins.add(_normalize_origin(item))
    return frozenset(_normalize_origin(item) for item in origins)


@dataclass(frozen=True)
class ServerAuth:
    """单个 HTTP/WS 服务使用的共享鉴权策略"""

    token: str
    allowed_origins: frozenset[str]
    loopback_binding: bool
    cookie_name: str
    browser_sessions: BrowserSessionStore
    loopback_bootstrap: bool

    @classmethod
    def create(
        cls,
        host: str,
        port: int,
        *,
        token: str | None = None,
        token_path: str | Path = DEFAULT_TOKEN_PATH,
        allowed_origins: frozenset[str] | None = None,
        session_namespace: str = "api",
        browser_session_ttl: int = DEFAULT_BROWSER_SESSION_TTL,
        max_browser_sessions: int = DEFAULT_MAX_BROWSER_SESSIONS,
    ) -> ServerAuth:
        """
        按监听地址构建策略, 非回环绑定必须显式配置令牌

        参数:
        - host: 监听主机
        - port: 监听端口
        - token: 显式 API token
        - token_path: 自动生成 API token 的持久化路径
        - allowed_origins: 浏览器 Origin 白名单
        - session_namespace: 浏览器 Cookie 服务命名空间
        - browser_session_ttl: 浏览器会话有效期秒数
        - max_browser_sessions: 最大浏览器会话数量

        返回:
        - 完整的服务鉴权策略
        """
        explicit_token = (token or os.getenv(TOKEN_ENV_NAME, "")).strip()
        loopback = is_loopback_host(host)
        if not loopback and not explicit_token:
            raise ValueError(f"绑定非回环地址 {host} 时必须显式设置 {TOKEN_ENV_NAME}")
        resolved_token = _validate_token(explicit_token) if explicit_token else load_or_create_api_token(token_path)
        namespace = re.sub(r"[^a-zA-Z0-9_]", "_", session_namespace.strip().lower()) or "api"
        bootstrap_value = os.getenv(LOOPBACK_BOOTSTRAP_ENV_NAME, "true").strip().lower()
        loopback_bootstrap = bootstrap_value not in {"0", "false", "no", "off"}
        return cls(
            token=resolved_token,
            allowed_origins=allowed_origins or default_allowed_origins(host, port),
            loopback_binding=loopback,
            cookie_name=f"{SESSION_COOKIE_NAME}_{namespace}",
            browser_sessions=BrowserSessionStore(browser_session_ttl, max_browser_sessions),
            loopback_bootstrap=loopback_bootstrap,
        )

    def origin_allowed(self, origin: str | None) -> bool:
        """
        校验请求来源是否允许访问

        参数:
        - origin: 浏览器 Origin; 原生客户端可不提供

        返回:
        - 无 Origin 或来源精确命中白名单时返回 True
        """
        if not origin:
            return True
        try:
            return _normalize_origin(origin) in self.allowed_origins
        except ValueError:
            return False

    def bearer_authorized(self, headers: dict[str, str]) -> bool:
        """
        校验 Authorization Bearer 令牌

        参数:
        - headers: 小写键名的请求头

        返回:
        - Bearer 凭据与共享令牌安全匹配时返回 True
        """
        scheme, _, credential = headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(credential.strip(), self.token)

    def cookie_authorized(self, headers: dict[str, str]) -> bool:
        """
        校验 HttpOnly 会话 Cookie

        参数:
        - headers: 小写键名的请求头

        返回:
        - Cookie 包含有效且未过期的浏览器会话时返回 True
        """
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:
            return False
        morsel = cookie.get(self.cookie_name)
        return morsel is not None and self.browser_sessions.validate(morsel.value)

    def authorized(self, headers: dict[str, str]) -> bool:
        """
        校验 Bearer 或 HttpOnly Cookie 凭据

        参数:
        - headers: 小写键名的请求头

        返回:
        - 任一凭据有效时返回 True
        """
        return self.bearer_authorized(headers) or self.cookie_authorized(headers)

    def cors_headers(self, origin: str | None) -> dict[str, str]:
        """
        为白名单来源构造精确 CORS 响应头

        参数:
        - origin: 浏览器 Origin

        返回:
        - 来源获准时返回 CORS 响应头; 否则返回空字典
        """
        if not origin or not self.origin_allowed(origin):
            return {}
        return {
            "Access-Control-Allow-Origin": _normalize_origin(origin),
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }

    def can_bootstrap(self, peer_host: str, origin: str | None, headers: dict[str, str]) -> bool:
        """
        判断请求能否签发浏览器会话

        参数:
        - peer_host: 客户端地址
        - origin: 浏览器 Origin
        - headers: 请求头

        返回:
        - Bearer 有效或允许回环 bootstrap 时返回 True
        """
        if self.bearer_authorized(headers):
            return True
        return (
            self.loopback_bootstrap
            and self.loopback_binding
            and is_loopback_host(peer_host)
            and bool(origin)
            and self.origin_allowed(origin)
        )

    def session_cookie(self) -> str:
        """
        签发不包含主 API token 的随机浏览器会话 Cookie

        返回:
        - 带 HttpOnly, SameSite 和有效期属性的 Set-Cookie 值
        """
        session_id = self.browser_sessions.issue()
        return (
            f"{self.cookie_name}={session_id}; HttpOnly; SameSite=Strict; Path=/; "
            f"Max-Age={self.browser_sessions.ttl_seconds}"
        )

    def revoke_cookie(self, headers: dict[str, str]) -> bool:
        """
        撤销请求 Cookie 中的浏览器会话

        参数:
        - headers: 请求头

        返回:
        - 找到并撤销活跃会话时返回 True
        """
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:
            return False
        morsel = cookie.get(self.cookie_name)
        return morsel is not None and self.browser_sessions.revoke(morsel.value)

    def expired_session_cookie(self) -> str:
        """
        生成清除当前服务浏览器会话的过期 Cookie

        返回:
        - Max-Age 为零的 Set-Cookie 值
        """
        return f"{self.cookie_name}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
