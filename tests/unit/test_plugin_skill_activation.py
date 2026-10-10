from pathlib import Path
import importlib
from typing import Any, cast

import pytest

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.utils.context import AsyncContextManager, ContextManager
from satrap.core.framework.providers import EdictumProvider, SessionProviderRegistry
from satrap.core.type import SessionConfig, UserCall
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.simple_session import AsyncSimpleSession, SimpleSession
from satrap.edictum.plugin_spec import PluginSpec
from satrap.edictum.plugin_runtime import PluginRuntimeState, reconcile_plugin_states, reconcile_plugin_states_async


def _write_plugin(root: Path, asynchronous: bool, *, fail_mcp: bool = False) -> Path:
    """
    创建含两个技能、独立自带工具和 MCP 的目录插件

    参数:
    - root: 临时插件目录
    - asynchronous: 工具是否使用异步类型
    - fail_mcp: 第二个技能是否模拟连接失败

    返回:
    - Path: 可直接安装的插件目录
    """
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
    text = wf.ctx.get_context()[0]["content"]
    assert text.count("<skill:alpha>") == 1
    assert text.count("<skill:beta>") == int(initial_beta)
    assert alpha.mcp_clients[0].connections == 1
    assert beta.mcp_clients[0].connections == int(initial_beta)
    assert "alpha_bundle" in wf.tools_manager.tools and "alpha_mcp" in wf.tools_manager.tools
    assert ("beta_bundle" in wf.tools_manager.tools) == initial_beta
    assert (await reconcile(spec))["ok"]
    assert alpha.mcp_clients[0].connections == 1
    assert wf.ctx.get_context()[0]["content"].count("<skill:alpha>") == 1
    with pytest.raises(ValueError, match="已安装"):
        if asynchronous:
            await session.install_plugin(str(plugin_dir))
        else:
            session.install_plugin(str(plugin_dir))
    assert len(session.list_plugins()) == 1
    enabled = PluginSpec(name="skill_probe", path=str(plugin_dir), capabilities={"skills": {"alpha": True, "beta": True}})
    beta.mcp_clients[0].fail = False
    assert (await reconcile(enabled))["ok"]
    assert wf.ctx.get_context()[0]["content"].count("<skill:beta>") == 1
    changed = PluginSpec(name="skill_probe", path=str(plugin_dir), capabilities={"skills": {"alpha": False, "beta": True}})
    assert (await reconcile(changed))["ok"]
    assert "<skill:alpha>" not in wf.ctx.get_context()[0]["content"]
    assert wf.ctx.get_context()[0]["content"].count("<skill:beta>") == 1
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
    assert wf.ctx.get_context()[0]["content"].count("<skill:beta>") == 1
    assert (await reconcile(enabled))["ok"]
    assert alpha.mcp_clients[0].connections == 2
    assert wf.ctx.get_context()[0]["content"].count("<skill:alpha>") == 1
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
    text = new._wf.ctx.get_context()[0]["content"]
    assert text.count("<skill:alpha>") == text.count("<skill:beta>") == 1
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
        assert text.count("<skill:alpha>") == text.count("<skill:beta>") == 1
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
    monkeypatch.setattr(importlib.import_module("satrap.core.framework.SessionManager"), "build_llm_from_config", lambda *_args, **_kwargs: (AsyncLLM if asynchronous else LLM)(api_key="test"))
    manager.register_session_from_provider_config("edictum", "assistant", session_id="hot-restart")
    assert await manager.activate_session_async("hot-restart")
    entry = manager.pool.get("hot-restart")
    assert entry is not None
    old: Any = entry.session
    if asynchronous:
        await old._wf.ctx.add_user_message("保留历史")
    else:
        old._wf.ctx.add_user_message("保留历史")
    configs.update("assistant", {"params": {"system_prompt": "新提示词", "thinking": "high", "model_params": {"temperature": 0.2}}})
    result = await manager.reconcile_edictum_runtime_async(config_name="assistant")
    assert result[0]["ok"]
    new: Any = entry.session
    assert new is not old
    assert new.default_thinking == "high" and new._wf.llm.temperature == 0.2
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
        assert text.count("<skill:alpha>") == text.count("<skill:beta>") == 1
        assert any(message.get("content") == "保留历史" for message in messages)
    installer_module = importlib.import_module("satrap.edictum.simple_session.async_plugins" if asynchronous else "satrap.edictum.simple_session.sync_plugins")
    collect = installer_module.collect_skills
    def failing_skills(*args):
        skills = collect(*args)
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
    assert text.count("<skill:alpha>") == text.count("<skill:beta>") == 1
    await manager.unload_session_async("hot-restart")
