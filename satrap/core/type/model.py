"""模型调用、上下文统计与用户请求契约"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, TypedDict, Literal, NotRequired, cast
import time

from satrap.core.type.conversation import CallOrigin, ConversationRoute, HistoryWindow


THINKING_LEVEL_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
THINKING_CLOSE_VALUES = ("off", "none")


def validate_runtime_thinking(value: object) -> str:
    """
    校验运行时思考强度, 统一关闭别名与已开放强度

    参数:
    - value: 调用方传入的思考强度

    返回:
    - str: 通过校验的思考强度, 非法值抛出 ValueError
    """
    valid = {"default", *THINKING_CLOSE_VALUES, *THINKING_LEVEL_VALUES}
    if not isinstance(value, str) or value not in valid:
        raise ValueError(f"不支持的思考强度: {value!r}")
    return value



def validate_thinking_levels(value: object) -> Optional[List[str]]:
    """
    校验并规范化模型支持的思考强度列表

    参数:
    - value: 待校验的思考强度列表或 None

    返回:
    - Optional[List[str]]: 去重后的思考强度列表或 None
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("thinking_levels 必须是字符串列表")
    raw_levels = cast(List[object], value)
    if not all(isinstance(item, str) for item in raw_levels):
        raise ValueError("thinking_levels 必须是字符串列表")
    levels = cast(List[str], raw_levels)
    invalid = [item for item in levels if item not in THINKING_LEVEL_VALUES]
    if invalid:
        raise ValueError(f"不支持的思考强度: {', '.join(invalid)}")
    return list(dict.fromkeys(levels))


@dataclass
class TokenUsage:
    """LLM 请求的 token 使用量"""
    input_tokens: Optional[int] = None
    """输入 token 数, 对应 prompt_tokens 或 input_tokens"""
    output_tokens: Optional[int] = None
    """输出 token 数, 对应 completion_tokens 或 output_tokens"""
    total_tokens: Optional[int] = None
    """总 token 数"""
    cached_tokens: Optional[int] = None
    """输入中命中供应商缓存的 token 数"""


@dataclass(frozen=True)
class ContextUsageSnapshot:
    """ContextManager 当前预算和最近一次模型 usage 快照"""
    history_tokens: int
    """当前完整历史的本地 token 估算值"""
    context_window_tokens: int
    """模型允许的总上下文长度"""
    reserved_output_tokens: int
    """为模型输出预留的 token 长度"""
    history_upper_tokens: int
    """历史上下文硬上限"""
    history_lower_tokens: int
    """历史上下文压缩后的目标下限"""
    last_output_tokens: Optional[int]
    """上一次正式模型请求的输出 token 数"""
    cache_hit_tokens: Optional[int]
    """上一次正式模型请求命中的缓存 token 数"""
    history_token_source: str
    """历史 token 的本地估算来源"""

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为可直接序列化的字典

        返回:
        - Dict[str, Any]: 上下文 usage 快照
        """
        return {
            "history_tokens": self.history_tokens,
            "context_window_tokens": self.context_window_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "history_upper_tokens": self.history_upper_tokens,
            "history_lower_tokens": self.history_lower_tokens,
            "last_output_tokens": self.last_output_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "history_token_source": self.history_token_source,
        }


@dataclass
class ModelContextRequestStats:
    """一次正式模型请求的上下文准备和 API usage 统计"""
    model: Optional[str]
    """上下文准备时使用的模型名称, 未指定时为 None"""
    strategy: str
    """本次上下文准备采用的压缩或截取策略"""
    compressed: bool
    """本次准备是否实际压缩或截取了历史"""
    original_turns: int
    """准备前参与统计的历史对话轮数"""
    prepared_turns: int
    """准备后发送给模型的历史对话轮数"""
    original_estimated_input_tokens: int
    """准备前输入的本地估算 token 数"""
    estimated_input_tokens: int
    """准备后输入的本地估算 token 数, 不是供应商计量"""
    effective_input_tokens: int
    """预检时用于预算判断的有效输入 token 数"""
    preflight_token_source: str
    """预检有效输入 token 数的来源标识"""
    history_budget: int
    """为历史上下文分配的 token 预算"""
    trigger_tokens: int
    """触发上下文处理的历史 token 阈值"""
    floor_tokens: int
    """上下文处理后的目标历史 token 下限"""
    api_input_tokens: Optional[int] = None
    """供应商报告的输入 token 数, 未提供时为 None"""
    api_output_tokens: Optional[int] = None
    """供应商报告的输出 token 数, 未提供时为 None"""
    api_total_tokens: Optional[int] = None
    """供应商报告的总 token 数, 未提供时为 None"""
    api_cached_tokens: Optional[int] = None
    """供应商报告的缓存命中输入 token 数, 未提供时为 None"""

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为可直接写入 JSON 的字典

        返回:
        - Dict[str, Any]: 请求统计字典
        """
        return {
            "model": self.model,
            "strategy": self.strategy,
            "compressed": self.compressed,
            "original_turns": self.original_turns,
            "prepared_turns": self.prepared_turns,
            "original_estimated_input_tokens": self.original_estimated_input_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "effective_input_tokens": self.effective_input_tokens,
            "preflight_token_source": self.preflight_token_source,
            "history_budget": self.history_budget,
            "trigger_tokens": self.trigger_tokens,
            "floor_tokens": self.floor_tokens,
            "api_input_tokens": self.api_input_tokens,
            "api_output_tokens": self.api_output_tokens,
            "api_total_tokens": self.api_total_tokens,
            "api_cached_tokens": self.api_cached_tokens,
        }


