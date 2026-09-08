from __future__ import annotations
import re
from satrap.core.utils.paths import get_project_root

WORKSPACE_ROOT = get_project_root()

DATA_ROOT = WORKSPACE_ROOT / ".satrap" / "coding"

_APPROVAL_PROMPT = """你是一个操作审批助手。请判断以下操作是否允许执行, 只回答一个词:
- allow: 操作安全或属于常规开发操作
- deny: 操作危险或明显有害
- ask: 不确定, 需要询问用户

操作类型: {operation}
风险等级: {risk} (0=只读 1=常规写 2=高危 3=禁止)
操作描述: {description}

回答 (allow/deny/ask):"""

_PROTECTED_DIRS = (".satrap", ".git", "node_modules")

_PROTECTED_FILES = (".env", ".env.local", "api-token", "model_config.json")

_SYSTEM_PROTECTED_ROOT = (get_project_root() / ".satrap").resolve()

_FILE_LINE_BREAK = re.compile(r"[\n\v\f\x1c-\x1e\x85\u2028\u2029]")

_SUBAGENT_PROMPT = """你是一个子代理, 在独立上下文中处理分配给你的子任务。
使用可用工具完成任务并返回结果摘要。不要修改与任务无关的内容。"""

DEFAULT_SANDBOX_ROOT = get_project_root() / ".satrap" / "sandbox"
