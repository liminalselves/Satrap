"""
edictum 目录插件: 工具 + skill + MCP + 命令 + 处理脚本的组合包

插件目录结构:
    插件名/
    |-- meta.yaml     # name(必填) / version / author / repo / description
    |                 # 及可选能力组成描述: tools/skills/handlers/commands/mcp (名字 -> 描述)
    |-- tools.py      # 可选: Tool 子类 (同步版) / AsyncTool 子类 (异步版), 或 get_tools(session) 工厂
    |-- skills.py     # 可选: 导出 skills: list[Skill]; 或 skills/ 子目录 (skill.md 文件夹式)
    |-- mcp.py        # 可选: 导出 clients: dict[str, MCPClient] 或 build_clients() (同步/异步均支持)
    |-- commands.py   # 可选: 导出 commands (同步) / async_commands (异步) 字典, 或 cmd_* / cmd_*_async 约定
    |-- handlers.py   # 可选: 导出 handlers: list[SessionHandler]; 或 4 个约定函数
    +-- ...           # 插件私有模块

meta.yaml 能力声明:
- 五类能力 (tools/skills/handlers/commands/mcp) 可声明 名字 -> 描述 字典, 供前端/文档展示
- 声明仅作描述补充, 自动扫描 (collect_*) 仍是注册的真相源; 声明了但扫描不到仅警告, 不改变安装行为

插件目录扫描:
- 官方预设目录 satrap/expend/plugins (只读基线)
- 用户插件目录 .satrap/plugins (用户自添加, 同名冲突时官方优先)

tools.py 工厂约定 (解决会话依赖注入):
- 优先 get_tools(session) (带会话实例), 签名不匹配时降级 get_tools()
- 无 get_tools 时收集模块内定义的 base 子类 (无参构造)

双层状态模型:
- 插件.enabled (聚合开关) x 能力独立状态 (tools/skills/mcp/handlers/commands 字典值)
- 能力生效 = 插件启用 AND 独立启用
- disable_plugin 只压制生效 (批量关闭 manager), 不改独立状态
- enable_plugin 按独立状态恢复; 独立启停请走插件命名空间接口 (plugin.disable_tool 等)
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
import threading
from pathlib import Path
import inspect
from typing import TYPE_CHECKING, Any, Callable, Protocol, TypeVar, cast
from types import ModuleType
import yaml
import sys

from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.utils.skills import Skill
from satrap.core.type import safe_getattr_callable, safe_getattr_str, safe_getattr_list, safe_getattr_dict

from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession
    from satrap.edictum.simple_session import SessionHandler

    SessionType = SimpleSession | AsyncSimpleSession


T = TypeVar("T")


@dataclass(frozen=True)
class _ModuleIndexEntry:
    """记录已加载模块及其规范化源路径"""

    module: ModuleType
    resolved_path: Path | None


_module_index_lock = threading.RLock()
_module_index_by_name: dict[str, _ModuleIndexEntry] = {}
_module_index_by_path: dict[Path, ModuleType] = {}


def _resolve_module_source(module: ModuleType) -> Path | None:
    """
    解析已加载模块的规范化源路径

    参数:
    - module: 已加载模块

    返回:
    - 模块源路径; 无来源文件或路径无法解析时返回 None
    """
    module_file = safe_getattr_str(module, "__file__")
    if not module_file:
        return None
    try:
        return Path(module_file).resolve()
    except (OSError, RuntimeError):
        return None


def _refresh_module_index() -> None:
    """增量解析新增或被替换的模块, 并按当前加载顺序重建路径索引"""
    loaded_modules = dict(sys.modules)
    stale_names = _module_index_by_name.keys() - loaded_modules.keys()
    for module_name in stale_names:
        _module_index_by_name.pop(module_name, None)

    for module_name, module in loaded_modules.items():
        indexed = _module_index_by_name.get(module_name)
        if indexed is not None and indexed.module is module:
            continue
        _module_index_by_name[module_name] = _ModuleIndexEntry(
            module=module,
            resolved_path=_resolve_module_source(module),
        )

    _module_index_by_path.clear()
    for module_name in loaded_modules:
        indexed = _module_index_by_name[module_name]
        if indexed.resolved_path is not None:
            _module_index_by_path.setdefault(indexed.resolved_path, indexed.module)


def _record_loaded_module(
    module_name: str,
    module: ModuleType,
    resolved_path: Path,
) -> None:
    """
    记录由插件加载器成功执行的模块

    参数:
    - module_name: 模块名
    - module: 已执行完成的模块
    - resolved_path: 模块的规范化源路径
    """
    _module_index_by_name[module_name] = _ModuleIndexEntry(
        module=module,
        resolved_path=resolved_path,
    )
    _module_index_by_path[resolved_path] = module


class _YamlLoader(Protocol):
    """声明插件加载只依赖的 YAML 解析接口"""

    def safe_load(self, stream: object) -> object:
        """
        解析 YAML 文本流

        参数:
        - stream: YAML 文本流

        返回:
        - 解析后的动态结构
        """
        ...


_yaml_loader = cast(_YamlLoader, yaml)

PLUGINS_PRESET_DIR = Path(__file__).resolve().parents[1] / "expend" / "plugins"
"""官方预设插件目录 (satrap/expend/plugins), 只读基线"""

USER_PLUGINS_DIR = Path(".satrap") / "plugins"
"""用户插件目录 (相对工作目录), 用户自添加插件"""

def load_plugin_meta(plugin_dir: Path) -> dict[str, Any]:
    """
    解析插件 meta.yaml, 校验存在且为字典

    参数:
    - plugin_dir: 插件目录

    返回:
    - dict[str, Any]: 解析插件 meta.yaml, 校验存在且为字典
    """
    meta_path = plugin_dir / "meta.yaml"
    if not meta_path.is_file():
        raise ValueError(f"插件目录缺少 meta.yaml: {plugin_dir}")
    with open(meta_path, encoding="utf-8") as f:
        raw = _yaml_loader.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"插件 meta.yaml 格式错误: {meta_path}")
    return {str(k): v for k, v in cast(dict[str, Any], raw).items()}


CAPABILITY_KINDS = ("tools", "skills", "handlers", "commands", "mcp")
# 可声明描述的能力类别 (与 Plugin 的 5 类能力字典对齐)


def parse_capability_descriptions(meta: dict[str, Any]) -> dict[str, dict[str, str]]:
    """
    从 meta.yaml 解析五类能力描述 (名字 -> 描述)

    参数:
    - meta: 元数据

    声明仅作描述补充 (供前端/文档展示), 自动扫描仍是注册的真相源;
    非字典的类别键跳过并警告, 其余未识别键忽略

    返回:
    - dict[str, dict[str, str]]: 从 meta.yaml 解析五类能力描述 (名字 -> 描述)
    """
    descriptions: dict[str, dict[str, str]] = {}
    for kind in CAPABILITY_KINDS:
        raw = meta.get(kind)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            logger.warning(f"[插件] meta.yaml 的 {kind} 应为字典 (名字 -> 描述), 已跳过: {raw!r}")
            continue
        descriptions[kind] = {str(k): str(v) for k, v in cast(dict[Any, Any], raw).items()}
    return descriptions


def _load_module(path: Path, module_name: str) -> ModuleType | None:
    """
    动态加载插件模块 (文件不存在返回 None)

    参数:
    - path: 路径
    - module_name: module名称

    返回:
    - 已加载模块; 文件不存在时返回 None

    若模块名已在 sys.modules 且来源路径一致 (如官方插件在包内), 复用已加载模块,
    避免同一文件被加载两次导致模块级状态 (如工具引用的 WORKSPACE_ROOT) 分裂
    """
    if not path.is_file():
        return None
    resolved_path = path.resolve()
    with _module_index_lock:
        _refresh_module_index()

        existing = _module_index_by_name.get(module_name)
        if existing is not None and existing.resolved_path == resolved_path:
            return existing.module

        indexed_module = _module_index_by_path.get(resolved_path)
        if indexed_module is not None:
            return indexed_module

        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ValueError(f"无法加载插件模块: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[module_name] = module
        _record_loaded_module(module_name, module, resolved_path)
        return module


def collect_cleanup(
    plugin_dir: Path,
    module_name: str,
    session: SessionType | None = None,
) -> Callable[..., Any] | None:
    """
    收集插件卸载清理回调 (可选约定): state.py / hooks.py 导出的 cleanup(session)

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称
    - session: 会话

    兼容旧命名 reset_plugin_state(session); 卸载时调用以回收插件级共享状态

    返回:
    - Callable[..., Any] | None: 收集插件卸载清理回调 (可选约定): state.py / hooks.py 导出的 cleanup(session)
    """
    for sub in ("state", "hooks"):
        mod = _load_module(plugin_dir / f"{sub}.py", f"{module_name}.{sub}")
        if mod is None:
            continue
        fn = safe_getattr_callable(mod, "cleanup")
        if fn is None:
            fn = safe_getattr_callable(mod, "reset_plugin_state")
        if fn is not None:
            return fn
    return None


def collect_tools(
    plugin_dir: Path,
    module_name: str,
    base: type[T],
    session: SessionType | None = None,
    config: dict[str, Any] | None = None,
) -> list[T]:
    """
    收集 tools.py 中的工具实例

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称
    - base: 基础
    - session: 会话
    - config: 配置信息

    优先 get_tools 工厂 (解决会话/配置依赖注入), 按签名自适应降级:
    get_tools(session, config) -> get_tools(session) -> get_tools();
    无工厂时收集模块内定义的 base 子类实例 (无参构造, 排除基类本身)

    返回:
    - list[T]: 收集 tools.py 中的工具实例
    """
    mod = _load_module(plugin_dir / "tools.py", f"{module_name}.tools")
    if mod is None:
        return []
    factory = safe_getattr_callable(mod, "get_tools")
    if factory is not None:
        attempts: list[tuple[Any, ...]] = []
        if session is not None:
            attempts.append((session, config or {}))
            attempts.append((session,))
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
        obj = getattr(mod, attr_name)   # dir() 返回的属性名必定存在, 无需 safe_getattr
        if isinstance(obj, type) and issubclass(obj, base) and obj is not base:
            found.append(cast(T, obj()))
    return found


def collect_commands(
    plugin_dir: Path,
    module_name: str,
    session: SessionType | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:
    """
    收集 commands.py 的命令映射, 返回 (同步命令, 异步命令)

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称
    - session: 会话
    - config: 会话级插件配置

    优先 build_commands(session, config) 工厂, 并兼容 build_commands(session);
    工厂返回 (同步映射, 异步映射) 二元组或同步映射, 解决会话和配置依赖注入;
    其次导出 commands (同步) / async_commands (异步) 字典;
    否则按约定收集: cmd_xxx 为同步命令, cmd_xxx_async 为异步命令

    返回:
    - tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:  (同步命令, 异步命令)
    """
    mod = _load_module(plugin_dir / "commands.py", f"{module_name}.commands")
    if mod is None:
        return {}, {}
    builder = safe_getattr_callable(mod, "build_commands")
    if builder is not None and session is not None:
        built: Any = None
        for args in ((session, config or {}), (session,)):
            try:
                built = builder(*args)
                break
            except TypeError:
                continue
        if isinstance(built, tuple):
            pair = cast(tuple[Any, ...], built)
            if len(pair) == 2:
                return cast(dict[str, Callable[..., Any]], pair[0]), cast(dict[str, Callable[..., Any]], pair[1])
        if isinstance(built, dict):
            return cast(dict[str, Callable[..., Any]], built), {}
    declared = safe_getattr_dict(mod, "commands")
    if declared:
        sync_map: dict[str, Callable[..., Any]] = {}
        for k, v in declared.items():
            if callable(v):
                sync_map[str(k)] = cast(Callable[..., Any], v)
        async_declared = safe_getattr_dict(mod, "async_commands")
        async_map: dict[str, Callable[..., Any]] = {}
        if async_declared:
            for k, v in async_declared.items():
                if callable(v):
                    async_map[str(k)] = cast(Callable[..., Any], v)
        return sync_map, async_map
    sync_map, async_map = {}, {}
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)   # dir() 返回的属性名必定存在, 无需 safe_getattr
        if not callable(obj) or not attr_name.startswith("cmd_"):
            continue
        if inspect.iscoroutinefunction(obj):
            base = attr_name[4:-6] if attr_name.endswith("_async") else attr_name[4:]
            async_map[base] = obj
        else:
            sync_map[attr_name[4:]] = obj
    return sync_map, async_map


def scan_plugin_dirs(plugins_dir: str | Path | None = None) -> list[Path]:
    """
    扫描插件目录, 返回全部含合法 meta.yaml 的插件目录

    参数:
    - plugins_dir: plugins目录

    官方预设目录在前, 用户目录在后; 同名插件冲突时官方优先 (用户同名被跳过)

    返回:
    - list[Path]: 全部含合法 meta.yaml 的插件目录
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