@dataclass
class ModelContextTurnStats:
    """一轮会话内全部正式模型请求的上下文统计"""
    requests: List[ModelContextRequestStats] = field(
        default_factory=lambda: list[ModelContextRequestStats]()
    )
    """本轮正式模型请求的有序统计, 默认空列表, 汇总时忽略未知用量"""

    def to_dict(self) -> Dict[str, Any]:
        """
        汇总并转换为可直接写入 JSON 的字典

        返回:
        - Dict[str, Any]: 轮次统计字典
        """
        if not self.requests:
            return {}

        def sum_usage(name: str) -> Optional[int]:
            """
            汇总存在的 API usage 字段

            参数:
            - name: ModelContextRequestStats 字段名

            返回:
            - Optional[int]: 汇总值, 所有请求均无该字段时为 None
            """
            values = [getattr(request, name) for request in self.requests]
            known = [value for value in values if isinstance(value, int)]
            return sum(known) if known else None

        return {
            "request_count": len(self.requests),
            "last_request": self.requests[-1].to_dict(),
            "total_api_input_tokens": sum_usage("api_input_tokens"),
            "total_api_output_tokens": sum_usage("api_output_tokens"),
            "total_api_tokens": sum_usage("api_total_tokens"),
            "total_api_cached_tokens": sum_usage("api_cached_tokens"),
        }


@dataclass(frozen=True)
class LLMErrorInfo:
    """可公开展示的模型调用错误, 不携带原始请求或认证资料"""
    code: str
    """项目定义的稳定错误分类, 可供调用方判断"""
    message: str
    """不包含原始请求和凭据的公开错误说明"""
    status: int | None = None
    """上游 HTTP 状态码, 不可用时为 None"""
    provider_code: str | None = None
    """经验证可公开的供应商错误码, 缺失时为 None"""
    request_id: str | None = None
    """上游请求的安全标识, 未提供或不可公开时为 None"""

    @property
    def text(self) -> str:
        """
        生成聊天与平台共用的简短失败说明

        返回:
        - 含稳定错误码和可用 HTTP 状态的展示文本
        """
        label = self.provider_code or self.code
        if self.status is not None:
            label = f"HTTP {self.status} / {label}"
        return f"请求失败 [{label}]: {self.message}"


class ModelProtocolPayload(TypedDict):
    """与一次助手输出关联的协议资料, 不保存整份输入或认证凭据"""

    api_type: Literal["responses"]
    """协议类型固定为 responses, 不用于回放其他协议"""
    provider: str
    """协议适配器的供应商来源摘要, 回放时必须与当前来源一致"""
    model: str
    """产生输出的模型名称, 回放时必须与本次模型一致"""
    response_id: str
    """供应商返回的本次响应标识"""
    items: List[Dict[str, object]]
    """本次助手输出的协议项, 来源与消息匹配时可作为历史回放"""
    message_fingerprint: NotRequired[str]
    """保存时计算的助手消息内容摘要, 缺失或不匹配时不回放"""


