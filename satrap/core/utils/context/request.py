"""本轮请求资料的校验, 恢复快照和非持久化模型视图"""
from __future__ import annotations

import copy
from typing import Any, Iterable, cast
from satrap.core.type import RequestContextBlock


BASE_AGENT_PROMPT = (
    "需要读取资料、查询最新信息或执行操作时，使用请求中提供的工具，不要凭猜测代替实际结果。\n"
    "按工具参数说明，只传当前需要的值。调用后先看结果：成功就继续，失败就按原因修正参数或换用合适的工具，不要不看原因就原样重试。\n"
    "回答以实际结果为准；没有执行、执行失败或仍不确定的地方直接说明。"
)
FINAL_RESPONSE_PROMPT = (
    "工具调用次数已到上限。不要再调用工具；根据已有结果回答，缺少的信息直接说明。"
)


def compose_agent_system_suffix(catalog: str = "", *, final: bool = False) -> str:
    """组合每次请求的固定工具规则、技能目录和收尾要求"""
    parts = [BASE_AGENT_PROMPT]
    if catalog:
        parts.append(catalog)
    if final:
        parts.append(FINAL_RESPONSE_PROMPT)
    return "\n\n".join(parts)


def attach_system_suffix(messages: list[dict[str, Any]], suffix: str) -> list[dict[str, Any]]:
    """
    在模型请求副本的系统消息中追加本次规则和技能目录, 不改写存档

    参数:
    - messages: 已准备的请求消息
    - suffix: 当前有效目录, 空字符串不追加

    返回:
    - 包含目录的独立请求副本
    """
    if not suffix:
        return messages
    result = copy.deepcopy(messages)
    for message in result:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] += "\n\n" + suffix
            return result
    result.insert(0, {"role": "system", "content": suffix})
    return result


def freeze_request_blocks(blocks: Iterable[RequestContextBlock]) -> tuple[RequestContextBlock, ...]:
    """固定本轮资料, 拒绝同一生产者重复命名或未声明的对象"""
    result = tuple(blocks)
    keys: set[tuple[str, str]] = set()
    for block in result:
        if not isinstance(block, RequestContextBlock):
            raise TypeError("请求资料必须是 RequestContextBlock")
        key = (block.producer, block.name)
        if key in keys:
            raise ValueError(f"请求资料重复: {block.producer}/{block.name}")
        keys.add(key)
    return result


def serialize_request_blocks(blocks: tuple[RequestContextBlock, ...]) -> list[dict[str, str]]:
    """保存执行快照, 不写入对话消息"""
    return [{"name": block.name, "producer": block.producer, "content": block.content} for block in blocks]


def restore_request_blocks(value: object) -> tuple[RequestContextBlock, ...]:
    """读取任务资料快照, 旧任务缺少字段时使用空列表"""
    if not isinstance(value, list):
        raise ValueError("请求资料快照必须是列表")
    blocks: list[RequestContextBlock] = []
    for value_item in cast(list[object], value):
        if not isinstance(value_item, dict):
            raise ValueError("请求资料快照字段无效")
        item = cast(dict[object, object], value_item)
        if set(item) != {"name", "producer", "content"}:
            raise ValueError("请求资料快照字段无效")
        name, producer, content = item["name"], item["producer"], item["content"]
        if not isinstance(name, str) or not isinstance(producer, str) or not isinstance(content, str):
            raise ValueError("请求资料快照字段必须是文本")
        blocks.append(RequestContextBlock(name, producer, content))
    return freeze_request_blocks(blocks)


def attach_request_blocks(
    messages: list[dict[str, Any]], blocks: tuple[RequestContextBlock, ...], current_start: int,
) -> list[dict[str, Any]]:
    """只在请求副本的本轮首条用户消息前注入资料, 保留媒体顺序"""
    if not blocks:
        return messages
    result = copy.deepcopy(messages)
    prefix = "\n\n".join(block.content for block in blocks) + "\n\n"
    for message in result[current_start:]:
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            message["content"] = prefix + content
        elif isinstance(content, list):
            message["content"] = [{"type": "text", "text": prefix}, *content]
        else:
            raise ValueError("本轮用户消息内容不支持请求资料")
        return result
    raise ValueError("请求资料需要关联本轮用户消息")
