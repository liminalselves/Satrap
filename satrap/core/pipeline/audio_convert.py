"""
语音字节的编码探测与本地 wav 转码

按文件头识别编码, 只有 ASR 接口不直接接受且 ffmpeg 可解码的内容才在本地转码;
SILK 裸流需由 OneBot 实现服务端转码 (get_record out_format), 此处只识别不解码
"""
from __future__ import annotations

from typing import Any, NamedTuple
import importlib
import io

from satrap.core.APICall.ASRCall.utils import ALLOWED_AUDIO_SUFFIXES

CONVERT_SAMPLE_RATE = 16000
"""转码输出采样率, 绝大多数转写接口的推荐输入"""
_SILK_SIGNATURE = b"#!SILK_V3"
_MAGIC_CODECS: tuple[tuple[bytes, str], ...] = (
    (b"#!AMR-WB\n", "amr"),
    (b"#!AMR\n", "amr"),
    (b"RIFF", "wav"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
    (b"\x1a\x45\xdf\xa3", "webm"),
)
_ACCEPTED_CODECS = frozenset({"wav", "ogg", "flac", "mp3", "webm", "m4a"})
"""探测到即可直接送 ASR 的编码 (与 ALLOWED_AUDIO_SUFFIXES 对应)"""
_CONVERTIBLE_CODECS = frozenset({"amr"})
"""需要且能够由 ffmpeg 在本地转码的编码"""


class AudioProbe(NamedTuple):
    """一次语音字节探测的结论"""

    codec: str
    """识别出的编码: amr, silk, wav, ogg, flac, mp3, webm, m4a 或 unknown"""
    accepted: bool
    """是否可以不转码直接送 ASR"""
    convertible: bool
    """当前环境能否在本地转为 wav"""
    reason: str
    """不可直接使用且不可转码时的原因键"""


class AudioConvertError(RuntimeError):
    """本地转码失败或输入不受支持"""


class AudioTooLong(AudioConvertError):
    """解码得到的时长超过预算"""


_AV_STATE: dict[str, Any] = {}


def _load_av() -> Any | None:
    """
    惰性导入 PyAV, 结果缓存

    返回:
    - 模块或 None (未安装)
    """
    if "module" not in _AV_STATE:
        try:
            _AV_STATE["module"] = importlib.import_module("av")
        except ImportError:
            _AV_STATE["module"] = None
    return _AV_STATE["module"]


def converter_available() -> bool:
    """
    本地转码器是否可用

    返回:
    - bool: PyAV 可导入时为 True
    """
    return _load_av() is not None


def _codec_from_suffix(suffix: str) -> str:
    """
    从扩展名推断编码, 只作为无法识别文件头时的兜底

    参数:
    - suffix: 含点的小写扩展名

    返回:
    - str: 编码名或 unknown
    """
    if suffix in {".m4a", ".mp4"}:
        return "m4a"
    if suffix in {".mpga", ".mpeg"}:
        return "mp3"
    if suffix == ".oga":
        return "ogg"
    if suffix in ALLOWED_AUDIO_SUFFIXES:
        return suffix.lstrip(".")
    if suffix in {".amr", ".awb"}:
        return "amr"
    if suffix in {".silk", ".slk"}:
        return "silk"
    return "unknown"


def probe_audio(data: bytes, suffix: str = "") -> AudioProbe:
    """
    识别语音字节的编码并判断处理方式

    参数:
    - data: 语音字节 (至少前 16 字节有效)
    - suffix: 平台上报的扩展名, 文件头无法识别时兜底; QQ 语音常以 .amr 命名但内容为 SILK

    返回:
    - AudioProbe: 编码与处理结论
    """
    head = data[:16]
    codec = "unknown"
    if _SILK_SIGNATURE in head[:12]:
        codec = "silk"
    else:
        for magic, name in _MAGIC_CODECS:
            if head.startswith(magic):
                codec = name
                break
        if codec == "unknown" and head[4:8] == b"ftyp":
            codec = "m4a"
    if codec == "unknown":
        codec = _codec_from_suffix(suffix.lower())
    if codec in _ACCEPTED_CODECS:
        return AudioProbe(codec, True, False, "")
    if codec == "silk":
        return AudioProbe(codec, False, False, "silk_needs_platform_transcode")
    if codec in _CONVERTIBLE_CODECS:
        available = converter_available()
        return AudioProbe(codec, False, available, "" if available else "av_missing")
    return AudioProbe(codec, False, False, "unknown_codec")


def convert_to_wav(data: bytes, *, max_seconds: float) -> bytes:
    """
    用 PyAV 解码任意 ffmpeg 支持的音频并重采样为 16 kHz 单声道 wav

    参数:
    - data: 原始音频字节
    - max_seconds: 允许的最大时长, 超出抛 AudioTooLong

    返回:
    - bytes: wav 文件字节; 解码失败抛 AudioConvertError
    """
    av = _load_av()
    if av is None:
        raise AudioConvertError("av_missing")
    max_samples = int(max_seconds * CONVERT_SAMPLE_RATE)
    output = io.BytesIO()
    produced = 0
    try:
        with av.open(io.BytesIO(data)) as source, av.open(output, "w", format="wav") as sink:
            stream = sink.add_stream("pcm_s16le", rate=CONVERT_SAMPLE_RATE, layout="mono")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=CONVERT_SAMPLE_RATE)
            for frame in source.decode(audio=0):
                for resampled in resampler.resample(frame):
                    produced += resampled.samples
                    if produced > max_samples:
                        raise AudioTooLong(f"audio_longer_than_{int(max_seconds)}s")
                    for packet in stream.encode(resampled):
                        sink.mux(packet)
            for packet in stream.encode(None):
                sink.mux(packet)
    except AudioConvertError:
        raise
    except Exception as error:
        raise AudioConvertError(type(error).__name__) from error
    return output.getvalue()
