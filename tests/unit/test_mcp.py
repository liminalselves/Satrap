"""MCP 扩展单元测试 (不依赖真实 MCP Server, 使用假会话)"""

from __future__ import annotations

from mcp.types import Tool as MCPTool
import threading
import asyncio
import inspect
from pathlib import Path
import pytest
from typing import Any
from types import SimpleNamespace

from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import AsyncToolsManager, Tool, ToolsManager
from satrap.core.utils.mcp import (
    MCPClient,
    MCPToolAdapter,
    MCPServerExporter,
    SyncMCPToolAdapter,
    content_to_text,
    export_tools_to_mcp,
)
from satrap.core.type import LLMCallResponse
from satrap.edictum import SimpleSession


def _make_mcp_tool(name: str = "read_file", description: str = "读取文件", schema: dict[str, Any] | None = None):
    return MCPTool(
        name=name,
        description=description,
        input_schema=schema if schema is not None else {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "max_length": {"type": "integer", "description": "最大长度", "default": 100},
            },
            "required": ["path"],
        },
    )


class FakeSession:
    """假 MCP 会话, 可配置 call_tool 的返回内容"""

    def __init__(self, content: list[SimpleNamespace] | None = None, is_error: bool = False, error: Exception | None = None):
        self._content = content if content is not None else [SimpleNamespace(type="text", text="ok")]
        self._is_error = is_error
        self._error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.tools = [_make_mcp_tool()]

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        self.calls.append((name, arguments))
        if self._error:
            raise self._error
        return SimpleNamespace(content=self._content, is_error=self._is_error)


def _make_adapter(session: FakeSession | None = None, name: str = "read_file", schema: dict[str, Any] | None = None):
    session = session or FakeSession()
    return MCPToolAdapter(session, _make_mcp_tool(name=name, schema=schema))


# ================= MCPToolAdapter 测试 =================

def test_adapter_tool_definition_preserves_full_schema():
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "查询词", "enum": ["a", "b"]},
            "limit": {"type": "integer", "description": "数量", "default": 5},
        },
        "required": ["query"],
    }
    adapter = _make_adapter(schema=schema)
    defined = adapter.get_tool_defined()

    assert defined["type"] == "function"
    fn = defined["function"]
    assert fn["name"] == "read_file"
    assert fn["description"] == "读取文件"
    assert fn["parameters"] == schema
    assert adapter.params_dict == {"query": ("string", "查询词"), "limit": ("integer", "数量")}


def test_adapter_empty_schema_defaults_to_object():
    adapter = _make_adapter(schema={})
    assert adapter.get_tool_defined()["function"]["parameters"] == {"type": "object", "properties": {}}


async def test_adapter_execute_returns_text_content():
    session = FakeSession(content=[SimpleNamespace(type="text", text="hello mcp")])
    adapter = _make_adapter(session)
    result = await adapter.execute(path="/tmp/a.txt")
    assert result == "hello mcp"
    assert session.calls == [("read_file", {"path": "/tmp/a.txt"})]


async def test_adapter_execute_empty_content():
    adapter = _make_adapter(FakeSession(content=[]))
    assert await adapter.execute(path="x") == "OK"


async def test_adapter_execute_error_content_returns_error_dict():
    session = FakeSession(
        content=[SimpleNamespace(type="text", text="file not found")],
        is_error=True,
    )
    result = await _make_adapter(session).execute(path="x")
    assert result["ok"] is False
    assert result["error_type"] == "mcp_error"
    assert "file not found" in result["error"]
    assert result["tool_name"] == "read_file"


async def test_adapter_execute_exception_returns_error_dict():
    session = FakeSession(error=RuntimeError("boom"))
    result = await _make_adapter(session).execute(path="x")
    assert result["ok"] is False
    assert "boom" in result["error"]


def test_adapter_prefix_applied_to_name():
    adapter = _make_adapter(name="read_file")
    assert adapter.get_tool_name() == "read_file"
    prefixed = MCPToolAdapter(FakeSession(), _make_mcp_tool(), name_prefix="fs")
    assert prefixed.get_tool_name() == "fs_read_file"


