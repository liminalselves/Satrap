"""重排 API 兼容入口, 保留同步异步类与解析函数"""

import requests
import aiohttp
import asyncio
from typing import List, Dict, Any, Optional, Union, Literal, cast
from typing import Protocol
import json
import math
from satrap.core.utils import normalize_openai_base_url
from satrap.core.log import logger
from .utils import _JSONResponse, _RequestsClient, parse_rerank_result
from .sync import ReRank
from .async_ import AsyncReRank
