"""ASR 音频输入校验与转录响应解析"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from satrap.core.type import ASRResponse

MAX_AUDIO_BYTES = 25 * 1024 * 1024
"""单个音频输入的最大字节数, 与 OpenAI 转录接口上限一致"""

ALLOWED_AUDIO_SUFFIXES = frozenset({
    ".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".oga", ".ogg", ".wav", ".webm",
})
"""OpenAI 兼容转录接口声明支持的音频扩展名"""

_MIME_BY_SUFFIX = {
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}


def prepare_audio_input(audio: str | Path | bytes | bytearray, *, filename: str | None = None) -> tuple[str, bytes, str]:
    """
    归一化音频输入为 (文件名, 字节, MIME) 三元组

    本地路径必须是已存在的普通文件, 字节输入必须显式给出文件名;
    扩展名不在接口支持范围或超过大小上限时直接拒绝

    参数:
    - audio: 受控本地文件路径或音频字节
    - filename: 字节输入的文件名, 路径输入时忽略

    返回:
    - tuple[str, bytes, str]: (文件名, 字节内容, MIME 类型)
    """
    if isinstance(audio, (str, Path)):
        path = Path(audio)
        if not path.is_file():
            raise ValueError("音频文件不存在或不是普通文件")
        name = path.name
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError("音频文件超过大小上限")
        data = path.read_bytes()
    elif isinstance(audio, (bytes, bytearray)):
        if not filename or not filename.strip():
            raise ValueError("字节输入必须提供文件名")
        name = Path(filename.strip()).name
        if not name or name != filename.strip():
            raise ValueError("文件名不能包含路径成分")
        data = bytes(audio)
        if len(data) > MAX_AUDIO_BYTES:
            raise ValueError("音频数据超过大小上限")
    else:
        raise ValueError("audio 必须是本地路径或字节输入")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_AUDIO_SUFFIXES:
        raise ValueError(f"不支持的音频格式: {suffix or '无扩展名'}")
    if not data:
        raise ValueError("音频内容为空")
    return name, data, _MIME_BY_SUFFIX[suffix]


def _response_field(response: Any, name: str) -> Any:
    """兼容 SDK 响应对象与字典两种形态读取字段"""
    if isinstance(response, dict):
        return cast(dict[str, Any], response).get(name)
    return getattr(response, name, None)


def parse_transcription(response: Any, model: str) -> ASRResponse:
    """
    从转录响应提取文本与可选元信息

    参数:
    - response: SDK 响应对象或字典
    - model: 实际请求使用的模型名

    返回:
    - ASRResponse: 空文本保持合法, 与失败返回 None 区分
    """
    text = _response_field(response, "text")
    language = _response_field(response, "language")
    duration = _response_field(response, "duration")
    return ASRResponse(
        text=str(text) if isinstance(text, str) else "",
        model=model,
        language=str(language) if isinstance(language, str) else "",
        duration=float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 0.0,
    )
