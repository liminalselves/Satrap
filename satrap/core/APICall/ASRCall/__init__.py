"""OpenAI 兼容语音转录入口, 保留同步异步类与工厂函数"""

from typing import Any

from satrap.core.type import ASRConfig, ASRResponse, safe_getattr, safe_getattr_str
from .async_ import AsyncASR
from .sync import ASR
from .utils import ALLOWED_AUDIO_SUFFIXES, MAX_AUDIO_BYTES

__all__ = [
    "ASR",
    "AsyncASR",
    "ASRResponse",
    "build_asr_from_config",
    "ALLOWED_AUDIO_SUFFIXES",
    "MAX_AUDIO_BYTES",
]


def build_asr_from_config(cfg: ASRConfig, *, async_: bool = False) -> "ASR | AsyncASR":
    """
    由 ASRConfig 统一构造 ASR / AsyncASR (字段映射)

    用 safe_getattr 兼容测试替身 (SimpleNamespace 可能缺字段);
    可选字段仅非 None 时透传, 避免覆盖类默认值

    参数:
    - cfg: ASRConfig (或含同名字段的替身对象)
    - async_: True 返回 AsyncASR, False 返回 ASR

    返回:
    - 'ASR | AsyncASR': 同步或异步转录客户端
    """
    cls = AsyncASR if async_ else ASR
    kwargs: dict[str, Any] = {
        "api_key": safe_getattr_str(cfg, "api_key"),
        "base_url": safe_getattr_str(cfg, "base_url"),
        "model": safe_getattr_str(cfg, "model"),
        "lock_api_key": safe_getattr(cfg, "lock_api_key", True),
        "allow_insecure_base_url": safe_getattr(cfg, "allow_insecure_base_url", False),
    }
    for name in ("language", "prompt", "timeout"):
        val = safe_getattr(cfg, name)
        if val is not None:
            kwargs[name] = val
    return cls(**kwargs)
