"""
edictum 目录插件: 工具 + 服务 + skill + MCP + 命令 + 处理脚本的组合包

插件目录结构:
    插件名/
    |-- meta.yaml     # name(必填) / version / author / repo / description
    |                 # 及可选能力组成描述: tools/services/skills/handlers/commands/mcp (名字 -> 描述)
    |-- tools.py      # 可选: Tool 子类 (同步版) / AsyncTool 子类 (异步版), 或 get_tools 工厂
    |-- services.py   # 可选: Service / AsyncService 子类, 或显式注入 plugin_host 的 get_services 工厂
    |-- skills.py     # 可选: 导出 skills: list[Skill]; 或 skills/ 子目录 (skill.md 文件夹式)
    |-- mcp.py        # 可选: 导出 clients: dict[str, MCPClient] 或 build_clients() (同步/异步均支持)
    |-- commands.py   # 可选: 导出 commands (同步) / async_commands (异步) 字典, 或 cmd_* / cmd_*_async 约定
    |-- handlers.py   # 可选: 导出 handlers: list[SessionHandler]; 或 4 个约定函数
    +-- ...           # 插件私有模块

meta.yaml 能力声明:
- 六类能力 (tools/services/skills/handlers/commands/mcp) 可声明 名字 -> 描述 字典, 供前端/文档展示
- 声明仅作描述补充, 自动扫描 (collect_*) 仍是注册的真相源; 声明了但扫描不到仅警告, 不改变安装行为

插件目录扫描:
- 官方预设目录 satrap/expend/plugins (只读基线)
- 用户插件目录 .satrap/plugins (用户自添加, 同名冲突时官方优先)

tools.py 工厂约定 (解决会话依赖注入):
- 统一签名 get_tools(session, config=None, resources=None); 会话和配置都可为空
- 兼容旧签名: 按签名自适应降级 get_tools(session, config) -> get_tools(session) -> get_tools()
- 无 get_tools 时收集模块内定义的 base 子类 (无参构造)

双层状态模型:
- 插件.enabled (聚合开关) x 能力独立状态 (tools/services/skills/mcp/handlers/commands 字典值)
- 能力生效 = 插件启用 AND 独立启用
- disable_plugin 只压制生效 (批量关闭 manager), 不改独立状态
- enable_plugin 按独立状态恢复; 独立启停请走插件命名空间接口 (plugin.disable_tool 等)
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
import threading
import inspect
import asyncio
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Protocol,
    TypeVar,
    cast,
)
from types import ModuleType
import yaml
import sys

from satrap.core.plugin_interop.contracts import CAPABILITY_KINDS, CapabilityKind, InteropSpec
from satrap.core.plugin_interop.capabilities import PluginHost
from satrap.core.plugin_interop.service import AsyncService, Service
from satrap.core.utils.skills import Skill
from satrap.core.type import (
    safe_getattr_callable,
    safe_getattr_str,
    safe_getattr_list,
    safe_getattr_dict,
)

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




def parse_capability_descriptions(meta: dict[str, Any]) -> dict[str, dict[str, str]]:
    """
    从 meta.yaml 解析六类能力描述 (名字 -> 描述)

    参数:
    - meta: 元数据

    声明仅作描述补充 (供前端/文档展示), 自动扫描仍是注册的真相源;
    非字典的类别键跳过并警告, 其余未识别键忽略

    返回:
    - dict[str, dict[str, str]]: 从 meta.yaml 解析六类能力描述 (名字 -> 描述)
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


