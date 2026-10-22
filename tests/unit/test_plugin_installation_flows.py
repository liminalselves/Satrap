"""业务链路场景与集中边界契约"""
from __future__ import annotations

import io
from pathlib import Path
import stat
import zipfile
import pytest
from satrap.edictum import plugin_archive
from satrap.edictum.plugin_archive import PluginArchiveInstaller
from satrap.edictum.plugin_catalog import PluginCatalog
import satrap.edictum.plugin as plugin_module
from collections.abc import Iterator
import sys
from unittest.mock import Mock
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.edictum.plugin_resources import PluginResources, model_reference_fingerprint
from satrap.core.APICall.ReRankCall import parse_rerank_result
from satrap.edictum.plugin_config import parse_config_schema
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from satrap.edictum.plugin import _invoke_factory
from satrap.core.type import EmbeddingConfig
import importlib as _plugin_skill_activation_importlib
from typing import Any, cast
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.framework.providers import EdictumProvider, SessionProviderRegistry
from satrap.core.type import SessionConfig, UserCall
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.simple_session import AsyncSimpleSession, SimpleSession
from satrap.edictum.plugin_spec import PluginSpec
from satrap.edictum.plugin_runtime import PluginRuntimeState, reconcile_plugin_states, reconcile_plugin_states_async


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_command_help_metadata_legacy_fallback_and_reinstallation(tmp_path, monkeypatch, asynchronous):
    """实际命令帮助使用声明文案, 保留旧插件与显式 intro, 重装和回滚使用对应元数据"""
    import inspect
    from textwrap import dedent
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    from satrap.edictum.plugin_settings import PluginInstallConfig

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    root = tmp_path / "command-help"
    root.mkdir()
    metadata = dedent("""\
        name: command_help_probe
        commands:
          declared: 面向用户的命令说明
          blank: "   "
          nullable:
          ghost: 只有声明, 没有实现
    """)
    (root / "meta.yaml").write_text(metadata, encoding="utf-8")
    (root / "commands.py").write_text(dedent('''\
        def declared():
            """内部同步实现说明"""
            return "done"
        async def declared_async():
            """内部异步实现说明"""
            return "done"
        def legacy():
            """旧插件帮助文案

            后续实现注释不应进入帮助
            """
            return "legacy"
        async def legacy_async():
            return legacy()
        legacy_async.__doc__ = legacy.__doc__
        def undocumented():
            return "done"
        async def undocumented_async():
            return "done"
        commands = {"declared": declared, "legacy": legacy, "blank": legacy,
                    "nullable": legacy, "undocumented": undocumented}
        async_commands = {"declared": declared_async, "legacy": legacy_async, "blank": legacy_async,
                          "nullable": legacy_async, "undocumented": undocumented_async}
    '''), encoding="utf-8")
    session = (AsyncSimpleSession if asynchronous else SimpleSession)(
        "command-help", Mock(spec=AsyncLLM if asynchronous else LLM), db_path=str(tmp_path / "chat.db"))

    async def resolve(value):
        return await value if inspect.isawaitable(value) else value

    async def command(text):
        registry = session.command_handler if asynchronous else session.cmd_handler
        result, matched = await resolve(registry.process_message(text))
        assert matched
        return result

    try:
        plugin = await resolve(session.install_plugin(str(root)))
        original_meta = dict(plugin._metadata)
        help_text = await command("/help")
        assert "/declared: 面向用户的命令说明" in help_text
        for name in ("legacy", "blank", "nullable"):
            assert f"/{name}: 旧插件帮助文案" in help_text
        assert "/undocumented: 暂无说明" in help_text
        assert "/ghost:" not in help_text and "内部" not in help_text and "后续实现" not in help_text
        assert await command("/declared") == "done"
        assert plugin.capability_descriptions["commands"]["declared"] == "面向用户的命令说明"
        async def manual_async():
            return "manual"
        session.add_command("manual", manual_async if asynchronous else lambda: "manual", intro="显式注册的说明")
        assert "/manual: 显式注册的说明" in await command("/help")
        await resolve(session.uninstall_plugin(plugin.name))
        assert "/declared:" not in await command("/help")
        (root / "meta.yaml").write_text(metadata.replace("面向用户的命令说明", "更新后的命令说明"), encoding="utf-8")
        await resolve(session.install_plugin(str(root)))
        assert "/declared: 更新后的命令说明" in await command("/help")
        await resolve(session.uninstall_plugin(plugin.name))
        await resolve(session.install_plugin(str(root), config=PluginInstallConfig({}, metadata_snapshot=original_meta)))
        assert "/declared: 面向用户的命令说明" in await command("/help")
    finally:
        for installed in list(session.list_plugins()):
            await resolve(session.uninstall_plugin(installed.name))

