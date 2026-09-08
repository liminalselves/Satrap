"""网页检索与读取工具兼容入口"""

import requests
import random
from typing import Any, cast
from typing import Protocol
import json
from bs4 import BeautifulSoup
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.utils.outbound import (
    OutboundHTTPError,
    UnsafeOutboundURLError,
    safe_async_get,
    safe_sync_get,
    validate_outbound_http_url,
    validate_outbound_redirect,
)
from .utils import (
    USER_AGENTS,
    _RequestsResponse,
    _RequestsGet,
    _requests_get,
    _TitleElement,
    _TitleSoup,
)
from .sync import SearchTool, FetchPageTool
from .async_ import AsyncSearchTool, AsyncFetchPageTool
