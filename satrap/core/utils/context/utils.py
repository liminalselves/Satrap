"""
对话上下文管理组件

提供消息的 SQLite 持久化, 查询, 分支和令牌预算裁剪,
并实现供同步与异步工作流使用的上下文管理器
"""

from __future__ import annotations
from dataclasses import dataclass
import sqlite3
from typing import List, Dict, Optional, Any, cast, TYPE_CHECKING, Literal
import json
from satrap.core.utils.tokenizer import tokenizer_estimate, experience_estimate
from satrap.core.utils.vision import (
    DEFAULT_IMAGE_TOKEN_COST,
    build_multimodal_content,
    content_text_projection,
    estimate_content_image_count,
)
from satrap.core.type import JsonRow, RestoreOptions, SnapshotDomain, StateScope
from satrap.core.log import logger


if TYPE_CHECKING:
    from satrap.core.APICall.LLMCall import AsyncLLM, LLM

TokenEstimateMethod = Literal["tokenizer", "experience"]

ContextStrategy = Literal["sliding", "mid_truncate", "summarize"]

_SUMMARY_PROMPT_VERSION = 1

_MIN_SUMMARY_OUTPUT_TOKENS = 512

_MAX_SUMMARY_OUTPUT_TOKENS = 2048

_SUMMARY_RETRY_OUTPUT_TOKENS = 4096


def _summary_output_budget(target_tokens: int) -> int:
    """
    返回不受主回复低额度影响的摘要输出预算

    参数:
    - target_tokens: 期望摘要长度

    返回:
    - int: 摘要模型调用的最大输出 token 数
    """
    return max(
        _MIN_SUMMARY_OUTPUT_TOKENS,
        min(_MAX_SUMMARY_OUTPUT_TOKENS, target_tokens),
    )


class ContextOverflowError(RuntimeError):
    """保留项本身已超过上下文预算时抛出的异常"""


@dataclass
class PreparedModelContext:
    """一次模型请求使用的派生上下文及计数元数据"""

    messages: List[Dict[str, Any]]
    """实际发送给模型的消息副本"""
    estimated_input_tokens: int
    """准备后请求的本地估算输入 token 数"""
    effective_input_tokens: int
    """应用历史 API 校准后的输入 token 数"""
    original_estimated_input_tokens: int
    """压缩前完整请求的本地估算输入 token 数"""
    token_source: str
    """计数来源, tokenizer, experience 或 api_calibrated"""
    strategy: str
    """本次采用的上下文策略"""
    compressed: bool
    """本次是否对模型视图执行了压缩"""
    original_turns: int
    """完整上下文轮数"""
    prepared_turns: int
    """模型视图中的轮数"""
    history_budget: int
    """本次请求使用的历史上下文预算"""
    trigger_tokens: int
    """本次请求触发上下文处理的 token 线"""
    floor_tokens: int
    """本次请求执行截取后的目标 token 线"""
    model: Optional[str] = None
    """本次请求的模型名, 用于隔离不同模型的 usage 校准"""


@dataclass
class _ContextRuntimeState:
    """不属于完整消息历史的可重建运行时状态"""

    summary: str = ""
    covered_turn_count: int = 0
    summary_model: Optional[str] = None
    summary_prompt_version: int = _SUMMARY_PROMPT_VERSION
    usage_model: Optional[str] = None
    api_input_tokens: Optional[int] = None
    estimated_input_tokens: Optional[int] = None
    api_output_tokens: Optional[int] = None
    api_total_tokens: Optional[int] = None
    api_cached_tokens: Optional[int] = None


def _estimate_text(text: str, method: TokenEstimateMethod) -> int:
    """
    使用指定方法估算文本 token 数

    参数:
    - text: 待估算文本
    - method: tokenizer 或 experience

    返回:
    - int: 估算 token 数
    """
    if method == "tokenizer":
        return tokenizer_estimate(text)
    return experience_estimate(text)


def _estimate_request_tokens(
    messages: List[Dict[str, Any]],
    method: TokenEstimateMethod,
    tools: Optional[List[Dict[str, Any]]] = None,
    img_urls: Optional[List[str]] = None,
) -> int:
    """
    估算实际请求中的消息, 工具定义和额外图片成本

    参数:
    - messages: 请求消息
    - method: token 估算方法
    - tools: 工具定义列表
    - img_urls: 尚未写入消息内容的额外图片

    返回:
    - int: 请求输入 token 估算值
    """
    token_count = 0
    for msg in messages:
        content = msg.get("content", "")
        image_count = estimate_content_image_count(content)
        token_count += 4 + image_count * DEFAULT_IMAGE_TOKEN_COST
        token_count += _estimate_text(content_text_projection(content), method)
        metadata = {
            key: value
            for key, value in msg.items()
            if key != "content" and value is not None
        }
        if metadata:
            token_count += _estimate_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True), method
            )

    if tools:
        token_count += _estimate_text(
            json.dumps(tools, ensure_ascii=False, sort_keys=True), method
        )
    if img_urls:
        token_count += len(img_urls) * DEFAULT_IMAGE_TOKEN_COST
    return token_count


