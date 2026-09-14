from unittest.mock import AsyncMock, Mock
from pathlib import Path
from typing import Any
import pytest

from satrap.expend.plugins.satrap_coding.core.permission import PermissionEngine
from satrap.expend.plugins.satrap_coding.tools import async_interaction, async_write, sync_interaction, sync_write
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.edictum import AsyncSimpleSession, SimpleSession


@pytest.mark.asyncio
async def test_all_unbound_tools_fail_before_other_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证五对工具在未绑定时优先报告会话错误

    参数:
    - tmp_path: 隔离文件与数据库的临时目录
    - monkeypatch: 替换外部请求与审批的夹具
    """
    engine = PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl")
    calls: list[tuple[Tool, AsyncTool, tuple[object, ...]]] = [
        (sync_write.WriteFileTool(engine), async_write.AsyncWriteFileTool(engine), ("file", "content")),
        (sync_write.EditFileTool(engine), async_write.AsyncEditFileTool(engine), ("file", "old", "new")),
        (sync_write.SearchReplaceTool(engine), async_write.AsyncSearchReplaceTool(engine), ("file", [])),
        (sync_interaction.AskUserTool(), async_interaction.AsyncAskUserTool(), ("question",)),
        (sync_interaction.ShellTool(engine), async_interaction.AsyncShellTool(engine), ("unused",)),
    ]
    for module in (sync_write, async_write, sync_interaction, async_interaction):
        monkeypatch.setattr(module, "_tool_root", Mock(side_effect=AssertionError("不得访问工作区")))
    for sync, async_tool, args in calls:
        with pytest.raises(RuntimeError, match=f"^{sync.tool_name} 未绑定会话$"):
            sync.execute(*args)
        with pytest.raises(RuntimeError, match=f"^{async_tool.tool_name} 未绑定会话$"):
            await async_tool.execute(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"old": "missing", "new": "x"}, {"old": "", "new": "x"}])
async def test_invalid_later_replacement_never_approves_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: dict[str, Any]
) -> None:
    """
    验证后续替换项错误不会导致审批或部分写入

    参数:
    - tmp_path: 隔离文件与数据库的临时目录
    - monkeypatch: 替换外部请求与审批的夹具
    - invalid: 位于合法替换项之后的无效输入
    """
    path = tmp_path / "file.txt"
    path.write_text("first second", encoding="utf-8")
    engine = PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl")
    sync, async_tool = sync_write.SearchReplaceTool(engine), async_write.AsyncSearchReplaceTool(engine)
    sync._bind(SimpleSession.__new__(SimpleSession))
    async_tool._bind(AsyncSimpleSession.__new__(AsyncSimpleSession))
    for module in (sync_write, async_write):
        monkeypatch.setattr(module, "_tool_root", lambda tool: tmp_path)
        monkeypatch.setattr(module, "_protection_reason_full", lambda path, root: None)
    sync_approve, async_approve = Mock(), AsyncMock()
    monkeypatch.setattr(sync_write, "_approve_file_write", sync_approve)
    monkeypatch.setattr(async_write, "_approve_file_write_async", async_approve)
    replacements = [{"old": "first", "new": "changed"}, invalid]
    result = sync.execute(str(path), replacements)
    assert result == await async_tool.execute(str(path), replacements)
    assert "第 2 个" in result
    assert path.read_text(encoding="utf-8") == "first second"
    sync_approve.assert_not_called()
    async_approve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_replacements_preserve_order_and_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    """
    验证已绑定的两版工具保留替换顺序并遵守审批结果

    参数:
    - tmp_path: 隔离文件与权限数据的临时目录
    - monkeypatch: 替换审批与工作区的夹具
    - allowed: 模拟审批是否允许写入
    """
    path = tmp_path / "file.txt"
    engine = PermissionEngine(rules_file=tmp_path / "rules.json", log_file=tmp_path / "log.jsonl")
    sync, async_tool = sync_write.SearchReplaceTool(engine), async_write.AsyncSearchReplaceTool(engine)
    sync._bind(SimpleSession.__new__(SimpleSession))
    async_tool._bind(AsyncSimpleSession.__new__(AsyncSimpleSession))
    for module in (sync_write, async_write):
        monkeypatch.setattr(module, "_tool_root", lambda tool: tmp_path)
        monkeypatch.setattr(module, "_protection_reason_full", lambda path, root: None)
    monkeypatch.setattr(sync_write, "_approve_file_write", Mock(return_value=(allowed, "拒绝")))
    monkeypatch.setattr(async_write, "_approve_file_write_async", AsyncMock(return_value=(allowed, "拒绝")))
    replacements = [{"old": "first", "new": "second"}, {"old": "second", "new": "third", "replace_all": True}]
    path.write_text("first second", encoding="utf-8")
    sync_result = sync.execute(str(path), replacements)
    sync_content = path.read_text(encoding="utf-8")
    path.write_text("first second", encoding="utf-8")
    async_result = await async_tool.execute(str(path), replacements)
    assert sync_result == async_result
    assert sync_content == path.read_text(encoding="utf-8") == ("third third" if allowed else "first second")
    if not allowed:
        assert sync_result == "拒绝"
