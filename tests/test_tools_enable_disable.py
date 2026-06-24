from __future__ import annotations

import asyncio

from satrap.core.utils.TCBuilder import AsyncTool, AsyncToolsManager, Tool, ToolsManager


class CountingTool(Tool):
    def __init__(self):
        super().__init__(
            tool_name="counting",
            description="计数工具",
            params_dict={"value": ("number", "输入值")},
        )
        self.calls = 0

    def execute(self, value: int):
        self.calls += 1
        return {"value": value}


class AsyncCountingTool(AsyncTool):
    def __init__(self):
        super().__init__(
            tool_name="async_counting",
            description="异步计数工具",
            params_dict={"value": ("number", "输入值")},
        )
        self.calls = 0

    async def execute(self, value: int):
        self.calls += 1
        return {"value": value}


class BrokenTool(Tool):
    def __init__(self):
        super().__init__()


def test_tools_manager_can_disable_and_enable_tool():
    manager = ToolsManager()
    tool = CountingTool()
    manager.register_tool(tool)

    assert manager.is_tool_enabled("counting") is True
    assert len(manager.get_tools_definitions()) == 1
    assert manager.disable_tool("counting") is True
    assert "counting" in manager.tools
    assert manager.is_tool_enabled("counting") is False
    assert manager.get_tools_definitions() == []

    result = manager.execute_tool("counting", {"value": 1})

    assert result == {"error": "工具 counting 已禁用"}
    assert tool.calls == 0

    assert manager.enable_tool("counting") is True
    assert len(manager.get_tools_definitions()) == 1
    assert manager.execute_tool("counting", {"value": 2}) == {"value": 2}
    assert tool.calls == 1


def test_tools_manager_bulk_enable_disable_and_unregister_regression():
    manager = ToolsManager()
    tool = CountingTool()
    manager.register_tool(tool)

    assert manager.disable_tool("missing") is False
    assert manager.enable_tool("missing") is False
    assert manager.disable_all_tools() is True
    assert manager.get_tools_definitions() == []
    assert manager.enable_all_tools() is True
    assert len(manager.get_tools_definitions()) == 1

    assert manager.unregister_tool("counting") is True
    assert "counting" not in manager.tools
    assert manager.unregister_all_tools() is False


def test_incomplete_tool_still_cannot_register():
    manager = ToolsManager()
    manager.register_tool(BrokenTool())

    assert manager.tools == {}


def test_async_tools_manager_can_disable_and_enable_tool():
    async def _run():
        manager = AsyncToolsManager()
        tool = AsyncCountingTool()
        manager.register_tool(tool)

        assert manager.is_tool_enabled("async_counting") is True
        assert len(manager.get_tools_definitions()) == 1
        assert manager.disable_tool("async_counting") is True
        assert "async_counting" in manager.tools
        assert manager.get_tools_definitions() == []

        result = await manager.execute_tool("async_counting", {"value": 1})

        assert result == {"error": "工具 async_counting 已禁用"}
        assert tool.calls == 0

        assert manager.enable_tool("async_counting") is True
        assert await manager.execute_tool("async_counting", {"value": 2}) == {"value": 2}
        assert tool.calls == 1

    asyncio.run(_run())


def test_execute_tool_call_preserves_return_shape_for_disabled_tool():
    manager = ToolsManager()
    manager.register_tool(CountingTool())
    manager.disable_tool("counting")

    tool_message, tool_result = manager.execute_tool_call(
        {"name": "counting", "id": "call_1", "arguments": {"value": 1}}
    )

    assert tool_message["id"] == "call_1"
    assert tool_result == {"error": "工具 counting 已禁用"}
