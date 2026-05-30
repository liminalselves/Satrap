from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Type

from satrap.core.framework.Base import AsyncSession, Session


DEFAULT_SESSION_SCAN_PATH = "satrap/sessions"
"""默认 Session 扫描目录"""


@dataclass
class DiscoveredSessionClass:
    """扫描到的 Session 类信息"""

    file_path: str
    module_name: str
    class_name: str
    class_path: str
    is_async: bool
    init_params: dict[str, Any]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为前端可消费的字典"""
        return asdict(self)


def normalize_scan_paths(paths: list[str] | tuple[str, ...] | None = None) -> list[Path]:
    """归一化扫描目录, 保留顺序并去重"""
    raw_paths = list(paths or [DEFAULT_SESSION_SCAN_PATH])
    result: list[Path] = []
    seen: set[str] = set()
    for item in raw_paths:
        text = str(item or "").strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved = path.resolve()
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


def ensure_session_scan_paths(paths: list[str] | tuple[str, ...] | None = None) -> list[Path]:
    """将扫描目录的导入根加入 sys.path"""
    normalized = normalize_scan_paths(paths)
    for path in normalized:
        import_root = _import_root_for_scan_path(path)
        text = str(import_root)
        if text not in sys.path:
            sys.path.insert(0, text)
    return normalized


def create_default_session_dir(paths: list[str] | tuple[str, ...] | None = None) -> Path:
    """创建默认 Session 扫描目录"""
    target = normalize_scan_paths(paths)[0]
    target.mkdir(parents=True, exist_ok=True)
    init_file = target / "__init__.py"
    if not init_file.exists():
        init_file.write_text('"""用户 Session 扫描目录"""\n', encoding="utf-8")
    return target


def discover_session_classes(paths: list[str] | tuple[str, ...] | None = None) -> list[DiscoveredSessionClass]:
    """扫描目录下的 Session/AsyncSession 子类"""
    scan_paths = ensure_session_scan_paths(paths)
    discovered: list[DiscoveredSessionClass] = []
    for scan_path in scan_paths:
        if not scan_path.exists():
            continue
        for file_path in sorted(scan_path.glob("*.py")):
            if _should_skip_file(file_path):
                continue
            module_name = _module_name_for_file(scan_path, file_path)
            try:
                module = importlib.import_module(module_name)
                module = importlib.reload(module)
            except Exception as e:
                discovered.append(
                    DiscoveredSessionClass(
                        file_path=str(file_path),
                        module_name=module_name,
                        class_name="",
                        class_path="",
                        is_async=False,
                        init_params={},
                        error=str(e),
                    )
                )
                continue

            for _, cls in inspect.getmembers(module, inspect.isclass):
                if not _is_declared_session_class(module_name, cls):
                    continue
                discovered.append(
                    DiscoveredSessionClass(
                        file_path=str(file_path),
                        module_name=module_name,
                        class_name=cls.__name__,
                        class_path=f"{cls.__module__}.{cls.__qualname__}",
                        is_async=issubclass(cls, AsyncSession),
                        init_params=_generate_template(_detect_params(cls)),
                    )
                )
    return discovered


def _should_skip_file(path: Path) -> bool:
    """判断是否跳过扫描文件"""
    name = path.name
    return name.startswith("_") or name.startswith(".") or path.parent.name == "__pycache__"


def _import_root_for_scan_path(scan_path: Path) -> Path:
    """推导应加入 sys.path 的导入根"""
    cwd = Path.cwd().resolve()
    try:
        scan_path.relative_to(cwd)
        return cwd
    except ValueError:
        return scan_path.parent


def _module_name_for_file(scan_path: Path, file_path: Path) -> str:
    """根据扫描目录和文件路径生成稳定模块名"""
    cwd = Path.cwd().resolve()
    try:
        rel = file_path.resolve().relative_to(cwd)
        return ".".join(rel.with_suffix("").parts)
    except ValueError:
        rel = file_path.resolve().relative_to(scan_path.parent.resolve())
        return ".".join(rel.with_suffix("").parts)


def _is_declared_session_class(module_name: str, cls: Type[Any]) -> bool:
    """判断类是否为当前模块声明的 Session 子类"""
    if cls in (Session, AsyncSession):
        return False
    if cls.__module__ != module_name:
        return False
    return issubclass(cls, (Session, AsyncSession))


def _detect_params(session_class: Type[Session] | Type[AsyncSession]) -> dict[str, inspect.Parameter]:
    """反射 __init__ 签名, 提取自定义参数"""
    exclude = {"self", "session_id", "content_callback", "command_handler", "session_config", "llm"}
    try:
        sig = inspect.signature(session_class.__init__)
    except Exception:
        return {}
    params: dict[str, inspect.Parameter] = {}
    for name, param in sig.parameters.items():
        if name in exclude:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        params[name] = param
    return params


def _generate_template(params_info: dict[str, inspect.Parameter]) -> dict[str, Any]:
    """根据类型注解生成占位值模板"""
    type_map: dict[str, Any] = {
        "str": "",
        "int": 0,
        "float": 0.0,
        "bool": False,
        "list": [],
        "dict": {},
    }
    template: dict[str, Any] = {}
    for name, param in params_info.items():
        ann = param.annotation
        if ann is inspect.Parameter.empty:
            template[name] = None
            continue
        ann_str = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
        template[name] = type_map.get(ann_str, None)
    return template
