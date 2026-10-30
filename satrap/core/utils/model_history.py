"""模型协议资料的消息绑定, 不依赖 SDK 或后端服务"""

from collections.abc import Mapping
import copy
import hashlib
import json
from typing import cast

from satrap.core.type import ModelProtocolPayload


def message_fingerprint(message: Mapping[str, object]) -> str:
    """
    标识协议资料对应的通用消息内容

    参数:
    - message: 助手消息, 不包含协议资料本身

    返回:
    - 内容及工具调用的稳定摘要, 编辑后不再回放旧协议资料
    """
    value = {key: message.get(key) for key in ("role", "content", "tool_calls")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def attach_protocol_payload(message: dict[str, object], payload: ModelProtocolPayload | None) -> None:
    """
    将一次输出资料绑定到实际保存的消息

    参数:
    - message: 将被持久化或继续传递的助手消息
    - payload: 模型返回的增量协议资料, None 表示普通消息
    """
    if payload is not None:
        message["protocol_payload"] = {**copy.deepcopy(payload), "message_fingerprint": message_fingerprint(message)}


def replay_items(message: Mapping[str, object], provider: str, model: str) -> list[dict[str, object]] | None:
    """
    读取仍与消息和模型来源一致的协议资料

    参数:
    - message: 当前准备发送的消息副本
    - provider: 当前协议适配器的来源摘要
    - model: 本次实际模型名称

    返回:
    - 可回放的输出项, 来源变化或消息编辑后返回 None
    """
    payload = message.get("protocol_payload")
    if not isinstance(payload, dict):
        return None
    data = cast(dict[str, object], payload)
    if data.get("api_type") != "responses" or data.get("provider") != provider or data.get("model") != model:
        return None
    if data.get("message_fingerprint") != message_fingerprint(message):
        return None
    items = data.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in cast(list[object], items)):
        return None
    return copy.deepcopy(cast(list[dict[str, object]], items))
