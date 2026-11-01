"""同步图片生成, 编辑和显式 Vibe 编码"""
from __future__ import annotations

from typing import Any, Sequence

import httpx

from .base import _ImageGenBase
from .media import MAX_IMAGE_BYTES, MAX_RESPONSE_BYTES, b64, decode_image, local_image, validate_url
from .request import build_request, source_key
from .response import parse_response
from .types import GeneratedImage, ImageGenError, ImageGenParams, ImageGenResponse, ImageInput, VibeEncoding


class ImageGen(_ImageGenBase[httpx.Client]):
    """同步图片客户端, 输出图片字节由调用方保存"""

    def _create_client(self) -> httpx.Client:
        """关闭自动重定向和自动重试, 保留完整端点"""
        return httpx.Client(timeout=self.network_timeout, follow_redirects=False)

    def _request(self, method: str, url: str, *, limit: int = MAX_RESPONSE_BYTES, **kwargs: Any) -> tuple[bytes, httpx.Headers]:
        """限制传输解压后的响应大小, 不重试或自动跟随 POST 重定向"""
        with self.client.stream(method, url, **kwargs) as response:
            self._http_error(response.status_code, response.headers)
            output = bytearray()
            for chunk in response.iter_bytes():
                output.extend(chunk)
                if len(output) > limit:
                    raise ImageGenError("response_size_limit")
            return bytes(output), response.headers

    def _download(self, url: str) -> bytes:
        """无鉴权下载图片, 每次重定向都重新验证地址"""
        for _ in range(6):
            validate_url(url, allow_insecure=self.allow_insecure_base_url)
            with self.client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ImageGenError("invalid_redirect")
                    url = str(response.url.join(location))
                    continue
                self._http_error(response.status_code, response.headers)
                output = bytearray()
                for chunk in response.iter_bytes():
                    output.extend(chunk)
                    if len(output) > MAX_IMAGE_BYTES:
                        raise ImageGenError("image_size_limit")
                return decode_image(bytes(output)).data
        raise ImageGenError("redirect_limit")

    def _resolve(self, source: ImageInput) -> bytes:
        """本地输入和远程输入均验证真实格式"""
        return self._download(str(source.value)) if source.kind == "url" else local_image(source)

    def _call(self, prompt: str, params: ImageGenParams | None, images: Sequence[ImageInput] | None, mask: ImageInput | None) -> ImageGenResponse | None:
        used, model, inputs, sources, url = self._prepare(prompt, params, images, mask)
        try:
            resolved = {}
            for source in sources:
                key = source_key(source)
                if key not in resolved:
                    resolved[key] = self._resolve(source)
            request = build_request(self.api_type, model, prompt, used, inputs, mask, resolved)
            data, headers = self._request("POST", url, headers=self._headers(), **request)
            parsed = parse_response(data, headers.get("content-type", ""))
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
                            downloaded[image_url] = self._download(image_url)
                        except ValueError:
                            raise ImageGenError("invalid_output_url") from None
                    output.append(decode_image(downloaded[image_url], seed=seed, revised_prompt=revised_prompt))
                total += len(output[-1].data)
                if total > MAX_RESPONSE_BYTES:
                    raise ImageGenError("response_size_limit")
            return parsed.finish(output, model, self._request_id(headers))
        except (ImageGenError, httpx.HTTPError) as error:
            self._failure(error)
            return None

    def generate(self, prompt: str, *, params: ImageGenParams | None = None) -> ImageGenResponse | None:
        """
        根据提示词生成图片, 参数可携带参考图

        参数:
        - prompt: 正向提示词
        - params: 覆盖实例默认值的公共或厂商参数

        返回:
        - 统一图片结果, 抑制 API 错误时失败返回 None
        """
        return self._call(prompt, params, None, None)

    def edit(self, prompt: str, images: Sequence[ImageInput], *, mask: ImageInput | None = None, params: ImageGenParams | None = None) -> ImageGenResponse | None:
        """
        编辑底图, 可同时传入独立参考图

        参数:
        - prompt: 编辑指令
        - images: 非空底图列表
        - mask: 可选遮罩, 白色表示编辑区域, 黑色表示保留区域
        - params: 覆盖实例默认值的参数

        返回:
        - 统一图片结果, 不自动保存或再次生成
        """
        return self._call(prompt, params, images, mask)

    def encode_vibe(self, image: ImageInput, *, base_url: str, model: str | None = None, information_extracted: float = 1.0) -> VibeEncoding | None:
        """
        显式编码可复用的 Vibe, 此操作可能产生厂商费用

        参数:
        - image: 原始参考图
        - base_url: 完整编码请求地址
        - model: 编码模型, 省略时使用实例模型
        - information_extracted: 信息提取值

        返回:
        - 可复用编码, 抑制 API 错误时失败返回 None
        """
        url, used_model = self._encoding_request(base_url, image, model, information_extracted)
        try:
            body = {"image": b64(self._resolve(image)), "model": used_model, "information_extracted": information_extracted}
            data, _ = self._request("POST", url, limit=MAX_IMAGE_BYTES, headers={**self._headers(), "Accept": "application/octet-stream"}, json=body)
            if not data or data.lstrip().startswith((b"{", b"[")):
                raise ImageGenError("invalid_vibe_encoding")
            return VibeEncoding(data=data, model=used_model, information_extracted=information_extracted)
        except (ImageGenError, httpx.HTTPError) as error:
            self._failure(error)
            return None

    def close(self) -> None:
        """释放 HTTP 连接池"""
        self.client.close()

    def __enter__(self) -> ImageGen:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
