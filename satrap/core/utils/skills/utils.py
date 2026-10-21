"""
技能元数据解析及可信资源收集

启用技能只登记目录和装配可信代码资源, 不改写持久系统提示词;
模型通过 load_skill 读取正文, 外部 Markdown 导入统一交给 skills 插件,
显式 SDK 扫描仍支持旧 skill.md, meta.yaml 和可信 tools.py
"""

from __future__ import annotations
import importlib.util
from pathlib import Path
from typing import Any, List, Tuple, Union, cast
from typing import Protocol
import yaml
import re
from satrap.core.log import logger
from satrap.core.type import safe_getattr, safe_getattr_callable
from satrap.core.utils.TCBuilder import AsyncTool, Tool, ToolsManager, AsyncToolsManager
from satrap.core.utils.context import AsyncContextManager, ContextManager


class _YamlLoader(Protocol):
    """声明技能加载只依赖的 YAML 解析接口"""

    def safe_load(self, stream: object) -> object:
        """
        解析 YAML 文本或文本流

        参数:
        - stream: YAML 文本或文本流

        返回:
        - 解析后的动态结构
        """
        ...


_yaml_loader = cast(_YamlLoader, yaml)


class SkillWorkflowProtocol(Protocol):
    """
    技能装配所需的最小工作流接口

    生产中的 ModelWorkflowFramework / AsyncModelWorkflowFramework 结构满足本接口,
    测试中的轻量替身 (如 SimpleNamespace) 亦可直接装配
    """

    @property
    def ctx(self) -> Union[ContextManager, AsyncContextManager]:
        """上下文管理器 (同步或异步)"""
        ...

    @property
    def tools_manager(self) -> Union[ToolsManager, AsyncToolsManager, None]:
        """工具管理器 (同步或异步), 可为 None"""
        ...


SKILLS_PRESET_DIR = str(Path(__file__).resolve().parents[3] / "expend" / "skills")

"""官方预设技能目录 (satrap/expend/skills), 只读基线"""

DEFAULT_USER_SKILLS_DIR = ".satrap/skills"

"""默认用户技能目录 (相对工作目录), 用户自添加技能"""

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_SKILL_TOOLS_MODULE_COUNTER = 0


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """
    解析 Markdown 文本的 YAML front matter, 返回 (元数据字典, 正文)

    参数:
    - text: 待处理文本

    返回:
    - tuple[dict[str, Any], str]:  (元数据字典, 正文)
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta: object = _yaml_loader.safe_load(match.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
        else:
            meta = cast(dict[str, Any], meta)
    except Exception as e:
        logger.warning(f"[技能] front matter 解析失败: {e}")
        meta = {}
    return meta, text[match.end() :]


def _load_skill_tools(tools_path: str) -> Tuple[List[Any], List[Any]]:
    """
    从技能的 tools.py 加载自带工具与 MCP 客户端

    支持:
    - `get_tools()` 工厂函数, 返回工具实例列表
    - `get_mcp_clients()` 工厂函数, 返回 MCPClient 实例列表
    - 模块内定义的 Tool / AsyncTool 子类 (无参构造函数自动实例化)

    参数:
    - tools_path: 工具列表路径

    返回:
    - (工具实例列表, MCPClient 实例列表)
    """
    global _SKILL_TOOLS_MODULE_COUNTER
    _SKILL_TOOLS_MODULE_COUNTER += 1
    module_name = f"_satrap_skill_tools_{_SKILL_TOOLS_MODULE_COUNTER}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, tools_path)
        if spec is None or spec.loader is None:
            return [], []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        logger.error(f"[技能管理] 加载自带工具失败: {tools_path}: {e}")
        return [], []

    tools: List[Any] = []
    get_tools = safe_getattr_callable(module, "get_tools")
    if get_tools is not None:
        try:
            result = get_tools()
            if isinstance(result, (list, tuple)):
                tools.extend(cast(list[Any], result))
            else:
                tools.append(result)
        except Exception as e:
            logger.error(f"[技能管理] get_tools() 执行失败: {e}")
    else:
        for attr in vars(module).values():
            if not (
                isinstance(attr, type)
                and safe_getattr(attr, "__module__") == module.__name__
            ):
                continue
            if issubclass(attr, (Tool, AsyncTool)):
                try:
                    tools.append(attr())
                except TypeError:
                    pass  # 构造函数需要参数, 应通过 get_tools() 提供

    mcp_clients: List[Any] = []
    get_mcp_clients = safe_getattr_callable(module, "get_mcp_clients")
    if get_mcp_clients is not None:
        try:
            result = get_mcp_clients()
            if isinstance(result, (list, tuple)):
                mcp_clients.extend(cast(list[Any], result))
            else:
                mcp_clients.append(result)
        except Exception as e:
            logger.error(f"[技能管理] get_mcp_clients() 执行失败: {e}")

    return tools, mcp_clients