def _load_module(path: Path, module_name: str, *, fresh: bool = False) -> ModuleType | None:
    """
    动态加载插件模块或同名包 (入口不存在返回 None)

    参数:
    - path: 路径
    - module_name: module名称
    - fresh: 是否重新实例化模块, 默认 False 复用已加载模块

    返回:
    - 已加载模块; 文件不存在时返回 None

    fresh 为 False 时, 若模块名已在 sys.modules 且来源路径一致 (如官方插件在包内), 复用已加载模块,
    避免同一文件被加载两次导致模块级状态 (如工具引用的 WORKSPACE_ROOT) 分裂
    """
    if not path.is_file() and path.suffix == ".py":
        path = path.with_suffix("") / "__init__.py"
    if not path.is_file():
        return None
    resolved_path = path.resolve()
    with _module_index_lock:
        _refresh_module_index()

        existing = _module_index_by_name.get(module_name)
        if not fresh and existing is not None and existing.resolved_path == resolved_path:
            return existing.module

        indexed_module = _module_index_by_path.get(resolved_path)
        if not fresh and indexed_module is not None:
            return indexed_module

        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ValueError(f"无法加载插件模块: {path}")
        module = importlib.util.module_from_spec(spec)
        created_parents: list[str] = []
        if spec.submodule_search_locations is not None:
            parent_path = path.parent.parent
            parents: list[tuple[str, Path]] = []
            parts = module_name.split(".")
            for index in range(len(parts) - 1, 0, -1):
                parents.append((".".join(parts[:index]), parent_path))
                parent_path = parent_path.parent
            for parent_name, parent_path in reversed(parents):
                if parent_name not in sys.modules:
                    namespace = ModuleType(parent_name)
                    namespace.__path__ = [str(parent_path)]
                    namespace.__package__ = parent_name
                    sys.modules[parent_name] = namespace
                    created_parents.append(parent_name)
        previous = {
            name: loaded for name, loaded in dict(sys.modules).items()
            if name == module_name or name.startswith(module_name + ".")
        }
        if spec.submodule_search_locations is not None:
            for name in previous:
                sys.modules.pop(name, None)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            for name in tuple(sys.modules):
                if name == module_name or name.startswith(module_name + "."):
                    sys.modules.pop(name, None)
            sys.modules.update(previous)
            for parent_name in reversed(created_parents):
                sys.modules.pop(parent_name, None)
            raise
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


def _invoke_factory(
    factory: Callable[..., Any], session: Any, config: dict[str, Any] | None, resources: Any,
    *, plugin_host: PluginHost | None = None, no_args_first: bool = False,
) -> Any:
    """
    先绑定旧签名及显式宿主参数, 工厂内部 TypeError 不触发重试

    参数:
    - factory: 实际插件工厂, 不同能力返回类型在收集边界核验
    - session: 当前会话, None 时保持独立工厂的旧无参调用方式
    - config: 已解析配置, 默认空映射
    - resources: 已构造模型资源
    - plugin_host: 仅传给显式声明同名仅关键字参数的工厂
    - no_args_first: MCP 旧无参工厂优先, 默认 False

    返回:
    - 工厂原返回值; 签名不兼容或实现失败时抛出异常
    """
    signature = inspect.signature(factory)
    attempts: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
        ((session, config or {}), {"resources": resources}),
        ((session, config or {}, resources), {}),
        ((session, config or {}), {}),
        ((session,), {}),
        ((), {}),
    ] if session is not None else [((), {})]
    if no_args_first:
        attempts.insert(0, ((), {}))
    host_parameter = signature.parameters.get("plugin_host")
    inject_host = host_parameter is not None and host_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    for args, kwargs in attempts:
        if inject_host:
            kwargs = {**kwargs, "plugin_host": plugin_host}
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return factory(*args, **kwargs)
    raise TypeError(f"插件工厂参数不匹配: {factory.__name__}")


