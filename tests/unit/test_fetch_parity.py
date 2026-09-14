from unittest.mock import AsyncMock, Mock
import pytest
import json

from satrap.core.utils.outbound import OutboundHTTPResponse, OutboundResponseTooLargeError, UnsafeOutboundURLError
from satrap.expend.tools.search import async_ as async_search
from satrap.expend.tools.search import sync as sync_search
from satrap.expend.tools import search


@pytest.mark.asyncio
@pytest.mark.parametrize("error,prefix", [
    (OSError("连接失败"), "请求失败"),
    (TimeoutError("超时"), "请求失败"),
    (OutboundResponseTooLargeError("超限"), "请求失败"),
    (UnsafeOutboundURLError("私网地址"), "请求被安全策略拒绝"),
    (UnsafeOutboundURLError("重定向次数超过限制"), "请求被安全策略拒绝"),
])
async def test_request_errors_match(monkeypatch: pytest.MonkeyPatch, error: Exception, prefix: str) -> None:
    """
    验证请求阶段的异常分类一致

    参数:
    - monkeypatch: 替换外部请求与审批的夹具
    - error: 模拟请求异常
    - prefix: 期望的错误分类
    """
    monkeypatch.setattr(sync_search, "validate_outbound_http_url", lambda url: url)
    monkeypatch.setattr(search, "_requests_get", Mock(side_effect=error))
    monkeypatch.setattr(async_search, "safe_async_get", AsyncMock(side_effect=error))
    url = "https://example.com"
    sync_result = json.loads(search.FetchPageTool().execute(url))
    async_result = json.loads(await search.AsyncFetchPageTool().execute(url))
    assert sync_result == async_result == {"url": url, "error": f"{prefix}: {error}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201, 204, 404, 503])
async def test_response_status_and_html_match(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """
    验证响应状态与页面正文输出一致

    参数:
    - monkeypatch: 替换外部请求与审批的夹具
    - status: 模拟响应状态码
    """
    url = "https://example.com"
    response = OutboundHTTPResponse(url=url, status_code=status, headers={}, content='<title>标题</title><p>正文内容</p><script>隐藏</script>'.encode())
    monkeypatch.setattr(sync_search, "validate_outbound_http_url", lambda url: url)
    monkeypatch.setattr(search, "_requests_get", Mock(return_value=response))
    monkeypatch.setattr(async_search, "safe_async_get", AsyncMock(return_value=response))
    sync_result = json.loads(search.FetchPageTool().execute(url, max_length=4))
    async_result = json.loads(await search.AsyncFetchPageTool().execute(url, max_length=4))
    assert sync_result == async_result
    if status != 200:
        assert sync_result == {"url": url, "error": f"HTTP {status}"}
    else:
        assert sync_result["title"] == "标题"
        assert sync_result["content"] == "标题\n正...(内容已截断)"


@pytest.mark.asyncio
async def test_parse_error_is_not_request_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证解析异常独立于请求失败

    参数:
    - monkeypatch: 替换外部请求与审批的夹具
    """
    url = "https://example.com"
    response = OutboundHTTPResponse(url=url, status_code=200, headers={}, content=b"html")
    monkeypatch.setattr(sync_search, "validate_outbound_http_url", lambda url: url)
    monkeypatch.setattr(search, "_requests_get", Mock(return_value=response))
    monkeypatch.setattr(async_search, "safe_async_get", AsyncMock(return_value=response))
    sync, async_tool = search.FetchPageTool(), search.AsyncFetchPageTool()
    sync._extract_text = Mock(side_effect=ValueError("坏页面"))
    async_tool._extract_text = Mock(side_effect=ValueError("坏页面"))
    assert json.loads(sync.execute(url)) == json.loads(await async_tool.execute(url)) == {
        "url": url, "error": "解析失败: 坏页面",
    }
