"""未知对象属性的类型安全访问辅助"""
from __future__ import annotations

from typing import Any, Callable, TypeVar, cast, overload


T = TypeVar("T")


@overload
def safe_getattr(obj: Any, name: str) -> Any | None: ...

@overload
def safe_getattr(obj: Any, name: str, default: T) -> Any | T: ...

def safe_getattr(obj: Any, name: str, default: T | None = None) -> Any | T | None:
    """
    类型安全的 getattr: 返回值类型 = 属性类型 | default 类型

    用于替代裸 getattr(obj, "attr", None), 让 pyright 能推断返回值类型

    参数:
    - obj: 目标对象
    - name: 属性名
    - default: 属性不存在时的默认值

    返回:
    - 属性值(若存在)或 default
    """
    return getattr(obj, name, default)


def safe_getattr_str(obj: Any, name: str, default: str = "") -> str:
    """
    类型安全的 getattr, 返回 str

    参数:
    - obj: 目标对象
    - name: 名称
    - default: 默认值

    返回:
    - str:  str
    """
    val = getattr(obj, name, default)
    return str(val) if val is not None else default


def safe_getattr_int(obj: Any, name: str, default: int = 0) -> int:
    """
    类型安全的 getattr, 返回 int

    参数:
    - obj: 目标对象
    - name: 名称
    - default: 默认值

    返回:
    - int:  int
    """
    val = getattr(obj, name, default)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def safe_getattr_float(obj: Any, name: str, default: float = 0.0) -> float:
    """
    类型安全的 getattr, 返回 float

    参数:
    - obj: 目标对象
    - name: 名称
    - default: 默认值

    返回:
    - float:  float
    """
    val = getattr(obj, name, default)
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def safe_getattr_bool(obj: Any, name: str, default: bool = False) -> bool:
    """
    类型安全的 getattr, 返回 bool

    参数:
    - obj: 目标对象
    - name: 名称
    - default: 默认值

    返回:
    - bool:  bool
    """
    val = getattr(obj, name, default)
    if val is None:
        return default
    return bool(val)


def safe_getattr_list(obj: Any, name: str) -> list[Any]:
    """
    类型安全的 getattr, 返回 list(不存在或 None 时返回空列表)

    参数:
    - obj: 目标对象
    - name: 名称

    返回:
    - list[Any]:  list(不存在或 None 时返回空列表)
    """
    val: Any = getattr(obj, name, None)
    if val is None:
        return []
    if isinstance(val, list):
        return cast(list[Any], val)
    return [val]


def safe_getattr_dict(obj: Any, name: str) -> dict[str, Any]:
    """
    类型安全的 getattr, 返回 dict(不存在或 None 时返回空字典)

    参数:
    - obj: 目标对象
    - name: 名称

    返回:
    - dict[str, Any]:  dict(不存在或 None 时返回空字典)
    """
    val: Any = getattr(obj, name, None)
    if val is None:
        return {}
    if isinstance(val, dict):
        return cast(dict[str, Any], val)
    return {}


def safe_getattr_callable(obj: Any, name: str) -> Callable[..., Any] | None:
    """
    类型安全的 getattr, 返回可调用对象(不存在或不可调用时返回 None)

    参数:
    - obj: 目标对象
    - name: 名称

    返回:
    - Callable[..., Any] | None: 可调用对象(不存在或不可调用时返回 None)
    """
    val = getattr(obj, name, None)
    return val if callable(val) else None