def archive_bytes(files):
    """构造内存 ZIP 测试文件"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files:
            if isinstance(name, str):
                info = zipfile.ZipInfo("placeholder")
                info.filename = name
                info.orig_filename = name
                info.compress_type = zipfile.ZIP_DEFLATED
                name = info
            archive.writestr(name, value)
    return buffer.getvalue()

@pytest.fixture
def installer(tmp_path):
    return PluginArchiveInstaller(PluginCatalog(tmp_path / "builtin", tmp_path / "data" / "plugins"))

@pytest.mark.parametrize("prefix", ["", "package/"])
def test_preview_install_without_execution_or_enable(installer, prefix):
    content = archive_bytes([(prefix + "meta.yaml", "name: probe\nversion: '1'\ntools:\n  probe_tool: 测试"), (prefix + "tools.py", "raise RuntimeError('不得执行')")])
    result = installer.preview(content)
    assert result["plugin"]["capabilities"]["tools"] == {"probe_tool": "测试"}
    assert not installer.catalog.user_dir.exists()
    installed = installer.install(result["token"])
    assert installed["plugin"]["name"] == "probe"
    assert (installer.catalog.user_dir / "probe" / "tools.py").is_file()
    assert not (installer.catalog.user_dir.parent / "chat_plugins.json").exists()
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())
    with pytest.raises(ValueError, match="过期或已使用"):
        installer.install(result["token"])

@pytest.mark.parametrize("path", ["../escape", "/escape", "C:/escape", "x\\escape", "CON.txt", "a/../escape", "a//escape", "x.", "LPT¹", "name:stream"])
def test_unsafe_paths_rejected_and_cleaned(installer, path):
    with pytest.raises(ValueError, match="非法路径"):
        installer.preview(archive_bytes([("meta.yaml", "name: probe"), (path, "payload")]))
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())

@pytest.mark.parametrize("files", [
    [("a/meta.yaml", "name: a"), ("b/meta.yaml", "name: b")],
    [("a/b/meta.yaml", "name: probe")],
    [("a/meta.yaml", "name: probe"), ("outside.txt", "x")],
    [("meta.yaml", "name: ../escape")],
    [("meta.yaml", "name: [broken")],
    [("meta.yaml", "name: true")],
    [("meta.yaml", "name: ' padded '")],
    [("meta.yaml", "name: probe"), ("Tools.py", "x"), ("tools.py", "x")],
    [("meta.yaml", "name: probe"), ("A/x", "x"), ("a/y", "x")],
])
def test_invalid_layout_manifest_and_case_conflict(installer, files):
    with pytest.raises(ValueError):
        installer.preview(archive_bytes(files))
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())

def test_link_rejected(installer):
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="链接或特殊文件"):
        installer.preview(archive_bytes([("meta.yaml", "name: probe"), (link, "../outside")]))

@pytest.mark.parametrize("source", ["builtin", "user"])
def test_existing_plugin_preserved(installer, source):
    base = installer.catalog.preset_dir if source == "builtin" else installer.catalog.user_dir
    existing = base / "existing"
    existing.mkdir(parents=True)
    (existing / "meta.yaml").write_text("name: probe\nversion: original", encoding="utf-8")
    with pytest.raises(ValueError, match="同名插件"):
        installer.preview(archive_bytes([("meta.yaml", "name: probe")]))
    assert "original" in (existing / "meta.yaml").read_text(encoding="utf-8")

def test_conflict_created_after_preview_cleans_staging(installer):
    result = installer.preview(archive_bytes([("meta.yaml", "name: probe")]))
    target = installer.catalog.user_dir / "probe"
    target.mkdir(parents=True)
    (target / "keep").write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="同名插件"):
        installer.install(result["token"])
    assert (target / "keep").read_text(encoding="utf-8") == "original"
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())

def test_limits_cancellation_and_expiry(installer, monkeypatch):
    content = archive_bytes([("meta.yaml", "name: probe")])
    monkeypatch.setattr(plugin_archive, "MAX_ARCHIVE_FILES", 1)
    with pytest.raises(ValueError, match="条目数量"):
        installer.preview(archive_bytes([("meta.yaml", "name: probe"), ("extra", "x")]))
    monkeypatch.setattr(plugin_archive, "MAX_EXPANDED_BYTES", 5)
    with pytest.raises(ValueError, match="大小"):
        installer.preview(content)
    monkeypatch.setattr(plugin_archive, "MAX_EXPANDED_BYTES", 1000)
    preview = installer.preview(content)
    installer.discard(preview["token"])
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())
    preview = installer.preview(content)
    monkeypatch.setattr(plugin_archive.time, "monotonic", lambda: float("inf"))
    with pytest.raises(ValueError, match="过期"):
        installer.install(preview["token"])
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())

def test_corrupt_archive_and_move_failure_cleanup(installer, monkeypatch):
    with pytest.raises(zipfile.BadZipFile):
        installer.preview(b"invalid zip")
    preview = installer.preview(archive_bytes([("meta.yaml", "name: probe")]))
    def fail_move(*args):
        raise OSError("测试移动失败")
    monkeypatch.setattr(plugin_archive, "_move_no_replace", fail_move)
    with pytest.raises(OSError, match="测试移动失败"):
        installer.install(preview["token"])
    assert not (installer.catalog.user_dir / "probe").exists()
    assert not list((installer.catalog.user_dir.parent / ".plugin-install").iterdir())

def test_nested_private_metadata_is_not_a_second_plugin(installer):
    result = installer.preview(archive_bytes([("package/meta.yaml", "name: probe"), ("package/resources/private/meta.yaml", "resource: example")]))
    installer.install(result["token"])
    assert (installer.catalog.user_dir / "probe" / "resources" / "private" / "meta.yaml").is_file()

def test_invalid_existing_metadata_does_not_break_other_installations(installer):
    broken = installer.catalog.preset_dir / "broken"
    broken.mkdir(parents=True)
    (broken / "meta.yaml").write_text("name: [invalid", encoding="utf-8")
    result = installer.preview(archive_bytes([("meta.yaml", "name: probe")]))
    installer.discard(result["token"])
    with pytest.raises(ValueError, match="同名插件"):
        installer.preview(archive_bytes([("meta.yaml", "name: broken")]))

@pytest.fixture
def loaded_module_names() -> Iterator[list[str]]:
    """记录测试动态加载的模块名并在测试后清理"""
    module_names: list[str] = []
    yield module_names
    for module_name in module_names:
        sys.modules.pop(module_name, None)

def _write_module(path: Path, source: str = "VALUE = object()\n") -> None:
    """写入测试插件模块"""
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

def test_factory_internal_type_error_is_not_retried():
    calls = []

    def factory(session, config=None):
        calls.append(config)
        raise TypeError("内部错误")

    with pytest.raises(TypeError, match="内部错误"):
        _invoke_factory(factory, object(), {"x": 1}, object())
    assert calls == [{"x": 1}]

def test_factory_supports_old_and_new_signatures():
    session, resources = object(), object()
    assert _invoke_factory(lambda: 1, session, {}, resources) == 1
    assert _invoke_factory(lambda s: s, session, {}, resources) is session
    assert _invoke_factory(lambda s, c: c, session, {"a": 1}, resources) == {"a": 1}
    assert _invoke_factory(lambda s, c, *, resources: resources, session, {}, resources) is resources

def test_model_reference_validation_and_revision(tmp_path):
    manager = ModelConfigManager(tmp_path / "models.json", auto_create=False)
    schema = parse_config_schema({"config_schema": {"embed": {"type": "embed", "required": True}}})
    with pytest.raises(ValueError, match="必填"):
        PluginResources(manager, schema, {"embed": ""}, async_=False)
    with pytest.raises(ValueError, match="不存在"):
        PluginResources(manager, schema, {"embed": "missing"}, async_=False)
    with pytest.raises(ValueError, match="缺少"):
        PluginResources(manager, schema, {"embed": "default"}, async_=False)
    manager.set_embedding_config(EmbeddingConfig(model="embedding-a", api_key="test", dimensions=3), "test")
    first = model_reference_fingerprint(manager, schema, {"embed": "test"})
    manager.set_embedding_config(EmbeddingConfig(model="embedding-b", api_key="test", dimensions=3), "test")
    assert first != model_reference_fingerprint(manager, schema, {"embed": "test"})

def test_lazy_resources_share_within_plugin_and_close(tmp_path, monkeypatch):
    manager = ModelConfigManager(tmp_path / "models.json", auto_create=False)
    manager.set_embedding_config(EmbeddingConfig(model="test", api_key="test"), "test")
    schema = parse_config_schema({"config_schema": {"embed": {"type": "embed"}}})
    client = Mock()
    build = Mock(return_value=client)
    monkeypatch.setattr("satrap.edictum.plugin_resources.build_model_client", build)
    resources = PluginResources(manager, schema, {"embed": "test"}, async_=False)
    build.assert_not_called()
    assert resources["embed"] is resources.get("embed")
    build.assert_called_once()
    resources.close()
    client.client.close.assert_called_once()

@pytest.mark.parametrize("configured_name", ["selected", " selected "])
def test_installed_model_snapshot_survives_source_deletion_and_client_close(tmp_path, monkeypatch, configured_name):
    """旧资源参数独立持有, 关闭旧客户端后回滚创建新客户端且不读取已删除引用"""
    manager = ModelConfigManager(tmp_path / "models.json", auto_create=False)
    manager.set_embedding_config(EmbeddingConfig(model="old", api_key="test", dimensions=3), "selected")
    schema = parse_config_schema({"config_schema": {"embed": {"type": "embed"}}})
    built = []

    def build(kind, config, **kwargs):
        """模拟独立客户端并记录实际参数, 不发送网络请求"""
        client = Mock()
        built.append((kind, config, client))
        return client

    monkeypatch.setattr("satrap.edictum.plugin_resources.build_model_client", build)
    original = PluginResources(manager, schema, {"embed": configured_name}, async_=False)
    old_client = original["embed"]
    snapshot = original.snapshot_models()
    original.close()
    assert manager.remove_embedding_config("selected")
    restored = PluginResources(snapshot, schema, {"embed": configured_name}, async_=False)
    assert restored._clients == {}
    snapshot.get_embedding_config("selected").dimensions = 99
    assert restored["embed"] is not old_client
    assert [(kind, config.model, config.dimensions) for kind, config, _ in built] == [("embed", "old", 3), ("embed", "old", 3)]
    old_client.client.close.assert_called_once()
    restored.close()
    built[1][2].client.close.assert_called_once()

@pytest.mark.parametrize("payload", [{}, {"results": {}}, {"results": [None]}, {"results": [{"index": 0, "relevance_score": float("nan")}]}, {"results": [{"index": True, "relevance_score": 1}]}])
def test_strict_rerank_rejects_malformed_responses(payload):
    with pytest.raises(ValueError):
        parse_rerank_result(payload, suppress_error=False)

def test_strict_rerank_accepts_empty_and_filters_scores():
    assert parse_rerank_result({"results": []}, suppress_error=False) == []
    result = parse_rerank_result({"results": [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.8}]}, min_score=0.5, suppress_error=False)
    assert result == [{"text": "", "score": 0.8, "original_index": 1}]

def _write_plugin(root: Path, asynchronous: bool, *, fail_mcp: bool = False) -> Path:
    """创建含两个技能、独立自带工具和 MCP 的目录插件"""
    root.mkdir()
    (root / "meta.yaml").write_text("name: skill_probe\nskills:\n  alpha: 第一个技能\n  beta: 第二个技能\ntools:\n  shared: 共用工具\n", encoding="utf-8")
    tool_type = "AsyncTool" if asynchronous else "Tool"
    (root / "tools.py").write_text(f"from satrap.core.utils.TCBuilder import {tool_type}\nclass Shared({tool_type}):\n    tool_name = 'shared'\n    description = '共用工具'\n    params_dict = {{}}\n", encoding="utf-8")
    (root / "skills.py").write_text(f"""from satrap.core.utils.skills import Skill