def resolve_plugin_dir(name: str, plugins_dir: str | Path | None = None) -> Path | None:
    """
    按插件元数据名称解析插件目录

    参数:
    - name: 插件名称
    - plugins_dir: 可选用户插件目录

    返回:
    - Path | None: 插件不存在时返回 None
    """
    target = name.strip()
    if not target:
        return None
    for plugin_dir in scan_plugin_dirs(plugins_dir):
        try:
            meta = load_plugin_meta(plugin_dir)
        except Exception:
            continue
        if str(meta.get("name") or "").strip() == target:
            return plugin_dir
    return None


def install_all_plugins(session: SimpleSession, plugins_dir: str | Path | None = None) -> list[Plugin]:
    """
    扫描并安装全部可用插件到同步会话 (单个失败不影响其余), 返回安装的 Plugin 列表

    参数:
    - session: 会话
    - plugins_dir: plugins目录

    返回:
    - list[Plugin]: 安装的 Plugin 列表
    """
    installed: list[Plugin] = []
    for plugin_dir in scan_plugin_dirs(plugins_dir):
        try:
            installed.append(session.install_plugin(str(plugin_dir)))
        except Exception as e:
            logger.error(f"[插件] 安装插件 {plugin_dir} 失败: {e}")
    return installed


async def install_all_plugins_async(session: AsyncSimpleSession, plugins_dir: str | Path | None = None) -> list[Plugin]:
    """
    扫描并安装全部可用插件到异步会话 (单个失败不影响其余), 返回安装的 Plugin 列表

    参数:
    - session: 会话
    - plugins_dir: plugins目录

    返回:
    - list[Plugin]: 安装的 Plugin 列表
    """
    installed: list[Plugin] = []
    for plugin_dir in scan_plugin_dirs(plugins_dir):
        try:
            installed.append(await session.install_plugin(str(plugin_dir)))
        except Exception as e:
            logger.error(f"[插件] 安装插件 {plugin_dir} 失败: {e}")
    return installed