def collect_tools(
    plugin_dir: Path,
    module_name: str,
    base: type[T],
    session: SessionType | None = None,
    config: dict[str, Any] | None = None,
    resources: Any = None,
    *, plugin_host: PluginHost | None = None,
) -> list[T]:
    """
    收集 tools.py 中的工具实例

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称
    - base: 基础
    - session: 会话
    - config: 配置信息
    - resources: 模型资源, 默认 None
    - plugin_host: 显式仅关键字注入的宿主接口, 默认 None

    优先 get_tools 工厂 (解决会话/配置依赖注入), 统一签名为 get_tools(session, config, resources);
    旧签名按适应性降级: get_tools(session, config) -> get_tools(session) -> get_tools();
    无工厂时收集模块内定义的 base 子类实例 (无参构造, 排除基类本身)

    返回:
    - list[T]: 收集 tools.py 中的工具实例
    """
    mod = _load_module(plugin_dir / "tools.py", f"{module_name}.tools")
    if mod is None:
        return []
    factory = safe_getattr_callable(mod, "get_tools")
    if factory is not None:
        result = _invoke_factory(factory, session, config, resources, plugin_host=plugin_host)
        if result is None:
            return []
        items_list = cast(list[Any], result) if isinstance(result, (list, tuple)) else [result]
        return [cast(T, item) for item in items_list]
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
    resources: Any = None,
    *, plugin_host: PluginHost | None = None,
) -> tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:
    """
    收集 commands.py 的命令映射, 返回 (同步命令, 异步命令)

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称
    - session: 会话
    - config: 会话级插件配置
    - resources: 模型资源, 默认 None
    - plugin_host: 显式仅关键字注入的宿主接口, 默认 None

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
        built = _invoke_factory(builder, session, config, resources, plugin_host=plugin_host)
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
    mod = _load_module(plugin_dir / "skills.py", f"{module_name}.skills", fresh=True)
    # 技能含工具和连接实例, 每次安装独立创建, 避免旧会话清理新会话的资源
    if mod is not None:
        declared = safe_getattr_list(mod, "skills")
        if declared:
            found.extend(declared)
    return found


def collect_mcp_clients(
    plugin_dir: Path, module_name: str, session: SessionType | None = None,
    config: dict[str, Any] | None = None, resources: Any = None,
    *, plugin_host: PluginHost | None = None,
) -> dict[str, Any]:
    """
    收集 mcp.py 的客户端映射: 导出 clients: dict 或 build_clients() 工厂

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称
    - session: 当前会话, 默认 None 保持旧无参工厂调用
    - config: 有效配置, 默认 None
    - resources: 模型资源, 默认 None
    - plugin_host: 显式仅关键字注入的宿主接口, 默认 None

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
        built = _invoke_factory(builder, session, config, resources, plugin_host=plugin_host, no_args_first=True)
        if isinstance(built, dict):
            return {str(k): v for k, v in cast(dict[str, Any], built).items()}
    return {}


def collect_handlers(
    plugin_dir: Path,
    module_name: str,
    session: SessionType | None = None,
    handler_class: type[SessionHandler] | None = None,
    config: dict[str, Any] | None = None,
    resources: Any = None,
    *, plugin_host: PluginHost | None = None,
) -> list[SessionHandler]:
    """
    收集 handlers.py: 优先 build_handlers(session) 工厂 (会话依赖注入);
    其次导出 handlers 列表; 否则按 4 个约定函数构建处理器

    参数:
    - plugin_dir: 插件目录
    - module_name: module名称
    - session: 会话
    - handler_class: 处理器类, 由会话模块注入以规避循环依赖
    - config: 有效配置, 默认 None
    - resources: 模型资源, 默认 None
    - plugin_host: 显式仅关键字注入的宿主接口, 默认 None

    返回:
    - list[SessionHandler]: 收集 handlers.py: 优先 build_handlers(session) 工厂 (会话依赖注入)
    """
    mod = _load_module(plugin_dir / "handlers.py", f"{module_name}.handlers")
    if mod is None:
        return []
    builder = safe_getattr_callable(mod, "build_handlers")
    if builder is not None and session is not None:
        built = _invoke_factory(builder, session, config, resources, plugin_host=plugin_host)
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


