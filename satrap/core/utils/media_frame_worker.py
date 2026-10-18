"""独立媒体解码进程, 只读父进程准备的本地文件并输出有界采样图片"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import TypedDict
import json
import math
import sys
from PIL import Image


MAX_FRAMES = 16
MAX_DECODE_FRAMES = 6000
MAX_DECODE_PIXELS = 2_000_000_000
MAX_OUTPUT_BYTES = 24 * 1024 * 1024
MAX_SIDE = 1600


class RangeRequired(ValueError):
    """当前密度无法装入单轮, 调用者应选择更小区间"""


class Frame(TypedDict):
    """单个采样画面的相对文件名和实际时间"""

    file: str
    time: float


class Result(TypedDict):
    """只有所有帧处理成功后才发布的采样元数据"""

    frames: list[Frame]
    duration: float
    start: float
    end: float
    interval: float
    kind: str


def extract(source: Path, output: Path, kind: str, interval: float, start: float, end: float | None) -> Result:
    """
    按播放时间采样并限制累计解码及输出工作量

    参数:
    - source: 父进程已固定的本地原媒体
    - output: 本次任务独占的临时目录
    - kind: animation 或 video
    - interval: 已校验的时间间隔
    - start: 起始秒数
    - end: 可选结束秒数, None 表示媒体结尾

    返回:
    - 带实际时间戳的完整采样结果, 超限或解码失败抛出异常
    """
    frames: list[Frame] = []
    pixels = size = decoded = 0
    next_time = start
    last_image: Image.Image | None = None
    last_time = -1.0

    def save(image: Image.Image, timestamp: float) -> None:
        """
        编码选中的画面并限制采样数及总字节数

        参数:
        - image: 已解码的完整播放画面
        - timestamp: 实际时间秒数
        """
        nonlocal size
        if frames and abs(timestamp - frames[-1]["time"]) < 0.0001:
            return
        if len(frames) >= MAX_FRAMES:
            raise RangeRequired(f"采样超过单次 {MAX_FRAMES} 帧, 请缩短起止时间区间; 不会自动增大间隔")
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        filename = f"frame-{len(frames):02d}.jpg"
        if "A" in image.getbands():
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            background.save(output / filename, format="JPEG", quality=82)
        else:
            image.convert("RGB").save(output / filename, format="JPEG", quality=82)
        size += (output / filename).stat().st_size
        if size > MAX_OUTPUT_BYTES:
            raise ValueError("抽帧图片超过 24 MiB, 请缩短读取区间")
        frames.append({"file": filename, "time": round(timestamp, 4)})

    def accept(image: Image.Image, timestamp: float, count: bool = True) -> None:
        """
        更新采样时间并只保存实际到达目标时间的帧

        参数:
        - image: 已解码的完整画面
        - timestamp: 相对媒体起点的实际秒数
        - count: 是否在此处累加解码工作量, 视频在转换前已经计数
        """
        nonlocal next_time, last_image, last_time, decoded, pixels
        decoded += int(count)
        pixels += image.width * image.height if count else 0
        if decoded > MAX_DECODE_FRAMES or pixels > MAX_DECODE_PIXELS or image.width * image.height > 40_000_000:
            raise ValueError("媒体解码工作量超限, 请缩短读取区间或降低源媒体分辨率")
        if timestamp < start:
            return
        last_image, last_time = image, timestamp
        if timestamp + 0.0001 >= next_time:
            save(image.copy(), timestamp)
            next_time = start + (math.floor((timestamp - start + 0.0001) / interval) + 1) * interval

    def check_range(duration: float) -> float:
        """
        根据真实时长检查区间和预期帧数

        参数:
        - duration: 原媒体真实时长

        返回:
        - 本次有效结束秒数, 超出单轮预算时要求分段读取
        """
        stop = duration if end is None else min(end, duration)
        if not math.isfinite(duration) or duration <= 0 or start >= stop:
            raise ValueError("读取时间区间超出媒体时长")
        expected = math.ceil((stop - start) / interval) + 1
        if expected > MAX_FRAMES:
            raise RangeRequired(f"媒体时长 {duration:.2f} 秒, 当前区间预计 {expected} 帧, 单次最多 {MAX_FRAMES} 帧; 请指定更短的 start_time/end_time")
        return stop

    if kind == "animation":
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as animation:
                count = getattr(animation, "n_frames", 1)
                if count > MAX_DECODE_FRAMES:
                    raise ValueError("动画帧数超过解码限制")
                first_index = 1 if getattr(animation, "default_image", False) else 0
                duration = 0.0
                durations: list[float] = []
                for index in range(first_index, count):
                    animation.seek(index)
                    if animation.format == "WEBP":
                        animation.load()   # WebP 的 duration 只有解码当前帧后才更新
                        decoded += 1
                        pixels += animation.width * animation.height
                        if decoded > MAX_DECODE_FRAMES or pixels > MAX_DECODE_PIXELS:
                            raise ValueError("动画时间线解析工作量超限, 请缩短源动画或降低分辨率")
                    delay = max(0.01, float(animation.info.get("duration", 100)) / 1000)
                    durations.append(delay)
                    duration += delay
                stop = check_range(duration)
                timestamp = 0.0
                for index, delay in enumerate(durations, start=first_index):
                    animation.seek(index)
                    if timestamp >= stop:
                        break
                    if timestamp + delay > start:
                        accept(animation.convert("RGBA"), max(start, timestamp))
                        # 区间起点可能落在当前帧的停留时间内, 此时仍应读取该播放画面
                    timestamp += delay
    else:
        try:
            import av
            from av.logging import set_level, PANIC
            set_level(PANIC)
        except ImportError as error:
            raise ValueError('缺少视频解码依赖, 请在当前 Python 环境运行: python -m pip install -e ".[video]"') from error
        with source.open("rb") as stream_file:
            header = stream_file.read(32)
        if header[4:8] == b"ftyp":
            container_format = "mov"
        elif header.startswith(b"\x1aE\xdf\xa3"):
            container_format = "matroska"
        elif header[:4] == b"RIFF" and header[8:12] == b"AVI ":
            container_format = "avi"
        else:
            raise ValueError("无法识别视频容器, 仅支持 MP4/MOV/WebM/MKV/AVI, 不读取播放列表或外部文件")
        with av.open(str(source), format=container_format, options={"protocol_whitelist": "file,pipe"}) as container:
            if not container.streams.video:
                raise ValueError("文件不包含视频画面")
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1   # 每个 worker 限制解码线程, 避免并发任务各自占满全部 CPU
            time_base = stream.time_base
            if time_base is None:
                raise ValueError("视频缺少可用时间基准, 无法按秒采样")
            duration = float(stream.duration * time_base) if stream.duration is not None else float(container.duration or 0) / av.time_base
            stop = check_range(duration)
            offset = float((stream.start_time or 0) * time_base)
            if start > 0:
                container.seek(int((start + offset) * av.time_base), backward=True)
            last_video_frame = None
            for frame in container.decode(stream):
                if frame.time is None:
                    raise ValueError("视频画面缺少时间戳, 无法按秒采样")
                timestamp = float(frame.time) - offset
                if timestamp >= stop:
                    break
                decoded += 1
                pixels += frame.width * frame.height
                if decoded > MAX_DECODE_FRAMES or pixels > MAX_DECODE_PIXELS or frame.width * frame.height > 40_000_000:
                    raise ValueError("视频解码工作量超限, 请缩短读取区间")
                if timestamp >= start:
                    last_video_frame = frame
                    if timestamp + 0.0001 >= next_time:
                        accept(frame.to_image(), timestamp, False)
            if last_video_frame is not None:
                accept(last_video_frame.to_image(), float(last_video_frame.time or 0) - offset, False)
    if last_image is not None:
        save(last_image, last_time)
    if not frames:
        raise ValueError("指定区间没有可读取的画面")
    return {"frames": frames, "duration": duration, "start": start, "end": stop, "interval": interval, "kind": kind}


def main() -> None:
    """读取单个 JSON 请求并返回完整结果, 不输出解码器的内部堆栈"""
    try:
        request = json.load(sys.stdin)
        result = extract(Path(request["source"]), Path(request["output"]), request["kind"],
                         request["interval"], request["start"], request.get("end"))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error), "code": type(error).__name__}, ensure_ascii=False))


if __name__ == "__main__":
    main()
