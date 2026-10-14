"""插件模型依赖: 命名配置校验, 客户端构造和独立生命周期"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import inspect
from typing import Any, Literal, TypeVar, overload
import json

from satrap.core.framework.BackGroundManager import ConfigTarget
from satrap.core.config.model_snapshot import ModelConfigReader, ModelConfigSnapshot
from satrap.core.type import ASRConfig, EmbeddingConfig, LLMConfig, ReRankConfig
from satrap.core.APICall.ReRankCall import AsyncReRank, ReRank
from satrap.core.APICall.EmbedCall import AsyncEmbedding, Embedding
from satrap.core.APICall.ASRCall import ASR, AsyncASR, build_asr_from_config
from satrap.edictum.plugin_config import ConfigField
from satrap.core.APICall.LLMCall import AsyncLLM, LLM, build_llm_from_config

from satrap.core.log import logger

T = TypeVar("T")

ModelConfig = LLMConfig | EmbeddingConfig | ReRankConfig | ASRConfig
"""已注册的命名模型配置类型"""
ModelClient = LLM | AsyncLLM | Embedding | AsyncEmbedding | ReRank | AsyncReRank | ASR | AsyncASR
"""插件可取得的模型客户端类型"""


MODEL_TYPES: dict[str, ConfigTarget] = {"llm": "llm", "embed": "embedding", "rerank": "rerank", "asr": "asr"}


@overload
def named_model_config(manager: ModelConfigReader, kind: Literal["llm"], name: str) -> LLMConfig: ...
@overload
def named_model_config(manager: ModelConfigReader, kind: Literal["embed", "embedding"], name: str) -> EmbeddingConfig: ...
@overload
def named_model_config(manager: ModelConfigReader, kind: Literal["rerank"], name: str) -> ReRankConfig: ...
@overload
def named_model_config(manager: ModelConfigReader, kind: Literal["asr"], name: str) -> ASRConfig: ...
@overload
def named_model_config(manager: ModelConfigReader, kind: str, name: str) -> ModelConfig: ...
def named_model_config(manager: ModelConfigReader, kind: str, name: str) -> ModelConfig:
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


@overload
def build_model_client(kind: Literal["llm"], config: LLMConfig, *, async_: Literal[False] = False) -> LLM: ...
@overload
def build_model_client(kind: Literal["llm"], config: LLMConfig, *, async_: Literal[True]) -> AsyncLLM: ...
@overload
def build_model_client(kind: Literal["embed", "embedding"], config: EmbeddingConfig, *, async_: Literal[False] = False) -> Embedding: ...
@overload
def build_model_client(kind: Literal["embed", "embedding"], config: EmbeddingConfig, *, async_: Literal[True]) -> AsyncEmbedding: ...
@overload
def build_model_client(kind: Literal["rerank"], config: ReRankConfig, *, async_: Literal[False] = False) -> ReRank: ...
@overload
def build_model_client(kind: Literal["rerank"], config: ReRankConfig, *, async_: Literal[True]) -> AsyncReRank: ...
@overload
def build_model_client(kind: Literal["asr"], config: ASRConfig, *, async_: Literal[False] = False) -> ASR: ...
@overload
def build_model_client(kind: Literal["asr"], config: ASRConfig, *, async_: Literal[True]) -> AsyncASR: ...
@overload
def build_model_client(kind: str, config: ModelConfig, *, async_: bool = False) -> ModelClient: ...
def build_model_client(kind: str, config: ModelConfig, *, async_: bool = False) -> ModelClient:
    """完整传递后端声明参数, 模型调用错误由工具显式处理"""
    if kind == "llm":
        if not isinstance(config, LLMConfig):
            raise ValueError(f"模型配置类型与 kind 不符: {kind}")
        return build_llm_from_config(config, async_=async_)
    if kind == "asr":
        if not isinstance(config, ASRConfig):
            raise ValueError(f"模型配置类型与 kind 不符: {kind}")
        client = build_asr_from_config(config, async_=async_)
        client.suppress_error = False
        return client
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
        self, manager: ModelConfigReader | None, schema: dict[str, ConfigField], config: dict[str, Any], *, async_: bool,
    ) -> None:
        self.async_ = async_
        self._configs: dict[str, tuple[str, ModelConfig]] = {}
        self._model_names: dict[str, str] = {}
        self._clients: dict[str, ModelClient] = {}
        for key, definition in schema.items():
            value = config.get(key)
            if definition.required and (value is None or value == "" or value == []):
                raise ValueError(f"插件必填配置未设置: {key}")
            if definition.type in MODEL_TYPES and value:
                if manager is None:
                    raise ValueError("插件模型依赖需要后端模型配置服务")
                self._configs[key] = (definition.type, named_model_config(manager, definition.type, value))
                self._model_names[key] = value

    def snapshot_models(self) -> ModelConfigSnapshot:
        """
        保留实际安装时的模型参数, 回滚不能重新读取已变化的文件

        返回:
        - 独立只读模型来源, 不复制客户端或授权状态
        """
        values: dict[str, dict[str, ModelConfig]] = {target: {} for target in MODEL_TYPES.values()}
        for key, (kind, config) in self._configs.items():
            values[MODEL_TYPES[kind]][self._model_names[key]] = config
        return ModelConfigSnapshot(None, values)

    @overload
    def get(self, key: str) -> ModelClient | None: ...
    @overload
    def get(self, key: str, default: T) -> ModelClient | T: ...
    def get(self, key: str, default: T | None = None) -> ModelClient | T | None:
        """首次使用才构造客户端, 安装预校验不发送网络请求"""
        if key not in self._configs:
            return default
        if key not in self._clients:
            kind, config = self._configs[key]
            self._clients[key] = build_model_client(kind, config, async_=self.async_)
        return self._clients[key]

    def __getitem__(self, key: str) -> ModelClient:
        if key not in self._configs:
            raise KeyError(key)
        client = self.get(key)
        if client is None:
            raise KeyError(key)
        return client

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


def model_reference_fingerprint(manager: ModelConfigReader, schema: dict[str, ConfigField], config: dict[str, Any]) -> str:
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
