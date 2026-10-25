"""三个服务共用的已鉴权日志查询响应, 仅读取进程内有限缓冲"""
from urllib.parse import parse_qs, urlsplit

from satrap.core.log.stream import standard_log_stream

from satrap.core.log import logger


def log_response(method: str, path: str) -> tuple[int, dict[str, object]] | None:
    """
    处理统一日志读取路径, 调用方负责已有服务认证

    参数:
    - method: HTTP 方法
    - path: 含查询参数的路径

    返回:
    - 有界日志响应, 输入无效返回 400, 无关路径返回 None
    """
    parsed = urlsplit(path)
    if parsed.path != "/api/logs":
        return None
    if method != "GET":
        logger.warning("[日志查询] 不支持的 HTTP 方法")
        return 405, {"error": "日志接口仅支持 GET"}
    try:
        query = parse_qs(parsed.query, keep_blank_values=True)
        snapshot = standard_log_stream.snapshot(
            after=int(query["after"][0]) if "after" in query else None,
            stream_id=query.get("stream_id", [None])[0], limit=int(query.get("limit", ["100"])[0]),
        )
        return 200, dict(snapshot)
    except (ValueError, TypeError):
        logger.warning("[日志查询] 参数无效, 需要有效的条数, 游标和运行流标识")
        return 400, {"error": "日志查询参数无效, limit 为 1 至 500, 增量读取需 stream_id 与非负 after"}
