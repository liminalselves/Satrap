"""
任务内不可变模型请求编码

压缩同一任务内请求的公共消息前缀, 校验引用层级与完整性,
恢复时使用原始请求记录, 不从当前会话历史重建请求
"""
from __future__ import annotations

import hashlib
from typing import Any
import json
import re

MAX_DEPTH = 7


def model_position(key: str) -> int:
    """
    步骤顺序不依赖 SQLite rowid, 归档重建后仍保持稳定

    参数:
    - key: 形如 model:0 的非负整数步骤标识

    返回:
    - 稳定的步骤序号, 标识非法时抛出 ValueError
    """
    if not isinstance(key, str) or re.fullmatch(r"model:(0|[1-9][0-9]*)", key) is None:
        raise ValueError("模型步骤标识无效")
    return int(key[6:])


def dump(value: Any) -> str:
    """
    保持 JSON 内容与顺序, 拒绝非 JSON 值

    参数:
    - value: 可序列化的 JSON 数据, 不接受任意 Python 对象

    返回:
    - 规范化 JSON 字符串, 无法序列化时抛出异常
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    """
    校验完整实际请求

    参数:
    - value: 可序列化的 JSON 数据, 不接受任意 Python 对象

    返回:
    - 完整请求的 SHA-256 指纹
    """
    return hashlib.sha256(dump(value).encode("utf-8")).hexdigest()


def pack(request: dict[str, Any], previous: tuple[str, dict[str, Any], int] | None = None) -> dict[str, Any]:
    """
    保存完整请求或同一任务内较早请求的公共前缀

    参数:
    - request: 完整模型请求, 包含消息及调用参数
    - previous: 同一任务内的前序请求及引用深度, 默认 None 表示无引用

    返回:
    - 完整或前缀引用编码, 仅在引用更省空间且未超过深度上限时使用引用
    """
    messages = request["messages"]
    params = {key: value for key, value in request.items() if key not in {"messages", "_prepared"}}
    actual = {**params, "messages": messages}
    full = {"version": 2, "base": None, "depth": 0, "prefix": 0, "suffix": messages,
            "params": params, "digest": digest(actual)}
    if previous is None or previous[2] >= MAX_DEPTH:
        return full
    key, before, depth = previous
    prefix = 0
    for left, right in zip(before["messages"], messages):
        if left != right:
            break
        prefix += 1
    delta = {**full, "base": key, "depth": depth + 1, "prefix": prefix, "suffix": messages[prefix:]}
    return delta if prefix and len(dump(delta)) < len(dump(full)) else full


def unpack(value: dict[str, Any], previous: tuple[dict[str, Any], int] | None = None) -> dict[str, Any]:
    """
    拒绝损坏引用与未知格式, 不猜测丢失消息

    参数:
    - value: 可序列化的 JSON 数据, 不接受任意 Python 对象
    - previous: 同一任务内的前序请求及引用深度, 默认 None 表示无引用

    返回:
    - 重建的完整模型请求, 编码或完整性校验失败时抛出 ValueError
    """
    if value.get("version") != 2:
        raise ValueError("不支持的请求编码版本")
    depth, prefix = value.get("depth"), value.get("prefix")
    if type(depth) is not int or not 0 <= depth <= MAX_DEPTH or type(prefix) is not int or prefix < 0:
        raise ValueError("请求引用范围无效")
    if not isinstance(value.get("suffix"), list) or not isinstance(value.get("params"), dict):
        raise ValueError("请求正文无效")
    messages = []
    if value.get("base") is not None:
        if previous is None or depth != previous[1] + 1 or prefix > len(previous[0]["messages"]):
            raise ValueError("请求引用丢失或层级无效")
        messages = previous[0]["messages"][:prefix]
    elif previous is not None or depth != 0 or prefix != 0:
        raise ValueError("完整请求编码无效")
    request = {**value["params"], "messages": messages + value["suffix"]}
    if digest(request) != value.get("digest"):
        raise ValueError("请求完整性校验失败")
    return request
