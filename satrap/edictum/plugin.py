"""edictum 目录插件: 工具 + skill + MCP + 命令 + 处理脚本的组合包

插件目录结构:
    插件名/
    ├── meta.yaml     # name(必填) / version / author / repo / description
    ├── tools.py      # 可选: Tool 子类 (同步版) / AsyncTool 子类 (异步版), 或 get_tools(session) 工厂
    ├── skills.py     # 可选: 导出 skills: list[Skill]; 或 skills/ 子目录 (skill.md 文件夹式)
    ├── mcp.py        # 可选: 导出 clients: dict[str, MCPClient] 或 build_clients() (仅异步版)
    ├── commands.py   # 可选: 导出 commands (同步) / async_commands (异步) 字典, 或 cmd_* / cmd_*_async 约定
    ├── handlers.py   # 可选: 导出 handlers: list[SessionHandler]; 或 4 个约定函数
    └── ...           # 插件私有模块

插件目录扫描:
- 官方预设目录 satrap/expend/plugins (只读基线)
- 用户插件目录 .satrap/plugins (用户自添加, 同名冲突时官方优先)

tools.py 工厂约定 (解决会话依赖注入):
- 优先 get_tools(session) (带会话实例), 签名不匹配时降级 get_tools()
- 无 get_tools 时收集模块内定义的 base 子类 (无参构造)

双层状态模型:
- 插件.enabled (聚合开关) × 能力独立状态 (tools/skills/mcp/handlers/commands 字典值)
- 能力生效 = 插件启用 ∧ 独立启用
- disable_plugin 只压制生效 (批量关闭 manager), 不改独立状态
- enable_plugin 按独立状态恢复; 独立启停请走插件命名空间接口 (plugin.disable_tool 等)
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

import yaml

from satrap.core.log import logger
from satrap.core.utils.skills import Skill
from satrap.core.utils.TCBuilder import AsyncTool, Tool

T = TypeVar("T")

PLUGINS_PRESET_DIR = Path(__file__).resolve().parents[1] / "expend" / "plugins"
"""官方预设插件目录 (satrap/expend/plugins), 只读基线"""

USER_PLUGINS_DIR = Path(".satrap") / "plugins"
"""用户插件目录 (相对工作目录), 用户自添加插件"""

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
    """动态加载插件模块 (文件不存在返回 None)

    若模块名已在 sys.modules 且来源路径一致 (如官方插件在包内), 复用已加载模块,
    避免同一文件被加载两次导致模块级状态 (如工具引用的 WORKSPACE_ROOT) 分裂
    """
    if not path.is_file():
        return None
    existing = sys.modules.get(module_name)
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file and Path(existing_file).resolve() == path.resolve():
            return existing
    # 模块名不精确匹配时 (如官方插件以包全名注册), 按源文件路径扫描复用
    resolved_path = path.resolve()
    for mod in list(sys.modules.values()):
        mod_file = getattr(mod, "__file__", None)
        if mod_file and Path(mod_file).resolve() == resolved_path:
            return mod
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载插件模块: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[module_name] = mod
    return mod


def collect_cleanup(
    plugin_dir: Path,
    module_name: str,
    session: Any | None = None,
) -> Callable[..., Any] | None:
    """收集插件卸载清理回调 (可选约定): state.py / hooks.py 导出的 cleanup(session)

    兼容旧命名 reset_plugin_state(session); 卸载时调用以回收插件级共享状态
    """
    for sub in ("state", "hooks"):
        mod = _load_module(plugin_dir / f"{sub}.py", f"{module_name}.{sub}")
        if mod is None:
            continue
        fn = getattr(mod, "cleanup", None)
        if not callable(fn):
            fn = getattr(mod, "reset_plugin_state", None)
        if callable(fn):
            return cast(Callable[..., Any], fn)
    return None


def collect_tools(
    plugin_dir: Path,
    module_name: str,
    base: type[T],
    session: Any | None = None,
) -> list[T]:
    """收集 tools.py 中的工具实例

    优先 get_tools(session) 工厂 (解决会话依赖注入), 签名不匹配时降级 get_tools();
    无工厂时收集模块内定义的 base 子类实例 (无参构造, 排除基类本身)
    """
    mod = _load_module(plugin_dir / "tools.py", f"{module_name}.tools")
    if mod is None:
        return []
    factory = getattr(mod, "get_tools", None)
    if callable(factory):
        attempts: list[tuple[Any, ...]] = [(session,)] if session is not None else [()]
        if session is not None:
            attempts.append(())
        for args in attempts:
            try:
                result: Any = factory(*args)
            except TypeError:
                continue
            if result is None:
                return []
            items_list = cast(list[Any], result) if isinstance(result, (list, tuple)) else [cast(Any, result)]
            return [cast(T, item) for item in items_list]
        raise ValueError(f"插件 {module_name} 的 get_tools 工厂调用失败 (参数不匹配)")
    found: list[T] = []
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if isinstance(obj, type) and issubclass(obj, base) and obj is not base:
            found.append(cast(T, obj()))
    return found


def collect_commands(
    plugin_dir: Path,
    module_name: str,
    session: Any | None = None,
) -> tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:
    """收集 commands.py 的命令映射, 返回 (同步命令, 异步命令)

    优先 build_commands(session) 工厂 (返回 (同步映射, 异步映射) 二元组或同步映射, 解决会话依赖注入);
    其次导出 commands (同步) / async_commands (异步) 字典;
    否则按约定收集: cmd_xxx 为同步命令, cmd_xxx_async 为异步命令
    """
    mod = _load_module(plugin_dir / "commands.py", f"{module_name}.commands")
    if mod is None:
        return {}, {}
    builder = getattr(mod, "build_commands", None)
    if callable(builder) and session is not None:
        built: Any = builder(session)
        if isinstance(built, tuple):
            pair = cast(tuple[Any, ...], built)
            if len(pair) == 2:
                return cast(dict[str, Callable[..., Any]], pair[0]), cast(dict[str, Callable[..., Any]], pair[1])
        if isinstance(built, dict):
            return cast(dict[str, Callable[..., Any]], built), {}
    declared = getattr(mod, "commands", None)
    if isinstance(declared, dict):
        sync_map: dict[str, Callable[..., Any]] = {}
        for k, v in cast(dict[str, Any], declared).items():
            if callable(v):
                sync_map[str(k)] = cast(Callable[..., Any], v)
        async_declared = getattr(mod, "async_commands", None)
        async_map: dict[str, Callable[..., Any]] = {}
        if isinstance(async_declared, dict):
            for k, v in cast(dict[str, Any], async_declared).items():
                if callable(v):
                    async_map[str(k)] = cast(Callable[..., Any], v)
        return sync_map, async_map
    sync_map, async_map = {}, {}
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if not callable(obj) or not attr_name.startswith("cmd_"):
            continue
        if inspect.iscoroutinefunction(obj):
            base = attr_name[4:-6] if attr_name.endswith("_async") else attr_name[4:]
            async_map[base] = obj
        else:
            sync_map[attr_name[4:]] = obj
    return sync_map, async_map


def scan_plugin_dirs(plugins_dir: str | Path | None = None) -> list[Path]:
    """扫描插件目录, 返回全部含合法 meta.yaml 的插件目录

    官方预设目录在前, 用户目录在后; 同名插件冲突时官方优先 (用户同名被跳过)
    """
    bases = [PLUGINS_PRESET_DIR]
    if plugins_dir is not None:
        bases.append(Path(plugins_dir))
    else:
        bases.append(USER_PLUGINS_DIR)
    found: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or not (entry / "meta.yaml").is_file():
                continue
            try:
                meta = load_plugin_meta(entry)
                name = str(meta.get("name") or "").strip()
            except Exception:
                name = ""
            if not name:
                logger.warning(f"[插件] 插件目录缺少合法 name, 跳过: {entry}")
                continue
            if name in seen:
                logger.info(f"[插件] 插件 {name} 已存在, 用户目录版本被跳过: {entry}")
                continue
            seen.add(name)
            found.append(entry)
    return found


def install_all_plugins(session: Any, plugins_dir: str | Path | None = None) -> list[Any]:
    """扫描并安装全部可用插件到同步会话 (单个失败不影响其余), 返回安装的 Plugin 列表"""
    installed: list[Any] = []
    for plugin_dir in scan_plugin_dirs(plugins_dir):
        try:
            installed.append(session.install_plugin(str(plugin_dir)))
        except Exception as e:
            logger.error(f"[插件] 安装插件 {plugin_dir} 失败: {e}")
    return installed


async def install_all_plugins_async(session: Any, plugins_dir: str | Path | None = None) -> list[Any]:
    """扫描并安装全部可用插件到异步会话 (单个失败不影响其余), 返回安装的 Plugin 列表"""
    installed: list[Any] = []
    for plugin_dir in scan_plugin_dirs(plugins_dir):
        try:
            installed.append(await session.install_plugin(str(plugin_dir)))
        except Exception as e:
            logger.error(f"[插件] 安装插件 {plugin_dir} 失败: {e}")
    return installed


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


def collect_handlers(plugin_dir: Path, module_name: str, session: Any | None = None) -> list[Any]:
    """收集 handlers.py: 优先 build_handlers(session) 工厂 (会话依赖注入);
    其次导出 handlers 列表; 否则按 4 个约定函数构建处理器"""
    mod = _load_module(plugin_dir / "handlers.py", f"{module_name}.handlers")
    if mod is None:
        return []
    builder = getattr(mod, "build_handlers", None)
    if callable(builder) and session is not None:
        built = builder(session)
        if isinstance(built, (list, tuple)):
            return list(cast(list[Any], built))
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
    commands: dict[str, bool] = field(default_factory=dict[str, bool])
    _session: Any = field(default=None, repr=False, compare=False)
    _cleanup: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    """卸载清理回调 (插件 state.py/hooks.py 的 cleanup(session) 约定)"""
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

    def enable_command(self, name: str) -> bool:
        """独立启用插件内命令"""
        if name not in self.commands:
            return False
        self.commands[name] = True
        if self.enabled:
            self._session.enable_command(name)
        return True

    def disable_command(self, name: str) -> bool:
        """独立停用插件内命令"""
        if name not in self.commands:
            return False
        self.commands[name] = False
        if self.enabled:
            self._session.disable_command(name)
        return True

    # ---------------- 状态查看 ----------------

    def list_capabilities(self) -> dict[str, list[dict[str, Any]]]:
        """列出插件内全部能力及其实效状态 (含聚合开关)"""
        return {
            "tools": [{"name": n, "enabled": self.enabled and s} for n, s in self.tools.items()],
            "skills": [{"name": n, "enabled": self.enabled and s} for n, s in self.skills.items()],
            "mcp": [{"name": n, "enabled": self.enabled and s} for n, s in self.mcp.items()],
            "handlers": [{"name": n, "enabled": self.enabled and s} for n, s in self.handlers.items()],
            "commands": [{"name": n, "enabled": self.enabled and s} for n, s in self.commands.items()],
        }