def _llm_model_name(llm: object | None) -> Optional[str]:
    """
    获取 LLM 实例当前模型名

    参数:
    - llm: LLM 实例

    返回:
    - Optional[str]: 模型名
    """
    if llm is None:
        return None
    getter = getattr(llm, "get_model", None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, str) and value:
                return value
        except (AttributeError, TypeError):
            pass
    value = getattr(llm, "model", None)
    return value if isinstance(value, str) and value else None


def _summary_lines(turns: List[List[Dict[str, Any]]]) -> List[str]:
    """
    将对话轮次投影为供总结模型读取的纯文本行

    参数:
    - turns: 待总结轮次

    返回:
    - List[str]: 纯文本消息行
    """
    lines: List[str] = []
    for turn in turns:
        for msg in turn:
            role = str(msg.get("role", "unknown"))
            text = content_text_projection(msg.get("content"))
            metadata = {
                key: value
                for key, value in msg.items()
                if key not in {"role", "content"} and value is not None
            }
            if metadata:
                text = f"{text}\n附加数据: {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}"
            lines.append(f"{role}: {text or '[空消息]'}")
    return lines


def _split_summary_chunks(lines: List[str], token_budget: int) -> List[str]:
    """
    将超长待总结文本切成可逐段归并的块

    参数:
    - lines: 对话文本行
    - token_budget: 每块的近似 token 预算

    返回:
    - List[str]: 文本块
    """
    budget = max(256, token_budget)
    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0
    for line in lines:
        parts = [line]
        if tokenizer_estimate(line) > budget:
            chars_per_chunk = max(256, budget * 3)
            parts = [
                line[index : index + chars_per_chunk]
                for index in range(0, len(line), chars_per_chunk)
            ]
        for part in parts:
            part_tokens = tokenizer_estimate(part)
            if current and current_tokens + part_tokens > budget:
                chunks.append("\n".join(current))
                current = []
                current_tokens = 0
            current.append(part)
            current_tokens += part_tokens
    if current:
        chunks.append("\n".join(current))
    return chunks


def _messages_domain() -> SnapshotDomain:
    """
    消息领域注册: 管理 chat_history 表的对话消息

    返回:
    - SnapshotDomain: 消息领域注册: 管理 chat_history 表的对话消息
    """

    def builder(conn: sqlite3.Connection, scope: StateScope) -> List[JsonRow]:
        """
        读取指定对话的全部消息行

        参数:
        - conn: 数据库连接
        - scope: 作用域

        返回:
        - List[JsonRow]: 读取指定对话的全部消息行
        """
        rows = conn.execute(
            "SELECT id, role, content, content_json, tool_call_id, tool_calls, "
            "reasoning_content FROM chat_history WHERE conversation_id = ? ORDER BY id",
            (scope.scope_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def restorer(
        conn: sqlite3.Connection,
        scope: StateScope,
        rows: List[JsonRow],
        options: RestoreOptions,
    ) -> None:
        """
        清空后按快照重写指定对话的消息 (与 save_context 的列保持一致)

        参数:
        - conn: 数据库连接
        - scope: 作用域
        - rows: 数据行集合
        - options: 选项集合
        """
        cleaner(conn, scope)
        for row in rows:
            conn.execute(
                "INSERT INTO chat_history "
                "(conversation_id, role, content, content_json, tool_call_id, "
                " tool_calls, reasoning_content) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.scope_id,
                    str(row.get("role") or ""),
                    row.get("content"),
                    row.get("content_json"),
                    row.get("tool_call_id"),
                    row.get("tool_calls"),
                    row.get("reasoning_content"),
                ),
            )

    def cleaner(conn: sqlite3.Connection, scope: StateScope) -> None:
        """
        清空指定对话的全部消息

        参数:
        - conn: 数据库连接
        - scope: 作用域
        """
        conn.execute(
            "DELETE FROM chat_history WHERE conversation_id = ?",
            (scope.scope_id,),
        )

    def position_provider(conn: sqlite3.Connection, scope: StateScope) -> int:
        """
        提供消息水位: 当前对话消息条数 (追加式指针检查点的截断基准)

        参数:
        - conn: 数据库连接
        - scope: 作用域

        返回:
        - int: 提供消息水位: 当前对话消息条数 (追加式指针检查点的截断基准)
        """
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM chat_history WHERE conversation_id = ?",
            (scope.scope_id,),
        ).fetchone()
        return int(row["count"])

    return SnapshotDomain(
        name="messages",
        builder=builder,
        restorer=restorer,
        cleaner=cleaner,
        position_provider=position_provider,
    )


