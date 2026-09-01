"""
satrap_coding 插件工具集: 文件 / ask_user / shell / subagent

> 联网搜索 (search/fetch_page) 与长期记忆 (memory) 已移交 base_take 插件,
> 本插件只保留 coding 专属能力

约定:
- get_tools(session) 工厂: 按会话形态 (SimpleSession / AsyncSimpleSession) 返回同步/异步工具,
  并注入会话依赖 (llm / 权限引擎 / 沙箱 / 目标状态)
- 审批模型: 全部写类操作走 PermissionEngine (user 询问 / auto-agent 判断 / full 放行),
  plan mode 下写类操作被引擎直接拒绝
- 沙箱语义: 沙箱内执行无需审批; 环境修改 (pip install 等) = 越界, 走审批;
  外部工具 (shell/文件工具) 直接操作沙箱目录被拒绝
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from satrap.core.framework.Base import AsyncModelWorkflowFramework, ModelWorkflowFramework
from satrap.core.log import logger
from satrap.core.type import safe_getattr, safe_getattr_callable
from satrap.core.utils.paths import get_project_root
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap.expend.plugins.satrap_coding.core.command_gate import classify_command
from satrap.expend.plugins.satrap_coding.core.permission import (
    PermissionDecision,
    PermissionEngine,
    RiskLevel,
)
from satrap.expend.plugins.satrap_coding.state import get_plugin_state

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


def _make_sync_judge(session: SimpleSession) -> Callable[[str, RiskLevel, str], str]:
    """
    构造同步 auto-agent 审批判断 (调用会话主模型, 只读判断不执行)

    参数:
    - session: 会话

    返回:
    - Callable[[str, RiskLevel, str], str]: 构造同步 auto-agent 审批判断 (调用会话主模型, 只读判断不执行)
    """

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


def _make_async_judge(session: AsyncSimpleSession) -> Callable[[str, RiskLevel, str], Awaitable[str]]:
    """
    构造异步 auto-agent 审批判断

    参数:
    - session: 会话

    返回:
    - Callable[[str, RiskLevel, str], Awaitable[str]]: 构造异步 auto-agent 审批判断
    """

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
    session: SimpleSession,
    question: str,
    options: list[str] | None = None,
) -> str | None:
    """
    同步询问用户, 未配置输入通道返回 None

    参数:
    - session: 会话
    - question: 问题内容
    - options: 选项集合

    返回:
    - str | None:  None
    """
    provider = safe_getattr_callable(session, "user_input_provider")
    if provider is None:
        return None
    try:
        return str(_call_user_input_provider(provider, question, options))
    except Exception as e:
        logger.warning(f"[satrap_coding] 用户输入通道异常: {e}")
        return None


async def _ask_user_async(
    session: AsyncSimpleSession,
    question: str,
    options: list[str] | None = None,
) -> str | None:
    """
    异步询问用户 (provider 可为同步或异步), 未配置输入通道返回 None

    参数:
    - session: 会话
    - question: 问题内容
    - options: 选项集合

    返回:
    - str | None:  None
    """
    provider = safe_getattr_callable(session, "user_input_provider")
    if provider is None:
        return None
    try:
        answer = _call_user_input_provider(provider, question, options)
        if hasattr(answer, "__await__"):
            answer = await answer
        return str(answer)
    except Exception as e:
        logger.warning(f"[satrap_coding] 用户输入通道异常: {e}")
        return None


def _call_user_input_provider(
    provider: Callable[..., Any],
    question: str,
    options: list[str] | None,
) -> Any:
    """
    调用用户输入通道, 新通道结构化传递选项, 旧通道保留拼接文本

    参数:
    - provider: 用户输入回调
    - question: 问题内容
    - options: 选项集合

    返回:
    - Any: Provider 返回值
    """
    normalized_options = [str(option).strip() for option in options or [] if str(option).strip()]
    if normalized_options:
        try:
            inspect.signature(provider).bind(question, normalized_options)
        except (TypeError, ValueError):
            numbered = "  ".join(
                f"{index}. {option}" for index, option in enumerate(normalized_options, 1)
            )
            return provider(f"{question} 可选: {numbered}")
        return provider(question, normalized_options)
    return provider(question)


def _approve_sync(
    session: SimpleSession,
    engine: PermissionEngine,
    operation: str,
    risk: RiskLevel,
    description: str,
) -> tuple[bool, str]:
    """
    同步审批入口: 返回 (是否放行, 结果消息)

    参数:
    - session: 会话
    - engine: 执行引擎
    - operation: 操作信息
    - risk: 风险级别
    - description: 说明文本

    返回:
    - tuple[bool, str]:  (是否放行, 结果消息)
    """
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
    session: AsyncSimpleSession,
    engine: PermissionEngine,
    operation: str,
    risk: RiskLevel,
    description: str,
) -> tuple[bool, str]:
    """
    异步审批入口: 返回 (是否放行, 结果消息)

    参数:
    - session: 会话
    - engine: 执行引擎
    - operation: 操作信息
    - risk: 风险级别
    - description: 说明文本

    返回:
    - tuple[bool, str]:  (是否放行, 结果消息)
    """
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


def _workspace_root(session: Any) -> Path:
    """
    按会话解析工作区根: 会话鸭子属性 coding_workspace_root 优先, 否则全局 WORKSPACE_ROOT

    参数:
    - session: 会话

    项目功能: 项目会话由 ChatService 在建会话/改绑时赋值 session.coding_workspace_root;
    无项目会话该属性不存在, 回落全局 -- 行为与引入项目前一致

    返回:
    - Path: 按会话解析工作区根: 会话鸭子属性 coding_workspace_root 优先, 否则全局 WORKSPACE_ROOT
    """
    if session is None:
        return WORKSPACE_ROOT
    return Path(safe_getattr(session, "coding_workspace_root") or WORKSPACE_ROOT).resolve()


def _tool_root(tool: Any) -> Path:
    """
    取工具所属会话的工作区根 (未绑会话时回落全局)

    参数:
    - tool: 工具

    返回:
    - Path: 取工具所属会话的工作区根 (未绑会话时回落全局)
    """
    return _workspace_root(getattr(tool, "_session", None))


def _resolve_path(path: str, root: Path | None = None) -> Path:
    """
    解析路径并校验在工作区内 (相对路径以工作区为基准), 越界抛 ValueError

    参数:
    - path: 路径
    - root: 根目录

    返回:
    - Path: 解析路径并校验在工作区内 (相对路径以工作区为基准), 越界抛 ValueError
    """
    root = root or WORKSPACE_ROOT
    raw = Path(path)
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise ValueError(f"路径越出工作区: {resolved}")
    return resolved


_OUTSIDE_PATH_RE = re.compile(r"(?:^|\s)([a-zA-Z]:\\|\\\\[^\\]+\\|/[a-zA-Z]/)")
# 参数里可被简单识别的绝对路径 (Windows 盘符或 UNC)


def _has_outside_workspace_path(command: str, root: Path | None = None) -> bool:
    """
    命令是否引用工作区外的绝对路径 (盘符/UNC 近似检测)

    参数:
    - command: 命令内容
    - root: 根目录

    无绝对路径或绝对路径都在工作区内 (含同盘符近似) 视为工作区内活动

    返回:
    - bool: 命令是否引用工作区外的绝对路径 (盘符/UNC 近似检测)
    """
    root_str = str(root or WORKSPACE_ROOT).replace("\\", "/").lower()
    for m in _OUTSIDE_PATH_RE.findall(command):
        candidate = m.replace("\\", "/").lower()
        if candidate.endswith("/"):
            candidate = candidate[:-1]
        if not candidate or root_str.startswith(candidate):
            continue   # 工作区根本身或其父级盘符前缀
        return True
    return False


def _protection_reason(path: Path, root: Path | None = None) -> str | None:
    """
    命中保护路径返回原因 (敏感目录/文件)

    参数:
    - path: 路径
    - root: 根目录

    返回:
    - str | None: 原因 (敏感目录/文件)
    """
    root = root or WORKSPACE_ROOT
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    parts = [p.lower() for p in rel.parts]
    for name in _PROTECTED_DIRS:
        if name in parts:
            return f"路径位于受保护目录 {name}/ 下"
    for name in _PROTECTED_FILES:
        if path.name.lower() == name:
            return f"路径命中受保护文件 {name}"
    return None


def _protection_reason_full(path: Path, root: Path | None = None) -> str | None:
    """
    完整保护检查入口 (沙箱目录由会话沙箱根单独判定, 见 _in_sandbox)

    参数:
    - path: 路径
    - root: 根目录

    返回:
    - str | None: 完整保护检查入口 (沙箱目录由会话沙箱根单独判定, 见 _in_sandbox)
    """
    return _protection_reason(path, root)


def _session_sandbox_root(session: SimpleSession | AsyncSimpleSession) -> Path:
    """
    获取会话沙箱根 (会话属性优先, 否则默认)

    参数:
    - session: 会话

    返回:
    - Path: 会话沙箱根 (会话属性优先, 否则默认)
    """
    return Path(safe_getattr(session, "coding_sandbox_root") or DEFAULT_SANDBOX_ROOT).resolve()


def _in_sandbox(path: Path, session: SimpleSession | AsyncSimpleSession) -> bool:
    """
    目标路径是否位于会话沙箱根内 (沙箱 = 免审批区)

    参数:
    - path: 路径
    - session: 会话

    返回:
    - bool: 目标路径是否位于会话沙箱根内 (沙箱 = 免审批区)
    """
    root = _session_sandbox_root(session)
    return path == root or path.is_relative_to(root)


def _approve_file_write(
    session: SimpleSession, engine: PermissionEngine, path: Path, action: str,
) -> tuple[bool, str]:
    """
    文件写审批: 目标在沙箱内免审批 (沙箱=免审批区), 其余按策略审批

    参数:
    - session: 会话
    - engine: 执行引擎
    - path: 路径
    - action: 操作类型

    返回:
    - tuple[bool, str]: 文件写审批: 目标在沙箱内免审批 (沙箱=免审批区), 其余按策略审批
    """
    if _in_sandbox(path, session):
        return True, ""
    return _approve_sync(session, engine, "file_write", RiskLevel.WRITE, f"{action} {path}")


async def _approve_file_write_async(
    session: AsyncSimpleSession, engine: PermissionEngine, path: Path, action: str,
) -> tuple[bool, str]:
    """
    异步文件写审批: 沙箱内免审批, 其余按策略审批

    参数:
    - session: 会话
    - engine: 执行引擎
    - path: 路径
    - action: 操作类型

    返回:
    - tuple[bool, str]: 异步文件写审批: 沙箱内免审批, 其余按策略审批
    """
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
        """初始化 ReadFileTool"""
        super().__init__()
    def _bind(self, session: SimpleSession) -> None:
        self._session = session

    def execute(self, path: str, offset: int = 0, limit: int = 200) -> str:
        """
        执行

        参数:
        - path: 路径
        - offset: 偏移量
        - limit: 数量上限

        返回:
        - str: 执行
        """
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
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
        """
        初始化 WriteFileTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine

    def execute(self, path: str, content: str, append: bool = False) -> str:
        """
        执行

        参数:
        - path: 路径
        - content: 内容
        - append: 是否追加

        返回:
        - str: 执行
        """
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
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

    def _bind(self, session: SimpleSession) -> None:
        """
        在工具注册前注入所属会话

        参数:
        - session: 会话
        """
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
        """
        初始化 EditFileTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine

    def execute(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        """
        执行

        参数:
        - path: 路径
        - old: 原值
        - new: 新值
        - replace_all: 是否全部替换

        返回:
        - str: 执行
        """
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
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

    def _bind(self, session: SimpleSession) -> None:
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
        """
        初始化 SearchReplaceTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine

    def execute(self, path: str, replacements: list[dict[str, Any]]) -> str:
        """
        执行

        参数:
        - path: 路径
        - replacements: 替换项列表

        返回:
        - str: 执行
        """
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
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

    def _bind(self, session: SimpleSession) -> None:
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
        """
        初始化 TodoWriteTool

        参数:
        - todos: 待办事项列表
        """
        super().__init__()
        self.todos = todos

    def execute(self, operation: str, item: str = "", index: int = 0) -> str:
        """
        执行

        参数:
        - operation: 操作信息
        - item: 条目
        - index: 索引

        返回:
        - str: 执行
        """
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
        """
        执行

        参数:
        - path: 路径

        返回:
        - str: 执行
        """
        try:
            root = _tool_root(self)
            base = _resolve_path(path, root) if path else root
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
    def _bind(self, session: SimpleSession) -> None:
        self._session = session


