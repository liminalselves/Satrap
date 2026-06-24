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


class RaisingTool(Tool):
    def __init__(self):
        super().__init__(
            tool_name="raising",
            description="异常工具",
            params_dict={"value": ("number", "输入值")},
        )
        self.calls = 0

    def execute(self, value: int):
        self.calls += 1
        raise RuntimeError(f"boom {value}")


class AsyncRaisingTool(AsyncTool):
    def __init__(self):
        super().__init__(
            tool_name="async_raising",
            description="异步异常工具",
            params_dict={"value": ("number", "输入值")},
        )
        self.calls = 0

    async def execute(self, value: int):
        self.calls += 1
        raise RuntimeError(f"async boom {value}")


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

    assert result["error"] == "工具 counting 已禁用"
    assert result["ok"] is False
    assert result["error_type"] == "disabled"
    assert result["tool_name"] == "counting"
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

        assert result["error"] == "工具 async_counting 已禁用"
        assert result["ok"] is False
        assert result["error_type"] == "disabled"
        assert result["tool_name"] == "async_counting"
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
    assert tool_result["error"] == "工具 counting 已禁用"
    assert tool_result["error_type"] == "disabled"


def test_tools_manager_returns_structured_errors_without_executing_invalid_calls():
    manager = ToolsManager()
    tool = CountingTool()
    manager.register_tool(tool)

    missing_result = manager.execute_tool("missing", {})
    assert missing_result["error_type"] == "not_found"
    assert missing_result["tool_name"] == "missing"

    invalid_name_result = manager.execute_tool("", {})
    assert invalid_name_result["error_type"] == "invalid_tool_call"

    invalid_args_result = manager.execute_tool("counting", "not a dict")   # type: ignore[arg-type]
    assert invalid_args_result["error_type"] == "invalid_arguments"
    assert tool.calls == 0

    none_args_result = manager.execute_tool("counting", None)   # type: ignore[arg-type]
    assert none_args_result["error_type"] == "execution_error"
    assert tool.calls == 0


def test_tools_manager_catches_tool_exceptions_as_structured_errors():
    manager = ToolsManager()
    tool = RaisingTool()
    manager.register_tool(tool)

    result = manager.execute_tool("raising", {"value": 3})

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"
    assert result["tool_name"] == "raising"
    assert "boom 3" in result["error"]
    assert tool.calls == 1


def test_execute_tool_call_handles_malformed_call_info_without_raising():
    manager = ToolsManager()
    manager.register_tool(CountingTool())

    tool_message, tool_result = manager.execute_tool_call("bad call")   # type: ignore[arg-type]
    assert tool_message["id"] == ""
    assert tool_message["function"]["name"] == ""
    assert tool_result["error_type"] == "invalid_tool_call"

    tool_message, tool_result = manager.execute_tool_call(
        {"id": "call_bad", "arguments": {"value": 1}}
    )
    assert tool_message["id"] == "call_bad"
    assert tool_result["error_type"] == "invalid_tool_call"

    tool_message, tool_result = manager.execute_tool_call(
        {"id": "call_bad_args", "name": "counting", "arguments": ["value", 1]}
    )
    assert tool_message["id"] == "call_bad_args"
    assert tool_result["error_type"] == "invalid_arguments"


def test_async_tools_manager_returns_structured_errors_and_catches_exceptions():
    async def _run():
        manager = AsyncToolsManager()
        counting_tool = AsyncCountingTool()
        raising_tool = AsyncRaisingTool()
        manager.register_tool(counting_tool)
        manager.register_tool(raising_tool)

        missing_result = await manager.execute_tool("missing", {})
        assert missing_result["error_type"] == "not_found"

        invalid_args_result = await manager.execute_tool("async_counting", "not a dict")   # type: ignore[arg-type]
        assert invalid_args_result["error_type"] == "invalid_arguments"
        assert counting_tool.calls == 0

        exception_result = await manager.execute_tool("async_raising", {"value": 7})
        assert exception_result["ok"] is False
        assert exception_result["error_type"] == "execution_error"
        assert exception_result["tool_name"] == "async_raising"
        assert "async boom 7" in exception_result["error"]
        assert raising_tool.calls == 1

        tool_message, tool_result = await manager.execute_tool_call(
            {"id": "call_bad_args", "name": "async_counting", "arguments": "bad"}
        )
        assert tool_message["id"] == "call_bad_args"
        assert tool_result["error_type"] == "invalid_arguments"

    asyncio.run(_run())
