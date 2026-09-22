"""OpenAI 兼容语音转录的异步客户端"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from openai import APIError, AsyncOpenAI

from satrap.core.log import logger
from satrap.core.type import ASRResponse

from .base import _ASRBase
from .utils import parse_transcription, prepare_audio_input


class AsyncASR(_ASRBase[AsyncOpenAI]):
    """异步 ASR 客户端, 调用 audio.transcriptions 端点"""

    def _create_client(self, api_key: str, timeout: int) -> AsyncOpenAI:
        return AsyncOpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)

    async def transcribe(
        self,
        audio: str | Path | bytes | bytearray,
        *,
        filename: str | None = None,
        model: str | None = None,
        language: str | None = None,
        prompt: str | None = None,
    ) -> ASRResponse | None:
        """
        异步转录一段音频, 参数与返回语义和同步版本一致

        参数:
        - audio: 受控本地文件路径或音频字节
        - filename: 字节输入的文件名, 路径输入时忽略
        - model: 覆盖实例默认模型, None 使用实例配置
        - language: 覆盖默认识别语言, None 使用实例配置
        - prompt: 覆盖默认提示词, None 使用实例配置

        返回:
        - ASRResponse | None: 空转录文本仍返回 ASRResponse; suppress_error 时失败返回 None
        """
        try:
            name, data, mime = prepare_audio_input(audio, filename=filename)
            parameters = self._transcription_parameters(model, language, prompt)
            # SDK 对 dict 展开的 **kwargs 无法匹配重载, 响应统一收窄为已知 Any 交给解析层
            response = cast(Any, await self.client.audio.transcriptions.create(file=(name, data, mime), **parameters))
            return parse_transcription(response, str(parameters["model"]))
        except APIError as e:
            if not self.suppress_error:
                raise e
            # 只记状态码与请求 ID, 不落服务端响应体 (可能回显请求内容)
            status = getattr(e, "status_code", None)
            request_id = getattr(e, "request_id", None)
            logger.error(f"[AsyncASR] 转录 API 错误: {type(e).__name__} status={status} request_id={request_id}")
            return None
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncASR] 转录过程发生异常: {type(e).__name__}")
            return None
