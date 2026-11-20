"""流程工作流与插件节点的模型资源计量包装"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
import inspect

if TYPE_CHECKING:
    from satrap.core.type.flow.definition import FlowNodeInstance
    from satrap.edictum.flow.session import AsyncFlowSession


class MeteredClient:
    """模型客户端计量代理: 每次 call/stream_call 前经回调扣减预算"""

    _METERED_METHODS = ("call", "stream_call", "chat", "generate", "embed",
                        "rerank", "transcribe", "invoke", "image", "images")

    def __init__(self, inner: Any, on_call: Any) -> None:
        self._inner = inner
        self._on_call = on_call

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in self._METERED_METHODS or not callable(attr):
            return attr
        if inspect.isasyncgenfunction(attr):
            async def gen(*args: Any, **kwargs: Any) -> Any:
                self._on_call()
                async for item in attr(*args, **kwargs):
                    yield item
            return gen
        if inspect.iscoroutinefunction(attr):
            async def acall(*args: Any, **kwargs: Any) -> Any:
                self._on_call()
                return await attr(*args, **kwargs)
            return acall
        return attr

    def __repr__(self) -> str:
        return f"<Metered {self._inner!r}>"


class NodeResources:
    """插件节点 ctx.resources: 按节点配置字段惰性构造并包装计量"""

    def __init__(self, session: AsyncFlowSession, node: FlowNodeInstance,
                 schema: dict[str, Any], resolved: dict[str, Any]) -> None:
        self._session = session
        self._node = node
        self._schema = schema
        self._config = resolved
        self._clients: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        field = self._schema.get(key)
        if field is None or field.type not in ("llm", "embed", "rerank", "asr", "img"):
            return default
        name = self._config.get(key)
        if not name:
            return default
        client = self._clients.get(key)
        if client is None:
            from satrap.edictum.plugin.resources import named_model_config, build_model_client
            config = named_model_config(self._session._model_reader(), field.type, str(name))
            raw = build_model_client(field.type, config, async_=True)
            node_id = self._node.id
            client = MeteredClient(raw, lambda: self._session._spend("plugin", node_id))
            self._clients[key] = client
        return client

    def __getitem__(self, key: str) -> Any:
        client = self.get(key)
        if client is None:
            raise KeyError(key)
        return client
