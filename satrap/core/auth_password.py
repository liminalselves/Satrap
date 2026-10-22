"""前端访问密码的 PBKDF2 存储, 明文只经服务端比对, 哈希自描述可升级"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

from satrap.core.config.document import find_config_path, load_config_document, save_config_document
from satrap.core.log import logger
from satrap.core.storage.file_lock import FileLock
from satrap.core.utils.paths import get_project_root

_PBKDF2_ALGORITHM = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_BYTES = 16
_PASSWORD_MIN_LENGTH = 8


def hash_dashboard_password(raw_password: str) -> str:
    """
    PBKDF2-HMAC-SHA256 加盐存储, 迭代次数写入结果供未来平滑升级

    参数:
    - raw_password: 明文密码

    返回:
    - pbkdf2_sha256$iterations$salt$digest 自描述哈希
    """
    if not isinstance(raw_password, str) or not raw_password:
        raise ValueError("密码不能为空")
    salt = secrets.token_hex(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", raw_password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS,
    ).hex()
    return f"{_PBKDF2_ALGORITHM}${_PBKDF2_ITERATIONS}${salt}${digest}"


def is_pbkdf2_hash(stored: object) -> bool:
    """
    判断存储值是否为自描述 PBKDF2 格式

    参数:
    - stored: 待判定值

    返回:
    - 格式前缀匹配时返回 True
    """
    return isinstance(stored, str) and stored.startswith(f"{_PBKDF2_ALGORITHM}$")


def validate_dashboard_password(raw_password: object) -> str:
    """
    校验新密码最低强度

    参数:
    - raw_password: 待设密码

    返回:
    - 合法明文; 非法时抛出 ValueError
    """
    if not isinstance(raw_password, str) or not raw_password:
        raise ValueError("密码不能为空")
    if len(raw_password) < _PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码至少需要 {_PASSWORD_MIN_LENGTH} 个字符")
    return raw_password


def verify_dashboard_password(stored_hash: object, candidate: object) -> bool:
    """
    按存储格式在服务端明文比对, 未知格式拒绝而不是放行

    参数:
    - stored_hash: 配置中的哈希值
    - candidate: 本次提交的明文密码

    返回:
    - 常量时间比对结果
    """
    if not isinstance(stored_hash, str) or not isinstance(candidate, str) or not candidate:
        return False
    if not is_pbkdf2_hash(stored_hash):
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4:
        return False
    _, iterations_raw, salt, digest = parts
    try:
        iterations = int(iterations_raw)
        if iterations <= 0:
            return False
        expected = bytes.fromhex(digest)
        salt_bytes = bytes.fromhex(salt)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt_bytes, iterations)
    return hmac.compare_digest(expected, actual)


def dashboard_password_path() -> Path:
    """返回保存访问密码的配置文件路径"""
    return find_config_path(get_project_root())


def dashboard_password_hash() -> str | None:
    """
    读取配置中的访问密码哈希, 每次调用走磁盘以支持跨进程热更新

    返回:
    - 未设置时 None; 非法存储值抛出 ValueError 由边界映射为明确错误
    """
    try:
        api = load_config_document(dashboard_password_path(), locked=True).get("api")
    except FileNotFoundError:
        return None
    stored = api.get("dashboard_password") if isinstance(api, dict) else None
    if stored in (None, ""):
        return None
    if not is_pbkdf2_hash(stored):
        raise ValueError("访问密码存储格式无效")
    return str(stored)


def dashboard_password_required() -> bool:
    """
    判定当前是否启用访问密码

    返回:
    - 已设置合法哈希时为 True; 存储损坏按启用处理, 由登录端点暴露错误
    """
    try:
        return dashboard_password_hash() is not None
    except ValueError:
        return True


def update_dashboard_password(new_password: str | None) -> str | None:
    """
    读改写同一文件锁内的访问密码字段, None 表示清除恢复免密

    参数:
    - new_password: 新明文密码或 None

    返回:
    - 写入后的哈希或 None
    """
    if new_password is not None:
        validate_dashboard_password(new_password)
        hashed: str | None = hash_dashboard_password(new_password)
    else:
        hashed = None
    path = dashboard_password_path().resolve()
    with FileLock(path.with_name(f".{path.name}.lock")):
        document = load_config_document(path)
        api = document.get("api")
        if not isinstance(api, dict):
            api = {}
            document["api"] = api
        if hashed is None:
            api.pop("dashboard_password", None)
        else:
            api["dashboard_password"] = hashed
        save_config_document(path, document)
    logger.info(f"[访问密码] 已{'清除' if hashed is None else '更新'}: {path}")
    return hashed
