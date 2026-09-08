from __future__ import annotations

import satrap.edictum.plugin as plugin_module
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator
import importlib.util
from pathlib import Path
import pytest
from types import ModuleType
import sys


@pytest.fixture
def loaded_module_names() -> Iterator[list[str]]:
    """
    记录测试动态加载的模块名并在测试后清理

    返回:
    - 供测试登记模块名的列表
    """
    module_names: list[str] = []
    yield module_names
    for module_name in module_names:
        sys.modules.pop(module_name, None)


def _write_module(path: Path, source: str = "VALUE = object()\n") -> None:
    """
    写入测试插件模块

    参数:
    - path: 模块路径
    - source: 模块源码
    """
    path.write_text(source, encoding="utf-8")


def test_load_package_supports_relative_imports_and_reuses_source(
    tmp_path: Path, loaded_module_names: list[str],
) -> None:
    """同名包可以相对导入子模块, 且别名加载复用同一对象"""
    package = tmp_path / "tools"
    package.mkdir()
    _write_module(package / "__init__.py", "from .worker import Result\nVALUE = Result('成功')\n")
    _write_module(package / "worker.py", "from dataclasses import dataclass\n@dataclass\nclass Result:\n    text: str\n")
    name = "_satrap_test_package.tools"
    loaded_module_names.extend([name, name + ".worker", "_satrap_test_package_alias.tools", "_satrap_test_package"])
    module = plugin_module._load_module(tmp_path / "tools.py", name)
    assert module is not None and module.VALUE.text == "成功"
    assert plugin_module._load_module(tmp_path / "tools.py", "_satrap_test_package_alias.tools") is module


def test_failed_package_replacement_restores_previous_modules(
    tmp_path: Path, loaded_module_names: list[str],
) -> None:
    """包加载失败时移除新子模块, 恢复之前的包及子模块"""
    first = tmp_path / "first" / "tools"
    second = tmp_path / "second" / "tools"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _write_module(first / "__init__.py", "from .worker import VALUE\n")
    _write_module(first / "worker.py", "VALUE = 'first'\n")
    _write_module(second / "__init__.py", "from .worker import VALUE\nfrom . import extra\nraise RuntimeError('加载失败')\n")
    _write_module(second / "worker.py", "VALUE = 'second'\n")
    _write_module(second / "extra.py", "VALUE = 'extra'\n")
    name = "_satrap_test_failed_package.tools"
    loaded_module_names.extend([name, name + ".worker", name + ".extra", "_satrap_test_failed_package"])
    previous = plugin_module._load_module(first.parent / "tools.py", name)
    previous_worker = sys.modules[name + ".worker"]
    with pytest.raises(RuntimeError, match="加载失败"):
        plugin_module._load_module(second.parent / "tools.py", name)
    assert sys.modules[name] is previous
    assert sys.modules[name + ".worker"] is previous_worker
    assert name + ".extra" not in sys.modules
    _write_module(second / "__init__.py", "from .worker import VALUE\n")
    replacement = plugin_module._load_module(second.parent / "tools.py", name)
    assert replacement is not None and replacement.VALUE == "second"
    assert sys.modules[name + ".worker"] is not previous_worker


def test_file_entry_keeps_precedence_over_package(
    tmp_path: Path, loaded_module_names: list[str],
) -> None:
    """文件与包同时存在时保持原文件入口优先"""
    package = tmp_path / "tools"
    package.mkdir()
    _write_module(package / "__init__.py", "VALUE = 'package'\n")
    _write_module(tmp_path / "tools.py", "VALUE = 'file'\n")
    name = "_satrap_test_file_precedence"
    loaded_module_names.append(name)
    module = plugin_module._load_module(tmp_path / "tools.py", name)
    assert module is not None and module.VALUE == "file"


def test_load_module_reuses_same_source_under_different_names(
    tmp_path: Path,
    loaded_module_names: list[str],
) -> None:
    """同一源文件以不同模块名加载时复用同一模块对象"""
    module_path = tmp_path / "shared.py"
    _write_module(module_path)
    loaded_module_names.extend(["_satrap_test_shared_a", "_satrap_test_shared_b"])

    first = plugin_module._load_module(module_path, loaded_module_names[0])
    second = plugin_module._load_module(module_path, loaded_module_names[1])

    assert first is not None
    assert second is first