def collect_skills(plugin_dir: Path, module_name: str) -> list[Skill]:
    """
    收集 skills/ 子目录 (skill.md 文件夹式) 与 skills.py 导出的 skills 列表

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称

    返回:
    - list[Skill]: 收集 skills/ 子目录 (skill.md 文件夹式) 与 skills.py 导出的 skills 列表
    """
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
        declared = safe_getattr_list(mod, "skills")
        if declared:
            found.extend(declared)
    return found


def collect_mcp_clients(plugin_dir: Path, module_name: str) -> dict[str, Any]:
    """
    收集 mcp.py 的客户端映射: 导出 clients: dict 或 build_clients() 工厂

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称

    返回:
    - dict[str, Any]: 收集 mcp.py 的客户端映射: 导出 clients: dict 或 build_clients() 工厂
    """
    mod = _load_module(plugin_dir / "mcp.py", f"{module_name}.mcp")
    if mod is None:
        return {}
    clients = safe_getattr_dict(mod, "clients")
    if clients:
        return {str(k): v for k, v in clients.items()}
    builder = safe_getattr_callable(mod, "build_clients")
    if builder is not None:
        built = builder()
        if isinstance(built, dict):
            return {str(k): v for k, v in cast(dict[str, Any], built).items()}
    return {}


