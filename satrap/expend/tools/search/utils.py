"""网页搜索与页面内容抓取工具"""

from typing import Protocol
from satrap.core.utils.outbound import safe_sync_get


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:88.0) Gecko/20100101 Firefox/88.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Version/14.1.2 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Version/14.1 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:88.0) Gecko/20100101 Firefox/88.0",
]


class _RequestsResponse(Protocol):
    """搜索工具依赖的 requests 响应最小接口"""

    encoding: str
    status_code: int
    headers: dict[str, str]

    @property
    def text(self) -> str:
        """返回按当前编码解码的响应正文"""
        ...

    @property
    def apparent_encoding(self) -> str | None:
        """返回响应推断编码"""
        ...

    @property
    def is_redirect(self) -> bool:
        """返回响应是否为重定向"""
        ...

    def raise_for_status(self) -> None:
        """在 HTTP 状态异常时抛出 requests 异常"""
        ...


class _RequestsGet(Protocol):
    """requests.get 的可检查调用接口"""

    def __call__(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int,
        allow_redirects: bool = True,
    ) -> _RequestsResponse:
        """
        发起同步 HTTP GET 请求

        参数:
        - url: 请求 URL
        - params: 查询参数
        - headers: 请求头
        - timeout: 超时秒数
        - allow_redirects: 是否自动跟随重定向

        返回:
        - _RequestsResponse: 可检查的响应接口
        """
        ...


_requests_get: _RequestsGet = safe_sync_get

"""执行地址绑定和响应限流的同步 GET 请求"""


class _TitleElement(Protocol):
    """声明页面标题节点所需的最小接口"""

    string: str | None


class _TitleSoup(Protocol):
    """声明页面标题提取所需的最小接口"""

    title: _TitleElement | None
