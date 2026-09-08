"""文本嵌入兼容入口, 同步和异步共用配置与批次处理"""

from openai import OpenAI, AsyncOpenAI, APIError
from typing import List, Dict, Any, Optional, Union, Literal, cast, overload
from satrap.core.utils import normalize_openai_base_url
from satrap.core.type import safe_getattr
from satrap.core.log import logger
from .utils import (
    _response_field,
    _handle_batch_parse_issue,
    _parse_embedding_batch_response,
    _align_embedding_dimensions,
    parse_embedding_response,
)
from .sync import Embedding
from .async_ import AsyncEmbedding
