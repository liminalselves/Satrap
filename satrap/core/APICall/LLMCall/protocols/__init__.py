"""按显式配置选择模型协议, 不执行网络探测"""

from .base import ModelProtocol
from .chat_completions import ChatCompletionsProtocol
from .responses import ResponsesProtocol


def select_protocol(api_type: str, provider: str) -> ModelProtocol:
    """
    创建不持有会话状态的协议适配器

    参数:
    - api_type: 显式协议名称
    - provider: 已规范化的客户端基址

    返回:
    - 对应协议适配器, 未知名称直接报错
    """
    if api_type == "chat_completions":
        return ChatCompletionsProtocol()
    if api_type == "responses":
        return ResponsesProtocol(provider)
    raise ValueError("api_type 必须是 chat_completions 或 responses")
