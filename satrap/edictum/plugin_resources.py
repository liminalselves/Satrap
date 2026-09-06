"""插件模型依赖: 命名配置校验, 客户端构造和独立生命周期"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import inspect
from typing import Any
import json

from satrap.core.framework.BackGroundManager import ConfigTarget, ModelConfigManager
from satrap.core.APICall.ReRankCall import AsyncReRank, ReRank
from satrap.core.APICall.EmbedCall import AsyncEmbedding, Embedding
from satrap.edictum.plugin_config import ConfigField
from satrap.core.APICall.LLMCall import build_llm_from_config

from satrap.core.log import logger


MODEL_TYPES: dict[str, ConfigTarget] = {"llm": "llm", "embed": "embedding", "rerank": "rerank"}


def named_model_config(manager: ModelConfigManager, kind: str, name: str) -> Any:
    """严格查找模型引用, 不使用配置管理器的缺失名称空对象兜底"""
    target = "embedding" if kind == "embedding" else MODEL_TYPES.get(kind)
    if target is None:
        raise ValueError(f"未知模型类型: {kind}")
    if not manager.has_config(target, name):
        raise ValueError(f"模型配置不存在: {target}/{name}")
    config = getattr(manager, f"get_{target}_config")(name)
    if not config.model or not config.api_key:
        raise ValueError(f"模型配置缺少 model 或 api_key: {target}/{name}")
    if target == "rerank" and not config.base_url:
        raise ValueError(f"重排模型配置缺少 base_url: {name}")
    return config


def build_model_client(kind: str, config: Any, *, async_: bool = False) -> Any:
    """完整传递后端声明参数, 模型调用错误由工具显式处理"""
    if kind == "llm":
        return build_llm_from_config(config, async_=async_)
    values = {key: value for key, value in asdict(config).items() if key != "name" and value is not None}
    if kind in {"embed", "embedding"}:
        values["suppress_error"] = False
        return (AsyncEmbedding if async_ else Embedding)(**values)
    if kind == "rerank":
        return (AsyncReRank if async_ else ReRank)(**values)
    raise ValueError(f"未知模型类型: {kind}")


class PluginResources:
    """每次插件安装独享客户端, config 保持为纯 JSON 数据"""

    def __init__(
        self, manager: ModelConfigManager | None, schema: dict[str, ConfigField], config: dict[str, Any], *, async_: bool,
    ) -> None:
        self.async_ = async_
        self._configs: dict[str, tuple[str, Any]] = {}
        self._clients: dict[str, Any] = {}
        for key, definition in schema.items():
            value = config.get(key)
            if definition.required and (value is None or value == "" or value == []):
                raise ValueError(f"插件必填配置未设置: {key}")
            if definition.type in MODEL_TYPES and value:
                if manager is None:
                    raise ValueError("插件模型依赖需要后端模型配置服务")
                self._configs[key] = (definition.type, named_model_config(manager, definition.type, value))

    def get(self, key: str, default: Any = None) -> Any:
        """首次使用才构造客户端, 安装预校验不发送网络请求"""
        if key not in self._configs:
            return default
        if key not in self._clients:
            kind, config = self._configs[key]
            self._clients[key] = build_model_client(kind, config, async_=self.async_)
        return self._clients[key]

    def __getitem__(self, key: str) -> Any:
        if key not in self._configs:
            raise KeyError(key)
        return self.get(key)

    def close(self) -> None:
        """关闭同步客户端, 调用方必须使用匹配的生命周期"""
        clients, self._clients = self._clients, {}
        for client in clients.values():
            target = getattr(client, "client", client)
            close = getattr(target, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.warning("插件模型客户端关闭失败")

    async def aclose(self) -> None:
        """关闭异步或同步客户端"""
        clients, self._clients = self._clients, {}
        for client in clients.values():
            target = getattr(client, "client", client)
            close = getattr(target, "aclose", None) or getattr(target, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.warning("插件模型客户端关闭失败")


def model_reference_fingerprint(manager: ModelConfigManager, schema: dict[str, ConfigField], config: dict[str, Any]) -> str:
    """计算引用的内部版本, 不把凭据放入插件配置或对外响应"""
    payload = {}
    for key, definition in schema.items():
        name = config.get(key)
        if definition.type not in MODEL_TYPES or not name:
            continue
        target = MODEL_TYPES[definition.type]
        payload[key] = asdict(getattr(manager, f"get_{target}_config")(name)) if manager.has_config(target, name) else {"missing": name}
    if not payload:
        return ""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
