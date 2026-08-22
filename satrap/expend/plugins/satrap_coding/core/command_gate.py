"""
satrap_coding 命令闸门: shell 命令风险分级

- 风险分级: READ(0) 只读放行 / WRITE(1) 常规写, 询问 / HIGH(2) 高危与环境修改 / FORBIDDEN(3) 永远拒绝
- 环境修改: pip install / npm install 等视为高危, 需审批
- 匹配策略: 按命令首词 (大小写不敏感) 匹配规则表; 未匹配命令保守归 WRITE (询问)
"""
from __future__ import annotations

import re

from satrap.core.log import logger

from satrap.expend.plugins.satrap_coding.core.permission import RiskLevel

# ---------- 规则表 (首词匹配, 大小写不敏感) ----------

_READ_COMMANDS = {
    # 通用
    "dir", "ls", "pwd", "cd", "echo", "find", "where", "which", "help", "type",
    "cat", "more", "less", "head", "tail", "grep", "rg", "findstr", "fc",
    "git",   # 细粒度在 _read_git_prefixes 判断
    "date", "time", "whoami", "hostname", "ver",
    # PowerShell 命令
    "get-childitem", "gci", "get-content", "gc", "get-location", "gl",
    "get-item", "gi", "get-process", "gps", "get-service", "get-help",
    "select-string", "sls", "get-date", "get-location", "test-path",
    "get-command", "gcm", "get-alias", "gal",
    # cmd 命令
    "tree", "attrib", "vol", "ipconfig", "netstat", "ping", "tasklist",
    "systeminfo", "reg",   # reg query 只读, 其他高危 (细粒度见下)
}

_WRITE_COMMANDS = {
    # 文件/目录常规写
    "copy", "cp", "xcopy", "robocopy", "move", "mv", "ren", "rename",
    "mkdir", "md", "new-item", "ni", "set-content", "add-content",
    "ac", "out-file", "clear-content", "clip", "start", "explorer",
    "git",   # add/commit/init 等写操作
    "python", "py", "node", "npm",   # 执行脚本 (npm 细粒度: install 是环境修改)
    "powershell", "pwsh", "cmd", "cmd.exe", "powershell.exe",
    # cmd 命令
    "md", "copy", "move", "ren", "set", "assoc", "ftype",
    "npm", "npx", "pip", "pip3", "pipx", "yarn", "pnpm", "uv", "poetry",
    "conda", "mamba", "apt", "apt-get", "dnf", "yum", "gem", "cargo", "go",
    "start", "taskkill", "stop-process", "kill",
}

_HIGH_COMMANDS = {
    # 删除/高危
    "rm", "del", "erase", "rmdir", "rd", "remove-item", "ri", "clear-recyclebin",
    "format", "diskpart", "shutdown", "restart", "restart-computer",
    "stop-computer", "regedit", "sc",   # 服务控制
    "setx", "reset", "takeown", "icacls", "cacls", "attrib",   # attrib 只读时其实无害, 保守
    "git",   # push/reset/clean 等破坏性 (细粒度见下)
    "net",   # user/group 管理
}

_FORBIDDEN_PATTERNS = [
    re.compile(r"^\s*(rm|Remove-Item)\s+(-rf\s+|--recursive\s+)?[\\/]", re.IGNORECASE),
    re.compile(r"^\s*rm\s+-rf\s+[a-zA-Z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\s*del\s+/[sfq]\s+[a-zA-Z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\s*del\s+(?:/[sfq]\s+)+[a-zA-Z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\s*format\s+[a-zA-Z]:", re.IGNORECASE),
    re.compile(r"^\s*rd\s+/[sfq]\s+[a-zA-Z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\s*rd\s+(?:/[sfq]\s+)+[a-zA-Z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\s*rmdir\s+/[sfq]\s+[a-zA-Z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\s*rmdir\s+(?:/[sfq]\s+)+[a-zA-Z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\s*remove-item\s+(?:-recurse\s+|-r\s+|-rf\s+|-fo\w*\s+)*[a-zA-Z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\s*git\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+.*-f)", re.IGNORECASE),
]

_ENV_MODIFY_SUBCOMMANDS = {
    "pip": {"install", "uninstall", "download", "wheel"},
    "pip3": {"install", "uninstall", "download", "wheel"},
    "pipx": {"install", "uninstall", "upgrade"},
    "npm": {"install", "i", "uninstall", "rm", "update", "add"},
    "yarn": {"add", "remove", "upgrade", "install"},
    "pnpm": {"add", "remove", "upgrade", "install"},
    "uv": {"add", "remove", "sync", "install", "python", "venv"},
    "poetry": {"add", "remove", "install", "update", "lock"},
    "conda": {"install", "update", "remove", "create", "env"},
    "mamba": {"install", "update", "remove", "create", "env"},
    "apt": {"install", "remove", "update", "upgrade", "autoremove"},
    "apt-get": {"install", "remove", "update", "upgrade", "autoremove"},
    "dnf": {"install", "remove", "update", "upgrade"},
    "yum": {"install", "remove", "update", "upgrade"},
    "gem": {"install", "uninstall", "update"},
    "cargo": {"install", "uninstall", "update", "add"},
    "go": {"get", "install", "mod"},
}
# 环境修改命令 (高危): 首词 + 子命令匹配

_GIT_READ = {"status", "log", "diff", "show", "branch", "remote", "tag", "ls-files", "grep", "rev-parse"}
# git 子命令分级
_GIT_WRITE = {"add", "commit", "init", "mv", "rm", "stash", "checkout", "merge", "rebase", "cherry-pick", "restore", "fetch", "pull"}
_GIT_HIGH = {"push", "clean", "reset", "revert"}


