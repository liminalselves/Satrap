"""技能导入, 按需加载, 命令与恢复的整合场景"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
import pytest

from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.type import LLMCallResponse
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.edictum import SimpleSession, AsyncSimpleSession
from satrap.edictum.plugin import PLUGINS_PRESET_DIR
from satrap.expend.plugins.skills import store as storage
from satrap.expend.plugins.skills.management import skill_management

MARKDOWN = "---\nname: weekly\ndescription: 工作记录整理周报时使用\ntools: [blocked]\n---\n正文专用标记: 按完成, 问题和下周计划整理"


class Script:
    """记录真实模型请求并按顺序返回响应"""

    def __init__(self):
        self.calls: list[list[dict[str, Any]]] = []
        self.responses: list[LLMCallResponse | Exception] = []

    def respond(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        response = self.responses.pop(0) if self.responses else LLMCallResponse("answer", "完成")
        if isinstance(response, Exception):
            raise response
        return response


class SyncModel(LLM, Script):
    def __init__(self):
        Script.__init__(self)

    def call(self, messages, **kwargs):
        return self.respond(messages, **kwargs)


class AsyncModel(AsyncLLM, Script):
    def __init__(self):
        Script.__init__(self)

    async def call(self, messages, **kwargs):
        return self.respond(messages, **kwargs)


def load_response() -> LLMCallResponse:
    return LLMCallResponse("tools_call", "", tool_calls=[{"name": "load_skill", "id": "load-weekly", "arguments": {"skill": "user.weekly"}}])


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_import_discover_load_force_and_disable(tmp_path, monkeypatch, asynchronous):
    """导入至真实插件后验证三种注入通道, 身份绑定和动态停用"""
    monkeypatch.setattr(storage, "get_project_root", lambda: tmp_path)
    entry = storage.SkillStore().save(MARKDOWN)
    with pytest.raises(storage.SkillConflict):
        storage.SkillStore().save(MARKDOWN)
    model = AsyncModel() if asynchronous else SyncModel()
    session: Any = (AsyncSimpleSession if asynchronous else SimpleSession)(
        "skills-task", model, system_prompt="基础指令", db_path=str(tmp_path / "context.db"), enable_checkpoint=False)
    if asynchronous:
        await session.initialize()
    blocked = (AsyncTool if asynchronous else Tool)(tool_name="blocked", description="关闭的工具", params_dict={})
    blocked.disable()
    session.add_tool(blocked)
    if asynchronous:
        await session.install_plugin(str(PLUGINS_PRESET_DIR / "skills"))
    else:
        session.install_plugin(str(PLUGINS_PRESET_DIR / "skills"))

    async def run(text):
        return await session.run(text) if asynchronous else session.run(text)

    assert "user.weekly" in await run("/skill list")
    model.responses = [load_response(), LLMCallResponse("answer", "周报完成")]
    assert await run("整理工作记录") == "周报完成"
    first, second = model.calls[-2:]
    assert any("user.weekly" in str(row["content"]) for row in first if row["role"] == "system")
    assert all("正文专用标记" not in str(row["content"]) for row in first)
    assert any("正文专用标记" in str(row["content"]) for row in second if row["role"] == "tool")
    assert all("正文专用标记" not in str(row["content"]) for row in second if row["role"] == "system")
    assert not blocked.is_enabled()
    assert session.ctx.get_context()[0]["content"] == "基础指令"

    assert await run("/skill use user.weekly 直接生成周报") == "完成"
    current = [row for row in model.calls[-1] if row["role"] == "user"][-1]["content"]
    assert "正文专用标记" in current and "直接生成周报" in current
    assert "正文专用标记" not in session.ctx.get_context()[-2]["content"]
    assert session.ctx.get_context()[-2]["role"] == "user"
    await run("普通下一轮")
    assert [row for row in model.calls[-1] if row["role"] == "user"][-1]["content"] == "普通下一轮"

    alice = CallOrigin("onebot", "bot", "GroupMessage", "group", "alice", "1", "r1")
    bob = CallOrigin("onebot", "bot", "GroupMessage", "group", "bob", "2", "r2")
    with bind_call_origin(alice):
        assert "下一次" in await run("/skill use weekly")
    with bind_call_origin(bob):
        await run("另一个人发言")
        assert [row for row in model.calls[-1] if row["role"] == "user"][-1]["content"] == "另一个人发言"
        from satrap.expend.plugins.skills import management
        def forbidden_download(*args, **kwargs):
            raise AssertionError("普通成员不能触发技能下载")
        with monkeypatch.context() as guarded:
            guarded.setattr(management, "download_markdown", forbidden_download)
            denied = await run("/skill import https://example.com/SKILL.md")
            assert "未获得" in denied or "权限" in denied
    with bind_call_origin(alice):
        await run("本人的任务")
        assert "正文专用标记" in [row for row in model.calls[-1] if row["role"] == "user"][-1]["content"]
        await run("本人的下一轮")
        assert [row for row in model.calls[-1] if row["role"] == "user"][-1]["content"] == "本人的下一轮"

    storage.SkillStore().save(MARKDOWN, expected_revision=entry.revision, enabled=False)
    await run("停用后任务")
    assert all("user.weekly" not in str(row["content"]) for row in model.calls[-1] if row["role"] == "system")
    with pytest.raises(ValueError, match="不可用"):
        session._skills_manager.read_skill("user.weekly", session._wf)
    assert "失败" in await run("/skill show user.weekly")
    storage.SkillStore().save(MARKDOWN, expected_revision=2, scopes=["chat"])
    await run("不适用当前会话类型")
    assert "user.weekly" not in session._skills_manager.catalog(session._wf)
    storage.SkillStore().save(MARKDOWN, expected_revision=3, scopes=["embedded"])
    await run("重新适用当前类型")
    assert "user.weekly" in session._skills_manager.catalog(session._wf)
    if asynchronous:
        await session.disable_plugin("skills")
    else:
        session.disable_plugin("skills")
    await run("插件停用后任务")
    assert "user.weekly" not in session._skills_manager.catalog(session._wf)
    if asynchronous:
        await session.enable_plugin("skills")
        await session.uninstall_plugin("skills")
    else:
        session.enable_plugin("skills")
        session.uninstall_plugin("skills")
    assert not session._skills_manager.sources


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_loaded_body_survives_recovery_and_context_compression(tmp_path, monkeypatch, asynchronous):
    """完成的加载步骤恢复时复用旧正文, 当前轮在历史压缩后仍保留工具证据"""
    monkeypatch.setattr(storage, "get_project_root", lambda: tmp_path)
    storage.SkillStore().save(MARKDOWN)
    model = AsyncModel() if asynchronous else SyncModel()
    session: Any = (AsyncSimpleSession if asynchronous else SimpleSession)(
        "recover-skill", model, system_prompt="基础指令", db_path=str(tmp_path / "context.db"), recoverable=True, enable_checkpoint=False)
    if asynchronous:
        await session.install_plugin(str(PLUGINS_PRESET_DIR / "skills"))
    else:
        session.install_plugin(str(PLUGINS_PRESET_DIR / "skills"))
    session.ctx.trigger_tokens = 1
    session.ctx.floor_tokens = 64
    session.ctx.exceed_process = "sliding"
    model.responses = [load_response(), RuntimeError("中断")]
    with pytest.raises(RuntimeError, match="中断"):
        if asynchronous:
            await session.run("需要使用技能")
        else:
            session.run("需要使用技能")
    run_id = session._wf.last_run_id
    assert run_id
    storage.SkillStore().save(MARKDOWN.replace("正文专用标记", "新版本正文"), expected_revision=1)
    if asynchronous:
        await session.resume_run(run_id)
    else:
        session.resume_run(run_id)
    assert any("正文专用标记" in str(row["content"]) for row in model.calls[-1] if row["role"] == "tool")
    assert all("新版本正文" not in str(row["content"]) for row in model.calls[-1])
    assert session.ctx.get_context()[0]["content"] == "基础指令"


def test_management_url_and_versioned_edits(tmp_path, monkeypatch):
    """文本与 URL 导入共用校验, 编辑冲突不覆盖原稿, 下载失败不留下半成品"""
    from satrap.expend.plugins.skills import management
    from satrap.core.utils.outbound import OutboundHTTPResponse
    store = storage.SkillStore(tmp_path / "skills")
    created = skill_management("POST", "", {"markdown": MARKDOWN}, store)
    assert created["ok"]
    for invalid in ("---\nname: [broken\n---\nbody", "---\nname: bad\nbody", "---\nname: [list]\n---\nbody"):
        with pytest.raises(ValueError):
            skill_management("POST", "", {"markdown": invalid, "name": "bad", "description": "用途"}, store)
    assert len(store.entries()) == 1
    with pytest.raises(storage.SkillConflict):
        skill_management("PUT", "weekly", {"markdown": "新版", "description": "新版用途", "expected_revision": 8}, store)
    assert store.entries()[0].revision == 1
    seen = []
    def download(url, **kwargs):
        seen.append((url, kwargs))
        return OutboundHTTPResponse(url, 200, {}, MARKDOWN.replace("weekly", "remote").encode("utf-8"))
    monkeypatch.setattr(management, "safe_sync_get", download)
    skill_management("POST", "", {"url": "https://github.com/example/skills/blob/main/SKILL.md"}, store)
    assert seen[0][0] == "https://raw.githubusercontent.com/example/skills/main/SKILL.md"
    assert seen[0][1]["max_response_bytes"] == storage.MAX_MARKDOWN_BYTES
    for url in ("file:///tmp/SKILL.md", "https://example.com/page.html"):
        with pytest.raises(ValueError):
            skill_management("POST", "", {"url": url}, store)
    assert len(store.entries()) == 2
    skill_management("DELETE", "weekly", {"expected_revision": 1}, store)
    assert [entry.name for entry in store.entries()] == ["remote"]


@pytest.mark.parametrize("cold", [False, True])
async def test_skill_management_http_and_control_share_crud(tmp_path, monkeypatch, cold):
    """运行态与停服控制入口共用导入, 冲突, 停用与删除语义"""
    import asyncio
    from satrap.core.backend.BackendManager import BackendConfig, BackendManager
    from satrap.core.backend.http_api import BackendHTTPServer
    from satrap.core.backend import control_server as control
    monkeypatch.setattr(storage, "get_project_root", lambda: tmp_path)
    server = BackendHTTPServer(BackendManager(BackendConfig(data_root=str(tmp_path))))

    async def request(method, path="/api/skills", payload=None):
        body = json.dumps(payload or {}).encode("utf-8")
        if not cold:
            return await server._route(method, path, body)
        reader = asyncio.StreamReader()
        reader.feed_data(body)
        reader.feed_eof()
        headers = f"{method} {path} HTTP/1.1\r\nContent-Length: {len(body)}\r\nContent-Type: application/json\r\n\r\n".encode("ascii")
        return await control._route_skills(control._RouteContext(method, path, path, reader, headers))

    assert (await request("GET"))[1]["items"] == []

    from satrap.expend.plugins.skills import catalog as skill_catalog
    from satrap.edictum.plugin_catalog import PluginCatalog
    plugin_root = tmp_path / "catalog-plugins"
    sample = plugin_root / "sample"
    sample.mkdir(parents=True)
    (sample / "meta.yaml").write_text("name: sample\nskills:\n  runtime: 动态技能\n", encoding="utf-8")
    folder = sample / "skills" / "notes"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: notes\ndescription: 整理笔记\ntools: [read_document]\n---\n只读说明正文", encoding="utf-8")
    (sample / "skills.py").write_text("raise AssertionError('查看目录不允许执行插件代码')", encoding="utf-8")
    monkeypatch.setattr(skill_catalog, "PluginCatalog", lambda: PluginCatalog(plugin_root, tmp_path / "empty"))
    preview_status, preview = await request("GET", "/api/skills/plugin-catalog")
    assert preview_status == 200
    assert preview["items"] == [
        {"plugin": "sample", "name": "notes", "description": "整理笔记", "source": "skills/notes/SKILL.md", "instructions": "只读说明正文", "tools": ["read_document"]},
        {"plugin": "sample", "name": "runtime", "description": "动态技能", "source": "插件能力声明", "instructions": None, "tools": []},
    ]
    assert "代码提供技能" in preview["notices"][0]
    oversized = sample / "skills" / "oversized"
    oversized.mkdir()
    (oversized / "skill.md").write_bytes(b"x" * (storage.MAX_MARKDOWN_BYTES + 1))
    _, bounded = await request("GET", "/api/skills/plugin-catalog")
    assert bounded["items"] == preview["items"]
    assert any("256 KiB" in notice for notice in bounded["notices"])
    status, created = await request("POST", payload={"markdown": MARKDOWN})
    assert status == 200 and created["item"]["revision"] == 1
    assert (await request("POST", payload={"markdown": MARKDOWN}))[0] == 409
    status, changed = await request("PUT", "/api/skills/weekly", {"markdown": MARKDOWN, "expected_revision": 1, "enabled": False})
    assert status == 200 and changed["item"]["enabled"] is False
    assert (await request("DELETE", "/api/skills/weekly", {"expected_revision": 1}))[0] == 409
    assert (await request("DELETE", "/api/skills/weekly", {"expected_revision": 2}))[0] == 200
    assert (await request("GET"))[1]["items"] == []


@pytest.mark.asyncio
async def test_skill_library_http_entry_auth_cors_and_static_routing(tmp_path, monkeypatch):
    """通过完整 TCP 入口验证技能接口不被 SPA 捕获, 同时保留 Cookie 认证和精确 CORS"""
    import asyncio
    from satrap.core.backend import control_server as control
    from satrap.core.backend.static_ui import SPAStaticService
    from satrap.core.server_auth import ServerAuth
    monkeypatch.setattr(storage, "get_project_root", lambda: tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa-only</html>", encoding="utf-8")
    monkeypatch.setattr(control, "CONTROL_STATIC_UI", SPAStaticService(
        dist, excluded_prefixes=control.CONTROL_STATIC_UI.excluded_prefixes))
    origin = "http://localhost:5173"
    auth = ServerAuth.create("127.0.0.1", 0, token="skills-http-test-token-for-complete-entry-regression",
                             allowed_origins=frozenset({origin}), session_namespace="skills-test")
    monkeypatch.setattr(control, "_CONTROL_AUTH", auth)
    server = await asyncio.start_server(control._handle_request, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]

        async def request(method, path, cookie="", bearer=False):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            headers = {"Host": f"127.0.0.1:{port}", "Origin": origin, "Content-Length": "0"}
            if cookie:
                headers["Cookie"] = cookie
            if bearer:
                headers["Authorization"] = f"Bearer {auth.token}"
            raw = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
            writer.write(raw.encode("utf-8"))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()
            await writer.wait_closed()
            head, body = response.split(b"\r\n\r\n", 1)
            lines = head.decode("utf-8").split("\r\n")
            return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:]), body

        status, headers, _ = await request("POST", "/auth/session", bearer=True)
        assert status == 200
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        for path in ("/api/skills", "/api/skills/plugin-catalog"):
            status, headers, body = await request("GET", path, cookie)
            assert status == 200
            assert headers["Content-Type"] == "application/json"
            assert headers["Access-Control-Allow-Origin"] == origin
            assert headers["Access-Control-Allow-Credentials"] == "true"
            assert isinstance(json.loads(body)["items"], list)
            assert b"spa-only" not in body
            status, _, _ = await request("GET", path)
            assert status == 401
        for method, path in (("POST", "/api/skills"), ("PUT", "/api/skills/example"),
                             ("DELETE", "/api/skills/example")):
            status, _, _ = await request(method, path)
            assert status == 401
        status, _, body = await request("GET", "/plugins/skills")
        assert status == 200 and b"spa-only" in body
