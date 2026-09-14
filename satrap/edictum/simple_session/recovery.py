"""会话恢复管理, 不重新执行输入和输出处理器"""
from __future__ import annotations

from typing import Any, cast
from pathlib import Path
import hashlib

from satrap.core.framework.Base.execution.engine import run_async, run_sync
from satrap.core.framework.Base.execution.store import RunStore, fingerprint


def plugin_fingerprint(path: Path, config: dict[str, Any], models: Any = None, schema: Any = None) -> str:
    """在加载时记录插件代码与有效配置摘要, 不保存配置明文"""
    files = [(str(file.relative_to(path)), hashlib.sha256(file.read_bytes()).hexdigest())
             for file in sorted(path.rglob("*"))
             if file.is_file() and file.suffix in {".py", ".yaml", ".yml", ".md"}]
    from satrap.edictum.plugin_resources import model_reference_fingerprint

    resources = model_reference_fingerprint(models, schema, config) if models is not None and schema is not None else ""
    return fingerprint({"files": files, "config": config, "resources": resources})


def prepare_session_recovery(session: Any) -> None:
    """将已加载插件和能力状态纳入恢复指纹"""
    wf = session._wf
    if wf is None:
        return
    plugins: list[dict[str, Any]] = [{"name": p.name, "version": p.version, "enabled": p.enabled,
                "tools": p.tools, "skills": p.skills, "mcp": p.mcp,
                "handlers": p.handlers, "commands": p.commands,
                "implementation": getattr(p, "recovery_fingerprint", "")}
               for p in sorted(cast(list[Any], session.list_plugins()), key=lambda p: p.name)]
    scope = {key: getattr(session, key, None) for key in (
        "coding_workspace_root", "coding_session_root", "coding_sandbox_root",
        "coding_upload_root", "coding_memory_db", "coding_memory_scope",
    )}
    wf.recovery_plugin_fingerprint = fingerprint({"plugins": plugins, "scope": scope})
    wf.recovery_origin = getattr(session, "recovery_origin", {})


def store_for_session(session: Any) -> RunStore:
    """只允许查询当前会话工作流的任务"""
    return RunStore(session.ctx.db_path, session.ctx.conversation_id)


def summaries(session: Any) -> list[dict[str, Any]]:
    """管理接口不返回模型请求正文和工具参数"""
    return store_for_session(session).summaries()["runs"]


def resume_sync(session: Any, run_id: str) -> str:
    """恢复固定 Agent 循环, 返回模型最终结果"""
    with session._run_lock:
        prepare_session_recovery(session)
        return run_sync(session._wf, run_id=run_id)


async def resume_async(session: Any, run_id: str) -> str:
    """异步恢复固定 Agent 循环, 不重放边界处理器"""
    await session.initialize()
    async with session._run_lock:
        prepare_session_recovery(session)
        return await run_async(session._wf, run_id=run_id)