class GlobFilesTool(Tool):
    """按 glob 模式搜索工作区内文件"""

    tool_name = "glob_files"
    description = "按 glob 模式递归搜索文件, 如 **/*.py"
    params_dict = {
        "pattern": ("string", "glob 模式 (相对工作区)"),
    }

    def execute(self, pattern: str) -> str:
        """
        执行

        参数:
        - pattern: 匹配模式

        返回:
        - str: 执行
        """
        root = _tool_root(self)
        matches = [
            str(p.relative_to(root)).replace(os.sep, "/")
            for p in root.glob(pattern)
            if p.is_file() and p.resolve().is_relative_to(root) and _protection_reason_full(p, root) is None
        ]
        matches.sort()
        if not matches:
            return "未找到匹配文件"
        return "\n".join(matches[:100]) + (f"\n... 共 {len(matches)} 个" if len(matches) > 100 else "")
    def _bind(self, session: SimpleSession) -> None:
        self._session = session


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
        """
        执行

        参数:
        - pattern: 匹配模式
        - path: 路径
        - glob: 文件匹配模式

        返回:
        - str: 执行
        """
        try:
            root = _tool_root(self)
            base = _resolve_path(path, root) if path else root
        except ValueError as e:
            return f"错误: {e}"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"错误: 正则无效: {e}"
        hits: list[str] = []
        for p in base.rglob(glob or "*"):
            if not p.is_file() or _protection_reason_full(p, root) is not None:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{p.relative_to(root)}:{lineno}: {line[:200]}")
                    if len(hits) >= 100:
                        break
            if len(hits) >= 100:
                break
        if not hits:
            return "未找到匹配内容"
        return "\n".join(hits) + ("\n... 结果截断" if len(hits) >= 100 else "")
    def _bind(self, session: SimpleSession) -> None:
        self._session = session


