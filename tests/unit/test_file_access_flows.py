"""宿主单文件读取的审批, 内容送达和等待期间失效链路"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
import asyncio
import inspect
import json
import threading

import pytest

from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot
from satrap.core.approval.contracts import ApprovalResult
from satrap.core.approval.state import session_approval
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap import AsyncLLM, LLM


async def _await(value):
    return await value if inspect.isawaitable(value) else value


@pytest.fixture(params=[False, True], ids=["sync", "async"])
async def file_host(request, tmp_path, monkeypatch):
    """真实安装两个读取工具, 共享独立会话目录和审批状态"""
    from satrap.edictum import simple_session as ss_mod

    asynchronous = request.param
    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    llm = MagicMock(spec=AsyncLLM if asynchronous else LLM)
    llm.supports_visual_input = True
    llm.chat = AsyncMock() if asynchronous else MagicMock()
    session = (AsyncSimpleSession if asynchronous else SimpleSession)("files", llm, db_path=str(tmp_path / "chat.db"))
    session_root = tmp_path / ".satrap/session"
    workspace, uploads, sandbox = tmp_path / "workspace", session_root / "uploads", session_root / "sandbox"
    for root in (workspace, uploads, sandbox):
        root.mkdir(parents=True)
        (root / "inside.txt").write_text("trusted content", encoding="utf-8")
    bind_environment(session, replace(environment_snapshot(session), workspace_root=workspace,
                                     upload_root=uploads, sandbox_root=sandbox, session_root=session_root))
    presets = Path(__file__).resolve().parents[2] / "satrap/expend/plugins"
    try:
        for name in ("base_take", "satrap_coding"):
            await _await(session.install_plugin(str(presets / name)))
        async def execute(name, **arguments):
            if asynchronous:
                return await session.tools_manager.execute_tool(name, arguments)
            return await asyncio.to_thread(session.tools_manager.execute_tool, name, arguments)
        yield session, execute, tmp_path, (workspace, uploads, sandbox), asynchronous
    finally:
        for plugin in reversed(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))


@pytest.mark.parametrize("tool", ["read_document", "read_file"])
async def test_single_file_modes_and_exact_single_use_approval(file_host, tool):
    """可信根免审批, 外部读取按三档处理且授权不扩散到下次或兄弟文件"""
    session, execute, root, trusted, _ = file_host
    engine = session_approval(session)
    engine.set_mode("user", persist=False)
    seen = []
    def human(request):
        seen.append(request)
        assert "outside-secret" not in json.dumps(request.payload())
        return ApprovalResult.for_request(request, "allow", "同意本次文件读取", "user")
    session.approval_provider = human
    for folder in trusted:
        assert "trusted content" in await execute(tool, path=str(folder / "inside.txt"))
    assert not seen
    path = root / "external.txt"
    path.write_text("outside-secret\nsecond line\n", encoding="utf-8")
    options = {"offset": 1, "limit": 1} if tool == "read_file" else {"max_length": 7, "mode": "text"}
    result = await execute(tool, path=str(path), **options)
    assert ("second line" if tool == "read_file" else "outside") in result
    action = seen[-1].payload()["action"]
    assert Path(action["path"]) == path.resolve() and len(action["fingerprint"]) == 4
    assert all(action["parameters"][key] == value for key, value in options.items())
    await execute(tool, path=str(path), **options)
    sibling = root / "sibling.txt"
    sibling.write_text("other", encoding="utf-8")
    await execute(tool, path=str(sibling))
    assert len({request.request_id for request in seen}) == 3
    session.approval_provider = None
    assert "approval_required" in await execute(tool, path=str(path))
    private = root / ".satrap/other-session/private.txt"
    private.parent.mkdir(parents=True)
    private.write_text("another session", encoding="utf-8")
    git_file = root / ".git/config"
    git_file.parent.mkdir()
    git_file.write_text("git config", encoding="utf-8")
    for mode in ("user", "auto", "full-access"):
        engine.set_mode(mode, persist=False)
        protected = root / ".env"
        protected.write_text("private", encoding="utf-8")
        assert "受保护" in await execute(tool, path=str(protected))
        assert "受保护" in await execute(tool, path=str(private))
        assert "受保护" in await execute(tool, path=str(git_file))
    assert "outside-secret" in await execute(tool, path=str(path))
    assert len(seen) == 3 and not session.llm.chat.called
    engine.set_mode("auto", persist=False)
    session.llm.chat.return_value = '{"decision":"allow","reason":"用户指定了文件"}'
    assert "outside-secret" in await execute(tool, path=str(path))
    session.llm.chat.return_value = '{"decision":"deny","reason":"没有授权"}'
    assert "approval_denied" in await execute(tool, path=str(path))
    session.llm.chat.return_value = "invalid decision"
    session.approval_provider = human
    assert "outside-secret" in await execute(tool, path=str(path))
    assert len(seen) == 4
    engine.set_plan_mode(True)
    assert "outside-secret" in await execute(tool, path=str(path))
    engine.set_mode("user", persist=False)
    assert "越出工作区" in await execute("list_dir", path=str(root))


@pytest.mark.parametrize("drift", ["file", "environment", "mode", "tool", "turn", "queued"])
async def test_approval_wait_cannot_return_changed_content(file_host, drift, monkeypatch):
    """人工回填后复核文件, 环境, 模式, 工具和原始轮次"""
    session, execute, root, _, _ = file_host
    session_approval(session).set_mode("user", persist=False)
    path = root / "external.txt"
    path.write_text("never returned", encoding="utf-8")
    from satrap.core.file_access.reads import FileRead
    parsed = []
    reader = session.tools_manager.tools["read_document"]
    original = reader._read_document
    def parse(path, options):
        parsed.append(path)
        return original(path, options)
    monkeypatch.setattr(reader, "_read_document", parse)
    if drift == "queued":
        original_read = FileRead._read
        def queued(self, reader):
            path.write_text("changed before worker starts", encoding="utf-8")
            return original_read(self, reader)
        monkeypatch.setattr(FileRead, "_read", queued)
    def human(request):
        if drift == "file":
            path.write_text("changed while waiting", encoding="utf-8")
        elif drift == "environment":
            bind_environment(session, replace(environment_snapshot(session), project="changed"))
        elif drift == "mode":
            session_approval(session).set_mode("full-access", persist=False)
        elif drift == "tool":
            session.tools_manager.tools["read_document"].disable()
        elif drift == "turn":
            session.recovery_origin = {"turn_id": "another-turn"}
        return ApprovalResult.for_request(request, "allow", "仅批准原始请求", "user")
    session.approval_provider = human
    result = await execute("read_document", path=str(path))
    assert not parsed
    assert "never returned" not in str(result) and "changed while waiting" not in str(result)
    if drift == "environment":
        assert result["error_type"] == "capability_stale"
    else:
        assert "错误" in result


async def test_external_media_is_decoded_only_after_approval(file_host, monkeypatch):
    """拒绝时不解码图片或渲染 PDF, 批准后返回固定媒体和指定页码"""
    from PIL import Image
    import pypdfium2 as pdfium

    session, execute, root, _, asynchronous = file_host
    await _await(session.uninstall_plugin("satrap_coding"))
    session_approval(session).set_mode("user", persist=False)
    image = root / "picture.png"
    Image.new("RGB", (12, 8), "red").save(image)
    pdf = root / "scan.pdf"
    with pdfium.PdfDocument.new() as document:
        for _ in range(2):
            document.new_page(120, 180).close()
        document.save(str(pdf))
    decoded = []
    reader = session.tools_manager.tools["read_document"]
    original = reader._read_document
    def decode(path, options):
        decoded.append(path)
        return original(path, options)
    monkeypatch.setattr(reader, "_read_document", decode)
    def deny(request):
        assert not decoded
        return ApprovalResult.for_request(request, "deny", "不读取", "user")
    session.approval_provider = deny
    for path in (image, pdf):
        assert "approval_denied" in await execute("read_document", path=str(path), mode="visual")
    assert not decoded
    loop_thread = threading.get_ident()
    async def allow_async(request):
        assert threading.get_ident() == loop_thread
        await asyncio.sleep(0)
        return ApprovalResult.for_request(request, "allow", "读取此文件", "user")
    def allow_sync(request):
        return ApprovalResult.for_request(request, "allow", "读取此文件", "user")
    session.approval_provider = allow_async if asynchronous else allow_sync
    picture = await execute("read_document", path=str(image), mode="visual")
    assert picture["media"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    pages = await execute("read_document", path=str(pdf), mode="visual", start_page=2, page_count=1)
    assert pages["metadata"]["start_page"] == pages["metadata"]["end_page"] == 2
    assert decoded == [image.resolve(), pdf.resolve()]
    if asynchronous:
        pending = asyncio.Event()
        async def wait(request):
            pending.set()
            await asyncio.Event().wait()
        session.approval_provider = wait
        task = asyncio.create_task(execute("read_document", path=str(image)))
        await pending.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(decoded) == 2
