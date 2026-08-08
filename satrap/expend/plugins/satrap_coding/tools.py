"""satrap_coding 插件工具集: 文件 / ask_user / memory / shell / subagent + 复用 search

约定:
- get_tools(session) 工厂: 按会话形态 (SimpleSession / AsyncSimpleSession) 返回同步/异步工具,
  并注入会话依赖 (llm / 权限引擎 / 记忆库 / 沙箱 / 目标状态)
- 审批模型: 全部写类操作走 PermissionEngine (user 询问 / auto-agent 判断 / full 放行),
  plan mode 下写类操作被引擎直接拒绝
- 沙箱语义: 沙箱内执行无需审批; 环境修改 (pip install 等) = 越界, 走审批;
  外部工具 (shell/文件工具) 直接操作沙箱目录被拒绝
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from satrap.core.log import logger
from satrap.core.utils.paths import get_project_root
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.edictum import AsyncSimpleSession
from satrap.expend.command.session_commands import _parse_user_id
from satrap.expend.plugins.satrap_coding.core.command_gate import classify_command
from satrap.expend.plugins.satrap_coding.core.goal_state import GoalState
from satrap.expend.plugins.satrap_coding.core.memory_store import MemoryStore
from satrap.expend.plugins.satrap_coding.core.permission import (
    PermissionDecision,
    PermissionEngine,
    RiskLevel,
)
from satrap.expend.tools import AsyncFetchPageTool, AsyncSearchTool, FetchPageTool, SearchTool

WORKSPACE_ROOT = get_project_root()
"""文件工具白名单根目录 (项目根)"""

DATA_ROOT = WORKSPACE_ROOT / ".satrap" / "coding"
"""插件数据目录"""

_APPROVAL_PROMPT = """你是一个操作审批助手。请判断以下操作是否允许执行, 只回答一个词:
- allow: 操作安全或属于常规开发操作
- deny: 操作危险或明显有害
- ask: 不确定, 需要询问用户

操作类型: {operation}
风险等级: {risk} (0=只读 1=常规写 2=高危 3=禁止)
操作描述: {description}

