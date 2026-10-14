"""平台注册实例的持久代次, 同名删除重建不能继承旧后台任务"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib


def platform_instance_id(config: Mapping[str, object]) -> str:
    """
    取得持久实例 ID, 旧配置使用稳定标识保持重启兼容

    参数:
    - config: 已校验的平台配置, 新建实例带显式 instance_id

    返回:
    - 重启不变的实例代次, 新建同名平台使用不同显式 ID
    """
    value = config.get("instance_id")
    if isinstance(value, str) and value:
        return value
    return hashlib.sha256(("legacy-platform:v1:" + str(config.get("id", ""))).encode("utf-8")).hexdigest()[:32]
