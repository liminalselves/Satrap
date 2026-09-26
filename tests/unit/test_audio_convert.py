"""语音字节编码探测与本地 wav 转码"""
from typing import Any
import io
import math
import struct

import pytest

from satrap.core.pipeline import audio_convert as module
from satrap.core.pipeline.audio_convert import AudioConvertError, AudioTooLong, convert_to_wav, probe_audio, probe_duration


def _wav_bytes(seconds: float = 0.5, rate: int = 8000) -> bytes:
    frames = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames))
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


def _amr_bytes(seconds: float = 0.5) -> bytes:
    av: Any = pytest.importorskip("av")
    output = io.BytesIO()
    with av.open(io.BytesIO(_wav_bytes(seconds))) as source, av.open(output, "w", format="amr") as sink:
        stream = sink.add_stream("libopencore_amrnb", rate=8000, layout="mono")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
        for frame in source.decode(audio=0):
            for resampled in resampler.resample(frame):
                for packet in stream.encode(resampled):
                    sink.mux(packet)
        for packet in stream.encode(None):
            sink.mux(packet)
    return output.getvalue()


@pytest.mark.parametrize("payload, suffix, codec, accepted", [
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", ".amr", "wav", True),
    (b"OggS\x00\x02" + b"\x00" * 10, "", "ogg", True),
    (b"ID3\x04\x00" + b"\x00" * 10, "", "mp3", True),
    (b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 4, "", "m4a", True),
    (b"#!AMR\n\x3c\x48", ".amr", "amr", False),
    (b"#!AMR-WB\n\x44", "", "amr", False),
    (b"\x00" * 12, ".mp3", "mp3", True),
    (b"\x00" * 12, ".amr", "amr", False),
])
def test_probe_identifies_codec_by_magic_then_suffix(payload: bytes, suffix: str, codec: str, accepted: bool):
    probe = probe_audio(payload, suffix)
    assert (probe.codec, probe.accepted) == (codec, accepted)


@pytest.mark.parametrize("payload", [b"#!SILK_V3" + b"\x00" * 7, b"\x02#!SILK_V3" + b"\x00" * 6, b"\x03#!SILK_V3" + b"\x00" * 6])
def test_probe_flags_silk_for_platform_transcode(payload: bytes):
    # QQ 语音以 .amr 命名但内容是 SILK, 文件头优先于扩展名
    probe = probe_audio(payload, ".amr")
    assert probe == ("silk", False, False, "silk_needs_platform_transcode")


def test_probe_unknown_and_missing_converter(monkeypatch: pytest.MonkeyPatch):
    assert probe_audio(b"\x00" * 16, ".bin").reason == "unknown_codec"
    monkeypatch.setitem(module._AV_STATE, "module", None)
    probe = probe_audio(b"#!AMR\n" + b"\x00" * 10, "")
    assert probe.codec == "amr" and not probe.convertible and probe.reason == "av_missing"
    with pytest.raises(AudioConvertError, match="av_missing"):
        convert_to_wav(b"#!AMR\n", max_seconds=10)


def test_amr_converts_to_16k_mono_wav():
    wav = convert_to_wav(_amr_bytes(), max_seconds=10)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    channels, rate = struct.unpack("<HI", wav[22:28])
    assert (channels, rate) == (1, 16000)
    assert probe_audio(wav).accepted


def test_conversion_rejects_overlong_and_garbage():
    with pytest.raises(AudioTooLong):
        convert_to_wav(_amr_bytes(seconds=1.0), max_seconds=0.2)
    with pytest.raises(AudioConvertError):
        convert_to_wav(b"#!AMR\n" + b"\xff" * 64, max_seconds=10)


def test_probe_duration_reads_wav_and_tolerates_garbage():
    short_duration = probe_duration(_wav_bytes(2.0), "wav")
    if short_duration is None:
        raise AssertionError("wav 时长应可探测")
    assert 1.99 < short_duration < 2.01
    long_duration = probe_duration(_wav_bytes(301.0), "wav")
    if long_duration is None:
        raise AssertionError("wav 时长应可探测")
    assert long_duration > 300
    assert probe_duration(b"not-a-wav", "wav") is None
    assert probe_duration(b"OggS" + bytes(64), "ogg") is None
