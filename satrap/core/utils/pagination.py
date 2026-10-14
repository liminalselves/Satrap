"""签名与 offset 游标的公共编解码, 签名仍由各领域的同一读事务生成"""
from __future__ import annotations

import base64
import json


def decode_offset(cursor: str, signature: str) -> int:
    """
    核验游标对应的筛选与内容版本, 保留既有游标编码

    参数:
    - cursor: 上次返回的游标, 空串表示第一页
    - signature: 当前事务生成的签名

    返回:
    - 非负整数偏移, 格式或签名不符时抛出 ValueError
    """
    if not cursor:
        return 0
    try:
        token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if token[0] != signature or type(token[1]) is not int or token[1] < 0:
            raise ValueError()
        return int(token[1])
    except (ValueError, TypeError, KeyError, IndexError) as error:
        raise ValueError("游标与当前内容版本不符") from error


def encode_offset(signature: str, offset: int) -> str:
    """
    生成与既有客户端兼容的下一页游标

    参数:
    - signature: 当前事务生成的签名
    - offset: 下一页起始位置

    返回:
    - URL 安全的 Base64 编码
    """
    return base64.urlsafe_b64encode(json.dumps([signature, offset]).encode()).decode()
