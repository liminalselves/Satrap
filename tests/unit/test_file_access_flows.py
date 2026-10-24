"""宿主单文件读取的审批, 内容送达和等待期间失效链路"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast
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
                                     upload_root=uploads, sandbox_root=sandbox, session_root=session_root, artifacts_root=session_root / "artifacts"))
    presets = Path(__file__).resolve().parents[2] / "satrap/expend/plugins"
    try:
        for name in ("base_take", "satrap_coding"):
            await _await(session.install_plugin(str(presets / name)))
        async def execute(tool_name, **arguments):
            if asynchronous:
                return await session.tools_manager.execute_tool(tool_name, arguments)
            return await asyncio.to_thread(session.tools_manager.execute_tool, tool_name, arguments)
        yield session, execute, tmp_path, (workspace, uploads, sandbox), asynchronous
    finally:
        for plugin in reversed(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))


async def test_send_file_uses_single_file_approval_and_immutable_current_host_artifact(file_host):
    """同步异步均经真实插件宿主交付, 文件修改和工作区外审批不扩散授权"""
    from satrap.core.file_access.artifacts import bind_file_receiver, MAX_FILE_BYTES
    session, execute, root, trusted, _ = file_host
    assert session.tools_manager.tools["send_file"].get_tool_defined()["function"]["parameters"]["required"] == ["path"]
    engine = session_approval(session)
    engine.set_mode("user", persist=False)
    requests, received = [], []
    def approve(request):
        from satrap.core.storage.file_lock import FileLock
        artifacts_root = cast(Path, environment_snapshot(session).artifacts_root)
        with FileLock(artifacts_root.parent.parent.parent / "locks" / (artifacts_root.parent.name + ".lock"), timeout=.02):
            pass
        requests.append(request)
        return ApprovalResult.for_request(request, "allow", "同意本次文件读取", "user")
    session.approval_provider = approve
    payload = b"PK\x03\x04" + bytes(range(256)) * 1200
    source = trusted[2] / "book.xlsx"
    source.write_bytes(payload)
    async def receive(artifact):
        received.append(artifact)
        source.write_bytes(b"changed")
        with artifact.open() as stream:
            assert stream.read() == payload
        return {"ok": True, "status": "available", "file": artifact.public()}
    with bind_file_receiver(receive):
        result = await execute("send_file", path="book.xlsx", name="统计.xlsx")
    assert result["ok"] and result["file"]["name"] == "统计.xlsx"
    assert "path" not in result["file"] and not requests
    with received[0].open() as stream:
        assert stream.read() == payload
    external = root / "outside.bin"
    external.write_bytes(payload)
    with bind_file_receiver(receive):
        assert (await execute("send_file", path=str(external)))["ok"]
        assert (await execute("send_file", path=str(external)))["ok"]
    assert len(requests) == 2
    assert requests[0].payload()["action"]["parameters"]["operation"] == "send_file"
    assert not (await execute("send_file", path=str(external), name="../x"))["ok"]
    assert not (await execute("send_file", path=str(trusted[0])))["ok"]
    oversized = trusted[0] / "large.zip"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    with bind_file_receiver(receive):
        assert not (await execute("send_file", path=str(oversized)))["ok"]
    assert len(received) == 3
    engine.set_mode("full-access", persist=False)
    generated = trusted[0] / "from-coding.txt"
    await execute("write_file", path=str(generated), content="coding-output")
    payload = b"coding-output"
    with bind_file_receiver(receive):
        assert (await execute("send_file", path=str(generated)))["ok"]
    assert len(received) == 4
    engine.set_plan_mode(True)
    with bind_file_receiver(receive):
        assert "plan_mode" in (await execute("send_file", path=str(external)))["reason"]
    assert len(received) == 4


@pytest.mark.parametrize("file_host", [True], indirect=True, ids=["async"])
async def test_cancelled_file_copy_finishes_before_artifact_and_lease_cleanup(file_host, monkeypatch):
    """取消有界复制后先等待线程退出, 不留下晚到产物或继续发出文件"""
    import importlib
    from satrap.core.file_access.artifacts import bind_file_receiver
    from satrap.core.storage.file_lock import FileLock
    session, execute, _, trusted, _ = file_host
    source = trusted[0] / "cancel.bin"
    source.write_bytes(b"cancel me")
    tool = session.tools_manager.tools["send_file"]
    module = importlib.import_module(type(tool).__module__)
    original = module.freeze_file
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "freeze_file", blocked)
    received = []
    async def receive(artifact):
        received.append(artifact)
        return {"ok": True, "status": "available"}
    with bind_file_receiver(receive):
        task = asyncio.create_task(execute("send_file", path=str(source)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    root = cast(Path, environment_snapshot(session).artifacts_root)
    assert not received and list(root.glob("sf_*")) == []
    with FileLock(root.parent.parent.parent / "locks" / (root.parent.name + ".lock"), timeout=.02):
        pass


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


def test_artifact_open_uses_metadata_and_explicit_verification(tmp_path, monkeypatch):
    """普通打开不预读, 显式核验发现同大小改写, 路径和大小检查仍生效"""
    from satrap.core.file_access.artifacts import freeze_file, display_name
    import hashlib
    import os
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    artifact = freeze_file(source, tmp_path / "artifacts")
    real_hash = hashlib.sha256
    monkeypatch.setattr(hashlib, "sha256", lambda: pytest.fail("普通打开不应计算完整摘要"))
    with artifact.open() as stream:
        assert stream.tell() == 0 and stream.read() == b"original"
    monkeypatch.setattr(hashlib, "sha256", real_hash)
    artifact.verify()
    artifact.path.write_bytes(b"modified")
    with artifact.open() as stream:
        assert stream.read() == b"modified"
    with pytest.raises(ValueError, match="校验失败"):
        artifact.verify()
    artifact.path.write_bytes(b"different size")
    with pytest.raises(ValueError, match="大小或类型"):
        artifact.open()
    if os.name != "nt":
        assert display_name("Q3:report.pdf") == "Q3:report.pdf"
    else:
        with pytest.raises(ValueError):
            display_name("Q3:report.pdf")


async def test_file_cleanup_error_releases_maintenance_lease(file_host, monkeypatch):
    """交付失败且产物无法删除时仍释放租约, 保留原交付结果"""
    from satrap.core.file_access.artifacts import bind_file_receiver
    from satrap.core.storage.file_lock import _FILE_LEASES, FileLock
    import importlib
    session, execute, _, trusted, _ = file_host
    source = trusted[0] / "cleanup.txt"
    source.write_text("cleanup", encoding="utf-8")
    environment = environment_snapshot(session)
    module = importlib.import_module(type(session.tools_manager.tools["send_file"]).__module__)
    original_lease = module.artifact_lease
    leases = []
    def tracked_lease(root):
        lease = original_lease(root)
        leases.append(lease)
        return lease
    monkeypatch.setattr(module, "artifact_lease", tracked_lease)
    original_unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path.parent == environment.artifacts_root:
            raise PermissionError("模拟文件占用")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", denied)
    async def receive(_):
        return {"ok": False, "status": "failed", "reason": "模拟交付失败"}
    try:
        with bind_file_receiver(receive):
            result = await execute("send_file", path=str(source))
        assert result["reason"] == "模拟交付失败"
        assert len(leases) == 1 and leases[0].released
        assert leases[0].path not in _FILE_LEASES
        with FileLock(leases[0].path, timeout=.05):
            pass
    finally:
        for lease in leases:
            lease.close()
