"""模型协议与逐请求流式解码器的最小契约"""

from collections.abc import Iterable, Mapping
from typing import Protocol

from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, LLMErrorInfo


class ModelProtocolError(ValueError):
    """不依赖宿主或 HTTP 异常的协议错误"""

    def __init__(self, code: str, message: str, provider_code: str | None = None) -> None:
        """
        保存可公开的错误资料

        参数:
        - code: 项目错误分类
        - message: 固定的公开说明
        - provider_code: 经验证的供应商错误码
        """
        self.info = LLMErrorInfo(code, message, provider_code=provider_code)
        super().__init__(self.info.text)


class StreamDecoder(Protocol):
    """一次请求专用的有序增量解码器"""

    finish_reason: str | None

    def consume(self, chunk: object) -> list[LLMCallStreamEvent]:
        """
        消费一个原始事件

        参数:
        - chunk: SDK 返回的原始增量

        返回:
        - 项目通用事件, 不在本层执行工具
        """
        ...

    def response(self, suppress_error: bool = True) -> LLMCallResponse:
        """
        读取终态响应

        参数:
        - suppress_error: 是否将协议终态错误转换为结构化响应

        返回:
        - 完整响应, 不能把缺失终态当成成功
        """
        ...


class ModelProtocol(Protocol):
    """仅转换数据, 不管理数据库, 工具执行或网络生命周期"""

    api_type: str

    def build_request(self, parameters: Mapping[str, object], messages: Iterable[dict[str, object]], *,
                      thinking_body: dict[str, object] | None, thinking: str,
                      tools: list[dict[str, object]] | None, tool_choice: object,
                      stream: bool, include_usage: bool, structured: bool) -> dict[str, object]:
        """
        构造 SDK 请求参数

        参数:
        - parameters: 已合并的模型与生成参数
        - messages: 项目内部消息副本
        - thinking_body: Chat Completions 兼容思考配置
        - thinking: 调用方显式思考强度
        - tools: 项目工具定义, None 表示不发送工具字段
        - tool_choice: 工具选择策略
        - stream: 是否流式请求
        - include_usage: 是否请求流式计量
        - structured: 是否使用 JSON 对象输出

        返回:
        - 可交给对应 SDK 端点的独立参数字典
        """
        ...

    def parse_response(self, response: object, suppress_error: bool = True) -> LLMCallResponse:
        """
        将原始响应转换为项目结构

        参数:
        - response: SDK 原始响应
        - suppress_error: 是否转换协议错误

        返回:
        - 统一模型响应
        """
        ...

    def new_stream_decoder(self) -> StreamDecoder:
        """
        创建独立解码器

        返回:
        - 本次请求独享的解码状态
        """
        ...
