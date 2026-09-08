from __future__ import annotations
from typing import Any, Dict, List, Optional, Union
import os
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from .utils import _parse_front_matter


class Skill:
    """技能; 由名称, 指令文本, 关联工具与可选自带工具组成"""

    def __init__(
        self,
        name: str,
        instructions: str,
        tool_names: Optional[List[str]] = None,
        description: str = "",
        source: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Any]] = None,
        mcp_clients: Optional[List[Any]] = None,
        skill_id: Optional[str] = None,
    ):
        """
        参数:
        - name: 技能名称
        - instructions: 技能指令文本, 激活时注入系统提示词
        - tool_names: 关联工具名列表, 激活时在 ToolsManager 中启用
        - description: 技能简介
        - source: 技能来源路径 (md 文件或文件夹, 可选)
        - meta: 元信息 (作者, 版本等, 来自 meta.yaml)
        - tools: 自带工具实例列表 (来自 tools.py)
        - mcp_clients: 自带 MCP 客户端实例列表 (来自 tools.py)
        - skill_id: 技能身份识别符 (来自 meta.yaml 的 satrap-skill-id), 同名区分
        """
        self.name = name
        self.skill_id = skill_id
        self.instructions = instructions
        self.tool_names = list(tool_names or [])
        self.description = description
        self.source = source
        self.meta: Dict[str, Any] = dict(meta or {})
        self.tools: List[Union[Tool, AsyncTool]] = list(tools or [])
        self.mcp_clients: List[Any] = list(mcp_clients or [])

    @classmethod
    def from_file(cls, file_path: str, default_name: Optional[str] = None) -> "Skill":
        """
        从 Markdown 技能文件加载技能 (支持 YAML front matter)

        参数:
        - file_path: 技能 md 文件路径
        - default_name: front matter 未声明 name 时使用的名称, 默认取文件名

        返回:
        - 'Skill': 从 Markdown 技能文件加载技能 (支持 YAML front matter)
        """
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        meta, body = _parse_front_matter(text)
        name = str(
            meta.get("name")
            or default_name
            or os.path.splitext(os.path.basename(file_path))[0]
        )
        tools: list[str] | str = meta.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        description = str(meta.get("description") or "")
        return cls(
            name=name,
            instructions=body.strip(),
            tool_names=[str(t) for t in tools],
            description=description,
            source=file_path,
        )

    def to_text(self) -> str:
        """
        生成可注入系统提示词的指令块 (带技能标记, 便于反激活时剥离)

        标记使用注册 key (satrap-skill-id or name), 同名不同 id 的技能注入标记互不相同,
        反激活时按 key 剥离不会误伤同名的其他技能

        返回:
        - str: 生成可注入系统提示词的指令块 (带技能标记, 便于反激活时剥离)
        """
        key = self.skill_id or self.name
        lines = [f"<skill:{key}>"]
        if self.description:
            lines.append(f"描述: {self.description}")
        if self.instructions:
            lines.append(self.instructions)
        if self.tool_names:
            lines.append(f"可用工具: {', '.join(self.tool_names)}")
        lines.append(f"</skill:{key}>")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Skill(name={self.name!r}, tools={self.tool_names!r})"
