"""子进程环境最小化 - 剥离密钥类环境变量"""
from __future__ import annotations

import os, re

_SENSITIVE_ENV_PATTERN = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)
"""命中即剥离的环境变量名特征; 覆盖 API key, 访问令牌, 密钥与凭据类命名"""


def sanitized_child_env(**overrides: str) -> dict[str, str]:
    """
    构造剥敏后的子进程环境变量

    复制当前进程环境并移除名称含 KEY / TOKEN / SECRET / PASSWORD / PASSWD /
    CREDENTIAL (大小写不敏感) 的变量, 避免批准执行的子命令读取父进程密钥;
    正常工具链所需的 PATH, TEMP, SYSTEMROOT 等原样保留

    参数:
    - overrides: 追加或覆盖的环境变量 (例如 PYTHONUTF8)

    返回:
    - dict[str, str]: 子进程 env 参数
    """
    env = {name: value for name, value in os.environ.items() if not _SENSITIVE_ENV_PATTERN.search(name)}
    env.update(overrides)
    return env
