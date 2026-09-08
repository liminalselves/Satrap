"""安全出站请求兼容入口, 同步异步共用地址校验规则"""

from __future__ import annotations
from collections.abc import Iterable, Mapping
from email.message import Message
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from aiohttp.abc import AbstractResolver, ResolveResult
from dataclasses import dataclass
import http.client
import ipaddress
import aiohttp
import asyncio
import socket
from http import HTTPStatus
from ssl import SSLContext, create_default_context
import os
from satrap.core.utils.async_worker import DNS_WORKERS
from .utils import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_DOWNLOAD_BYTES,
    TRUSTED_DOWNLOAD_HOSTS_ENV_NAME,
    _REDIRECT_STATUSES,
    UnsafeOutboundURLError,
    OutboundHTTPError,
    OutboundResponseTooLargeError,
    ResolvedOutboundTarget,
    OutboundHTTPResponse,
    normalize_hostname,
    trusted_hosts_from_env,
    _resolve_addresses,
    resolve_outbound_http_url,
    validate_outbound_http_url,
    resolve_outbound_redirect,
    validate_outbound_redirect,
    _merge_query_params,
    same_origin,
)
from .sync import (
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
    _read_sync_response,
    _sync_request_once,
    safe_sync_get,
)
from .async_ import (
    _PinnedResolver,
    _read_async_response,
    _async_request_once,
    safe_async_get,
)