from satrap.core.utils.TCBuilder import {tool_type}

class ProbeTool({tool_type}):
    def __init__(self, name):
        super().__init__(tool_name=name, description='技能工具', params_dict={{}})

class ProbeMCP:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.connections, self.closed = name, fail, 0, False
    def sync_register_tools(self, manager):
        self.connections += 1
        self.manager = manager
        tool = ProbeTool(self.name)
        manager.register_tool(tool)
        if self.fail:
            raise RuntimeError('MCP 激活失败')
        return [tool]
    async def register_tools(self, manager):
        return self.sync_register_tools(manager)
    def sync_close(self):
        self.closed = True
        if hasattr(self, 'manager'):
            self.manager.unregister_tool(self.name)
    async def close(self):
        self.sync_close()

skills = [
    Skill('alpha', 'ALPHA 指令', tool_names=['shared'], tools=[ProbeTool('alpha_bundle')], mcp_clients=[ProbeMCP('alpha_mcp')]),
    Skill('beta', 'BETA 指令', tool_names=['shared'], tools=[ProbeTool('beta_bundle')], mcp_clients=[ProbeMCP('beta_mcp', fail={fail_mcp!r})]),
]
""", encoding="utf-8")
    return root

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("initial_beta", [False, True])
async def test_first_install_independent_skills_and_reconciliation(tmp_path, asynchronous, initial_beta):
    """首次安装即注入启用技能, 重复协调不重复注入, 独立停用不影响另一技能"""
    plugin_dir = _write_plugin(tmp_path / "plugin", asynchronous, fail_mcp=not initial_beta)
    session: Any = (AsyncSimpleSession if asynchronous else SimpleSession)(
        "skills", cast(Any, object()), system_prompt="基础提示词", db_path=str(tmp_path / "context.db"), enable_checkpoint=False,
    )
    if asynchronous:
        await session.initialize()
    states: list[PluginRuntimeState] = []
    spec = PluginSpec(name="skill_probe", path=str(plugin_dir), capabilities={"skills": {"alpha": True, "beta": initial_beta}})

    async def reconcile(target):
        if asynchronous:
            return await reconcile_plugin_states_async(states, [target], session.install_plugin, session.uninstall_plugin)
        return reconcile_plugin_states(states, [target], session.install_plugin, session.uninstall_plugin)

    assert (await reconcile(spec))["ok"]
    mgr = session._skills_manager
    assert mgr is not None
    wf = session._wf
    alpha, beta = mgr.get_skill("alpha"), mgr.get_skill("beta")
    assert alpha is not None and beta is not None
    text = mgr.catalog(wf)
    assert text.count("- alpha:") == 1
    assert text.count("- beta:") == int(initial_beta)
    assert alpha.mcp_clients[0].connections == 1
    assert beta.mcp_clients[0].connections == int(initial_beta)
    assert "alpha_bundle" in wf.tools_manager.tools and "alpha_mcp" in wf.tools_manager.tools
    assert ("beta_bundle" in wf.tools_manager.tools) == initial_beta
    assert (await reconcile(spec))["ok"]
    assert alpha.mcp_clients[0].connections == 1
    assert mgr.catalog(wf).count("- alpha:") == 1
    with pytest.raises(ValueError, match="已安装"):
        if asynchronous:
            await session.install_plugin(str(plugin_dir))
        else:
            session.install_plugin(str(plugin_dir))
    assert len(session.list_plugins()) == 1
    enabled = PluginSpec(name="skill_probe", path=str(plugin_dir), capabilities={"skills": {"alpha": True, "beta": True}})
    beta.mcp_clients[0].fail = False
    assert (await reconcile(enabled))["ok"]
    assert mgr.catalog(wf).count("- beta:") == 1
    changed = PluginSpec(name="skill_probe", path=str(plugin_dir), capabilities={"skills": {"alpha": False, "beta": True}})
    assert (await reconcile(changed))["ok"]
    assert "- alpha:" not in mgr.catalog(wf)
    assert mgr.catalog(wf).count("- beta:") == 1
    assert alpha.mcp_clients[0].closed and not beta.mcp_clients[0].closed
    assert "alpha_bundle" not in wf.tools_manager.tools and "alpha_mcp" not in wf.tools_manager.tools
    assert wf.tools_manager.is_tool_enabled("shared")
    assert wf.tools_manager.is_tool_enabled("beta_bundle")
    prompt = session.compose_system_prompt("新的基础提示词", wf.ctx)
    if asynchronous:
        await wf.ctx.reset_system_prompt(prompt)
    else:
        wf.ctx.reset_system_prompt(prompt)
    assert wf.ctx.get_context()[0]["content"].startswith("新的基础提示词")
    assert mgr.catalog(wf).count("- beta:") == 1
    assert (await reconcile(enabled))["ok"]
    assert alpha.mcp_clients[0].connections == 2
    assert mgr.catalog(wf).count("- alpha:") == 1
    if asynchronous:
        await session.uninstall_plugin("skill_probe")
    else:
        session.uninstall_plugin("skill_probe")
    assert all("<skill:" not in message.get("content", "") for message in wf.ctx.get_context())
    assert wf.tools_manager.tools == {}
    assert mgr._active == {} and mgr._active_mcp == {} and mgr._owned_tools == {}

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("borrowed_enabled", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_failed_first_activation_rolls_back_instructions_and_tools(tmp_path, caplog, asynchronous, borrowed_enabled, cleanup_fails):
    """首次技能连接失败应回滚全部已装配能力, 不把失败状态显示为已加载"""
    plugin_dir = _write_plugin(tmp_path / "plugin", asynchronous, fail_mcp=True)
    (plugin_dir / "hooks.py").write_text(
        "def cleanup(session):\n    session.cleanup_calls = getattr(session, 'cleanup_calls', 0) + 1\n"
        + ("    raise ValueError('清理失败测试')\n" if cleanup_fails else ""), encoding="utf-8",
    )
    session: Any = (AsyncSimpleSession if asynchronous else SimpleSession)(
        "failed", cast(Any, object()), system_prompt="基础提示词", db_path=str(tmp_path / "context.db"), enable_checkpoint=False,
    )
    if asynchronous:
        await session.initialize()
    from satrap.core.utils.TCBuilder import AsyncTool, Tool
    borrowed = (AsyncTool if asynchronous else Tool)(tool_name="borrowed", description="已有工具", params_dict={})
    session._wf.tools_manager.register_tool(borrowed)
    if not borrowed_enabled:
        session._wf.tools_manager.disable_tool("borrowed")
    skills_file = plugin_dir / "skills.py"
    skills_file.write_text(skills_file.read_text(encoding="utf-8").replace("tool_names=['shared']", "tool_names=['shared', 'borrowed']"), encoding="utf-8")
    if asynchronous:
        with pytest.raises(RuntimeError, match="MCP 激活失败"):
            await session.install_plugin(str(plugin_dir))
    else:
        with pytest.raises(RuntimeError, match="MCP 激活失败"):
            session.install_plugin(str(plugin_dir))
    assert session.list_plugins() == []
    assert session._wf.tools_manager.tools == {"borrowed": borrowed}
    assert session._wf.tools_manager.is_tool_enabled("borrowed") == borrowed_enabled
    assert session._wf.ctx.get_context()[0]["content"] == "基础提示词"
    assert session._skills_manager._active == {}
    assert session._skills_manager._active_mcp == {}
    assert session._skills_manager._owned_tools == {}
    assert session.cleanup_calls == 1
    if cleanup_fails:
        assert "安装回滚清理失败" in caplog.text and "清理失败测试" in caplog.text

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_restart_deduplicates_persisted_skill_instructions(tmp_path, asynchronous):
    """从历史恢复后重新装配技能只保留一个指令块"""
    plugin_dir = _write_plugin(tmp_path / "plugin", asynchronous)
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    kwargs = {"db_path": str(tmp_path / "context.db"), "enable_checkpoint": False}
    old: Any = session_type("restart", cast(Any, object()), **kwargs)
    if asynchronous:
        await old.install_plugin(str(plugin_dir))
    else:
        old.install_plugin(str(plugin_dir))
    new: Any = session_type("restart", cast(Any, object()), **kwargs)
    if asynchronous:
        await new.install_plugin(str(plugin_dir))
    else:
        new.install_plugin(str(plugin_dir))
    text = new._skills_manager.catalog(new._wf)
    assert text.count("- alpha:") == text.count("- beta:") == 1
    assert all("<skill:" not in str(row.get("content", "")) for row in new.ctx.get_context())
    assert old._skills_manager.get_skill("alpha") is not new._skills_manager.get_skill("alpha")
    if asynchronous:
        await old.uninstall_plugin("skill_probe")
    else:
        old.uninstall_plugin("skill_probe")
    assert {"alpha_bundle", "alpha_mcp", "beta_bundle", "beta_mcp"} <= new._wf.tools_manager.tools.keys()

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_platform_prompt_override_preserves_active_skills_and_history(tmp_path, asynchronous):
    """真实平台提示词覆盖保留活跃技能, 清空基础提示词也不删除技能和对话"""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _write_plugin(plugins / "skill_probe", asynchronous)
    catalog = PluginCatalog(preset_dir=plugins, user_dir=tmp_path / "user")
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    configs.plugin_catalog = catalog
    configs.create("assistant", {"edictum_type": "async_simple" if asynchronous else "simple", "params": {"system_prompt": "基础提示词"}, "plugins": ["skill_probe"]})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(tmp_path / "context.db"))
    provider.plugin_catalog = catalog
    cfg = SessionConfig(session_id="platform", session_type_name="assistant", provider_name="edictum")
    session: Any = provider.create_session(cfg, cast(Any, object()))
    await provider.prepare_session_async(session)
    if asynchronous:
        await session._wf.ctx.add_user_message("保留历史消息")
    else:
        session._wf.ctx.add_user_message("保留历史消息")
    manager = object.__new__(SessionManager)
    manager.provider_registry = SessionProviderRegistry()
    manager.provider_registry.register(provider)
    manager._model_cfg_mgr = None
    for prompt in ["群提示词", ""]:
        await manager._apply_group_session_overrides(cfg, session, UserCall(group_session_overrides={"prompt": prompt}), None)
        messages = session._wf.ctx.get_context()
        text = messages[0]["content"]
        assert "<skill:" not in text
        assert "基础提示词" not in text
        assert "群提示词" in text if prompt else "群提示词" not in text
        assert any(message.get("content") == "保留历史消息" for message in messages)
    await provider.release_session_async(session)

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_hot_restart_persists_new_prompt_and_reactivated_skills(tmp_path, monkeypatch, asynchronous):
    """配置热重启后, 模型内存和持久化上下文都保留新提示词、技能和历史"""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _write_plugin(plugins / "skill_probe", asynchronous)
    catalog = PluginCatalog(preset_dir=plugins, user_dir=tmp_path / "user")
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    configs.plugin_catalog = catalog
    configs.create("assistant", {"edictum_type": "async_simple" if asynchronous else "simple", "model_name": "demo", "params": {"system_prompt": "旧提示词"}, "plugins": ["skill_probe"]})
    db_path = tmp_path / "platform.db"
    manager = SessionManager(db_path=db_path, platform_id="onebot-test")
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(db_path))
    provider.plugin_catalog = catalog
    manager.register_provider(provider)
    models = ModelConfigManager(storage_path=tmp_path / "models.json")
    models.update_llm_config("demo", api_key="test", model="probe")
    manager.model_config_manager = models
    monkeypatch.setattr(_plugin_skill_activation_importlib.import_module("satrap.core.framework.SessionManager"), "build_llm_from_config", lambda *_args, **_kwargs: (AsyncLLM if asynchronous else LLM)(api_key="test"))
    manager.register_session_from_provider_config("edictum", "assistant", session_id="hot-restart")
    assert await manager.activate_session_async("hot-restart")
    entry = manager.pool.get("hot-restart")
    assert entry is not None
    old: Any = entry.session
    if asynchronous:
        await old._wf.ctx.add_user_message("保留历史")
    else:
        old._wf.ctx.add_user_message("保留历史")
    configs.update("assistant", {"params": {"system_prompt": "新提示词", "thinking": "high", "max_iterations": 3, "model_params": {"temperature": 0.2}}})
    result = await manager.reconcile_edictum_runtime_async(config_name="assistant")
    assert result[0]["ok"]
    new: Any = entry.session
    assert new is not old
    assert new.default_thinking == "high" and new._wf.llm.temperature == 0.2
    assert new.default_max_iterations == 3
    assert {"alpha_bundle", "alpha_mcp", "beta_bundle", "beta_mcp"} <= new._wf.tools_manager.tools.keys()
    assert not new._skills_manager.get_skill("alpha").mcp_clients[0].closed
    memory = new._wf.ctx.get_context()
    context_id = new._wf.ctx.conversation_id
    if asynchronous:
        persisted = AsyncContextManager(context_id, db_path=str(db_path))
        await persisted.initialize()
    else:
        persisted = ContextManager(context_id, db_path=str(db_path))
    for messages in [memory, persisted.get_context()]:
        text = messages[0]["content"]
        assert text.startswith("新提示词")
        assert "<skill:" not in text
        assert any(message.get("content") == "保留历史" for message in messages)
    installer_module = _plugin_skill_activation_importlib.import_module("satrap.edictum.simple_session.async_plugins" if asynchronous else "satrap.edictum.simple_session.sync_plugins")
    collect = installer_module.collect_skills
    def failing_skills(*args, **kwargs):
        skills = collect(*args, **kwargs)
        for skill in skills:
            if skill.name == "beta":
                skill.mcp_clients[0].fail = True
        return skills
    monkeypatch.setattr(installer_module, "collect_skills", failing_skills)
    configs.update("assistant", {"params": {"system_prompt": "失败的候选提示词"}})
    failed = await manager.reconcile_edictum_runtime_async(config_name="assistant")
    assert not failed[0]["ok"] and failed[0]["old_runtime_preserved"]
    assert entry.session is new
    assert {"alpha_bundle", "alpha_mcp", "beta_bundle", "beta_mcp"} <= new._wf.tools_manager.tools.keys()
    assert not new._skills_manager.get_skill("beta").mcp_clients[0].closed
    if asynchronous:
        restored = AsyncContextManager(context_id, db_path=str(db_path))
        await restored.initialize()
    else:
        restored = ContextManager(context_id, db_path=str(db_path))
    text = restored.get_context()[0]["content"]
    assert text.startswith("新提示词")
    assert "<skill:" not in text
    await manager.unload_session_async("hot-restart")
