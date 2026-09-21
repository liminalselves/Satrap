"""模型配置共享领域服务"""
from __future__ import annotations

from dataclasses import fields, replace
from time import monotonic
from typing import Any, cast

from satrap.core.framework.BackGroundManager import ConfigTarget, ModelConfigManager
from satrap.core.utils.context_policy import resolve_context_policy
from satrap.core.APICall.ASRCall import AsyncASR, build_asr_from_config
from satrap.core.type import ASRConfig, EmbeddingConfig, LLMConfig, MODEL_CONFIG_CLASSES, ReRankConfig, validate_thinking_levels

ASR_TEST_MAX_AUDIO_BYTES = 8 * 1024 * 1024
"""ASR 转录测试接受的解码后音频字节上限"""


_CONFIG_CLASSES = MODEL_CONFIG_CLASSES
"""模型配置类型到数据类的映射"""


class ModelConfigService:
    """供平台后端和控制服务共用的模型配置增删改查服务"""

    def __init__(self, manager: ModelConfigManager) -> None:
        """
        初始化模型配置服务

        参数:
        - manager: 模型配置管理器
        """
        self.manager = manager

    def list_configs(self, target: str) -> dict[str, dict[str, Any]]:
        """
        列出指定类型的脱敏模型配置

        参数:
        - target: 模型配置类型

        返回:
        - dict[str, dict[str, Any]]: 按名称组织的脱敏配置
        """
        normalized = self._validate_target(target)
        if normalized == "llm":
            return self.manager.list_llm_configs(mask_api_key=True)
        if normalized == "embedding":
            return self.manager.list_embedding_configs(mask_api_key=True)
        if normalized == "asr":
            return self.manager.list_asr_configs(mask_api_key=True)
        return self.manager.list_rerank_configs(mask_api_key=True)

    def create(self, target: str, name: str, payload: object) -> None:
        """
        创建或覆盖模型配置

        参数:
        - target: 模型配置类型
        - name: 配置名称
        - payload: 配置字段
        """
        normalized = self._validate_target(target)
        config_name = self._validate_name(name)
        cleaned = self._validate_payload(normalized, payload, allow_masked_key=False)
        cleaned["name"] = config_name
        if normalized == "llm":
            config = LLMConfig(**cleaned)
            resolve_context_policy(config)
            self.manager.set_llm_config(config, name=config_name)
        elif normalized == "embedding":
            self.manager.set_embedding_config(EmbeddingConfig(**cleaned), name=config_name)
        elif normalized == "asr":
            self.manager.set_asr_config(ASRConfig(**cleaned), name=config_name)
        else:
            self.manager.set_rerank_config(ReRankConfig(**cleaned), name=config_name)

    def update(self, target: str, name: str, payload: object) -> None:
        """
        更新模型配置并保留未修改字段

        参数:
        - target: 模型配置类型
        - name: 配置名称
        - payload: 待更新字段
        """
        normalized = self._validate_target(target)
        config_name = self._validate_name(name)
        cleaned = self._validate_payload(normalized, payload, allow_masked_key=True)
        requested_name = cleaned.pop("name", config_name)
        new_name = self._validate_name(str(requested_name or ""))
        if normalized == "llm":
            current = self.manager.get_llm_config(config_name)
            resolve_context_policy(replace(current, **cleaned))
        self.manager.update_named_config(
            normalized,
            config_name,
            cleaned,
            new_name=new_name,
        )

    def delete(self, target: str, name: str) -> bool:
        """
        删除模型配置

        参数:
        - target: 模型配置类型
        - name: 配置名称

        返回:
        - bool: 配置是否存在并已删除或重置
        """
        normalized = self._validate_target(target)
        config_name = self._validate_name(name)
        if normalized == "llm":
            return self.manager.remove_llm_config(config_name)
        if normalized == "embedding":
            return self.manager.remove_embedding_config(config_name)
        if normalized == "asr":
            return self.manager.remove_asr_config(config_name)
        return self.manager.remove_rerank_config(config_name)

    async def test_asr_config(self, name: str, filename: str, audio: bytes) -> dict[str, Any]:
        """
        用已保存的 ASR 配置转录一段短音频, 密钥只在后端读取, 不落临时文件

        参数:
        - name: 配置名称
        - filename: 原始文件名, 用于格式校验
        - audio: 解码后的音频字节

        返回:
        - dict[str, Any]: 转录文本, 模型, 语言, 音频秒数与耗时毫秒
        """
        config_name = self._validate_name(name)
        if not audio:
            raise ValueError("音频内容为空")
        if len(audio) > ASR_TEST_MAX_AUDIO_BYTES:
            raise ValueError("音频超过测试大小上限")
        if not self.manager.has_config("asr", config_name):
            raise ValueError(f"模型配置不存在: asr/{config_name}")
        config = self.manager.get_asr_config(config_name)
        if not config.model or not config.api_key:
            raise ValueError("ASR 配置缺少 model 或 api_key")
        client = cast(AsyncASR, build_asr_from_config(config, async_=True))
        client.suppress_error = False
        started = monotonic()
        try:
            result = await client.transcribe(audio, filename=filename)
        finally:
            await client.client.close()
        elapsed_ms = int((monotonic() - started) * 1000)
        if result is None:
            raise RuntimeError("转录失败且无详细错误")
        return {
            "text": result.text,
            "model": result.model,
            "language": result.language,
            "duration": result.duration,
            "elapsed_ms": elapsed_ms,
        }

    @staticmethod
    def _validate_target(target: str) -> ConfigTarget:
        """
        校验模型配置类型

        参数:
        - target: 模型配置类型

        返回:
        - ConfigTarget: 合法的模型配置类型
        """
        if target not in _CONFIG_CLASSES:
            raise ValueError(f"未知模型类型: {target}")
        return cast(ConfigTarget, target)

    @staticmethod
    def _validate_name(name: str) -> str:
        """
        校验模型配置名称

        参数:
        - name: 配置名称

        返回:
        - str: 去除首尾空格后的名称
        """
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("模型配置名称不能为空")
        if len(normalized) > 128 or "/" in normalized or "\\" in normalized or not normalized.isprintable():
            raise ValueError("模型配置名称不能超过 128 字符且不能包含路径分隔符或控制字符")
        return normalized

    @staticmethod
    def _validate_payload(
        target: ConfigTarget,
        payload: object,
        *,
        allow_masked_key: bool,
    ) -> dict[str, Any]:
        """
        校验配置字段并处理展示用脱敏密钥

        参数:
        - target: 模型配置类型
        - payload: 配置字段
        - allow_masked_key: 是否允许忽略脱敏后的 API Key

        返回:
        - dict[str, Any]: 可写入管理器的配置字段
        """
        if not isinstance(payload, dict):
            raise ValueError("模型配置必须是对象")
        config_class = _CONFIG_CLASSES[target]
        allowed = {field.name for field in fields(config_class)}
        raw = dict(cast(dict[str, Any], payload))
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"未知模型配置字段: {', '.join(unknown)}")
        api_key = raw.get("api_key")
        if isinstance(api_key, str) and "*" in api_key:
            if not allow_masked_key:
                raise ValueError("不能使用脱敏后的 API Key 创建配置")
            raw.pop("api_key", None)
        if target == "llm" and "thinking_levels" in raw:
            raw["thinking_levels"] = validate_thinking_levels(raw["thinking_levels"])
        return raw
