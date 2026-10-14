"""普通业务配置中的文本集合解析, 不用于权限身份名单"""
from __future__ import annotations


def comma_names(value: object) -> frozenset[str]:
    """
    沿用插件环境变量配置的逗号分隔规则

    参数:
    - value: 配置原值, 空值按空集合处理

    返回:
    - 去除首尾空白与重复项的名称集合
    """
    return frozenset(name.strip() for name in str(value or "").split(",") if name.strip())