def collect_services(
    plugin_dir: Path, module_name: str, base: type[Service] | type[AsyncService],
    session: SessionType | None = None, config: dict[str, Any] | None = None,
    resources: Any = None, *, plugin_host: PluginHost | None = None,
) -> dict[str, Service | AsyncService]:
    """
    按当前会话形态收集插件专用服务, 不创建模型工具

    参数:
    - plugin_dir: 插件目录
    - module_name: 插件实际名称
    - base: 当前会话接受的同步或异步服务基类
    - session: 当前会话, 独立收集时默认 None
    - config: 插件有效配置, 默认 None
    - resources: 模型资源, 默认 None
    - plugin_host: 工厂显式接收的宿主接口, 默认 None

    返回:
    - 服务名到实例的内部映射; 名称重复或实现无效时抛出异常
    """
    module = _load_module(plugin_dir / "services.py", f"{module_name}.services")
    if module is None:
        return {}
    factory = safe_getattr_callable(module, "get_services")
    if factory is not None:
        value: object = _invoke_factory(factory, session, config, resources, plugin_host=plugin_host)
        if not isinstance(value, list):
            raise ValueError("get_services 必须返回服务实例列表")
        items: list[object] = list(value)
    else:
        items = []
        seen: set[type] = set()
        for value in vars(module).values():
            if (isinstance(value, type) and issubclass(value, base) and value is not base
                    and value.__module__ == module.__name__ and value not in seen
                    and not inspect.isabstract(value)):
                inspect.signature(value).bind()
                items.append(value())
                seen.add(value)
    result: dict[str, Service | AsyncService] = {}
    for item in items:
        if not isinstance(item, base):
            raise ValueError("服务实现与当前会话的同步/异步形态不匹配")
        name = item.service_name
        if not isinstance(name, str) or not name.strip() or name != name.strip() or not item.description:
            raise ValueError("服务需要非空 service_name 和 description")
        if name in result:
            raise ValueError(f"插件重复注册服务: {module_name}.{name}")
        if inspect.iscoroutinefunction(item.execute) != (base is AsyncService):
            raise ValueError(f"服务 execute 的同步/异步形态不匹配: {name}")
        result[name] = item
    return result