def _message_content_json(content: Any) -> str | None:
    """
    序列化多模态 content

    参数:
    - content: 内容

    返回:
    - str | None: 序列化多模态 content
    """
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    return None


def _load_message_content(content: str | None, content_json: str | None) -> Any:
    """
    加载消息 content, 优先使用完整 JSON

    参数:
    - content: 内容
    - content_json: 内容json

    返回:
    - Any: 加载消息 content, 优先使用完整 JSON
    """
    if content_json:
        try:
            parsed = json.loads(content_json)
            if isinstance(parsed, list):
                return cast(list[dict[str, Any]], parsed)
        except Exception:
            pass
    return content


def _load_tool_calls(
    raw_tool_calls: str | None,
    conversation_id: str,
    row_index: int,
) -> list[object] | None:
    """
    逐行加载工具调用, 损坏数据只影响当前字段

    参数:
    - raw_tool_calls: 数据库中的工具调用 JSON
    - conversation_id: 对话 ID
    - row_index: 消息行序号

    返回:
    - list[object] | None: 有效工具调用列表, 无效时返回 None
    """
    if raw_tool_calls is None:
        return None
    try:
        parsed = json.loads(raw_tool_calls)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning(
            f"[上下文管理器] 忽略损坏的工具调用字段, 对话ID: {conversation_id}, 行号: {row_index}"
        )
        return None
    if not isinstance(parsed, list):
        logger.warning(
            f"[上下文管理器] 忽略非列表工具调用字段, 对话ID: {conversation_id}, 行号: {row_index}"
        )
        return None
    return cast(list[object], parsed)


def add_user_message(
    context: list[dict[str, Any]], message: str, img_urls: list[str] | None = None
):
    """
    向上下文中添加一条用户消息

    参数:
    - context: 上下文列表
    - message: 用户消息内容
    - img_urls: 图片 URL 或本地路径列表, 可选
    """
    context.append(
        {"role": "user", "content": build_multimodal_content(message, img_urls)}
    )


def add_bot_message(
    context: list[dict[str, Any]],
    message: str,
    tools_calls: list[dict[str, Any]] | None = None,
    reasoning: str | None = None,
):
    """
    向上下文中添加一条助手消息

    参数:
    - context: 上下文列表
    - message: 助手消息内容
    - tools_calls: 工具调用列表, 默认 None
    - reasoning: 思考内容, 默认 None
    """
    if tools_calls:
        context.append(
            {
                "role": "assistant",
                "content": message,
                "reasoning_content": (
                    reasoning if reasoning is not None and reasoning != "" else None
                ),
                "tool_calls": tools_calls,
            }
        )
    else:
        context.append(
            {
                "role": "assistant",
                "content": message,
                "reasoning_content": (
                    reasoning if reasoning is not None and reasoning != "" else None
                ),
            }
        )


def add_tool_message(
    context: list[dict[str, Any]], tool_call_id: str, tool_result: dict[str, Any] | str
):
    """
    向上下文中添加一条工具调用结果消息

    参数:
    - context: 上下文列表
    - tool_call_id: 工具调用 ID
    - tool_result: 工具调用结果
    """
    context.append(
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": (
                json.dumps(tool_result, ensure_ascii=False)
                if isinstance(tool_result, dict)
                else tool_result
            ),
        }
    )


def add_tools_call_flow(
    context: list[dict[str, Any]],
    message: str,
    tool_messages: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    reasoning: str | None = None,
):
    """
    添加一个完整的工具调用消息流到上下文中

    相当于:
    ``` python
    ctx.add_bot_message(message, tool_messages, reasoning)
    for tool_msg, tool_res in zip(tool_messages, tool_results):
        ctx.add_tool_message(tool_msg["id"], tool_res)
    ```

    参数:
    - context: 上下文列表
    - message: 助手消息内容
    - tool_messages: 工具调用消息列表
    - tool_results: 工具调用结果列表
    - reasoning: 思考内容, 默认 None
    """
    add_bot_message(context, message, tools_calls=tool_messages, reasoning=reasoning)
    for tool_msg, tool_res in zip(tool_messages, tool_results):
        add_tool_message(context, tool_msg["id"], tool_res)


def clear_reasoning_content(messages: list[dict[str, Any]]):
    """
    清除上下文中所有消息的思考内容 (reasoning_content 字段)

    参数:
    - messages: 消息列表
    """
    for message in messages:
        if "reasoning_content" in message:
            message["reasoning_content"] = None