回答 (allow/deny/ask):"""


def user_scope(session_id: str) -> str:
    """从 session_id 解析用户作用域 (user_id), 解析失败回落完整 session_id 保证隔离"""
    return _parse_user_id(session_id) or session_id


def _make_sync_judge(session: Any) -> Callable[[str, RiskLevel, str], str]:
    """构造同步 auto-agent 审批判断 (调用会话主模型, 只读判断不执行)"""

    def judge(operation: str, risk: RiskLevel, description: str) -> str:
        try:
            prompt = _APPROVAL_PROMPT.format(
                operation=operation, risk=int(risk), description=description,
            )
            resp = session.llm.chat(messages=[{"role": "user", "content": prompt}])
            return str(resp or "ask")[:20]
        except Exception as e:
            logger.warning(f"[satrap_coding] 审批判断失败, 降级询问: {e}")
            return "ask"

    return judge


def _make_async_judge(session: Any) -> Callable[[str, RiskLevel, str], Awaitable[str]]:
    """构造异步 auto-agent 审批判断"""

    async def judge(operation: str, risk: RiskLevel, description: str) -> str:
        try:
            prompt = _APPROVAL_PROMPT.format(
                operation=operation, risk=int(risk), description=description,
            )
            resp = await session.llm.chat(messages=[{"role": "user", "content": prompt}])
            return str(resp or "ask")[:20]
        except Exception as e:
            logger.warning(f"[satrap_coding] 审批判断失败, 降级询问: {e}")
            return "ask"

    return judge


def _ask_user_sync(
    session: Any,
    question: str,
    options: list[str] | None = None,
) -> str | None:
    """同步询问用户, 未配置输入通道返回 None"""
    provider = getattr(session, "user_input_provider", None)
    if provider is None:
        return None
    text = question
    if options:
        numbered = "  ".join(f"{i}. {opt}" for i, opt in enumerate(options, 1))
        text += f" 可选: {numbered}"
    try:
        return str(provider(text))
    except Exception as e:
        logger.warning(f"[satrap_coding] 用户输入通道异常: {e}")
        return None


async def _ask_user_async(
    session: Any,
    question: str,
    options: list[str] | None = None,
) -> str | None:
    """异步询问用户 (provider 可为同步或异步), 未配置输入通道返回 None"""
    provider = getattr(session, "user_input_provider", None)
    if provider is None:
        return None
    text = question
    if options:
        numbered = "  ".join(f"{i}. {opt}" for i, opt in enumerate(options, 1))
        text += f" 可选: {numbered}"
    try:
        answer = provider(text)
        if hasattr(answer, "__await__"):
            answer = await answer
        return str(answer)
    except Exception as e:
        logger.warning(f"[satrap_coding] 用户输入通道异常: {e}")
        return None


def _approve_sync(
    session: Any,
    engine: PermissionEngine,
    operation: str,
    risk: RiskLevel,
    description: str,
) -> tuple[bool, str]:
    """同步审批入口: 返回 (是否放行, 结果消息)"""
    decision = engine.evaluate(operation, risk, description, judge=_make_sync_judge(session))
    if decision == PermissionDecision.ALLOW:
        return True, ""
    if decision == PermissionDecision.DENY:
        return False, f"操作被拒绝: {description} (风险级 {int(risk)})"
    answer = _ask_user_sync(session, f"是否允许执行: {description}? (y 仅本次批准 / n 拒绝 / all 本会话全部放行)")
    if answer is None:
        return False, f"需要用户批准: {description} (未配置 user_input_provider, 已拒绝)"
    answer = answer.strip().lower()
    if answer in ("y", "yes", "允许", "批准"):
        engine.approve(operation, risk, remember=False)
        return True, ""
    if answer in ("all", "全部", "全放行"):
        engine.set_mode("full", persist=False)
        return True, "已授予本会话全部权限"
    return False, f"用户拒绝了操作: {description}"


async def _approve_async(
    session: Any,
    engine: PermissionEngine,
    operation: str,
    risk: RiskLevel,
    description: str,
) -> tuple[bool, str]:
    """异步审批入口: 返回 (是否放行, 结果消息)"""
    decision = await engine.evaluate_async(operation, risk, description, judge=_make_async_judge(session))
    if decision == PermissionDecision.ALLOW:
        return True, ""
    if decision == PermissionDecision.DENY:
        return False, f"操作被拒绝: {description} (风险级 {int(risk)})"
    answer = await _ask_user_async(session, f"是否允许执行: {description}? (y 仅本次批准 / n 拒绝 / all 本会话全部放行)")
    if answer is None:
        return False, f"需要用户批准: {description} (未配置 user_input_provider, 已拒绝)"
    answer = answer.strip().lower()
    if answer in ("y", "yes", "允许", "批准"):
        engine.approve(operation, risk, remember=False)
        return True, ""
    if answer in ("all", "全部", "全放行"):
        engine.set_mode("full", persist=False)
        return True, "已授予本会话全部权限"
    return False, f"用户拒绝了操作: {description}"


# ================= 文件工具 (工作区白名单 + 写审批) =================

_PROTECTED_DIRS = (".satrap", ".git", "node_modules")
_PROTECTED_FILES = (".env", ".env.local")


def _resolve_path(path: str) -> Path:
    """解析路径并校验在工作区内 (相对路径以工作区为基准), 越界抛 ValueError"""
    raw = Path(path)
    candidate = raw if raw.is_absolute() else WORKSPACE_ROOT / raw
    resolved = candidate.resolve()
    if resolved != WORKSPACE_ROOT and not resolved.is_relative_to(WORKSPACE_ROOT):
        raise ValueError(f"路径越出工作区: {resolved}")
    return resolved


# 参数里可被简单识别的绝对路径 (Windows 盘符或 UNC)
_OUTSIDE_PATH_RE = re.compile(r"(?:^|\s)([a-zA-Z]:\\|\\\\[^\\]+\\|/[a-zA-Z]/)")


def _has_outside_workspace_path(command: str) -> bool:
    """命令是否引用工作区外的绝对路径 (盘符/UNC 近似检测)

    无绝对路径或绝对路径都在工作区内 (含同盘符近似) 视为工作区内活动
    """
    root = str(WORKSPACE_ROOT).replace("\\", "/").lower()
    for m in _OUTSIDE_PATH_RE.findall(command):
        candidate = m.replace("\\", "/").lower()
        if candidate.endswith("/"):
            candidate = candidate[:-1]
        if not candidate or root.startswith(candidate):
            continue  # 工作区根本身或其父级盘符前缀
        return True
    return False


def _protection_reason(path: Path) -> str | None:
    """命中保护路径返回原因 (敏感目录/文件/沙箱目录)"""
    rel = path.relative_to(WORKSPACE_ROOT) if path.is_relative_to(WORKSPACE_ROOT) else path
    parts = [p.lower() for p in rel.parts]
    for name in _PROTECTED_DIRS:
        if name in parts:
            return f"路径位于受保护目录 {name}/ 下"
    for name in _PROTECTED_FILES:
        if path.name.lower() == name:
            return f"路径命中受保护文件 {name}"
    return None


def _register_protected_dir(path: str) -> None:
    """注册额外保护目录 (沙箱根), 全局生效"""
    _EXTRA_PROTECTED_DIRS.append(Path(path).resolve())


_EXTRA_PROTECTED_DIRS: list[Path] = []


def _protection_reason_full(path: Path) -> str | None:
    """含沙箱等额外保护目录的完整保护检查"""
    reason = _protection_reason(path)
    if reason is not None:
        return reason
    for extra in _EXTRA_PROTECTED_DIRS:
        if extra == WORKSPACE_ROOT:
            continue  # 工作区即沙箱时由工作区白名单统一管辖, 不重复拦截
        if path == extra or path.is_relative_to(extra):
            return f"路径位于受保护目录 {extra} 下 (敏感区域)"
    return None


def _session_sandbox_root(session: Any) -> Path:
    """获取会话沙箱根 (会话属性优先, 否则默认)"""
    return Path(getattr(session, "coding_sandbox_root", None) or DEFAULT_SANDBOX_ROOT).resolve()


def _in_sandbox(path: Path, session: Any) -> bool:
    """目标路径是否位于会话沙箱根内 (沙箱 = 免审批区)"""
    root = _session_sandbox_root(session)
    return path == root or path.is_relative_to(root)


def _approve_file_write(
    session: Any, engine: PermissionEngine, path: Path, action: str,
) -> tuple[bool, str]:
    """文件写审批: 目标在沙箱内免审批 (沙箱=免审批区), 其余按策略审批"""
    if _in_sandbox(path, session):
        return True, ""
    return _approve_sync(session, engine, "file_write", RiskLevel.WRITE, f"{action} {path}")


async def _approve_file_write_async(
    session: Any, engine: PermissionEngine, path: Path, action: str,
) -> tuple[bool, str]:
    """异步文件写审批: 沙箱内免审批, 其余按策略审批"""
    if _in_sandbox(path, session):
        return True, ""
    return await _approve_async(session, engine, "file_write", RiskLevel.WRITE, f"{action} {path}")


class ReadFileTool(Tool):
    """读取工作区内文件 (支持分页)"""

    tool_name = "read_file"
    description = "读取工作区内文件内容, offset/limit 支持分页读取大文件"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "offset": ("number", "起始行号, 默认 0"),
        "limit": ("number", "读取行数上限, 默认 200"),
    }

    def __init__(self) -> None:
        super().__init__()

    def execute(self, path: str, offset: int = 0, limit: int = 200) -> str:
        try:
            abs_path = _resolve_path(path)
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path)
        if reason is not None:
            return f"拒绝读取: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return f"错误: 读取失败: {e}"
        start = max(0, int(offset))
        end = min(len(lines), start + max(1, int(limit)))
        body = "\n".join(lines[start:end])
        return f"{abs_path} 行 {start}-{end}/{len(lines)}:\n{body}"


class WriteFileTool(Tool):
    """写入/追加工作区内文件 (写操作走审批)"""

    tool_name = "write_file"
    description = "写入或追加内容到工作区内文件 (需用户批准)"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "content": ("string", "要写入的内容"),
        "append": ("boolean", "是否追加, 默认 False (覆盖)"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        super().__init__()
        self.engine = engine

    def execute(self, path: str, content: str, append: bool = False) -> str:
        try:
            abs_path = _resolve_path(path)
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path)
        if reason is not None:
            return f"拒绝写入: {reason}"
        allowed, message = _approve_file_write(self._session, self.engine, abs_path, "写入文件")
        if not allowed:
            return message
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(abs_path, mode, encoding="utf-8") as f:
                f.write(content)
            return f"已{'追加' if append else '写入'}: {abs_path}"
        except OSError as e:
            return f"错误: 写入失败: {e}"

    # 会话注入 (工厂在注册前设置)
    def _bind(self, session: Any) -> None:
        self._session = session


class EditFileTool(Tool):
    """精确替换工作区内文件内容 (写操作走审批)"""

    tool_name = "edit_file"
    description = "在文件中精确替换一段文本 (old -> new), 用于定向修改"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "old": ("string", "要替换的原文 (必须精确匹配)"),
        "new": ("string", "替换后的内容"),
        "replace_all": ("boolean", "是否替换全部匹配, 默认 False (仅首处)"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        super().__init__()
        self.engine = engine

    def execute(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        try:
            abs_path = _resolve_path(path)
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path)
        if reason is not None:
            return f"拒绝修改: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            return f"错误: 读取失败: {e}"
        if old not in content:
            return f"错误: 未找到匹配文本: {old[:80]}"
        allowed, message = _approve_file_write(self._session, self.engine, abs_path, "编辑文件")
        if not allowed:
            return message
        try:
            if replace_all:
                updated = content.replace(old, new)
            else:
                updated = content.replace(old, new, 1)
            abs_path.write_text(updated, encoding="utf-8")
            return f"已编辑: {abs_path} ({content.count(old)} 处匹配, 修改 {'全部' if replace_all else '首处'})"
        except OSError as e:
            return f"错误: 写入失败: {e}"

    def _bind(self, session: Any) -> None:
        self._session = session


class SearchReplaceTool(Tool):
    """批量精确替换: 一个文件内多对 old->new (类似 IDE search & replace)"""

    tool_name = "search_replace"
    description = (
        "在一个文件中一次性执行多对精确文本替换 (类似 IDE 的 search & replace), "
        "每对可独立控制是否替换全部匹配; 审批规则同 edit_file"
    )
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "replacements": (
            "array",
            "替换列表, 每项为对象 {old: 原文, new: 新文, replace_all?: 是否替换全部匹配(默认 False)}",
        ),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        super().__init__()
        self.engine = engine

    def execute(self, path: str, replacements: list[dict[str, Any]]) -> str:
        try:
            abs_path = _resolve_path(path)
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path)
        if reason is not None:
            return f"拒绝修改: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            return f"错误: 读取失败: {e}"
        # 先校验全部 old 均存在, 避免部分替换后语义混乱
        pairs: list[tuple[str, str, bool]] = []
        for i, rep in enumerate(replacements or [], 1):
            if not isinstance(rep, dict) or not str(rep.get("old") or ""):
                return f"错误: 第 {i} 个替换项格式无效 (需 {{old, new, replace_all?}})"
            old = str(rep["old"])
            if old not in content:
                return f"错误: 第 {i} 个替换项未找到匹配: {old[:80]}"
            pairs.append((old, str(rep.get("new") or ""), bool(rep.get("replace_all"))))
        allowed, message = _approve_file_write(self._session, self.engine, abs_path, "批量替换")
        if not allowed:
            return message
        updated = content
        counts: list[int] = []
        for old, new, all_ in pairs:
            counts.append(updated.count(old))
            updated = updated.replace(old, new, -1 if all_ else 1)
        try:
            abs_path.write_text(updated, encoding="utf-8")
        except OSError as e:
            return f"错误: 写入失败: {e}"
        detail = ", ".join(f"'{old[:20]}' {c} 处" for (old, _, _), c in zip(pairs, counts))
        return f"已批量替换: {abs_path} ({detail})"

    def _bind(self, session: Any) -> None:
        self._session = session


class TodoWriteTool(Tool):
    """任务清单: 多步任务跟踪 (会话级状态)"""

    tool_name = "todo_write"
    description = "管理任务清单, 用于多步任务跟踪: add 添加, done 完成, list 查看, clear 清空"
    params_dict = {
        "operation": ("string", "操作: add / done / list / clear"),
        "item": ("string", "任务内容 (add 时必填)"),
        "index": ("number", "任务序号, 从 1 开始 (done 时必填)"),
    }

    def __init__(self, todos: dict[str, Any]) -> None:
        super().__init__()
        self.todos = todos

    def execute(self, operation: str, item: str = "", index: int = 0) -> str:
        op = (operation or "").strip().lower()
        items = self.todos["items"]
        if op == "add":
            text = item.strip()
            if not text:
                return "用法: todo_write operation=add item=<任务内容>"
            items.append({"text": text, "done": False})
            return f"任务已添加 ({len(items)}): {text}"
        if op == "done":
            if not 1 <= int(index) <= len(items):
                return f"任务序号无效: {index} (共 {len(items)} 项)"
            items[int(index) - 1]["done"] = True
            return f"任务 {index} 已完成: {items[int(index) - 1]['text']}"
        if op == "list":
            if not items:
                return "任务清单为空"
            return "\n".join(
                f"{i}. [{'x' if t['done'] else ' '}] {t['text']}" for i, t in enumerate(items, 1)
            )
        if op == "clear":
            count = len(items)
            items.clear()
            return f"任务清单已清空 ({count} 项)"
        return "用法: todo_write operation=add|done|list|clear (add 需 item, done 需 index)"


class ListDirTool(Tool):
    """列出工作区内目录内容"""

    tool_name = "list_dir"
    description = "列出工作区内目录下的文件与子目录"
    params_dict = {
        "path": ("string", "目录路径, 默认工作区根"),
    }

    def execute(self, path: str = "") -> str:
        try:
            base = _resolve_path(path) if path else WORKSPACE_ROOT
        except ValueError as e:
            return f"错误: {e}"
        if not base.is_dir():
            return f"错误: 目录不存在: {base}"
        entries = sorted(base.iterdir())
        lines = [f"{base}/:"]
        for entry in entries[:200]:
            tag = "/" if entry.is_dir() else ""
            lines.append(f"  {entry.name}{tag}")
        if len(entries) > 200:
            lines.append(f"  ... 共 {len(entries)} 项")
        return "\n".join(lines)


class GlobFilesTool(Tool):
    """按 glob 模式搜索工作区内文件"""

    tool_name = "glob_files"
    description = "按 glob 模式递归搜索文件, 如 **/*.py"
    params_dict = {
        "pattern": ("string", "glob 模式 (相对工作区)"),
    }

    def execute(self, pattern: str) -> str:
        matches = [
            str(p.relative_to(WORKSPACE_ROOT)).replace(os.sep, "/")
            for p in WORKSPACE_ROOT.glob(pattern)
            if p.is_file() and p.resolve().is_relative_to(WORKSPACE_ROOT) and _protection_reason_full(p) is None
        ]
        matches.sort()
        if not matches:
            return "未找到匹配文件"
        return "\n".join(matches[:100]) + (f"\n... 共 {len(matches)} 个" if len(matches) > 100 else "")


class GrepFilesTool(Tool):
    """正则搜索工作区内文件内容"""

    tool_name = "grep_files"
    description = "在指定目录内按正则表达式搜索文件内容, 返回匹配行"
    params_dict = {
        "pattern": ("string", "正则表达式"),
        "path": ("string", "搜索目录, 默认工作区根"),
        "glob": ("string", "文件过滤, 如 *.py"),
    }

    def execute(self, pattern: str, path: str = "", glob: str = "") -> str:
        try:
            base = _resolve_path(path) if path else WORKSPACE_ROOT
        except ValueError as e:
            return f"错误: {e}"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"错误: 正则无效: {e}"
        hits: list[str] = []
        for p in base.rglob(glob or "*"):
            if not p.is_file() or _protection_reason_full(p) is not None:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{p.relative_to(WORKSPACE_ROOT)}:{lineno}: {line[:200]}")
                    if len(hits) >= 100:
                        break
            if len(hits) >= 100:
                break
        if not hits:
            return "未找到匹配内容"
        return "\n".join(hits) + ("\n... 结果截断" if len(hits) >= 100 else "")


# ================= ask_user =================


class AskUserTool(Tool):
    """向用户询问请求, 等待用户回复"""

    tool_name = "ask_user"
    description = (
        "向用户提出一个问题并等待回复, 用于获取缺失信息或确认意图; "
        "建议提供 2-3 个推荐选项 (options), 用户可直接输入序号选择"
    )
    params_dict = {
        "question": ("string", "要询问的问题"),
        "options": ("array", "推荐回答选项 (2-3 个), 如 ['方案A', '方案B']"),
    }

    def __init__(self) -> None:
        super().__init__()

    def execute(self, question: str, options: list[str] | None = None) -> str:
        answer = _ask_user_sync(self._session, question, options)
        if answer is None:
            return "需要用户回复: " + question + " (未配置 user_input_provider, 请回复后继续)"
        return f"用户回复: {answer}"

    def _bind(self, session: Any) -> None:
        self._session = session


# ================= memory (模型自驱增删改) =================


class _MemoryToolBase(Tool):
    """记忆工具基类: 模式检查 + store/engine 绑定"""

    def __init__(self, store: MemoryStore, engine: PermissionEngine) -> None:
        super().__init__()
        self.store = store
        self.engine = engine

    def _plan_blocked(self) -> bool:
        """计划模式下写记忆被拒绝"""
        return self.engine.plan_mode


class AddMemoryTool(_MemoryToolBase):
    """添加一条长期记忆 (用户偏好/项目约定/关键决策)"""

    tool_name = "add_memory"
    description = "添加一条长期记忆, 记忆会注入后续对话上下文; 适合记录用户偏好、项目约定、关键决策"
    params_dict = {
        "title": ("string", "简短记忆标题"),
        "content": ("string", "记忆内容"),
        "tags": ("array", "分类标签"),
        "importance": ("number", "重要程度 1-5, 默认 1"),
    }

    def execute(self, title: str, content: str, tags: list[str] | None = None, importance: int = 1) -> str:
        if self._plan_blocked():
            return "拒绝: 计划模式下记忆写操作被禁用"
        if not self.store.can_write():
            return "记忆处于只读模式, 无法添加"
        result = self.store.add(title, content, tags, importance)
        if result.get("ok"):
            return f"记忆已添加: [{title}] {content}"
        return f"添加失败: {result.get('error')}"


class UpdateMemoryTool(_MemoryToolBase):
    """更新一条已有记忆"""

    tool_name = "update_memory"
    description = "按记忆 ID 更新已有长期记忆 (信息变化或修正时使用)"
    params_dict = {
        "memory_id": ("string", "要更新的记忆 ID"),
        "content": ("string", "更新后的内容"),
        "title": ("string", "更新后的标题"),
    }

    def execute(self, memory_id: str, content: str = "", title: str = "") -> str:
        if self._plan_blocked():
            return "拒绝: 计划模式下记忆写操作被禁用"
        if not self.store.can_write():
            return "记忆处于只读模式, 无法更新"
        fields: dict[str, Any] = {}
        if content:
            fields["content"] = content
        if title:
            fields["title"] = title
        result = self.store.update(memory_id, **fields)
        if result.get("ok"):
            return f"记忆已更新: [{result['title']}] {result['content']}"
        return f"更新失败: {result.get('error')}"


class DeleteMemoryTool(_MemoryToolBase):
    """删除一条记忆 (过时/矛盾时使用)"""

    tool_name = "delete_memory"
    description = "按记忆 ID 删除一条长期记忆"
    params_dict = {
        "memory_id": ("string", "要删除的记忆 ID"),
    }

    def execute(self, memory_id: str) -> str:
        if self._plan_blocked():
            return "拒绝: 计划模式下记忆写操作被禁用"
        if not self.store.can_write():
            return "记忆处于只读模式, 无法删除"
        result = self.store.delete(memory_id)
        if result.get("ok"):
            return f"记忆已删除: {memory_id}"
        return f"删除失败: {result.get('error')}"


class ListMemoriesTool(_MemoryToolBase):
    """查看全部长期记忆"""

    tool_name = "list_memories"
    description = "列出当前全部长期记忆 (含 ID, 供 update/delete 定位)"
    params_dict = {}

    def execute(self) -> str:
        memories = self.store.list_all()
        if not memories:
            return "当前没有长期记忆"
        lines = [f"共 {len(memories)} 条记忆:"]
        for m in memories:
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            lines.append(f"- {m['id'][:8]} [{m['title']}] {m['content']}{tags} (重要度 {m['importance']})")
        return "\n".join(lines)


# ================= shell (本机 cmd/powershell + 审批) =================


class ShellTool(Tool):
    """执行本机 shell 命令 (PowerShell/cmd), 写操作走审批"""

    tool_name = "shell"
    description = "在本机执行 shell 命令 (PowerShell/cmd), 返回输出; 只读命令直接执行, 写/高危命令需批准"
    params_dict = {
        "command": ("string", "要执行的命令"),
        "cwd": ("string", "工作目录, 默认项目根"),
        "timeout": ("number", "超时秒数, 默认 120"),
        "shell": ("string", "shell 类型: powershell / cmd, 默认 powershell"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        super().__init__()
        self.engine = engine

    def execute(self, command: str, cwd: str = "", timeout: int = 120, shell: str = "powershell") -> str:
        risk, escape = classify_command(command)
        if escape:
            allowed, message = _approve_sync(
                self._session, self.engine, "sandbox_escape", risk,
                f"环境修改命令: {command}",
            )
            if not allowed:
                return message
        elif risk == RiskLevel.FORBIDDEN:
            return f"命令被拒绝 (黑名单): {command}"
        elif risk > RiskLevel.READ:
            if _has_outside_workspace_path(command):
                allowed, message = _approve_sync(
                    self._session, self.engine, "shell", risk, f"执行命令: {command}",
                )
                if not allowed:
                    return message
            elif self.engine.plan_mode:
                return "拒绝: 计划模式下写类命令被禁用"
        try:
            workdir = str(_resolve_path(cwd)) if cwd else str(WORKSPACE_ROOT)
        except ValueError as e:
            return f"错误: {e}"
        try:
            executable = "powershell.exe" if shell.lower() == "powershell" else "cmd.exe"
            result = subprocess.run(
                [executable, "-NoProfile", "-Command", command] if executable == "powershell.exe" else [executable, "/C", command],
                cwd=workdir,
                capture_output=True,
                text=True,
                check=False,
                timeout=max(1, int(timeout)),
            )
            output = (result.stdout or "")[-20000:]
            error = (result.stderr or "")[-20000:]
            if result.returncode == 0:
                return output or "(无输出)"
            return f"退出码 {result.returncode}:\n{error or output}"
        except subprocess.TimeoutExpired:
            return f"执行超时 ({timeout}s): {command}"
        except OSError as e:
            return f"错误: 执行失败: {e}"

    def _bind(self, session: Any) -> None:
        self._session = session


# ================= subagent (独立上下文 + 权限继承) =================


class _CodingSubAgent:
    """独立上下文的子代理 (自定义 system prompt / 工具白名单 / 迭代上限)"""

    def __init__(self, llm: Any, tools_manager: Any, system_prompt: str, tools: list[str] | None = None):
        from satrap.core.framework.Base import ModelWorkflowFramework

        sub_manager = tools_manager
        if tools is not None:
            sub_manager = type(tools_manager)()
            for name in tools:
                tool = tools_manager.tools.get(name)
                if tool is not None:
                    sub_manager.register_tool(tool)
        self.llm = llm
        self.tools_manager = sub_manager
        self.system_prompt = system_prompt
        self.context_id = f"coding_sub_{uuid.uuid4().hex[:8]}"

    def forward(self, task: str, max_turns: int = 20) -> str:
        from satrap.core.framework.Base import ModelWorkflowFramework

        sub = ModelWorkflowFramework(
            self.llm,
            context_id=self.context_id,
            tools_manager=self.tools_manager,
            system_prompt=self.system_prompt,
        )
        return sub.tools_agent(task, max_iterations=max_turns)


class _AsyncCodingSubAgent:
    """异步独立上下文的子代理"""

    def __init__(self, llm: Any, tools_manager: Any, system_prompt: str, tools: list[str] | None = None):
        from satrap.core.framework.Base import AsyncModelWorkflowFramework

        sub_manager = tools_manager
        if tools is not None:
            sub_manager = type(tools_manager)()
            for name in tools:
                tool = tools_manager.tools.get(name)
                if tool is not None:
                    sub_manager.register_tool(tool)
        self.llm = llm
        self.tools_manager = sub_manager
        self.system_prompt = system_prompt
        self.context_id = f"coding_sub_{uuid.uuid4().hex[:8]}"

    async def forward(self, task: str, max_turns: int = 20) -> str:
        from satrap.core.framework.Base import AsyncModelWorkflowFramework

        sub = AsyncModelWorkflowFramework(
            self.llm,
            context_id=self.context_id,
            tools_manager=self.tools_manager,
            system_prompt=self.system_prompt,
        )
        return await sub.tools_agent(task, max_iterations=max_turns)


_SUBAGENT_PROMPT = """你是一个子代理, 在独立上下文中处理分配给你的子任务。
使用可用工具完成任务并返回结果摘要。不要修改与任务无关的内容。"""


class SubAgentTool(Tool):
    """子代理: 独立上下文处理任务, 继承主会话工具与审批策略"""

    tool_name = "subagent"
    description = "在独立上下文中运行子代理处理任务, 返回结果; 用于并行调研/独立子任务"
    params_dict = {
        "task": ("string", "子代理要完成的任务描述"),
        "tools": ("array", "允许使用的工具名白名单, 缺省使用全部工具"),
        "system_prompt": ("string", "自定义子代理系统提示词"),
        "max_turns": ("number", "最大工具迭代轮数, 默认 20"),
    }

    def __init__(self, llm: Any, tools_manager: Any) -> None:
        super().__init__()
        self.llm = llm
        self.tools_manager = tools_manager

    def execute(self, task: str, tools: list[str] | None = None, system_prompt: str = "", max_turns: int = 20) -> str:
        agent = _CodingSubAgent(
            self.llm, self.tools_manager, system_prompt or _SUBAGENT_PROMPT, tools,
        )
        return agent.forward(task, max(max_turns, 1))


# ================= 异步工具 (AsyncSimpleSession 形态) =================


class AsyncAskUserTool(AsyncTool):
    """向用户询问请求 (异步)"""

    tool_name = "ask_user"
    description = (
        "向用户提出一个问题并等待回复, 用于获取缺失信息或确认意图; "
        "建议提供 2-3 个推荐选项 (options), 用户可直接输入序号选择"
    )
    params_dict = {
        "question": ("string", "要询问的问题"),
        "options": ("array", "推荐回答选项 (2-3 个), 如 ['方案A', '方案B']"),
    }

    def __init__(self) -> None:
        super().__init__()
        self._session: Any = None

    async def execute(self, question: str, options: list[str] | None = None) -> str:
        answer = await _ask_user_async(self._session, question, options)
        if answer is None:
            return "需要用户回复: " + question + " (未配置 user_input_provider, 请回复后继续)"
        return f"用户回复: {answer}"

    def _bind(self, session: Any) -> None:
        self._session = session


class AsyncAddMemoryTool(AsyncTool):
    """添加长期记忆 (异步)"""

    tool_name = "add_memory"
    description = "添加一条长期记忆, 记忆会注入后续对话上下文; 适合记录用户偏好、项目约定、关键决策"
    params_dict = {
        "title": ("string", "简短记忆标题"),
        "content": ("string", "记忆内容"),
        "tags": ("array", "分类标签"),
        "importance": ("number", "重要程度 1-5, 默认 1"),
    }

    def __init__(self, store: MemoryStore, engine: PermissionEngine) -> None:
        super().__init__()
        self.store = store
        self.engine = engine

    async def execute(self, title: str, content: str, tags: list[str] | None = None, importance: int = 1) -> str:
        if self.engine.plan_mode:
            return "拒绝: 计划模式下记忆写操作被禁用"
        if not self.store.can_write():
            return "记忆处于只读模式, 无法添加"
        result = self.store.add(title, content, tags, importance)
        if result.get("ok"):
            return f"记忆已添加: [{title}] {content}"
        return f"添加失败: {result.get('error')}"


class AsyncUpdateMemoryTool(AsyncTool):
    """更新长期记忆 (异步)"""

    tool_name = "update_memory"
    description = "按记忆 ID 更新已有长期记忆 (信息变化或修正时使用)"
    params_dict = {
        "memory_id": ("string", "要更新的记忆 ID"),
        "content": ("string", "更新后的内容"),
        "title": ("string", "更新后的标题"),
    }

    def __init__(self, store: MemoryStore, engine: PermissionEngine) -> None:
        super().__init__()
        self.store = store
        self.engine = engine

    async def execute(self, memory_id: str, content: str = "", title: str = "") -> str:
        if self.engine.plan_mode:
            return "拒绝: 计划模式下记忆写操作被禁用"
        if not self.store.can_write():
            return "记忆处于只读模式, 无法更新"
        fields: dict[str, Any] = {}
        if content:
            fields["content"] = content
        if title:
            fields["title"] = title
        result = self.store.update(memory_id, **fields)
        if result.get("ok"):
            return f"记忆已更新: [{result['title']}] {result['content']}"
        return f"更新失败: {result.get('error')}"


class AsyncDeleteMemoryTool(AsyncTool):
    """删除长期记忆 (异步)"""

    tool_name = "delete_memory"
    description = "按记忆 ID 删除一条长期记忆"
    params_dict = {
        "memory_id": ("string", "要删除的记忆 ID"),
    }

    def __init__(self, store: MemoryStore, engine: PermissionEngine) -> None:
        super().__init__()
        self.store = store
        self.engine = engine

    async def execute(self, memory_id: str) -> str:
        if self.engine.plan_mode:
            return "拒绝: 计划模式下记忆写操作被禁用"
        if not self.store.can_write():
            return "记忆处于只读模式, 无法删除"
        result = self.store.delete(memory_id)
        if result.get("ok"):
            return f"记忆已删除: {memory_id}"
        return f"删除失败: {result.get('error')}"


class AsyncListMemoriesTool(AsyncTool):
    """查看全部长期记忆 (异步)"""

    tool_name = "list_memories"
    description = "列出当前全部长期记忆 (含 ID, 供 update/delete 定位)"
    params_dict = {}

    def __init__(self, store: MemoryStore, engine: PermissionEngine) -> None:
        super().__init__()
        self.store = store
        self.engine = engine

    async def execute(self) -> str:
        memories = self.store.list_all()
        if not memories:
            return "当前没有长期记忆"
        lines = [f"共 {len(memories)} 条记忆:"]
        for m in memories:
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            lines.append(f"- {m['id'][:8]} [{m['title']}] {m['content']}{tags} (重要度 {m['importance']})")
        return "\n".join(lines)


class AsyncShellTool(AsyncTool):
    """执行本机 shell 命令 (异步), 写操作走审批"""

    tool_name = "shell"
    description = "在本机执行 shell 命令 (PowerShell/cmd), 返回输出; 只读命令直接执行, 写/高危命令需批准"
    params_dict = {
        "command": ("string", "要执行的命令"),
        "cwd": ("string", "工作目录, 默认项目根"),
        "timeout": ("number", "超时秒数, 默认 120"),
        "shell": ("string", "shell 类型: powershell / cmd, 默认 powershell"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        super().__init__()
        self.engine = engine
        self._session: Any = None

    async def execute(self, command: str, cwd: str = "", timeout: int = 120, shell: str = "powershell") -> str:
        risk, escape = classify_command(command)
        if escape:
            allowed, message = await _approve_async(
                self._session, self.engine, "sandbox_escape", risk,
                f"环境修改命令: {command}",
            )
            if not allowed:
                return message
        elif risk == RiskLevel.FORBIDDEN:
            return f"命令被拒绝 (黑名单): {command}"
        elif risk > RiskLevel.READ:
            if _has_outside_workspace_path(command):
                allowed, message = await _approve_async(
                    self._session, self.engine, "shell", risk, f"执行命令: {command}",
                )
                if not allowed:
                    return message
            elif self.engine.plan_mode:
                return "拒绝: 计划模式下写类命令被禁用"
        try:
            workdir = str(_resolve_path(cwd)) if cwd else str(WORKSPACE_ROOT)
        except ValueError as e:
            return f"错误: {e}"
        try:
            executable = "powershell.exe" if shell.lower() == "powershell" else "cmd.exe"
            args = (
                [executable, "-NoProfile", "-Command", command]
                if executable == "powershell.exe" else [executable, "/C", command]
            )
            result = await asyncio.to_thread(
                subprocess.run, args,
                cwd=workdir, capture_output=True, text=True, check=False,
                timeout=max(1, int(timeout)),
            )
            output = (result.stdout or "")[-20000:]
            error = (result.stderr or "")[-20000:]
            if result.returncode == 0:
                return output or "(无输出)"
            return f"退出码 {result.returncode}:\n{error or output}"
        except subprocess.TimeoutExpired:
            return f"执行超时 ({timeout}s): {command}"
        except OSError as e:
            return f"错误: 执行失败: {e}"

    def _bind(self, session: Any) -> None:
        self._session = session


class AsyncSubAgentTool(AsyncTool):
    """子代理 (异步): 独立上下文, 继承主会话工具与审批策略"""

    tool_name = "subagent"
    description = "在独立上下文中运行子代理处理任务, 返回结果; 用于并行调研/独立子任务"
    params_dict = {
        "task": ("string", "子代理要完成的任务描述"),
        "tools": ("array", "允许使用的工具名白名单, 缺省使用全部工具"),
        "system_prompt": ("string", "自定义子代理系统提示词"),
        "max_turns": ("number", "最大工具迭代轮数, 默认 20"),
    }

    def __init__(self, llm: Any, tools_manager: Any) -> None:
        super().__init__()
        self.llm = llm
        self.tools_manager = tools_manager

    async def execute(self, task: str, tools: list[str] | None = None, system_prompt: str = "", max_turns: int = 20) -> str:
        agent = _AsyncCodingSubAgent(
            self.llm, self.tools_manager, system_prompt or _SUBAGENT_PROMPT, tools,
        )
        return await agent.forward(task, max(max_turns, 1))


# ================= 异步文件工具 =================


class AsyncReadFileTool(AsyncTool):
    """读取工作区内文件 (异步)"""

    tool_name = "read_file"
    description = "读取工作区内文件内容, offset/limit 支持分页读取大文件"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "offset": ("number", "起始行号, 默认 0"),
        "limit": ("number", "读取行数上限, 默认 200"),
    }

    async def execute(self, path: str, offset: int = 0, limit: int = 200) -> str:
        try:
            abs_path = _resolve_path(path)
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path)
        if reason is not None:
            return f"拒绝读取: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            lines = await asyncio.to_thread(
                lambda: abs_path.read_text(encoding="utf-8", errors="replace").splitlines(),
            )
        except OSError as e:
            return f"错误: 读取失败: {e}"
        start = max(0, int(offset))
        end = min(len(lines), start + max(1, int(limit)))
        body = "\n".join(lines[start:end])
        return f"{abs_path} 行 {start}-{end}/{len(lines)}:\n{body}"


class AsyncWriteFileTool(AsyncTool):
    """写入/追加工作区内文件 (异步, 写操作走审批)"""

    tool_name = "write_file"
    description = "写入或追加内容到工作区内文件 (需用户批准)"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "content": ("string", "要写入的内容"),
        "append": ("boolean", "是否追加, 默认 False (覆盖)"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        super().__init__()
        self.engine = engine
        self._session: Any = None

    async def execute(self, path: str, content: str, append: bool = False) -> str:
        try:
            abs_path = _resolve_path(path)
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path)
        if reason is not None:
            return f"拒绝写入: {reason}"
        allowed, message = await _approve_file_write_async(
            self._session, self.engine, abs_path, "写入文件",
        )
        if not allowed:
            return message

        def _do_write() -> None:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(abs_path, mode, encoding="utf-8") as f:
                f.write(content)

        try:
            await asyncio.to_thread(_do_write)
            return f"已{'追加' if append else '写入'}: {abs_path}"
        except OSError as e:
            return f"错误: 写入失败: {e}"

    def _bind(self, session: Any) -> None:
        self._session = session


class AsyncEditFileTool(AsyncTool):
    """精确替换工作区内文件内容 (异步, 写操作走审批)"""

    tool_name = "edit_file"
    description = "在文件中精确替换一段文本 (old -> new), 用于定向修改"
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "old": ("string", "要替换的原文 (必须精确匹配)"),
        "new": ("string", "替换后的内容"),
        "replace_all": ("boolean", "是否替换全部匹配, 默认 False (仅首处)"),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        super().__init__()
        self.engine = engine
        self._session: Any = None

    async def execute(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        try:
            abs_path = _resolve_path(path)
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path)
        if reason is not None:
            return f"拒绝修改: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            content = await asyncio.to_thread(abs_path.read_text, encoding="utf-8")
        except OSError as e:
            return f"错误: 读取失败: {e}"
        if old not in content:
            return f"错误: 未找到匹配文本: {old[:80]}"
        allowed, message = await _approve_file_write_async(
            self._session, self.engine, abs_path, "编辑文件",
        )
        if not allowed:
            return message

        def _do_edit() -> None:
            updated = content.replace(old, new, -1 if replace_all else 1)
            abs_path.write_text(updated, encoding="utf-8")

        try:
            await asyncio.to_thread(_do_edit)
            return f"已编辑: {abs_path} ({content.count(old)} 处匹配, 修改 {'全部' if replace_all else '首处'})"
        except OSError as e:
            return f"错误: 写入失败: {e}"

    def _bind(self, session: Any) -> None:
        self._session = session


class AsyncSearchReplaceTool(AsyncTool):
    """批量精确替换 (异步): 一个文件内多对 old->new"""

    tool_name = "search_replace"
    description = (
        "在一个文件中一次性执行多对精确文本替换 (类似 IDE 的 search & replace), "
        "每对可独立控制是否替换全部匹配; 审批规则同 edit_file"
    )
    params_dict = {
        "path": ("string", "文件路径 (绝对或相对工作区)"),
        "replacements": (
            "array",
            "替换列表, 每项为对象 {old: 原文, new: 新文, replace_all?: 是否替换全部匹配(默认 False)}",
        ),
    }

    def __init__(self, engine: PermissionEngine) -> None:
        super().__init__()
        self.engine = engine
        self._session: Any = None

    async def execute(self, path: str, replacements: list[dict[str, Any]]) -> str:
        try:
            abs_path = _resolve_path(path)
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path)
        if reason is not None:
            return f"拒绝修改: {reason}"
        if not abs_path.is_file():
            return f"错误: 文件不存在: {abs_path}"
        try:
            content = await asyncio.to_thread(abs_path.read_text, encoding="utf-8")
        except OSError as e:
            return f"错误: 读取失败: {e}"
        pairs: list[tuple[str, str, bool]] = []
        for i, rep in enumerate(replacements or [], 1):
            if not isinstance(rep, dict) or not str(rep.get("old") or ""):
                return f"错误: 第 {i} 个替换项格式无效 (需 {{old, new, replace_all?}})"
            old = str(rep["old"])
            if old not in content:
                return f"错误: 第 {i} 个替换项未找到匹配: {old[:80]}"
            pairs.append((old, str(rep.get("new") or ""), bool(rep.get("replace_all"))))
        allowed, message = await _approve_file_write_async(
            self._session, self.engine, abs_path, "批量替换",
        )
        if not allowed:
            return message

        def _do_replace() -> tuple[list[int], str]:
            updated = content
            counts: list[int] = []
            for old, new, all_ in pairs:
                counts.append(updated.count(old))
                updated = updated.replace(old, new, -1 if all_ else 1)
            abs_path.write_text(updated, encoding="utf-8")
            return counts, updated

        try:
            counts, _ = await asyncio.to_thread(_do_replace)
        except OSError as e:
            return f"错误: 写入失败: {e}"
        detail = ", ".join(f"'{old[:20]}' {c} 处" for (old, _, _), c in zip(pairs, counts))
        return f"已批量替换: {abs_path} ({detail})"

    def _bind(self, session: Any) -> None:
        self._session = session


class AsyncTodoWriteTool(AsyncTool):
    """任务清单 (异步): 多步任务跟踪"""

    tool_name = "todo_write"
    description = "管理任务清单, 用于多步任务跟踪: add 添加, done 完成, list 查看, clear 清空"
    params_dict = {
        "operation": ("string", "操作: add / done / list / clear"),
        "item": ("string", "任务内容 (add 时必填)"),
        "index": ("number", "任务序号, 从 1 开始 (done 时必填)"),
    }

    def __init__(self, todos: dict[str, Any]) -> None:
        super().__init__()
        self.todos = todos

    async def execute(self, operation: str, item: str = "", index: int = 0) -> str:
        op = (operation or "").strip().lower()
        items = self.todos["items"]
        if op == "add":
            text = item.strip()
            if not text:
                return "用法: todo_write operation=add item=<任务内容>"
            items.append({"text": text, "done": False})
            return f"任务已添加 ({len(items)}): {text}"
        if op == "done":
            if not 1 <= int(index) <= len(items):
                return f"任务序号无效: {index} (共 {len(items)} 项)"
            items[int(index) - 1]["done"] = True
            return f"任务 {index} 已完成: {items[int(index) - 1]['text']}"
        if op == "list":
            if not items:
                return "任务清单为空"
            return "\n".join(
                f"{i}. [{'x' if t['done'] else ' '}] {t['text']}" for i, t in enumerate(items, 1)
            )
        if op == "clear":
            count = len(items)
            items.clear()
            return f"任务清单已清空 ({count} 项)"
        return "用法: todo_write operation=add|done|list|clear (add 需 item, done 需 index)"


class AsyncListDirTool(AsyncTool):
    """列出工作区内目录内容 (异步)"""

    tool_name = "list_dir"
    description = "列出工作区内目录下的文件与子目录"
    params_dict = {
        "path": ("string", "目录路径, 默认工作区根"),
    }

    async def execute(self, path: str = "") -> str:
        try:
            base = _resolve_path(path) if path else WORKSPACE_ROOT
        except ValueError as e:
            return f"错误: {e}"
        if not base.is_dir():
            return f"错误: 目录不存在: {base}"
        entries = sorted(await asyncio.to_thread(lambda: list(base.iterdir())))
        lines = [f"{base}/:"]
        for entry in entries[:200]:
            tag = "/" if entry.is_dir() else ""
            lines.append(f"  {entry.name}{tag}")
        if len(entries) > 200:
            lines.append(f"  ... 共 {len(entries)} 项")
        return "\n".join(lines)


class AsyncGlobFilesTool(AsyncTool):
    """按 glob 模式搜索工作区内文件 (异步)"""

    tool_name = "glob_files"
    description = "按 glob 模式递归搜索文件, 如 **/*.py"
    params_dict = {
        "pattern": ("string", "glob 模式 (相对工作区)"),
    }

    async def execute(self, pattern: str) -> str:
        matches = [
            str(p.relative_to(WORKSPACE_ROOT)).replace(os.sep, "/")
            for p in await asyncio.to_thread(lambda: list(WORKSPACE_ROOT.glob(pattern)))
            if p.is_file() and p.resolve().is_relative_to(WORKSPACE_ROOT) and _protection_reason_full(p) is None
        ]
        matches.sort()
        if not matches:
            return "未找到匹配文件"
        return "\n".join(matches[:100]) + (f"\n... 共 {len(matches)} 个" if len(matches) > 100 else "")


class AsyncGrepFilesTool(AsyncTool):
    """正则搜索工作区内文件内容 (异步)"""

    tool_name = "grep_files"
    description = "在指定目录内按正则表达式搜索文件内容, 返回匹配行"
    params_dict = {
        "pattern": ("string", "正则表达式"),
        "path": ("string", "搜索目录, 默认工作区根"),
        "glob": ("string", "文件过滤, 如 *.py"),
    }

    async def execute(self, pattern: str, path: str = "", glob: str = "") -> str:
        try:
            base = _resolve_path(path) if path else WORKSPACE_ROOT
        except ValueError as e:
            return f"错误: {e}"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"错误: 正则无效: {e}"

        def _scan() -> list[str]:
            hits: list[str] = []
            for p in base.rglob(glob or "*"):
                if not p.is_file() or _protection_reason_full(p) is not None:
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        hits.append(f"{p.relative_to(WORKSPACE_ROOT)}:{lineno}: {line[:200]}")
                        if len(hits) >= 100:
                            return hits
            return hits

        hits = await asyncio.to_thread(_scan)
        if not hits:
            return "未找到匹配内容"
        return "\n".join(hits) + ("\n... 结果截断" if len(hits) >= 100 else "")


# ================= get_tools 工厂 (会话依赖注入) =================

DEFAULT_SANDBOX_ROOT = DATA_ROOT / "sandbox"
"""默认沙箱根目录 (可被 session.coding_sandbox_root 覆盖; 工作区与文件免审批判定共用)"""


def get_tools(session: Any) -> list[Any]:
    """按会话形态构建全部工具 (注入 llm / 权限引擎 / 记忆库 / 会话引用)"""
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state

    state = get_plugin_state(session)
    engine = cast(PermissionEngine, state["engine"])
    store = cast(MemoryStore, state["store"])
    todos = cast(dict[str, Any], state["todos"])

    if isinstance(session, AsyncSimpleSession):
        tools: list[Any] = [
            AsyncAskUserTool(),
            AsyncSearchTool(),
            AsyncFetchPageTool(),
            AsyncAddMemoryTool(store, engine),
            AsyncUpdateMemoryTool(store, engine),
            AsyncDeleteMemoryTool(store, engine),
            AsyncListMemoriesTool(store, engine),
            AsyncShellTool(engine),
            AsyncSubAgentTool(session.llm, session.tools_manager),
            AsyncReadFileTool(),
            AsyncWriteFileTool(engine),
            AsyncEditFileTool(engine),
            AsyncSearchReplaceTool(engine),
            AsyncTodoWriteTool(todos),
            AsyncListDirTool(),
            AsyncGlobFilesTool(),
            AsyncGrepFilesTool(),
        ]
    else:
        tools = [
            AskUserTool(),
            SearchTool(),
            FetchPageTool(),
            AddMemoryTool(store, engine),
            UpdateMemoryTool(store, engine),
            DeleteMemoryTool(store, engine),
            ListMemoriesTool(store, engine),
            ShellTool(engine),
            SubAgentTool(session.llm, session.tools_manager),
            ReadFileTool(),
            WriteFileTool(engine),
            EditFileTool(engine),
            SearchReplaceTool(engine),
            TodoWriteTool(todos),
            ListDirTool(),
            GlobFilesTool(),
            GrepFilesTool(),
        ]
    for tool in tools:
        bind = getattr(tool, "_bind", None)
        if bind is not None:
            bind(session)
    return tools