@dataclass
class LLMCallResponse:
    """LLM 调用响应数据结构"""
    type: str
    """LLM 调用响应类型"""
    content: str
    """LLM 调用响应内容"""
    thinking: Optional[str] = None
    """LLM 调用响应思考"""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    """LLM 调用响应工具调用, 包含 name, id, arguments"""
    usage: Optional[TokenUsage] = None
    """API 返回的真实 token 使用量, 供应商未返回时为 None"""
    finish_reason: Optional[str] = None
    """模型结束原因; length 可帮助识别被截断的工具调用"""
    error_info: LLMErrorInfo | None = None
    """失败响应的脱敏资料, 工作流不得将其提交为正常回答"""
    protocol_payload: Optional["ModelProtocolPayload"] = None
    """本次输出的可重放协议资料, 与展示用思考文本分离"""

    def __iter__(self) -> Iterator[Any]:
        """
        支持解包操作

        返回:
        - Iterator[Any]: 支持解包操作
        """
        yield self.type
        yield self.content
        yield self.thinking
        yield self.tool_calls or field(default_factory=list)

    def __len__(self) -> int:
        """
        返回可解包的元素数量

        返回:
        - int: 可解包的元素数量
        """
        return 4


@dataclass
class LLMCallStreamEvent:
    """LLM 流式调用事件"""
    kind: str
    """事件类型, 如 content_delta, thinking_delta, tool_call_delta, done 或 error"""
    delta: str = ""
    """本次事件携带的文本或工具参数增量"""
    tool_call: Optional[Dict[str, Any]] = None
    """工具调用增量, 包含 index, id, name 和 arguments 字段"""
    response: Optional[object] = None
    """done 事件携带的完整 LLMCallResponse 或 False"""
    finish_reason: Optional[str] = None
    """模型结束原因"""
    error: Optional[str] = None
    """error 事件的错误信息"""
    error_info: LLMErrorInfo | None = None
    """与非流式响应共用的结构化错误"""

@dataclass
class LLMCallRequest:
    """LLM 调用请求数据结构"""
    role: str
    """LLM 调用请求角色"""
    content: str
    """LLM 调用请求内容"""
    tools: Optional[List[Dict[str, Any]]] = None
    """工具定义列表, 用于 Function Calling"""
    tool_choice: Optional[str] = None
    """工具选择策略, 如 "auto" 或 "none" , 或 {"type": "function", "function": {"name": "工具名"}}"""
    img_urls: Optional[List[str]] = None
    """随本次模型请求发送的图片 URL, None 表示不携带图片输入"""

@dataclass(frozen=True)
class RequestContextBlock:
    """可信生产者提供的本轮资料, 内容不作为系统指令或授权来源"""

    name: str
    """资料块名称, 由可信生产者提供且不得为空白"""
    producer: str
    """可信资料生产者标识, 不得为空白, 不赋予正文授权"""
    content: str
    """仅供本轮参考的非空白内容, 不作为系统指令"""

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.name, self.producer, self.content)):
            raise ValueError("请求资料的名称, 生产者和内容必须是非空文本")


@dataclass
class UserCall:
    """用户调用数据结构"""
    session_id: Optional[str] = None
    """会话 ID, 如 `sr7dws`"""
    session_provider: Optional[str] = None
    """会话 Provider 名称, 未指定时使用 session_class"""
    session_type: Optional[str] = None
    """会话类型"""
    message: Optional[str] = None
    """用户输入消息"""
    img_urls: Optional[List[str]] = None
    """用户随本轮输入提供的图片 URL, None 表示没有图片输入"""
    video_urls: Optional[List[str]] = None
    """用户随本轮输入提供的视频 URL, None 表示没有视频输入"""
    route: ConversationRoute | None = None
    """平台来源路由, 用于显式更新会话映射"""
    origin: CallOrigin | None = None
    """本轮可信来源, 不从消息正文或模型参数解析"""
    group_session_overrides: dict[str, object] | None = None
    """当前群已核验的会话覆盖快照, 不持久化为实例配置"""
    group_config_revision: int | None = None
    """本轮会话覆盖对应的群配置修订号"""
    group_route_generation: int | None = None
    """本轮会话覆盖对应的群路由代次"""
    request_blocks: tuple[RequestContextBlock, ...] = ()
    """本轮平台资料, 独立于需要保存的正文"""
    received_at: float = field(default_factory=time.monotonic, kw_only=True)
    """宿主接收请求的单调时钟, 平台调用沿用真实入队时刻"""
    received_wall_at: float = field(default_factory=time.time, kw_only=True)
    """宿主接收请求的 UTC 时间戳, 用于持久化会话档案起点"""
    context_reset_at: float | None = field(default=None, init=False)
    """仅在绑定成功后发布的命令接收边界, 用于撤销命令之前的窗口资料"""
    history_window: HistoryWindow | None = field(default=None, init=False)
    """宿主从真实会话配置冻结的查询下界, 不从模型输入构造"""
