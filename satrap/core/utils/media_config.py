"""模型视频能力及抽帧参数的共用校验"""
from __future__ import annotations

import math


def validate_video_settings(native: object, interval: object, transport: object = "data_url") -> None:
    """
    拒绝错误的原生视频声明与抽帧间隔

    参数:
    - native: 必须为布尔值的原生视频能力
    - interval: 0.5 至 5.0 秒之间的有限数字
    - transport: data_url 或 url, 默认 data_url
    """
    if type(native) is not bool:
        raise ValueError("supports_native_video_input 必须是布尔值")
    if transport not in ("data_url", "url"):
        raise ValueError("native_video_transport 必须为 data_url 或 url")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0.5 <= interval <= 5:
        raise ValueError("video_frame_interval 必须是 0.5 至 5.0 秒之间的有限数字")
