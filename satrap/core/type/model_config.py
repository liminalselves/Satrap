"""模型与多模态客户端配置契约"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional


@dataclass
class LLMConfig:
    """LLM 配置数据结构"""
    name: Optional[str] = None
    """LLM 名称"""
    model: Optional[str] = None
    """LLM 模型"""
    base_url: Optional[str] = None
    """LLM 基础 URL"""
    api_key: Optional[str] = None
    """LLM API 密钥"""
    temperature: Optional[float] = None
    """LLM 温度"""
    top_p: Optional[float] = None
    """核采样的累积概率阈值, None 表示未显式配置"""
    max_tokens: Optional[int] = None
    """LLM 最大 token 数量"""
    network_timeout: float = 60.0
    """模型 SDK 的网络读写超时秒数, 不表示整轮或完整流式生成限时"""
    context_window: Optional[int] = None
    """总上下文窗口, 与 CM max_context 同源"""
    history_ratio: Optional[float] = None
    """历史上下文比例, 输出预算 = context_window x (1 - history_ratio)"""
    context_strategy: str = "sliding"
    """上下文超限处理策略, 可选 sliding, mid_truncate 或 summarize"""
    context_threshold: float = 0.8
    """触发上下文处理的历史预算比例"""
    truncation_floor: float = 0.4
    """滑动窗口或截取策略处理后的目标历史预算比例"""
    summary_keep_recent_turns: int = 6
    """总结压缩时必须原样保留的最近对话轮数"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""
    allow_insecure_base_url: bool = False
    """是否显式允许非回环 HTTP API 地址"""
    thinking_field_name: Optional[str] = None
    """思考内容的字段名称"""
    thinking_fields: Optional[List[str]] = None
    """该模型需要的思考字段列表, 如 ["reasoning_effort", "thinking.type"]"""
    thinking_levels: Optional[List[str]] = None
    """该模型在前端开放的思考强度列表"""
    omit_none_thinking_fields: bool = False
    """关闭思考时是否省略值为 none 的思考字段"""

    supports_visual_input: bool = False
    """是否声明模型支持图片输入, 默认关闭"""
    supports_native_video_input: bool = False
    """是否声明当前 API 支持原生视频, 默认关闭"""
    video_frame_interval: float = 2.0
    """动画和兼容视频的抽帧间隔秒数, 范围 0.5 至 5.0"""
    api_type: Literal["chat_completions", "responses"] = "chat_completions"
    """模型请求协议, 不根据模型名称或错误自动切换"""
    native_video_transport: Literal["data_url", "url"] = "data_url"
    """原生视频使用固定字节的 Data URL 或当前会话登记的公网来源"""

    def __post_init__(self) -> None:
        """校验模型网络超时和视觉能力声明"""
        from satrap.core.utils.validation import positive_timeout
        if self.api_type not in ("chat_completions", "responses"):
            raise ValueError("api_type 必须是 chat_completions 或 responses")
        self.network_timeout = positive_timeout(self.network_timeout, "network_timeout")
        if self.thinking_fields is not None and "thinking_level" in self.thinking_fields:
            self.thinking_fields = [item for item in self.thinking_fields if item != "thinking_level"] or None
        if not isinstance(self.supports_visual_input, bool):
            raise ValueError("supports_visual_input 必须是布尔值")
        from satrap.core.utils.media_config import validate_video_settings
        validate_video_settings(self.supports_native_video_input, self.video_frame_interval, self.native_video_transport)


