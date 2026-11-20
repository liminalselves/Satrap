"""跨领域共用的递归 JSON 值类型"""
from __future__ import annotations

from typing import TypeAlias

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
