"""仅在临时目录写验收日志, 用屏障定位被强制结束的步骤"""
import asyncio
import json
import os
from pathlib import Path

from satrap.core.utils.TCBuilder import AsyncTool


class ProbeRead(AsyncTool):
    tool_name = "probe_read"
    description = "读取验收资料"
    params_dict = {"index": ("number", "资料序号")}
    recovery_policy = "retry"

    async def execute(self, index=1):
        root = Path(os.environ["RECOVERY_PROBE_ROOT"])
        with (root / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"tool": "read", "index": index}) + "\n")
        if os.environ["RECOVERY_PROBE_SCENARIO"] == "read" and index == 2 and not (root / "released").exists():
            (root / "blocked").write_text("read", encoding="utf-8")
            await asyncio.Event().wait()
        return f"资料{index}"


class ProbeWrite(AsyncTool):
    tool_name = "probe_write"
    description = "追加一条验收写入记录"
    params_dict = {}

    async def execute(self):
        root = Path(os.environ["RECOVERY_PROBE_ROOT"])
        with (root / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"tool": "write"}) + "\n")
        if os.environ["RECOVERY_PROBE_SCENARIO"] == "manual" and not (root / "released").exists():
            (root / "blocked").write_text("write", encoding="utf-8")
            await asyncio.Event().wait()
        return "写入完成"