@dataclass
class EmbeddingConfig:
    """Embedding 配置数据结构"""
    name: Optional[str] = None
    """Embedding 名称"""
    model: Optional[str] = None
    """Embedding 模型"""
    base_url: Optional[str] = None
    """Embedding 基础 URL"""
    api_key: Optional[str] = None
    """Embedding API 密钥"""
    dimensions: Optional[int] = None
    """Embedding 维度"""
    max_batch_size: Optional[int] = None
    """Embedding 最大批量大小"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""
    allow_insecure_base_url: bool = False
    """是否显式允许非回环 HTTP API 地址"""

@dataclass
class ReRankConfig:
    """ReRank 配置数据结构"""
    name: Optional[str] = None
    """ReRank 名称"""
    model: Optional[str] = None
    """ReRank 模型"""
    base_url: Optional[str] = None
    """ReRank 服务的基础 URL, None 表示未配置"""
    api_key: Optional[str] = None
    """ReRank API 密钥"""
    top_k: Optional[int] = None
    """重排后返回的结果数量上限, None 表示未显式配置"""
    min_score: Optional[float] = None
    """ReRank 返回最小分数"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""
    allow_insecure_base_url: bool = False
    """是否显式允许非回环 HTTP API 地址"""

@dataclass
class ASRConfig:
    """ASR 配置数据结构"""
    name: Optional[str] = None
    """ASR 名称"""
    model: Optional[str] = None
    """ASR 模型"""
    base_url: Optional[str] = None
    """ASR 基础 URL"""
    api_key: Optional[str] = None
    """ASR API 密钥"""
    language: Optional[str] = None
    """默认识别语言, 为空由服务端自动检测"""
    prompt: Optional[str] = None
    """默认提示词, 按服务端能力透传"""
    timeout: Optional[int] = None
    """请求超时秒数, 为空使用客户端默认值"""
    lock_api_key: bool = True
    """是否锁定 API 密钥的获取以防止泄露"""
    allow_insecure_base_url: bool = False
    """是否显式允许非回环 HTTP API 地址"""

@dataclass
class ImgConfig:
    """图片模型配置, 默认参数仅保存 JSON 元数据"""

    name: Optional[str] = None
    """供配置管理引用的图片模型名称, None 表示未填写"""
    model: Optional[str] = None
    """发送给图片服务的模型标识, 创建客户端时必须提供"""
    base_url: Optional[str] = None
    """图片生成请求的完整地址, 创建客户端时必须提供"""
    edit_base_url: Optional[str] = None
    """可选的图片编辑完整地址, None 或空值时使用默认地址"""
    api_key: Optional[str] = None
    """图片服务认证密钥, 创建客户端时必须提供"""
    api_type: Literal["openai_images", "seedream", "novelai"] = "openai_images"
    """请求协议, 默认 openai_images, 也可选 seedream 或 novelai"""
    timeout: float = 180.0
    """图片客户端网络超时秒数, 默认 180, 必须为正有限数"""
    default_params: Optional[Dict[str, Any]] = None
    """可持久化的 JSON 默认参数, None 表示无配置, 不得包含图片或 Vibe 内容"""
    lock_api_key: bool = True
    """是否锁定密钥获取以防泄露, 默认开启"""
    allow_insecure_base_url: bool = False
    """是否允许非回环 HTTP 服务地址, 默认禁止"""

    def __post_init__(self) -> None:
        """创建和重载均验证配置, 不进行网络调用"""
        from satrap.core.APICall.ImageCall.config import validate_img_config

        validate_img_config(self)


@dataclass
class ASRResponse:
    """一次语音转录的类型化结果"""
    text: str
    """转录文本, 空字符串表示服务端确认无语音内容, 与失败区分"""
    model: str
    """实际请求使用的模型名"""
    language: str = ""
    """服务端返回的识别语言, 可能为空"""
    duration: float = 0.0
    """服务端返回的音频秒数, 0 表示未提供"""


MODEL_CONFIG_CLASSES: dict[str, type[LLMConfig] | type[EmbeddingConfig] | type[ReRankConfig] | type[ASRConfig] | type[ImgConfig]] = {
    "llm": LLMConfig, "embedding": EmbeddingConfig, "rerank": ReRankConfig, "asr": ASRConfig, "img": ImgConfig,
}
"""模型配置类型到数据类的映射, 读写分派和客户端工厂须同步适配"""