# ================= ask_user 工具 =================


class AskUserTool(Tool):
    """向用户询问请求, 等待用户回复"""

    tool_name = "ask_user"
    description = (
        "向用户提出一个问题并等待回复, 用于获取缺失信息或确认意图; "
        "建议提供 2-3 个推荐选项 (options), 用户可直接输入序号选择; "
        "需要宿主通过 session.user_input_provider 适配用户输入通道"
    )
    params_dict = {
        "question": ("string", "要询问的问题"),
        "options": ("array", "推荐回答选项 (2-3 个), 如 ['方案A', '方案B']"),
    }

    def __init__(self) -> None:
        """初始化 AskUserTool"""
        super().__init__()

    def execute(self, question: str, options: list[str] | None = None) -> str:
        """
        执行

        参数:
        - question: 问题内容
        - options: 选项集合

        返回:
        - str: 执行
        """
        answer = _ask_user_sync(self._session, question, options)
        if answer is None:
            return "需要用户回复: " + question + " (未配置 user_input_provider, 请回复后继续)"
        return f"用户回复: {answer}"

    def _bind(self, session: SimpleSession) -> None:
        self._session = session


# ================= shell (本机 cmd/powershell + 审批) =================


def _resolve_shell_executable(shell: str) -> str:
    """
    解析 shell 可执行文件: PATH 优先, 回落系统目录绝对路径

    参数:
    - shell: Shell 类型

    System32 不在 PATH 的环境 (部分 Git Bash / 服务进程) 下裸文件名会 WinError 2

    返回:
    - str: 解析 shell 可执行文件: PATH 优先, 回落系统目录绝对路径
    """
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if shell.lower() == "powershell":
        found = shutil.which("powershell.exe")
        return found or str(system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    found = shutil.which("cmd.exe")
    return found or str(system_root / "System32" / "cmd.exe")


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
        """
        初始化 ShellTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine

    def execute(self, command: str, cwd: str = "", timeout: int = 120, shell: str = "powershell") -> str:
        """
        执行

        参数:
        - command: 命令内容
        - cwd: 当前工作目录
        - timeout: 超时时间
        - shell: Shell 类型

        返回:
        - str: 执行
        """
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
            if _has_outside_workspace_path(command, _tool_root(self)):
                allowed, message = _approve_sync(
                    self._session, self.engine, "shell", risk, f"执行命令: {command}",
                )
                if not allowed:
                    return message
            elif self.engine.plan_mode:
                return "拒绝: 计划模式下写类命令被禁用"
        try:
            root = _tool_root(self)
            workdir = str(_resolve_path(cwd, root)) if cwd else str(root)
        except ValueError as e:
            return f"错误: {e}"
        try:
            use_ps = shell.lower() == "powershell"
            executable = _resolve_shell_executable("powershell" if use_ps else "cmd")
            result = subprocess.run(
                [executable, "-NoProfile", "-Command", command] if use_ps else [executable, "/C", command],
                cwd=workdir,
                capture_output=True,
                text=True,
                errors="replace",
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

    def _bind(self, session: SimpleSession) -> None:
        self._session = session


# ================= subagent (独立上下文 + 权限继承) =================


class _CodingSubAgent:
    """独立上下文的子代理 (自定义 system prompt / 工具白名单 / 迭代上限)"""

    def __init__(self, llm: Any, tools_manager: Any, system_prompt: str, tools: list[str] | None = None):
        """
        初始化 _CodingSubAgent

        参数:
        - llm: 模型实例
        - tools_manager: 工具管理器实例
        - system_prompt: 系统prompt
        - tools: 工具集合
        """
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
        """
        执行 `forward` 操作

        参数:
        - task: 任务描述
        - max_turns: 最大轮数

        返回:
        - str: 执行 `forward` 操作
        """
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
        """
        初始化 _AsyncCodingSubAgent

        参数:
        - llm: 模型实例
        - tools_manager: 工具管理器实例
        - system_prompt: 系统prompt
        - tools: 工具集合
        """
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
        """
        执行 `forward` 操作

        参数:
        - task: 任务描述
        - max_turns: 最大轮数

        返回:
        - str: 执行 `forward` 操作
        """
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
        """
        初始化 SubAgentTool

        参数:
        - llm: 模型实例
        - tools_manager: 工具管理器实例
        """
        super().__init__()
        self.llm = llm
        self.tools_manager = tools_manager

    def execute(self, task: str, tools: list[str] | None = None, system_prompt: str = "", max_turns: int = 20) -> str:
        """
        执行

        参数:
        - task: 任务描述
        - tools: 工具集合
        - system_prompt: 系统prompt
        - max_turns: 最大轮数

        返回:
        - str: 执行
        """
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
        "建议提供 2-3 个推荐选项 (options), 用户可直接输入序号选择; "
        "需要宿主通过 session.user_input_provider 适配用户输入通道"
    )
    params_dict = {
        "question": ("string", "要询问的问题"),
        "options": ("array", "推荐回答选项 (2-3 个), 如 ['方案A', '方案B']"),
    }

    def __init__(self) -> None:
        """初始化 AsyncAskUserTool"""
        super().__init__()
        self._session: AsyncSimpleSession | None = None

    async def execute(self, question: str, options: list[str] | None = None) -> str:
        """
        执行

        参数:
        - question: 问题内容
        - options: 选项集合

        返回:
        - str: 执行
        """
        assert self._session is not None, "ask_user 未绑定会话"
        answer = await _ask_user_async(self._session, question, options)
        if answer is None:
            return "需要用户回复: " + question + " (未配置 user_input_provider, 请回复后继续)"
        return f"用户回复: {answer}"

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


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
        """
        初始化 AsyncShellTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: AsyncSimpleSession | None = None

    async def execute(self, command: str, cwd: str = "", timeout: int = 120, shell: str = "powershell") -> str:
        """
        执行

        参数:
        - command: 命令内容
        - cwd: 当前工作目录
        - timeout: 超时时间
        - shell: Shell 类型

        返回:
        - str: 执行
        """
        assert self._session is not None, "shell 未绑定会话"
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
            if _has_outside_workspace_path(command, _tool_root(self)):
                allowed, message = await _approve_async(
                    self._session, self.engine, "shell", risk, f"执行命令: {command}",
                )
                if not allowed:
                    return message
            elif self.engine.plan_mode:
                return "拒绝: 计划模式下写类命令被禁用"
        try:
            root = _tool_root(self)
            workdir = str(_resolve_path(cwd, root)) if cwd else str(root)
        except ValueError as e:
            return f"错误: {e}"
        try:
            use_ps = shell.lower() == "powershell"
            executable = _resolve_shell_executable("powershell" if use_ps else "cmd")
            args = (
                [executable, "-NoProfile", "-Command", command]
                if use_ps else [executable, "/C", command]
            )
            result = await asyncio.to_thread(
                subprocess.run, args,
                cwd=workdir, capture_output=True, text=True, errors="replace", check=False,
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

    def _bind(self, session: AsyncSimpleSession) -> None:
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
        """
        初始化 AsyncSubAgentTool

        参数:
        - llm: 模型实例
        - tools_manager: 工具管理器实例
        """
        super().__init__()
        self.llm = llm
        self.tools_manager = tools_manager

    async def execute(self, task: str, tools: list[str] | None = None, system_prompt: str = "", max_turns: int = 20) -> str:
        """
        执行

        参数:
        - task: 任务描述
        - tools: 工具集合
        - system_prompt: 系统prompt
        - max_turns: 最大轮数

        返回:
        - str: 执行
        """
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
        """
        执行

        参数:
        - path: 路径
        - offset: 偏移量
        - limit: 数量上限

        返回:
        - str: 执行
        """
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
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
    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


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
        """
        初始化 AsyncWriteFileTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: AsyncSimpleSession | None = None

    async def execute(self, path: str, content: str, append: bool = False) -> str:
        """
        执行

        参数:
        - path: 路径
        - content: 内容
        - append: 是否追加

        返回:
        - str: 执行
        """
        assert self._session is not None, "write_file 未绑定会话"
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
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

    def _bind(self, session: AsyncSimpleSession) -> None:
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
        """
        初始化 AsyncEditFileTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: AsyncSimpleSession | None = None

    async def execute(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        """
        执行

        参数:
        - path: 路径
        - old: 原值
        - new: 新值
        - replace_all: 是否全部替换

        返回:
        - str: 执行
        """
        assert self._session is not None, "edit_file 未绑定会话"
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
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

    def _bind(self, session: AsyncSimpleSession) -> None:
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
        """
        初始化 AsyncSearchReplaceTool

        参数:
        - engine: 执行引擎
        """
        super().__init__()
        self.engine = engine
        self._session: AsyncSimpleSession | None = None

    async def execute(self, path: str, replacements: list[dict[str, Any]]) -> str:
        """
        执行

        参数:
        - path: 路径
        - replacements: 替换项列表

        返回:
        - str: 执行
        """
        assert self._session is not None, "search_replace 未绑定会话"
        try:
            abs_path = _resolve_path(path, _tool_root(self))
        except ValueError as e:
            return f"错误: {e}"
        reason = _protection_reason_full(abs_path, _tool_root(self))
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

    def _bind(self, session: AsyncSimpleSession) -> None:
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
        """
        初始化 AsyncTodoWriteTool

        参数:
        - todos: 待办事项列表
        """
        super().__init__()
        self.todos = todos

    async def execute(self, operation: str, item: str = "", index: int = 0) -> str:
        """
        执行

        参数:
        - operation: 操作信息
        - item: 条目
        - index: 索引

        返回:
        - str: 执行
        """
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
        """
        执行

        参数:
        - path: 路径

        返回:
        - str: 执行
        """
        try:
            root = _tool_root(self)
            base = _resolve_path(path, root) if path else root
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
    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


class AsyncGlobFilesTool(AsyncTool):
    """按 glob 模式搜索工作区内文件 (异步)"""

    tool_name = "glob_files"
    description = "按 glob 模式递归搜索文件, 如 **/*.py"
    params_dict = {
        "pattern": ("string", "glob 模式 (相对工作区)"),
    }

    async def execute(self, pattern: str) -> str:
        """
        执行

        参数:
        - pattern: 匹配模式

        返回:
        - str: 执行
        """
        root = _tool_root(self)
        matches = [
            str(p.relative_to(root)).replace(os.sep, "/")
            for p in await asyncio.to_thread(lambda: list(root.glob(pattern)))
            if p.is_file() and p.resolve().is_relative_to(root) and _protection_reason_full(p, root) is None
        ]
        matches.sort()
        if not matches:
            return "未找到匹配文件"
        return "\n".join(matches[:100]) + (f"\n... 共 {len(matches)} 个" if len(matches) > 100 else "")
    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


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
        """
        执行

        参数:
        - pattern: 匹配模式
        - path: 路径
        - glob: 文件匹配模式

        返回:
        - str: 执行
        """
        try:
            root = _tool_root(self)
            base = _resolve_path(path, root) if path else root
        except ValueError as e:
            return f"错误: {e}"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"错误: 正则无效: {e}"

        def _scan() -> list[str]:
            hits: list[str] = []
            for p in base.rglob(glob or "*"):
                if not p.is_file() or _protection_reason_full(p, root) is not None:
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        hits.append(f"{p.relative_to(root)}:{lineno}: {line[:200]}")
                        if len(hits) >= 100:
                            return hits
            return hits

        hits = await asyncio.to_thread(_scan)
        if not hits:
            return "未找到匹配内容"
        return "\n".join(hits) + ("\n... 结果截断" if len(hits) >= 100 else "")
    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


# ================= get_tools 工厂 (会话依赖注入) =================

DEFAULT_SANDBOX_ROOT = get_project_root() / ".satrap" / "sandbox"
"""默认沙箱根目录 (全局共享, 与 base_take 一致; 可被 session.coding_sandbox_root 覆盖)"""


def _apply_config(config: dict[str, Any]) -> None:
    """
    把合成配置应用到模块级死参 (WORKSPACE_ROOT/DATA_ROOT/SANDBOX_ROOT/超时/保护目录)

    参数:
    - config: 配置信息

    注: 这些死参是模块级常量, 作为**全局兜底**被路径辅助函数 (_resolve_path 等) 引用;
    在 get_tools 工厂调用时更新为配置值 (全局单例语义);
    项目会话的独立工作区不经此处 -- 由会话鸭子属性 coding_workspace_root 按会话覆盖
    (_workspace_root/_tool_root 优先读会话属性, 回落此处全局值);
    空配置不动任何模块变量 (保持代码默认/测试 monkeypatch)
    """
    if not config:
        return
    global WORKSPACE_ROOT, DATA_ROOT, DEFAULT_SANDBOX_ROOT, _PROTECTED_DIRS
    if config.get("workspace_root"):
        WORKSPACE_ROOT = Path(str(config["workspace_root"])).resolve()
        DATA_ROOT = WORKSPACE_ROOT / ".satrap" / "coding"
    if config.get("data_root"):
        DATA_ROOT = Path(str(config["data_root"])).resolve()
    if config.get("sandbox_root"):
        DEFAULT_SANDBOX_ROOT = Path(str(config["sandbox_root"])).resolve()
    if config.get("protected_dirs"):
        extra = tuple(d.strip() for d in str(config["protected_dirs"]).split(",") if d.strip())
        _PROTECTED_DIRS = (".satrap", ".git", "node_modules") + extra


def get_tools(session: SimpleSession | AsyncSimpleSession, config: dict[str, Any] | None = None) -> list[Any]:
    """
    按会话形态构建全部工具 (注入 llm / 权限引擎 / 会话引用 + 应用插件配置)

    参数:
    - session: 会话
    - config: 配置信息

    注: search/fetch_page/memory 已移交 base_take 插件, 本插件只保留 coding 专属能力

    返回:
    - list[Any]: 按会话形态构建全部工具 (注入 llm / 权限引擎 / 会话引用 + 应用插件配置)
    """
    _apply_config(config or {})

    session_cache_root = safe_getattr(session, "coding_cache_root")
    state_root = Path(str(session_cache_root)) / "satrap_coding" if session_cache_root else None
    state = get_plugin_state(session, state_root)
    engine = cast(PermissionEngine, state["engine"])
    todos = cast(dict[str, Any], state["todos"])

    if isinstance(session, AsyncSimpleSession):
        tools: list[Any] = [
            AsyncAskUserTool(),
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
        bind = safe_getattr_callable(tool, "_bind")
        if bind is not None:
            bind(session)
    return tools