def collect_handlers(
    plugin_dir: Path,
    module_name: str,
    session: SessionType | None = None,
    handler_class: type[SessionHandler] | None = None,
) -> list[SessionHandler]:
    """
    收集 handlers.py: 优先 build_handlers(session) 工厂 (会话依赖注入);
    其次导出 handlers 列表; 否则按 4 个约定函数构建处理器

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称
    - session: 会话
    - handler_class: 处理器类, 由会话模块注入以规避循环依赖

    返回:
    - list[SessionHandler]: 收集 handlers.py: 优先 build_handlers(session) 工厂 (会话依赖注入)
    """
    mod = _load_module(plugin_dir / "handlers.py", f"{module_name}.handlers")
    if mod is None:
        return []
    builder = safe_getattr_callable(mod, "build_handlers")
    if builder is not None and session is not None:
        built = builder(session)
        if isinstance(built, (list, tuple)):
            return list(cast("list[SessionHandler]", built))
    declared = safe_getattr_list(mod, "handlers")
    if declared:
        return list(cast("list[SessionHandler]", declared))

    funcs: dict[str, Any] = {}
    for key in ("before_user_send", "after_user_send", "before_model_reply", "after_model_reply"):
        fn = safe_getattr_callable(mod, key)
        if fn is not None:
            funcs[key] = fn
    if not funcs:
        return []
    if handler_class is None:
        raise ValueError("按约定函数构建处理器时必须提供 handler_class")
    return [handler_class(name=f"{module_name}.handlers", **funcs)]


