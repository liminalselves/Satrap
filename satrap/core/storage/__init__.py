"""Satrap v2 平台与会话数据布局"""
from satrap.core.storage.layout import (
    CHAT_PLATFORM_ID,
    LOCAL_PLATFORM_ID,
    StorageLayout,
    StorageScope,
    default_storage_layout,
    storage_key,
)
from satrap.core.storage.database import delete_session_domain_rows

__all__ = [
    "CHAT_PLATFORM_ID",
    "LOCAL_PLATFORM_ID",
    "StorageLayout",
    "StorageScope",
    "default_storage_layout",
    "storage_key",
    "delete_session_domain_rows",
]