async def test_adapter_in_async_tools_manager():
    class FakeMCPTool:
        name = "echo"
        description = "回显"

    class FakeSession2:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=str(arguments))],
                is_error=False,
            )

    manager = AsyncToolsManager()
    manager.register_tool(MCPToolAdapter(FakeSession2(), FakeMCPTool(), name_prefix="mcp"))
    assert len(manager.get_tools_definitions()) == 1
    tool_message, result = await manager.execute_tool_call(
        {"name": "mcp_echo", "arguments": {"msg": "hi"}}
    )
    assert tool_message["function"]["name"] == "mcp_echo"
    assert result == "{'msg': 'hi'}"


# ================= MCPClient 测试 =================

def test_client_requires_command_or_url():
    with pytest.raises(ValueError):
        MCPClient()


async def test_client_register_and_close(monkeypatch: pytest.MonkeyPatch):
    fake_session = FakeSession()

    async def fake_connect(self: MCPClient):
        self.session = fake_session
        self._connected = True
        return fake_session

    monkeypatch.setattr(MCPClient, "connect", fake_connect)

    mcp = MCPClient(command="npx", args=["-y", "some-server"], name="fs")
    manager = AsyncToolsManager()
    adapters = await mcp.register_tools(manager)

    assert len(adapters) == 1
    assert adapters[0].get_tool_name() == "fs_read_file"
    assert "fs_read_file" in manager.tools
    assert manager.is_tool_enabled("fs_read_file")

    await mcp.close()
    assert "fs_read_file" not in manager.tools
    assert mcp._connected is False


# ================= 同步模式 (SimpleSession) =================


def _background_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """
    启动一个后台事件循环线程 (daemon), 返回 (loop, thread)

    返回:
    - tuple[asyncio.AbstractEventLoop, threading.Thread]:  (loop, thread)
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def test_sync_adapter_executes_via_background_loop():
    """同步适配器: 通过后台事件循环桥接异步 MCP 调用"""
    loop, thread = _background_loop()
    try:
        session = FakeSession(content=[SimpleNamespace(type="text", text="sync ok")])
        inner = MCPToolAdapter(session, _make_mcp_tool())
        adapter = SyncMCPToolAdapter(inner, loop)
        assert adapter.get_tool_defined()["function"]["name"] == "read_file"

        result = adapter.execute(path="/tmp/a.txt")
        assert result == "sync ok"
        assert session.calls == [("read_file", {"path": "/tmp/a.txt"})]
    finally:
        _stop_loop(loop, thread)


def test_sync_adapter_error_returns_error_dict():
    """同步适配器: 远端异常返回错误字典而非抛出"""
    loop, thread = _background_loop()
    try:
        session = FakeSession(error=RuntimeError("boom"))
        adapter = SyncMCPToolAdapter(MCPToolAdapter(session, _make_mcp_tool()), loop)
        result = adapter.execute(path="x")
        assert result["ok"] is False
        assert "boom" in result["error"]
    finally:
        _stop_loop(loop, thread)


def test_sync_adapter_timeout_returns_error_dict():
    """同步适配器: 调用超时返回错误字典而非抛出"""
    class SlowSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
            await asyncio.sleep(30)

    loop, thread = _background_loop()
    try:
        adapter = SyncMCPToolAdapter(MCPToolAdapter(SlowSession(), _make_mcp_tool()), loop, timeout=0.1)
        result = adapter.execute(path="x")
        assert result["ok"] is False
        assert "超时" in result["error"]
    finally:
        _stop_loop(loop, thread)


def test_sync_register_and_close(monkeypatch: pytest.MonkeyPatch):
    """
    同步注册: 工具注册进同步 ToolsManager, 可执行, 关闭后注销

    参数:
    - monkeypatch: pytest monkeypatch 夹具
    """
    fake_session = FakeSession()

    async def fake_connect(self: MCPClient):
        self.session = fake_session
        self._connected = True
        return fake_session

    monkeypatch.setattr(MCPClient, "connect", fake_connect)

    mcp = MCPClient(command="npx", args=["-y", "some-server"], name="fs")
    manager = ToolsManager()
    adapters = mcp.sync_register_tools(manager)

    assert len(adapters) == 1
    assert adapters[0].get_tool_name() == "fs_read_file"
    assert "fs_read_file" in manager.tools
    assert manager.is_tool_enabled("fs_read_file")
    assert manager.execute_tool("fs_read_file", {"path": "/x"}) == "ok"

    mcp.sync_close()
    assert "fs_read_file" not in manager.tools
    assert mcp._connected is False


def test_sync_plugin_install_mcp_register_and_uninstall(tmp_path: Path):
    """
    同步 SimpleSession: 含 mcp.py 插件可安装, 工具注册, 卸载时断开连接

    参数:
    - tmp_path: tmp路径
    """
    class _FakeLLM(LLM):
        def __init__(self) -> None:
            super().__init__(api_key="demo")

        def call(self, messages: list[dict[str, Any]], model: str | None = None, **kwargs: Any):
            return LLMCallResponse(type="answer", content="回复")

    plugin_dir = tmp_path / "mcp-demo"
    plugin_dir.mkdir()
    (plugin_dir / "meta.yaml").write_text("name: mcp-demo\nversion: 0.1\n", encoding="utf-8")
    (plugin_dir / "mcp.py").write_text(
        """
