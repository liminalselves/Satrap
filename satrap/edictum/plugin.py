"""edictum 目录插件: 工具 + skill + MCP + 处理脚本的组合包

插件目录结构:
    插件名/
    ├── meta.yaml     # name(必填) / version / author / repo / description
    ├── tools.py      # 可选: Tool 子类 (同步版) / AsyncTool 子类 (异步版), 约定收集
    ├── skills.py     # 可选: 导出 skills: list[Skill]; 或 skills/ 子目录 (skill.md 文件夹式)
    ├── mcp.py        # 可选: 导出 clients: dict[str, MCPClient] 或 build_clients() (仅异步版)
    ├── handlers.py   # 可选: 导出 handlers: list[SessionHandler]; 或 4 个约定函数
    └── ...           # 插件私有模块

双层状态模型:
- 插件.enabled (聚合开关) × 能力独立状态 (tools/skills/mcp/handlers 字典值)
- 能力生效 = 插件启用 ∧ 独立启用
- disable_plugin 只压制生效 (批量关闭 manager), 不改独立状态
- enable_plugin 按独立状态恢复; 独立启停请走插件命名空间接口 (plugin.disable_tool 等)
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast

import yaml

from satrap.core.log import logger
from satrap.core.utils.skills import Skill
from satrap.core.utils.TCBuilder import AsyncTool, Tool

T = TypeVar("T")


def load_plugin_meta(plugin_dir: Path) -> dict[str, Any]:
    """解析插件 meta.yaml, 校验存在且为字典"""
    meta_path = plugin_dir / "meta.yaml"
    if not meta_path.is_file():
        raise ValueError(f"插件目录缺少 meta.yaml: {plugin_dir}")
    with open(meta_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"插件 meta.yaml 格式错误: {meta_path}")
    return {str(k): v for k, v in cast(dict[str, Any], raw).items()}


def _load_module(path: Path, module_name: str) -> Any | None:
    """动态加载插件模块 (文件不存在返回 None)"""
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载插件模块: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def collect_tools(plugin_dir: Path, module_name: str, base: type[T]) -> list[T]:
    """收集 tools.py 中定义的 base 子类实例 (约定收集, 排除基类本身)"""
    mod = _load_module(plugin_dir / "tools.py", f"{module_name}.tools")
    if mod is None:
        return []
    found: list[T] = []
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if isinstance(obj, type) and issubclass(obj, base) and obj is not base:
            found.append(cast(T, obj()))
    return found


def collect_skills(plugin_dir: Path, module_name: str) -> list[Skill]:
    """收集 skills/ 子目录 (skill.md 文件夹式) 与 skills.py 导出的 skills 列表"""
    found: list[Skill] = []
    skills_dir = plugin_dir / "skills"
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            md = entry / "skill.md"
            if md.is_file():
                found.append(Skill.from_file(str(md), default_name=entry.name))
    mod = _load_module(plugin_dir / "skills.py", f"{module_name}.skills")
    if mod is not None:
        declared = getattr(mod, "skills", None)
        if declared:
            found.extend(declared)
    return found


def collect_mcp_clients(plugin_dir: Path, module_name: str) -> dict[str, Any]:
    """收集 mcp.py 的客户端映射: 导出 clients: dict 或 build_clients() 工厂"""
    mod = _load_module(plugin_dir / "mcp.py", f"{module_name}.mcp")
    if mod is None:
        return {}
    clients = getattr(mod, "clients", None)
    if isinstance(clients, dict):
        return {str(k): v for k, v in cast(dict[str, Any], clients).items()}
    builder = getattr(mod, "build_clients", None)
    if callable(builder):
        built = builder()
        if isinstance(built, dict):
            return {str(k): v for k, v in cast(dict[str, Any], built).items()}
    return {}


def collect_handlers(plugin_dir: Path, module_name: str) -> list[Any]:
    """收集 handlers.py: 优先导出 handlers 列表; 否则按 4 个约定函数构建处理器"""
    mod = _load_module(plugin_dir / "handlers.py", f"{module_name}.handlers")
    if mod is None:
        return []
    declared = getattr(mod, "handlers", None)
    if declared:
        return list(declared)
    from satrap.edictum.simple_session import SessionHandler

    funcs: dict[str, Any] = {}
    for key in ("before_user_send", "after_user_send", "before_model_reply", "after_model_reply"):
        fn = getattr(mod, key, None)
        if fn is not None:
            funcs[key] = fn
    if not funcs:
        return []
    return [SessionHandler(name=f"{module_name}.handlers", **funcs)]


@dataclass
class Plugin:
    """已安装的目录插件: 元信息 + 能力清单 + 双层启停状态

    - tools/skills/mcp/handlers: 能力名 -> 独立启用状态 (True=独立启用)
    - enabled: 插件聚合开关; 能力生效 = enabled ∧ 独立状态
    """

    name: str
    version: str = ""
    author: str = ""
    repo: str = ""
    description: str = ""
    path: str = ""
    enabled: bool = True
    tools: dict[str, bool] = field(default_factory=dict[str, bool])
    skills: dict[str, bool] = field(default_factory=dict[str, bool])
    mcp: dict[str, bool] = field(default_factory=dict[str, bool])
    handlers: dict[str, bool] = field(default_factory=dict[str, bool])
    _session: Any = field(default=None, repr=False, compare=False)
    _mcp_clients: dict[str, tuple[Any, list[Any]]] = field(
        default_factory=dict[str, tuple[Any, list[Any]]], repr=False, compare=False,
    )

    # ---------------- 命名空间独立启停 (推荐用法) ----------------

    def enable_tool(self, name: str) -> bool:
        """独立启用插件内工具 (插件停用期间只改状态, 不生效)"""
        if name not in self.tools:
            return False
        self.tools[name] = True
        if self.enabled:
            self._session._wf.tools_manager.enable_tool(name)
        return True

    def disable_tool(self, name: str) -> bool:
        """独立停用插件内工具"""
        if name not in self.tools:
            return False
        self.tools[name] = False
        if self.enabled:
            self._session._wf.tools_manager.disable_tool(name)
        return True

    def enable_skill(self, name: str) -> Any:
        """独立启用插件内技能

        返回: 同步版为 bool; 异步版为 coroutine (await 后为 bool)
        """
        if name not in self.skills:
            return False
        self.skills[name] = True
        if self.enabled:
            return self._session._activate_plugin_skill(name)
        return True

    def disable_skill(self, name: str) -> Any:
        """独立停用插件内技能 (异步版返回 coroutine, await 后为 bool)"""
        if name not in self.skills:
            return False
        self.skills[name] = False
        if self.enabled:
            return self._session._deactivate_plugin_skill(name)
        return True

    def enable_mcp(self, name: str) -> bool:
        """独立启用插件内 MCP 连接的全部工具"""
        if name not in self.mcp:
            return False
        self.mcp[name] = True
        if self.enabled:
            for adapter in self._mcp_clients.get(name, (None, []))[1]:
                self._session._wf.tools_manager.enable_tool(adapter.get_tool_name())
        return True

    def disable_mcp(self, name: str) -> bool:
        """独立停用插件内 MCP 连接的全部工具"""
        if name not in self.mcp:
            return False
        self.mcp[name] = False
        if self.enabled:
            for adapter in self._mcp_clients.get(name, (None, []))[1]:
                self._session._wf.tools_manager.disable_tool(adapter.get_tool_name())
        return True

    def enable_handler(self, name: str) -> bool:
        """独立启用插件内处理器"""
        if name not in self.handlers:
            return False
        self.handlers[name] = True
        if self.enabled:
            self._session.enable_handler(name)
        return True

    def disable_handler(self, name: str) -> bool:
        """独立停用插件内处理器"""
        if name not in self.handlers:
            return False
        self.handlers[name] = False
        if self.enabled:
            self._session.disable_handler(name)
        return True

    # ---------------- 状态查看 ----------------

    def list_capabilities(self) -> dict[str, list[dict[str, Any]]]:
        """列出插件内全部能力及其实效状态 (含聚合开关)"""
        return {
            "tools": [{"name": n, "enabled": self.enabled and s} for n, s in self.tools.items()],
            "skills": [{"name": n, "enabled": self.enabled and s} for n, s in self.skills.items()],
            "mcp": [{"name": n, "enabled": self.enabled and s} for n, s in self.mcp.items()],
            "handlers": [{"name": n, "enabled": self.enabled and s} for n, s in self.handlers.items()],
        }
