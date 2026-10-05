"""
项目路径与媒体来源边界

提供项目默认路径, 根据后端实际存储根校验媒体来源,
统一 file URI 解析, 本地文件白名单和远程来源分类
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit
from functools import lru_cache
from pathlib import Path
import os


def get_project_root() -> Path:
    """
    获取项目根目录

    返回:
    - Path: satrap 包的父目录
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def get_data_dir() -> Path:
    """
    获取项目配置目录

    返回:
    - Path: 项目根目录下的 .satrap
    """
    return get_project_root() / ".satrap"


def ensure_data_dir() -> Path:
    """
    确保项目配置目录存在

    返回:
    - Path: 已创建的 .satrap 目录
    """
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_storage_dir() -> Path:
    """
    获取默认 v2 持久化数据根目录

    返回:
    - Path: .satrap/data, 不包含后端 data_root 覆盖
    """
    return get_data_dir() / "data"


class MediaSourcePermissionError(PermissionError):
    """媒体来源越出允许范围或无法安全解析"""


def file_uri_to_path(source: str) -> str:
    """
    按标准 URI 语法解析本地文件来源

    参数:
    - source: file URI 或裸路径, 裸路径原样返回

    返回:
    - str: 解码后的本地路径; Windows authority 转为 UNC, localhost 视为本机
      非本机 POSIX authority, 相对 file URI 或非法路径抛出 MediaSourcePermissionError
    """
    if not source.lower().startswith("file:"):
        return source
    try:
        parsed = urlsplit(source)
        if parsed.query or parsed.fragment:
            raise ValueError("file URI 不允许 query 或 fragment, 文件名须先转义")
        host = unquote(parsed.netloc, errors="strict")
        path = unquote(parsed.path, errors="strict")
        if not path.startswith("/") or "\0" in path or "\0" in host:
            raise ValueError("file URI 必须包含有效的绝对路径")
        if host and host.lower() != "localhost":
            if os.name != "nt" or any(char in host for char in "@:/\\"):
                raise ValueError("不支持此 file URI 主机")
            return "\\\\" + host + path.replace("/", "\\")
        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    except ValueError as error:
        raise MediaSourcePermissionError(f"无效媒体 file URI: {source}") from error


def normalize_media_source(source: str) -> str:
    """
    统一媒体来源分类, 所有本地路径均先过白名单

    参数:
    - source: HTTP(S), base64, Data URL, file URI 或裸路径

    返回:
    - str: 远程或内联来源原样保留内容并统一 scheme 大小写, 本地来源返回真实绝对路径
      不存在的本地路径仍校验白名单, 无效或越界来源抛出 MediaSourcePermissionError
    """
    lowered = source.lower()
    for prefix in ("http://", "https://", "base64://", "data:"):
        if lowered.startswith(prefix):
            return prefix + source[len(prefix):]
    return ensure_allowed_media_path(file_uri_to_path(source))


def get_db_path(*, platform_id: str = "local") -> str:
    """
    获取默认布局的平台数据库路径

    参数:
    - platform_id: 平台实例 ID, 默认 local

    返回:
    - str: 默认布局中的 platform.db 路径
    """
    from satrap.core.storage import default_storage_layout

    return str(default_storage_layout.platform_db(platform_id))


_configured_media_roots: tuple[str, ...] = ()
_media_storage_root: Path | None = None
_SESSION_MEDIA_DIRS = frozenset({"uploads", "artifacts", "sandbox", "cache"})


def set_media_allowed_roots(roots: list[str] | None) -> None:
    """
    注册后端媒体白名单配置

    参数:
    - roots: 非空列表替换默认目录, None 或空列表恢复默认; 环境变量追加项始终生效
    """
    global _configured_media_roots
    if roots is not None and (
        not isinstance(roots, list) or any(not isinstance(item, str) for item in roots)
    ):
        raise ValueError("media_allowed_roots 必须是字符串列表或 null")
    _configured_media_roots = tuple(item.strip() for item in roots or [] if item.strip())


def set_media_storage_root(root: str | None) -> None:
    """
    注册实际 StorageLayout.root

    参数:
    - root: 后端实际数据根, None 恢复默认 .satrap/data
    """
    global _media_storage_root
    _media_storage_root = Path(root).resolve() if root else None


def get_media_storage_root() -> Path:
    """
    获取媒体校验和临时文件共用的数据根

    返回:
    - Path: 后端注入的实际根目录, 未注入时返回默认数据根
    """
    return _media_storage_root if _media_storage_root is not None else get_storage_dir().resolve()


@lru_cache(maxsize=8)
def _resolve_allowed_roots(configured: tuple[str, ...], extra: str) -> tuple[Path, ...]:
    """
    缓存显式配置和默认前缀目录的解析结果

    参数:
    - configured: 原始配置路径元组, 不经分隔符编码
    - extra: SATRAP_EXTRA_MEDIA_ROOTS, 用 os.pathsep 分隔

    返回:
    - tuple[Path, ...]: 显式目录, 或默认沙箱, 加环境变量追加项
    """
    if configured:
        roots = [Path(item).expanduser().resolve() for item in configured]
    else:
        roots = [(get_data_dir() / "sandbox").resolve()]
    roots.extend(
        Path(item.strip()).expanduser().resolve()
        for item in extra.split(os.pathsep)
        if item.strip()
    )
    return tuple(roots)


def get_allowed_media_roots() -> list[Path]:
    """
    获取媒体白名单的前缀目录

    返回:
    - list[Path]: 显式根目录或默认沙箱, 加环境变量追加项
      默认 StorageLayout 媒体子目录由校验函数按布局结构单独判断
    """
    extra = os.environ.get("SATRAP_EXTRA_MEDIA_ROOTS", "")
    return list(_resolve_allowed_roots(_configured_media_roots, extra))


def _under_media_subdir(resolved: Path) -> bool:
    """
    按 v2 存储布局识别媒体目录, 不按任意祖先目录名放行

    参数:
    - resolved: 已解析的真实绝对路径

    返回:
    - bool: 仅平台 cache 或会话 uploads, artifacts, sandbox, cache 内的路径为 True
    """
    root = get_media_storage_root()
    if not resolved.is_relative_to(root):
        return False
    parts = resolved.relative_to(root).parts
    if len(parts) < 4 or parts[0] != "platforms":
        return False
    if parts[2] == "cache":
        return True
    return len(parts) >= 6 and parts[2] == "sessions" and parts[4] in _SESSION_MEDIA_DIRS


def ensure_allowed_media_path(path: str) -> str:
    """
    校验真实本地媒体路径, 拒绝白名单外来源

    参数:
    - path: 本地路径, 支持 expanduser, 不要求文件已存在

    返回:
    - str: 解析符号链接和 .. 后的真实绝对路径
      无效或白名单外路径抛出 MediaSourcePermissionError
    """
    if not path or "\0" in path:
        raise MediaSourcePermissionError("媒体路径为空或包含 NUL")
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise MediaSourcePermissionError(f"无法解析媒体路径: {path}") from error
    if any(resolved.is_relative_to(root) for root in get_allowed_media_roots()):
        return str(resolved)
    if not _configured_media_roots and _under_media_subdir(resolved):
        return str(resolved)
    raise MediaSourcePermissionError(
        f"媒体来源不在允许目录内 (可用 media_allowed_roots 或 SATRAP_EXTRA_MEDIA_ROOTS 配置): {resolved}"
    )