@dataclass
class Plugin:
    """
    已安装的目录插件: 元信息 + 能力清单 + 双层启停状态

    - tools/skills/mcp/handlers: 能力名 -> 独立启用状态 (True=独立启用)
    - enabled: 插件聚合开关; 能力生效 = enabled AND 独立状态
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
    capability_descriptions: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    """五类能力描述 (meta.yaml 声明): kind(tools/skills/handlers/commands/mcp) -> {能力名: 描述}"""
    config_schema: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    """配置项声明 (meta.yaml config_schema): 键 -> {type/default/description/options}, 供前端渲染表单"""
    _session: SessionType | None = field(default=None, repr=False, compare=False)
    _cleanup: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    """卸载清理回调 (插件 state.py/hooks.py 的 cleanup(session) 约定)"""
    _mcp_clients: dict[str, tuple[Any, list[Any]]] = field(
        default_factory=dict[str, tuple[Any, list[Any]]], repr=False, compare=False,
    )

    # ---------- 命名空间独立启停 (推荐用法) ----------

    def enable_tool(self, name: str) -> bool:
        """
        独立启用插件内工具 (插件停用期间只改状态, 不生效)

        参数:
        - name: 名称

        返回:
        - bool: 独立启用插件内工具 (插件停用期间只改状态, 不生效)
        """
        wf = self._session._wf if self._session is not None else None
        if wf is None:
            logger.warning("[插件] 插件未绑定会话或工作流未初始化, 启用工具忽略")
            return False
        if name not in self.tools:
            return False
        self.tools[name] = True
        if self.enabled:
            wf.tools_manager.enable_tool(name)
        return True

    def disable_tool(self, name: str) -> bool:
        """
        独立停用插件内工具

        参数:
        - name: 名称

        返回:
        - bool: 独立停用插件内工具
        """
        wf = self._session._wf if self._session is not None else None
        if wf is None:
            logger.warning("[插件] 插件未绑定会话或工作流未初始化, 停用工具忽略")
            return False
        if name not in self.tools:
            return False
        self.tools[name] = False
        if self.enabled:
            wf.tools_manager.disable_tool(name)
        return True

    def enable_skill(self, name: str) -> Any:
        """
        独立启用插件内技能

        参数:
        - name: 名称

        返回: 同步版为 bool; 异步版为 coroutine (await 后为 bool)

        返回:
        - Any: 独立启用插件内技能
        """
        session = self._session
        if session is None:
            logger.warning("[插件] 插件未绑定会话, 启用技能忽略")
            return False
        if name not in self.skills:
            return False
        self.skills[name] = True
        if self.enabled:
            return session._activate_plugin_skill(name)
        return True

    def disable_skill(self, name: str) -> Any:
        """
        独立停用插件内技能 (异步版返回 coroutine, await 后为 bool)

        参数:
        - name: 名称

        返回:
        - Any:  coroutine, await 后为 bool)
        """
        session = self._session
        if session is None:
            logger.warning("[插件] 插件未绑定会话, 停用技能忽略")
            return False
        if name not in self.skills:
            return False
        self.skills[name] = False
        if self.enabled:
            return session._deactivate_plugin_skill(name)
        return True

    def enable_mcp(self, name: str) -> bool:
        """
        独立启用插件内 MCP 连接的全部工具

        参数:
        - name: 名称

        返回:
        - bool: 独立启用插件内 MCP 连接的全部工具
        """
        wf = self._session._wf if self._session is not None else None
        if wf is None:
            logger.warning("[插件] 插件未绑定会话或工作流未初始化, 启用 MCP 忽略")
            return False
        if name not in self.mcp:
            return False
        self.mcp[name] = True
        if self.enabled:
            for adapter in self._mcp_clients.get(name, (None, []))[1]:
                wf.tools_manager.enable_tool(adapter.get_tool_name())
        return True

    def disable_mcp(self, name: str) -> bool:
        """
        独立停用插件内 MCP 连接的全部工具

        参数:
        - name: 名称

        返回:
        - bool: 独立停用插件内 MCP 连接的全部工具
        """
        wf = self._session._wf if self._session is not None else None
        if wf is None:
            logger.warning("[插件] 插件未绑定会话或工作流未初始化, 停用 MCP 忽略")
            return False
        if name not in self.mcp:
            return False
        self.mcp[name] = False
        if self.enabled:
            for adapter in self._mcp_clients.get(name, (None, []))[1]:
                wf.tools_manager.disable_tool(adapter.get_tool_name())
        return True

    def enable_handler(self, name: str) -> bool:
        """
        独立启用插件内处理器 (独立位委托会话, 无条件改; 聚合开关由执行路径合成)

        参数:
        - name: 名称

        返回:
        - bool: 独立启用插件内处理器 (独立位委托会话, 无条件改; 聚合开关由执行路径合成)
        """
        session = self._session
        if session is None:
            logger.warning("[插件] 插件未绑定会话, 启用处理器忽略")
            return False
        if name not in self.handlers:
            return False
        return session.enable_handler(name)

    def disable_handler(self, name: str) -> bool:
        """
        独立停用插件内处理器 (独立位委托会话)

        参数:
        - name: 名称

        返回:
        - bool: 独立停用插件内处理器 (独立位委托会话)
        """
        session = self._session
        if session is None:
            logger.warning("[插件] 插件未绑定会话, 停用处理器忽略")
            return False
        if name not in self.handlers:
            return False
        return session.disable_handler(name)

    def enable_command(self, name: str) -> bool:
        """
        独立启用插件内命令

        参数:
        - name: 名称

        返回:
        - bool: 独立启用插件内命令
        """
        session = self._session
        if session is None:
            logger.warning("[插件] 插件未绑定会话, 启用命令忽略")
            return False
        if name not in self.commands:
            return False
        self.commands[name] = True
        if self.enabled:
            session.enable_command(name)
        return True

    def disable_command(self, name: str) -> bool:
        """
        独立停用插件内命令

        参数:
        - name: 名称

        返回:
        - bool: 独立停用插件内命令
        """
        session = self._session
        if session is None:
            logger.warning("[插件] 插件未绑定会话, 停用命令忽略")
            return False
        if name not in self.commands:
            return False
        self.commands[name] = False
        if self.enabled:
            session.disable_command(name)
        return True

    # ---------- 状态查看 ----------

    def list_capabilities(self) -> dict[str, list[dict[str, str | bool]]]:
        """
        列出插件内全部能力及其实效状态 (含聚合开关), 每项带 meta.yaml 声明的描述

        handlers 状态读会话侧独立位合成值 (唯一真相源: handler.enabled),
        与执行路径一致; 会话/处理器缺失时防御为 False

        返回:
        - dict[str, list[dict[str, str | bool]]]: 列出插件内全部能力及其实效状态 (含聚合开关), 每项带 meta.yaml 声明的描述
        """
        session = self._session
        handler_effective: dict[str, bool] = {}
        if session is not None:
            for n in self.handlers:
                h = session._handlers.get(n)
                handler_effective[n] = h is not None and h.enabled

        def _items(kind: str, states: dict[str, bool], effective: dict[str, bool] | None = None) -> list[dict[str, str | bool]]:
            descriptions = self.capability_descriptions.get(kind, {})
            return [
                {
                    "name": n,
                    "enabled": self.enabled and (effective.get(n, False) if effective is not None else s),
                    "description": descriptions.get(n, ""),
                }
                for n, s in states.items()
            ]

        return {
            "tools": _items("tools", self.tools),
            "skills": _items("skills", self.skills),
            "mcp": _items("mcp", self.mcp),
            "handlers": _items("handlers", self.handlers, handler_effective),
            "commands": _items("commands", self.commands),
        }