def test_load_module_replaces_same_name_from_different_source(
    tmp_path: Path,
    loaded_module_names: list[str],
) -> None:
    """相同模块名切换源文件时不复用旧模块"""
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _write_module(first_path, "MARKER = 'first'\n")
    _write_module(second_path, "MARKER = 'second'\n")
    loaded_module_names.append("_satrap_test_replaced")

    first = plugin_module._load_module(first_path, loaded_module_names[0])
    second = plugin_module._load_module(second_path, loaded_module_names[0])

    assert first is not None
    assert second is not None
    assert second is not first
    assert second.MARKER == "second"


def test_load_module_discovers_module_added_after_index_seed(
    tmp_path: Path,
    loaded_module_names: list[str],
) -> None:
    """索引建立后由其他入口加载的模块仍可按源路径发现"""
    seed_path = tmp_path / "seed.py"
    external_path = tmp_path / "external.py"
    _write_module(seed_path)
    _write_module(external_path, "MARKER = 'external'\n")
    loaded_module_names.extend([
        "_satrap_test_seed",
        "_satrap_test_external_origin",
        "_satrap_test_external_alias",
    ])
    assert plugin_module._load_module(seed_path, loaded_module_names[0]) is not None

    spec = importlib.util.spec_from_file_location(loaded_module_names[1], external_path)
    assert spec is not None
    assert spec.loader is not None
    external_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(external_module)
    sys.modules[loaded_module_names[1]] = external_module

    discovered = plugin_module._load_module(external_path, loaded_module_names[2])

    assert discovered is external_module


def test_load_module_does_not_reuse_removed_module(
    tmp_path: Path,
    loaded_module_names: list[str],
) -> None:
    """模块从 sys.modules 移除后对应路径缓存随刷新失效"""
    module_path = tmp_path / "removed.py"
    _write_module(module_path)
    loaded_module_names.extend(["_satrap_test_removed", "_satrap_test_reloaded"])

    first = plugin_module._load_module(module_path, loaded_module_names[0])
    sys.modules.pop(loaded_module_names[0], None)
    second = plugin_module._load_module(module_path, loaded_module_names[1])

    assert first is not None
    assert second is not None
    assert second is not first


def test_load_module_does_not_cache_failed_execution(
    tmp_path: Path,
    loaded_module_names: list[str],
) -> None:
    """模块执行失败时不写入 sys.modules 或路径索引"""
    module_path = tmp_path / "broken.py"
    _write_module(module_path, "raise RuntimeError('broken module')\n")
    loaded_module_names.append("_satrap_test_broken")

    with pytest.raises(RuntimeError, match="broken module"):
        plugin_module._load_module(module_path, loaded_module_names[0])

    assert loaded_module_names[0] not in sys.modules
    assert module_path.resolve() not in plugin_module._module_index_by_path


def test_load_module_serializes_concurrent_execution(
    tmp_path: Path,
    loaded_module_names: list[str],
) -> None:
    """并发加载同一源文件时只创建一个模块对象"""
    module_path = tmp_path / "concurrent.py"
    _write_module(module_path, "import time\ntime.sleep(0.05)\nVALUE = object()\n")
    loaded_module_names.append("_satrap_test_concurrent")

    def load_module(_: int) -> ModuleType | None:
        return plugin_module._load_module(module_path, loaded_module_names[0])

    with ThreadPoolExecutor(max_workers=4) as executor:
        modules = list(executor.map(load_module, range(4)))

    assert modules[0] is not None
    assert all(module is modules[0] for module in modules)


def test_load_module_does_not_resolve_unchanged_modules_again(
    tmp_path: Path,
    loaded_module_names: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引热路径不再解析未变化模块的源路径"""
    module_path = tmp_path / "cached.py"
    _write_module(module_path)
    loaded_module_names.append("_satrap_test_cached")
    assert plugin_module._load_module(module_path, loaded_module_names[0]) is not None
    resolve_calls: list[ModuleType] = []
    original_resolver = plugin_module._resolve_module_source

    def counting_resolver(module: ModuleType) -> Path | None:
        resolve_calls.append(module)
        return original_resolver(module)

    monkeypatch.setattr(plugin_module, "_resolve_module_source", counting_resolver)

    assert plugin_module._load_module(module_path, loaded_module_names[0]) is not None
    assert resolve_calls == []
