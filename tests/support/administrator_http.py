"""业务场景共享夹具与隔离构造器"""
from __future__ import annotations

import asyncio
import json

def document():
    return {"platforms": [{"id": "ob", "type": "onebot", "instance_id": "a" * 32}], "extra": "保留原字段"}

def context(control, method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    headers = f"{method} {path} HTTP/1.1\r\nContent-Length: {len(data)}\r\n\r\n".encode()
    return control._RouteContext(method, path, path, reader, headers)
