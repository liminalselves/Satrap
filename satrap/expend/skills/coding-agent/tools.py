"""coding-agent 技能的自带工具

约定:
- `get_tools()`: 可选, 返回工具实例列表 (构造函数需要参数的场景)
- `get_mcp_clients()`: 可选, 返回 MCPClient 实例列表 (激活时自动连接并注册)
- 无 get_tools 时, 模块内定义的 Tool / AsyncTool 子类会被自动实例化并收集

自定义工具名会自动加入技能的工具列表, 无需在 skill.md 的 tools 中重复声明
"""

from satrap.core.utils.TCBuilder import Tool


class DateTool(Tool):
    """获取当前日期 (自带的简单示例工具)"""

    tool_name = "current_date"
    description = "获取当前日期, 格式 YYYY-MM-DD"
    params_dict = {}

    def execute(self) -> str:
        import datetime
        return str(datetime.date.today())


def get_tools():
    """返回本技能自带工具实例列表"""
    return [DateTool()]
