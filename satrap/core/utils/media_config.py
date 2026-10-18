"""模型视频能力及抽帧参数的共用校验"""
from __future__ import annotations

import math


def validate_video_settings(native: object, interval: object) -> None:
    """
    拒绝错误的原生视频声明与抽帧间隔

    参数:
    - native: 必须为布尔值的原生视频能力
    - interval: 0.5 至 5.0 秒之间的有限数字
    """
    if type(native) is not bool:
        raise ValueError("supports_native_video_input 必须是布尔值")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0.5 <= interval <= 5:
        raise ValueError("video_frame_interval 必须是 0.5 至 5.0 秒之间的有限数字")
