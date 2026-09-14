"""启动真实 Chat HTTP 服务, 用离线模型精确控制强制结束位置"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.storage import StorageLayout
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent, LLMConfig
from satrap.display import service as service_module, plugins as plugins_module
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.server import ChatHTTPServer
from satrap.display.service import ChatService
from satrap.edictum import plugin_config


class ProbeModel(AsyncLLM):
    model = "recovery-probe"

    def __init__(self):
        pass

    def set_parameters(self, *args, **kwargs):
        pass

    async def stream_call(self, messages, *args, **kwargs):
        root = Path(os.environ["RECOVERY_PROBE_ROOT"])
        user_index = max(i for i, item in enumerate(messages) if item["role"] == "user")
        count = sum(item["role"] == "tool" for item in messages[user_index:])
        with (root / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"model": count, "messages": messages}, ensure_ascii=False) + "\n")
        if count == 2 and os.environ["RECOVERY_PROBE_SCENARIO"] == "model" and not (root / "released").exists():
            yield LLMCallStreamEvent("content_delta", delta="尚未完成的流式内容")
            (root / "blocked").write_text("model", encoding="utf-8")
            await asyncio.Event().wait()
        if count == 0:
            response = LLMCallResponse("tools_call", "", tool_calls=[
                {"id": "read_1", "name": "probe_read", "arguments": {"index": 1}},
                {"id": "write_1", "name": "probe_write", "arguments": {}},
            ])
        elif count == 2:
            response = LLMCallResponse("tools_call", "", tool_calls=[
                {"id": "read_2", "name": "probe_read", "arguments": {"index": 2}},
            ])
        else:
            response = LLMCallResponse("message", "验收完成")
            yield LLMCallStreamEvent("content_delta", delta=response.content)
        yield LLMCallStreamEvent("done", response=response)


async def main():
    root = Path(os.environ["RECOVERY_PROBE_ROOT"])
    plugins_module.PLUGINS_PRESET_DIR = root / "plugins"
    plugins_module.USER_PLUGINS_DIR = root / "empty-plugins"
    plugin_config.CONFIG_DIR = root / "plugin-config"
    service_module.build_llm = lambda cfg: ProbeModel()
    models = ModelConfigManager(root / "models.json")
    if not models.list_llm_configs():
        models.set_llm_config(LLMConfig(model="recovery-probe", api_key="offline", base_url="http://127.0.0.1:1"), "default")
    registry = ChatPluginRegistry(state_path=root / "plugins.json")
    registry.set_enabled("recovery_probe", True)
    service = ChatService(models, registry, storage_layout=StorageLayout(root / "data"), workspace_roots=[root])
    server = ChatHTTPServer(service, port=0)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    Path(sys.argv[1]).write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
