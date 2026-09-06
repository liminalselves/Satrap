"""浏览器上传回归使用真实 HTTP 和存储, 仅替换模型调用"""
from pathlib import Path
import asyncio
from types import SimpleNamespace
import json
import sys

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.server_auth import ServerAuth
from satrap.core.backend import control_server
from satrap.core.storage import StorageLayout
from satrap.core.type import EmbeddingConfig
from satrap.core import rag

failed_once = False


class OfflineEmbedding:
    """确定性三维向量替身"""

    def embed(self, value: str | list[str]) -> list[float] | list[list[float]]:
        """
        返回与输入数量一致的向量

        参数:
        - value: 问题或文档片段

        返回:
        - 单条或批量三维向量
        """
        global failed_once
        if isinstance(value, list) and any("FAIL_ONCE" in text for text in value) and not failed_once:
            failed_once = True
            raise RuntimeError("Embedding 服务返回 429: quota exceeded")
        return [[1.0, 0.0, 0.0] for _ in value] if isinstance(value, list) else [1.0, 0.0, 0.0]


async def main() -> None:
    """标准输入关闭时停止测试服务"""
    root = Path(sys.argv[1])
    layout = StorageLayout(root / "data")
    models = ModelConfigManager(root / "models.json", auto_create=False)
    models.set_embedding_config(EmbeddingConfig(model="offline", api_key="test", dimensions=3), "embed")
    rag.build_model_client = lambda *args, **kwargs: OfflineEmbedding()
    service = rag.RagService(layout, models, "local")
    service.create("上传验证知识库", "global", {"embed": "embed", "chunk_size": 100000, "chunk_overlap": 0})
    control_server._configured_storage_layout = lambda: layout
    control_server._model_config_service = lambda: SimpleNamespace(manager=models)
    control_server._configured_platform_ids = lambda: []
    control_server._CONTROL_AUTH = ServerAuth.create("127.0.0.1", 0, token=sys.argv[3], allowed_origins=frozenset({sys.argv[2]}))
    server = await asyncio.start_server(control_server._handle_request, "127.0.0.1", 0)
    print("READY " + json.dumps({"port": server.sockets[0].getsockname()[1]}), flush=True)
    try:
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
