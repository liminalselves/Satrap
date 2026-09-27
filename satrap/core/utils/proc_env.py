"""子进程环境最小化 - 剥离密钥类环境变量"""
from __future__ import annotations

import os, re
from collections.abc import Iterable

_SENSITIVE_ENV_PATTERN = re.compile(r"(?:^|_)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)S?$")
"""变量名以 KEY / TOKEN / SECRET / PASSWORD / PASSWD / CREDENTIAL 为完整尾部分段
(允许复数 S 结尾) 时视为敏感; TOKENIZERS_PARALLELISM 等含敏感词前缀的正常
变量不受影响"""


def sanitized_child_env(allow: Iterable[str] = (), **overrides: str) -> dict[str, str]:
    """
    构造剥敏后的子进程环境变量

    复制当前进程环境并移除名称命中敏感特征的变量, 避免批准执行的子命令读取
    父进程密钥; PATH, TEMP, TOKENIZERS_PARALLELISM 等正常变量原样保留

    参数:
    - allow: 显式放行的变量名列表, 用于向受信任的子进程提供受限凭据
    - overrides: 追加或覆盖的环境变量 (例如 PYTHONUTF8)

    返回:
    - dict[str, str]: 子进程 env 参数
    """
    allowed = {name.strip() for name in allow if name.strip()}
    env = {
        name: value
        for name, value in os.environ.items()
        if name in allowed or not _SENSITIVE_ENV_PATTERN.search(name)
    }
    env.update(overrides)
    return env
