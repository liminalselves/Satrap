"""编程插件的固定默认路径与提示文本, 不承载会话配置"""

from __future__ import annotations
import re

from satrap.core.file_access.paths import PROTECTED_DIRS as _PROTECTED_DIRS, PROTECTED_FILES as _PROTECTED_FILES
from satrap.core.utils.paths import get_project_root

WORKSPACE_ROOT = get_project_root()

_SYSTEM_PROTECTED_ROOT = (get_project_root() / ".satrap").resolve()

_FILE_LINE_BREAK = re.compile(r"[\n\v\f\x1c-\x1e\x85\u2028\u2029]")

_SUBAGENT_PROMPT = """你负责一个独立子任务。只处理任务范围内的内容，需要时使用工具，最后返回结果摘要和未解决的问题。"""

DEFAULT_SANDBOX_ROOT = get_project_root() / ".satrap" / "sandbox"
