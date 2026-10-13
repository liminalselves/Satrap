"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

from io import BytesIO
from PIL import Image

def png() -> bytes:
    """生成实际可解码的测试图片"""
    stream = BytesIO()
    Image.new("RGB", (16, 8), "red").save(stream, format="PNG")
    return stream.getvalue()
