"""ReRank 凭据与同步异步响应边界测试"""
from __future__ import annotations

import pytest
from typing import cast

from satrap.core.APICall.ReRankCall import AsyncReRank, ReRank
from satrap.core.APICall import ReRankCall as rerank_module


class _SyncResponse:
    """同步重排响应替身"""

    def __init__(self, status_code: int, payload: object) -> None:
        """
        初始化同步响应替身

        参数:
        - status_code: HTTP 状态码
        - payload: JSON 响应对象
        """
        self.status_code = status_code
        self.payload = payload

    def json(self) -> object:
        """
        返回测试响应体

        返回:
        - 初始化时提供的响应对象
        """
        return self.payload


class _AsyncResponse:
    """异步重排响应替身"""

    def __init__(self, status: int, payload: object) -> None:
        """
        初始化异步响应替身

        参数:
        - status: HTTP 状态码
        - payload: JSON 响应对象
        """
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _AsyncResponse:
        """
        进入响应上下文

        返回:
        - 当前响应替身
        """
        return self

    async def __aexit__(self, *_: object) -> None:
        """
        退出响应上下文

        参数:
        - _: 上下文管理器异常信息
        """

    async def json(self) -> object:
        """
        返回测试响应体

        返回:
        - 初始化时提供的响应对象
        """
        return self.payload


class _AsyncSession:
    """异步 HTTP 会话替身"""

    def __init__(self, response: _AsyncResponse, capture: dict[str, object]) -> None:
        """
        初始化异步 HTTP 会话替身

        参数:
        - response: 待返回的异步响应
        - capture: 请求参数收集字典
        """
        self.response = response
        self.capture = capture

    async def __aenter__(self) -> _AsyncSession:
        """
        进入会话上下文

        返回:
        - 当前会话替身
        """
        return self

    async def __aexit__(self, *_: object) -> None:
        """
        退出会话上下文

        参数:
        - _: 上下文管理器异常信息
        """

    def post(self, url: str, **kwargs: object) -> _AsyncResponse:
        """
        记录异步请求参数并返回响应

        参数:
        - url: 请求地址
        - kwargs: 请求关键字参数

        返回:
        - 预设异步响应
        """
        self.capture.update({"url": url, **kwargs})
        return self.response


def test_rerank_masks_public_key_but_uses_private_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    同步 ReRank 不公开原始凭据, 请求仍使用真实凭据

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    capture: dict[str, object] = {}

    def post(url: str, **kwargs: object) -> _SyncResponse:
        """
        记录同步请求参数并返回成功响应

        参数:
        - url: 请求地址
        - kwargs: 请求关键字参数

        返回:
        - 固定成功响应
        """
        capture.update({"url": url, **kwargs})
        return _SyncResponse(200, {"results": []})

    monkeypatch.setattr(rerank_module.requests, "post", post)
    rerank = ReRank("secret-key", "https://rerank.example/v1", "model")

    assert rerank.api_key == "api key locked"
    assert rerank.get_api_key() == "api key locked"
    assert rerank.call("query", ["document"]) == []
    headers = cast(dict[str, str], capture["headers"])
    assert headers["Authorization"] == "Bearer secret-key"


def test_rerank_rejects_non_success_before_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    同步 ReRank 遇到非成功状态时不解析或回传响应正文

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    class ErrorResponse(_SyncResponse):
        def json(self) -> object:
            """
            阻止非成功响应被解析

            返回:
            - 不返回结果, 始终抛出 AssertionError
            """
            raise AssertionError("非成功响应不应解析 JSON")

    def post(*_args: object, **_kwargs: object) -> ErrorResponse:
        """
        返回固定限流响应

        参数:
        - _args: 未使用的位置参数
        - _kwargs: 未使用的关键字参数

        返回:
        - 固定限流响应
        """
        return ErrorResponse(429, {"secret": "provider-detail"})

    monkeypatch.setattr(
        rerank_module.requests,
        "post",
        post,
    )

    assert ReRank("key", "https://rerank.example/v1", "model").call("query", ["doc"]) == []


@pytest.mark.asyncio
async def test_async_rerank_normalizes_url_masks_key_and_checks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    异步 ReRank 与同步版保持 URL, 凭据和状态码语义一致

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    capture: dict[str, object] = {}
    response = _AsyncResponse(401, {"secret": "provider-detail"})
    monkeypatch.setattr(
        rerank_module.aiohttp,
        "ClientSession",
        lambda: _AsyncSession(response, capture),
    )
    rerank = AsyncReRank("secret-key", "https://rerank.example/v1/responses", "model")

    assert rerank.base_url == "https://rerank.example/v1/rerank"
    assert rerank.api_key == "api key locked"
    assert await rerank.call("query", ["doc"]) == []
    headers = cast(dict[str, str], capture["headers"])
    assert headers["Authorization"] == "Bearer secret-key"