def _split_segments(command: str) -> list[str]:
    """
    按命令分隔符切分 (跳过引号内), 返回各段

    参数:
    - command: 命令内容

    返回:
    - list[str]: 各段
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch in ("&", "|", ";"):
            segments.append("".join(buf))
            buf = []
            if i + 1 < len(command) and command[i + 1] == ch:
                i += 1   # && / || 整体作为边界, 不进入任何段
        else:
            buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments


def _has_redirect(text: str) -> bool:
    """
    引号外是否含重定向符 > (写文件) 或 <

    参数:
    - text: 待处理文本

    返回:
    - bool: 引号外是否含重定向符 > (写文件) 或 <
    """
    quote: str | None = None
    for ch in text:
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch in (">", "<"):
            return True
    return False


def classify_command(command: str) -> tuple[RiskLevel, bool]:
    """
    分类 shell 命令 (整串逐段分类, 取最高风险级)

    参数:
    - command: 命令内容

    返回:
    - (风险级, 是否环境修改): 环境修改类命令需审批
    """
    text = command.strip()
    if not text:
        return RiskLevel.READ, False
    max_risk = RiskLevel.READ
    escape = False
    for segment in _split_segments(text):
        seg = segment.strip()
        if not seg:
            continue
        risk, esc = _classify_segment(seg)
        if risk > max_risk:
            max_risk = risk
        escape = escape or esc
    return max_risk, escape


def _classify_segment(segment: str) -> tuple[RiskLevel, bool]:
    """
    分类单段命令 (不含分隔符)

    参数:
    - segment: 分段

    返回:
    - tuple[RiskLevel, bool]: 分类单段命令 (不含分隔符)
    """
    text = segment.strip()
    if not text:
        return RiskLevel.READ, False
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(text):
            return RiskLevel.FORBIDDEN, False

    tokens = [t for t in text.split() if t]
    if not tokens:
        return RiskLevel.READ, False
    head = tokens[0].lower().strip('"').strip("'")

    if head in _ENV_MODIFY_SUBCOMMANDS:
        if _is_env_modify(tokens):
            return RiskLevel.HIGH, True

    if head in _HIGH_COMMANDS:
        if head == "git":
            return _classify_git(tokens[1:]), False
        if head == "sc" and len(tokens) > 1 and tokens[1].lower() in ("query", "queryex"):
            return RiskLevel.READ, False
        return RiskLevel.HIGH, False
    if head in _WRITE_COMMANDS:
        if head == "git":
            return _classify_git(tokens[1:]), False
        return RiskLevel.WRITE, False
    if head in _READ_COMMANDS:
        if _has_redirect(text):
            return RiskLevel.WRITE, False
        return RiskLevel.READ, False
    logger.debug(f"[satrap_coding] 未匹配命令表, 保守归 WRITE: {head}")
    return RiskLevel.WRITE, False


def _is_env_modify(tokens: list[str]) -> bool:
    """
    判断命令是否为环境修改, 如 pip install / npm install

    参数:
    - tokens: token 数量

    返回:
    - bool: 判断命令是否为环境修改, 如 pip install / npm install
    """
    if len(tokens) < 2:
        return False
    head = tokens[0].lower().strip('"').strip("'")
    sub = tokens[1].lower().rstrip(",").strip('"').strip("'")
    if head == "npm" and sub in ("i", "install", "add", "uninstall", "rm", "update"):
        return True
    return sub in _ENV_MODIFY_SUBCOMMANDS.get(head, set())


def _classify_git(sub_tokens: list[str]) -> RiskLevel:
    """
    git 子命令分级 (config/branch/tag/remote 按参数细粒度)

    参数:
    - sub_tokens: subtoken 数量

    返回:
    - RiskLevel: git 子命令分级 (config/branch/tag/remote 按参数细粒度)
    """
    sub = sub_tokens[0] if sub_tokens else ""
    if sub == "config":
        if len(sub_tokens) > 1:
            rest = [t for t in sub_tokens[1:] if not t.startswith("--") and t != "-l"]
            if len(rest) >= 2:
                return RiskLevel.WRITE   # git config key value = 写配置
        return RiskLevel.READ
    if sub in _GIT_READ:
        if sub == "branch":
            if any(t in ("-d", "-D", "-m", "-M", "-c", "-C", "-u") for t in sub_tokens[1:]):
                return RiskLevel.WRITE
            # 纯查询 (空/-a/-r/-v/--list) 只读; 其余 (创建/-d/-m 等) 为写
            rest = [t for t in sub_tokens[1:] if t not in ("-a", "-r", "-v", "-vv", "--list", "--no-color") and not t.startswith("--")]
            return RiskLevel.WRITE if rest else RiskLevel.READ
        if sub == "tag":
            rest = [t for t in sub_tokens[1:] if t not in ("-l", "--list", "-n") and not t.startswith("--")]
            return RiskLevel.WRITE if rest else RiskLevel.READ
        if sub == "remote":
            if len(sub_tokens) > 1 and sub_tokens[1] in ("add", "remove", "rename", "set-url", "set-head"):
                return RiskLevel.WRITE
            return RiskLevel.READ
        return RiskLevel.READ
    if sub in _GIT_WRITE:
        return RiskLevel.WRITE
    if sub in _GIT_HIGH:
        return RiskLevel.HIGH
    return RiskLevel.WRITE
