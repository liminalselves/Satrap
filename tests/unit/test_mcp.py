"""MCP 扩展单元测试 (不依赖真实 MCP Server, 使用假会话)"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from mcp.types import Tool as MCPTool

from satrap.core.utils.TCBuilder import AsyncToolsManager, Tool, ToolsManager
from satrap.core.utils.mcp import (
    MCPClient,
    MCPToolAdapter,
    MCPServerExporter,
    content_to_text,
    export_tools_to_mcp,
)


def _make_mcp_tool(name: str = "read_file", description: str = "读取文件", schema: dict | None = None):
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
        self.calls = []
        self.tools = [_make_mcp_tool()]

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name: str, arguments: dict | None = None):
        self.calls.append((name, arguments))
        if self._error:
            raise self._error
        return SimpleNamespace(content=self._content, is_error=self._is_error)


def _make_adapter(session: FakeSession | None = None, name: str = "read_file", schema: dict | None = None):
    session = session or FakeSession()
    return MCPToolAdapter(session, _make_mcp_tool(name=name, schema=schema))


# ================= MCPToolAdapter =================

def test_adapter_tool_definition_preserves_full_schema():
    schema = {
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

        async def call_tool(self, name: str, arguments: dict | None = None):
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


# ================= MCPClient =================

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


# ================= content_to_text =================

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


# ================= MCPServerExporter =================

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