@dataclass
class Plugin:
    """
    已安装的目录插件: 元信息 + 能力清单 + 双层启停状态

    - tools/services/skills/mcp/handlers/commands: 能力名 -> 独立启用状态 (True=独立启用)
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
    services: dict[str, bool] = field(default_factory=dict[str, bool])
    interop: InteropSpec = field(default_factory=InteropSpec, repr=False)
    _services: dict[str, Service | AsyncService] = field(default_factory=dict, repr=False, compare=False)
    _metadata: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    capability_descriptions: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    """六类能力描述 (meta.yaml 声明): kind(tools/services/skills/handlers/commands/mcp) -> {能力名: 描述}"""
    config_schema: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    resources: Any = field(default=None, repr=False)
    recovery_fingerprint: str = ""
    """配置项声明 (meta.yaml config_schema): 键 -> {type/default/description/options}, 供前端渲染表单"""
    _session: SessionType | None = field(default=None, repr=False, compare=False)
    _cleanup: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    """卸载清理回调 (插件 state.py/hooks.py 的 cleanup(session) 约定)"""
    _mcp_clients: dict[str, tuple[Any, list[Any]]] = field(
        default_factory=dict[str, tuple[Any, list[Any]]], repr=False, compare=False,
    )

    def _check_capability_change(self, kind: CapabilityKind, name: str, enabled: bool) -> None:
        """
        提交独立开关前核验关系, 失败时保持原运行状态

        参数:
        - kind: 实际能力类别
        - name: 实际能力名
        - enabled: 目标独立状态
        """
        if self._session is not None:
            runtime = self._session._plugin_interop_runtime
            if runtime is not None:
                runtime.check_change(self.name, kind=kind, name=name, enabled=enabled)
                if not enabled and getattr(self, kind).get(name, False):
                    runtime.revoke_references(self.name)

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
        self._check_capability_change("tools", name, True)
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
        self._check_capability_change("tools", name, False)
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
        return self._set_skill_enabled(name, True)

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
        return self._set_skill_enabled(name, False)

    def _set_skill_enabled(self, name: str, enabled: bool) -> bool | Awaitable[bool]:
        """
        应用技能独立状态, 资源装配失败时恢复原开关和有效技能

        参数:
        - name: 已确认属于本插件的技能名
        - enabled: 目标独立开关

        返回:
        - 同步会话返回 bool, 异步会话返回等待同一装配过程的协程
        """
        from satrap.edictum.simple_session.async_ import AsyncSimpleSession

        session = self._session
        if session is None:
            return False
        self._check_capability_change("skills", name, enabled)
        previous = self.skills[name]
        self.skills[name] = enabled
        if not self.enabled:
            return True
        runtime = session._plugin_interop
        if isinstance(session, AsyncSimpleSession):
            async def apply() -> bool:
                """
                等待异步资源装配, 失败或取消后恢复旧状态

                返回:
                - 技能状态装配成功时返回 True
                """
                try:
                    await runtime.synchronize_skills_async()
                    return True
                except (Exception, asyncio.CancelledError):
                    self.skills[name] = previous
                    await runtime.synchronize_skills_async()
                    raise
            return apply()
        try:
            runtime.synchronize_skills()
            return True
        except Exception:
            self.skills[name] = previous
            runtime.synchronize_skills()
            raise

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
        self._check_capability_change("mcp", name, True)
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
        self._check_capability_change("mcp", name, False)
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
        self._check_capability_change("handlers", name, True)
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
        self._check_capability_change("handlers", name, False)
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
        self._check_capability_change("commands", name, True)
        self.commands[name] = True
        if self.enabled:
            session._plugin_interop.command_registry().enable_command(name)
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
        self._check_capability_change("commands", name, False)
        self.commands[name] = False
        if self.enabled:
            session._plugin_interop.command_registry().disable_command(name)
        return True

    def enable_service(self, name: str) -> bool:
        """
        启用插件专用服务独立开关

        参数:
        - name: 实际服务名

        返回:
        - 服务存在且已更新时为 True
        """
        service = self._services.get(name)
        if service is None:
            return False
        self._check_capability_change("services", name, True)
        self.services[name] = True
        service.enable()
        return True

    def disable_service(self, name: str) -> bool:
        """
        停用插件专用服务, 不修改导出声明

        参数:
        - name: 实际服务名

        返回:
        - 服务存在且已更新时为 True
        """
        service = self._services.get(name)
        if service is None:
            return False
        self._check_capability_change("services", name, False)
        self.services[name] = False
        service.disable()
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

        def _items(kind: CapabilityKind, states: dict[str, bool], effective: dict[str, bool] | None = None) -> list[dict[str, str | bool]]:
            """
            保留用户开关并单独展示关系合成结果

            参数:
            - kind: 当前能力类别
            - states: 已保存的独立开关
            - effective: 处理器的实际独立位, 默认使用 states

            返回:
            - 含期望状态, 实效原因与公开范围的能力资料
            """
            descriptions = self.capability_descriptions.get(kind, {})
            runtime = session._plugin_interop_runtime if session is not None else None
            result: list[dict[str, str | bool]] = []
            for name, state in states.items():
                requested = effective.get(name, False) if effective is not None else state
                enabled = self.enabled and requested
                reason, replacements = "", ()
                if runtime is not None:
                    enabled = enabled and runtime.capability_available(self.name, kind, name)
                    if not enabled:
                        reason, replacements = runtime.capability_reason(self.name, kind, name)
                elif not enabled:
                    reason = "plugin_disabled" if not self.enabled else "disabled"
                service = self._services.get(name) if kind == "services" else None
                result.append({
                    "name": name,
                    "enabled": enabled,
                    "requested_enabled": requested,
                    "exported": name in self.interop.exports.get(kind, ()),
                    "reason": reason,
                    "replaced_by": ", ".join(replacements),
                    "description": descriptions.get(name, service.description if service is not None else ""),
                })
            return result

        return {
            "tools": _items("tools", self.tools),
            "skills": _items("skills", self.skills),
            "mcp": _items("mcp", self.mcp),
            "handlers": _items("handlers", self.handlers, handler_effective),
            "commands": _items("commands", self.commands),
            "services": _items("services", self.services),
        }