from satrap.core.utils.TCBuilder import Tool

class _EchoTool(Tool):
    tool_name = "remote_echo"
    description = "远端回显"
    params_dict = {"msg": ("string", "消息")}

    def execute(self, *input, **kwargs):
        return "remote:" + str(kwargs.get("msg", ""))

class _FakeMCPClient:
    def __init__(self):
        self.closed = False

    def sync_register_tools(self, tools_manager, name_prefix=None):
        tool = _EchoTool()
        tools_manager.register_tool(tool)
        return [tool]

    def sync_close(self):
        self.closed = True

clients = {"fake": _FakeMCPClient()}
""",
        encoding="utf-8",
    )

    session = SimpleSession("mcp-sync", _FakeLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=False)
    plugin = session.install_plugin(str(plugin_dir))

    assert plugin.mcp == {"fake": True}
    assert "remote_echo" in session.tools_manager.tools
    assert session.tools_manager.execute_tool("remote_echo", {"msg": "hi"}) == "remote:hi"

    assert session.uninstall_plugin("mcp-demo") is True
    assert "remote_echo" not in session.tools_manager.tools
    assert plugin._mcp_clients["fake"][0].closed is True


# ================= content_to_text 测试 =================

def test_content_to_text_handles_various_blocks():
    assert content_to_text(None) == "OK"
    assert content_to_text([SimpleNamespace(type="text", text="a")]) == "a"
    assert (
        content_to_text(
            [SimpleNamespace(type="text", text="a"), SimpleNamespace(type="text", text="b")]
        )
        == "a\nb"
    )
    assert content_to_text(
        [SimpleNamespace(type="image", mimeType="image/png", data="xx")]
    ).startswith("[image:")


# ================= MCPServerExporter 测试 =================

def test_exporter_registers_sync_tools():
    class EchoTool(Tool):
        def __init__(self):
            super().__init__(
                tool_name="echo",
                description="回显消息",
                params_dict={"msg": ("string", "消息内容")},
            )

        def execute(self, msg: str):
            return {"echo": msg}

    manager = ToolsManager()
    manager.register_tool(EchoTool())

    server = export_tools_to_mcp(manager, name="test-server")
    assert server.name == "test-server"

    exporter = MCPServerExporter(manager, name="test-server-2")
    handler = exporter._build_handler(EchoTool())
    assert list(inspect.signature(handler).parameters) == ["msg"]
    assert handler(msg="hi") == {"echo": "hi"}
    assert inspect.signature(handler).parameters["msg"].annotation is str
