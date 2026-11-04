"""异步图片生成, 编辑和显式 Vibe 编码"""
from __future__ import annotations

import asyncio
from typing import Any, Sequence

import httpx

from .base import _ImageGenBase
from .media import MAX_IMAGE_BYTES, MAX_RESPONSE_BYTES, b64, decode_image, local_image, validate_url
from .request import build_request, source_key
from .response import parse_response
from .types import GeneratedImage, ImageGenError, ImageGenParams, ImageGenResponse, ImageInput, VibeEncoding


class AsyncImageGen(_ImageGenBase[httpx.AsyncClient]):
    """异步图片客户端, 网络不阻塞事件循环, 文件和图片处理在线程执行"""

    def _create_client(self) -> httpx.AsyncClient:
        """创建异步连接池, 不自动重定向或重试生成"""
        return httpx.AsyncClient(timeout=self.network_timeout, follow_redirects=False)

    async def _request(self, method: str, url: str, *, limit: int = MAX_RESPONSE_BYTES, **kwargs: Any) -> tuple[bytes, httpx.Headers]:
        """限制响应大小, 取消直接传播, 不自动重试生成请求"""
        async with self.client.stream(method, url, **kwargs) as response:
            self._http_error(response.status_code, response.headers)
            output = bytearray()
            async for chunk in response.aiter_bytes():
                output.extend(chunk)
                if len(output) > limit:
                    raise ImageGenError("response_size_limit")
            return bytes(output), response.headers

    async def _download(self, url: str) -> bytes:
        """无鉴权下载图片, 不将生成密钥传给资源地址"""
        for _ in range(6):
            validate_url(url, allow_insecure=self.allow_insecure_base_url)
            async with self.client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ImageGenError("invalid_redirect")
                    url = str(response.url.join(location))
                    continue
                self._http_error(response.status_code, response.headers)
                output = bytearray()
                async for chunk in response.aiter_bytes():
                    output.extend(chunk)
                    if len(output) > MAX_IMAGE_BYTES:
                        raise ImageGenError("image_size_limit")
                return (await asyncio.to_thread(decode_image, bytes(output))).data
        raise ImageGenError("redirect_limit")

    async def _resolve(self, source: ImageInput) -> bytes:
        """显式下载远程输入或在线程中读取本地图片"""
        return await self._download(str(source.value)) if source.kind == "url" else await asyncio.to_thread(local_image, source)

    async def _call(self, prompt: str, params: ImageGenParams | None, images: Sequence[ImageInput] | None, mask: ImageInput | None) -> ImageGenResponse | None:
        used, model, inputs, sources, url = self._prepare(prompt, params, images, mask)
        try:
            resolved = {}
            for source in sources:
                key = source_key(source)
                if key not in resolved:
                    resolved[key] = await self._resolve(source)
            request = await asyncio.to_thread(build_request, self.api_type, model, prompt, used, inputs, mask, resolved)
            data, headers = await self._request("POST", url, headers=self._headers(), **request)
            parsed = await asyncio.to_thread(parse_response, data, headers.get("content-type", ""))
            output = []
            downloaded = {}
            total = 0
            for entry in parsed.entries:
                if isinstance(entry, GeneratedImage):
                    output.append(entry)
                else:
                    image_url, seed, revised_prompt = entry
                    if image_url not in downloaded:
                        try:
                            downloaded[image_url] = await self._download(image_url)
                        except ValueError:
                            raise ImageGenError("invalid_output_url") from None
                    output.append(await asyncio.to_thread(decode_image, downloaded[image_url], seed=seed, revised_prompt=revised_prompt))
                total += len(output[-1].data)
                if total > MAX_RESPONSE_BYTES:
                    raise ImageGenError("response_size_limit")
            return parsed.finish(output, model, self._request_id(headers))
        except (ImageGenError, httpx.HTTPError) as error:
            self._failure(error)
            return None

    async def generate(self, prompt: str, *, params: ImageGenParams | None = None) -> ImageGenResponse | None:
        """
        异步生成图片, 与同步版本共享参数和返回语义

        参数:
        - prompt: 正向提示词
        - params: 覆盖默认值的公共或厂商参数

        返回:
        - 统一图片结果, 抑制 API 错误时失败返回 None
        """
        return await self._call(prompt, params, None, None)

    async def edit(self, prompt: str, images: Sequence[ImageInput], *, mask: ImageInput | None = None, params: ImageGenParams | None = None) -> ImageGenResponse | None:
        """
        异步编辑底图, 白色遮罩表示编辑区域

        参数:
        - prompt: 编辑指令
        - images: 非空底图列表
        - mask: 可选局部重绘遮罩
        - params: 覆盖默认值的参数

        返回:
        - 统一图片结果, 取消不会被抑制或触发重新提交
        """
        return await self._call(prompt, params, images, mask)

    async def encode_vibe(self, image: ImageInput, *, base_url: str, model: str | None = None, information_extracted: float = 1.0) -> VibeEncoding | None:
        """
        异步执行显式 Vibe 编码, 不推导端点或自动触发计费操作

        参数:
        - image: 原始参考图
        - base_url: 完整编码请求地址
        - model: 可选编码模型
        - information_extracted: 信息提取值

        返回:
        - 可复用编码或失败时的 None
        """
        url, used_model = self._encoding_request(base_url, image, model, information_extracted)
        try:
            body = {"image": b64(await self._resolve(image)), "model": used_model, "information_extracted": information_extracted}
            data, _ = await self._request("POST", url, limit=MAX_IMAGE_BYTES, headers={**self._headers(), "Accept": "application/octet-stream"}, json=body)
            if not data or data.lstrip().startswith((b"{", b"[")):
                raise ImageGenError("invalid_vibe_encoding")
            return VibeEncoding(data=data, model=used_model, information_extracted=information_extracted)
        except (ImageGenError, httpx.HTTPError) as error:
            self._failure(error)
            return None

    async def aclose(self) -> None:
        """释放异步 HTTP 连接池"""
        await self.client.aclose()

    async def __aenter__(self) -> AsyncImageGen:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
