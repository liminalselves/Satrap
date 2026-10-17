"""真实插件安装, 服务调用及撤销的集中业务链路"""
from __future__ import annotations

from pathlib import Path
from contextvars import copy_context
import asyncio
import inspect
import textwrap

import pytest
import yaml

from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.plugin_interop.contracts import PluginInteractionError, parse_interop
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap import AsyncLLM, LLM


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _plugin(tmp_path: Path, name: str, meta: dict, **sources: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "meta.yaml").write_text(yaml.safe_dump({"name": name, **meta}), encoding="utf-8")
    for filename, source in sources.items():
        (root / (filename + ".py")).write_text(textwrap.dedent(source), encoding="utf-8")
    return root


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_service_install_call_isolation_and_revocation(tmp_path, monkeypatch, asynchronous):
    """模型工具经真实分派复用服务, 保留来源并拒绝停用或失效引用"""
    base = "AsyncService" if asynchronous else "Service"
    tool_base = "AsyncTool" if asynchronous else "Tool"
    prefix, wait = ("async ", "await ") if asynchronous else ("", "")
    provider = _plugin(tmp_path, "interop_backend", {
        "interop": {"exports": {"services": ["normalize_text"]}},
        "services": {"normalize_text": "文本整理"},
        "permission_schema_version": 1,
        "config_schema": {"service_callers": {"type": "textarea", "default": "alice\nbob"}},
        "management_permissions": {"service_use": {
            "description": "限定服务调用者", "caller_list": "service_callers", "empty_policy": "deny", "system_admin": False,
        }},
        "service_permissions": {"normalize_text": ["service_use"]},
    }, services=f'''
        from satrap.edictum import {base}
        from satrap.core.call_context import current_call_origin
        import asyncio

        class Normalize({base}):
            service_name = "normalize_text"
            description = "测试文本处理"
            {prefix}def execute(self, text: str, *, lowercase: bool = False, **extra) -> dict:
                {"await asyncio.sleep(0)" if asynchronous else "pass"}
                origin = current_call_origin()
                return {{"text": " ".join(text.split()).lower() if lowercase else " ".join(text.split()),
                        "actor": origin.actor_id if origin else "", "extra": extra}}
    ''')
    consumer = _plugin(tmp_path, "interop_consumer", {
        "interop": {"requires": {"interop_backend": {"services": ["normalize_text"]}}},
    }, tools=f'''
        from satrap.core.utils.TCBuilder import {tool_base}

        class Consumer({tool_base}):
            tool_name = "consumer"
            description = "测试服务复用"
            params_dict = {{"text": ("string", "文本")}}
            def __init__(self, plugin_host):
                super().__init__()
                self.plugin_host = plugin_host
            {prefix}def execute(self, text: str):
                return {wait}self.plugin_host.plugins.services.call_{"async" if asynchronous else "sync"}(
                    "interop_backend", "normalize_text", {{"text": text, "lowercase": True, "tag": "附加参数"}})

        def get_tools(session, *, plugin_host=None):
            return [Consumer(plugin_host)]
    ''')
    llm = AsyncLLM(api_key="test", model="test") if asynchronous else LLM(api_key="test", model="test")
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    session = session_type("interop-flow", llm, db_path=str(tmp_path / "chat.db"), enable_checkpoint=False)
    try:
        backend = await _await(session.install_plugin(str(provider)))
        await _await(session.install_plugin(str(consumer)))
        manager = session._wf.tools_manager
        from satrap.edictum.simple_session.recovery import prepare_session_recovery
        from satrap.core.framework.Base.execution.engine import configuration
        from satrap.core.framework.Base.execution.store import RunStore, RunConflictError
        prepare_session_recovery(session)
        store = RunStore(session._wf.ctx.db_path, session._wf.ctx.conversation_id)
        run_id = store.create({}, store.history_signature(), configuration(session._wf))
        def changed_signature(text: str, required: int) -> str:
            raise AssertionError("恢复配置已不兼容, 不得执行新服务")
        with monkeypatch.context() as changed:
            changed.setattr(backend._services["normalize_text"], "execute", changed_signature)
            with pytest.raises(RunConflictError, match="配置"):
                await _await(session.resume_run(run_id))
        assert [item["function"]["name"] for item in manager.get_tools_definitions()] == ["consumer"]
        tool = manager.tools["consumer"]
        host = tool.plugin_host
        reference = host.plugins.services.find("interop_backend", "normalize_text")
        assert reference is not None
        assert host.plugins.find("interop_backend").name == "interop_backend"
        with pytest.raises(PluginInteractionError, match="执行作用域"):
            await _await(getattr(reference, "call_async" if asynchronous else "call_sync")({"text": "outside"}))

        async def execute(actor):
            origin = CallOrigin("test", "bot", "GroupMessage", "group", actor, "msg", "request")
            with bind_call_origin(origin):
                return await _await(manager.execute_tool("consumer", {"text": " Hello   WORLD "}))

        results = await asyncio.gather(execute("alice"), execute("bob"))
        assert results == [
            {"text": "hello world", "actor": "alice", "extra": {"tag": "附加参数"}},
            {"text": "hello world", "actor": "bob", "extra": {"tag": "附加参数"}},
        ]
        denied = await execute("charlie")
        assert denied["ok"] is False
        assert denied["error_type"] == "permission_denied"
        unknown = await _await(manager.execute_tool("normalize_text", {"text": "guess"}))
        assert unknown["ok"] is False and unknown["error_type"] == "not_found"
        with session._plugin_interop.execution("interop_consumer", "tools", "consumer"):
            inherited = copy_context()
        with pytest.raises(PluginInteractionError):
            inherited.run(reference.call_sync, {"text": "stale scope"})

        assert backend.disable_service("normalize_text")
        assert host.plugins.services.find("interop_backend", "normalize_text") is None
        with pytest.raises(PluginInteractionError):
            with session._plugin_interop.execution("interop_consumer", "tools", "consumer"):
                await _await(getattr(reference, "call_async" if asynchronous else "call_sync")({"text": "disabled"}))
        assert backend.enable_service("normalize_text")
        with session._plugin_interop.execution("interop_consumer", "tools", "consumer"):
            with pytest.raises(PluginInteractionError) as error:
                await _await(getattr(reference, "call_async" if asynchronous else "call_sync")({"text": "old enable"}))
            assert error.value.code == "capability_stale"
        await _await(session.uninstall_plugin("interop_backend"))
        await _await(session.install_plugin(str(provider)))
        with pytest.raises(PluginInteractionError) as error:
            with session._plugin_interop.execution("interop_consumer", "tools", "consumer"):
                await _await(getattr(reference, "call_async" if asynchronous else "call_sync")({"text": "old"}))
        assert error.value.code == "capability_stale"
        await _await(session.uninstall_plugin("interop_consumer"))
        with pytest.raises(PluginInteractionError) as error:
            host.environment_snapshot()
        assert error.value.code == "capability_stale"
    finally:
        for plugin in session.list_plugins():
            await _await(session.uninstall_plugin(plugin.name))
        await _await(llm.client.close())


def test_interop_static_declarations_and_factory_opt_in():
    """声明拒绝硬矛盾, 宿主注入只匹配显式仅关键字参数"""
    from satrap.edictum.plugin import _invoke_factory

    for raw in (
        {"exports": {"servcies": ["x"]}},
        {"requires": {"self": True}},
        {"requires": {"x": True}, "conflicts": {"x": True}},
        {"requires": {"x": {"tools": ["a"]}}, "conflicts": {"x": {"tools": ["a"]}}},
    ):
        with pytest.raises(ValueError):
            parse_interop({"name": "self", "interop": raw})
    optional = parse_interop({"name": "self", "interop": {"requires": {"x": {"optional": True}}}})
    assert optional.requires["x"].optional
    marker = object()
    assert _invoke_factory(lambda *, plugin_host=None: plugin_host, None, None, None, plugin_host=marker) is marker
    assert _invoke_factory(lambda host="0.0.0.0": host, None, None, None, plugin_host=marker) == "0.0.0.0"
    assert _invoke_factory(lambda **kwargs: kwargs, None, None, None, plugin_host=marker) == {}


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_nested_services_and_command_authority(tmp_path, asynchronous):
    """服务工厂继续调用依赖, 命令复用保留入口来源且不能提升原生权限"""
    base = "AsyncService" if asynchronous else "Service"
    prefix, wait = ("async ", "await ") if asynchronous else ("", "")
    suffix = "async" if asynchronous else "sync"
    leaf = _plugin(tmp_path, "interop_leaf", {
        "interop": {"exports": {"services": ["identity"]}},
    }, services=f'''
        from satrap.edictum import {base}
        from satrap.core.call_context import current_call_origin

        class Identity({base}):
            service_name = "identity"
            description = "返回来源身份"
            {prefix}def execute(self):
                return current_call_origin().actor_id
    ''')
    relay = _plugin(tmp_path, "interop_relay", {
        "interop": {
            "exports": {"services": ["relay"], "commands": ["relay_command", "secure"]},
            "requires": {"interop_leaf": {"services": ["identity"]}},
        },
    }, services=f'''
        from satrap.edictum import {base}

        class Relay({base}):
            service_name = "relay"
            description = "继续调用依赖服务"
            def __init__(self, plugin_host):
                super().__init__()
                self.plugin_host = plugin_host
            {prefix}def execute(self):
                return {wait}self.plugin_host.plugins.services.call_{suffix}("interop_leaf", "identity", {{}})

        def get_services(session, *, plugin_host=None):
            return [Relay(plugin_host)]
    ''', commands=f'''
        def build_commands(session, *, plugin_host=None):
            {prefix}def relay_command():
                return {wait}plugin_host.plugins.services.call_{suffix}("interop_leaf", "identity", {{}})
            {prefix}def secure():
                return "native"
            secure.requires_native_scope = True
            return {{"relay_command": relay_command, "secure": secure}} if {not asynchronous!r} else ({{}}, {{"relay_command": relay_command, "secure": secure}})
    ''')
    consumer = _plugin(tmp_path, "interop_nested_consumer", {
        "interop": {"requires": {"interop_relay": {"services": ["relay"], "commands": ["relay_command", "secure"]}}},
    }, tools=f'''
        from satrap.core.utils.TCBuilder import {"AsyncTool" if asynchronous else "Tool"}

        class Entry({"AsyncTool" if asynchronous else "Tool"}):
            tool_name = "nested_entry"
            description = "嵌套调用入口"
            params_dict = {{}}
            def __init__(self, plugin_host):
                super().__init__()
                self.plugin_host = plugin_host
            {prefix}def execute(self):
                return "entry"

        def get_tools(session, *, plugin_host=None):
            return [Entry(plugin_host)]
    ''')
    llm = AsyncLLM(api_key="test", model="test") if asynchronous else LLM(api_key="test", model="test")
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    session = session_type("interop-nested", llm, db_path=str(tmp_path / "nested.db"), enable_checkpoint=False)
    try:
        for path in (leaf, relay, consumer):
            await _await(session.install_plugin(str(path)))
        runtime = session._plugin_interop
        host = session._wf.tools_manager.tools["nested_entry"].plugin_host
        origin = CallOrigin("test", "bot", "GroupMessage", "group", "alice", "msg", "request")
        with bind_call_origin(origin):
            with runtime.execution("interop_nested_consumer", "tools", "nested_entry"):
                assert await _await(getattr(host.plugins.services, f"call_{suffix}")("interop_relay", "relay", {})) == "alice"
                command = host.plugins.commands.find("interop_relay", "relay_command")
                assert await _await(getattr(command, f"run_{suffix}")([])) == "alice"
                secure = host.plugins.commands.find("interop_relay", "secure")
                with pytest.raises(PermissionError):
                    await _await(getattr(secure, f"run_{suffix}")([]))
                assert await _await(getattr(host.plugins.services, f"call_{suffix}")("interop_relay", "relay", {})) == "alice"
            registry = runtime.command_registry()
            assert await _await(registry._execute("secure", [])) == "native"
            assert await _await(registry._execute("relay_command", [])) == "alice"
    finally:
        for plugin in reversed(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))
        await _await(llm.client.close())


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_replacement_dependency_protection_and_conflict_rollback(tmp_path, asynchronous):
    """替代按增量保护既有依赖, 关系冲突在提交前拒绝且保留原开关"""
    base = "AsyncService" if asynchronous else "Service"
    prefix = "async " if asynchronous else ""

    def service_plugin(plugin_name, service_name, interop):
        return _plugin(tmp_path, plugin_name, {"interop": interop}, services=f'''
            from satrap.edictum import {base}
            class Implementation({base}):
                service_name = {service_name!r}
                description = "关系测试服务"
                {prefix}def execute(self):
                    return {plugin_name!r}
        ''')

    source = service_plugin("relation_source", "read", {"exports": {"services": ["read"]}})
    report = _plugin(tmp_path, "relation_report", {"interop": {"requires": {"relation_source": {"services": ["read"]}}}})
    replacement = service_plugin("relation_replacement", "read_new", {
        "replaces": {"relation_source": {"services": {"read": "read_new"}}},
    })
    second = service_plugin("relation_second", "other_read", {
        "replaces": {"relation_source": {"services": {"read": "other_read"}}},
    })
    cycle = service_plugin("relation_cycle", "cycle_read", {
        "requires": {"relation_source": {"services": ["read"]}},
        "replaces": {"relation_source": {"services": {"read": "cycle_read"}}},
    })
    conflict = service_plugin("relation_conflict", "own", {"conflicts": {"relation_source": {"services": ["read"]}}})
    llm = AsyncLLM(api_key="test", model="test") if asynchronous else LLM(api_key="test", model="test")
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    session = session_type("relation-flow", llm, db_path=str(tmp_path / "relations.db"), enable_checkpoint=False)
    try:
        source_handle = await _await(session.install_plugin(str(source)))
        report_handle = await _await(session.install_plugin(str(report)))
        runtime = session._plugin_interop
        with pytest.raises(PluginInteractionError) as error:
            await _await(session.install_plugin(str(replacement)))
        assert error.value.code == "replacement_breaks_dependency"
        assert "relation_replacement" not in runtime.bindings
        assert runtime.available("relation_source", "services", "read")
        assert await _await(session.disable_plugin("relation_report"))
        replacer = await _await(session.install_plugin(str(replacement)))
        assert not runtime.available("relation_source", "services", "read")
        assert source_handle.services["read"] is True
        with pytest.raises(PluginInteractionError) as error:
            await _await(session.enable_plugin("relation_report"))
        assert error.value.code == "replacement_breaks_dependency"
        assert report_handle.enabled is False
        with pytest.raises(PluginInteractionError) as error:
            await _await(session.install_plugin(str(second)))
        assert error.value.code == "replacement_conflict"
        assert "relation_second" not in runtime.bindings
        with pytest.raises(PluginInteractionError) as error:
            await _await(session.install_plugin(str(cycle)))
        assert error.value.code == "interaction_cycle"
        assert "relation_cycle" not in runtime.bindings
        assert await _await(session.disable_plugin("relation_replacement"))
        assert runtime.available("relation_source", "services", "read")
        assert await _await(session.enable_plugin("relation_report"))
        with pytest.raises(PluginInteractionError):
            await _await(session.enable_plugin("relation_replacement"))
        assert replacer.enabled is False

        assert source_handle.disable_service("read")
        assert await _await(session.enable_plugin("relation_replacement"))
        with pytest.raises(PluginInteractionError) as error:
            source_handle.enable_service("read")
        assert error.value.code == "replacement_breaks_dependency"
        assert source_handle.services["read"] is False
        await _await(session.disable_plugin("relation_replacement"))
        conflict_handle = await _await(session.install_plugin(str(conflict)))
        with pytest.raises(PluginInteractionError) as error:
            source_handle.enable_service("read")
        assert error.value.code == "plugin_conflict"
        assert conflict_handle.enabled and not source_handle.services["read"]
    finally:
        for plugin in reversed(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))
        await _await(llm.client.close())


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_full_configuration_order_counterfactual_and_update_rollback(tmp_path, asynchronous):
    """完整目标独立于配置顺序, 目标删名在实际构造核验失败后恢复整批旧配置"""
    from satrap.edictum.plugin_runtime import reconcile_plugin_states_async, preview_plugin_reconciliation
    from satrap.edictum.plugin_catalog import PluginCatalog
    from satrap.edictum.plugin_spec import parse_plugin_specs

    base, prefix = ("AsyncService", "async ") if asynchronous else ("Service", "")
    source_code = f'''
        from pathlib import Path
        Path(__file__).with_name("executed").write_text("loaded", encoding="utf-8")
        from satrap.edictum import {base}
        class Read({base}):
            service_name = "read"
            description = "原读取能力"
            {prefix}def execute(self):
                return "old"
    '''
    _plugin(tmp_path, "z_source", {"version": "1", "interop": {"exports": {"services": ["read"]}}}, services=source_code)
    _plugin(tmp_path, "a_report", {"interop": {"requires": {"z_source": {"services": ["read"]}}}})
    _plugin(tmp_path, "m_replacement", {"interop": {"replaces": {"z_source": {"services": {"read": "read_new"}}}}},
            services=source_code.replace('service_name = "read"', 'service_name = "read_new"'))
    catalog = PluginCatalog(tmp_path, tmp_path / "unused")
    llm = AsyncLLM(api_key="test", model="test") if asynchronous else LLM(api_key="test", model="test")
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    session = session_type("full-target", llm, db_path=str(tmp_path / "target.db"), enable_checkpoint=False)
    states = []

    async def apply(values):
        catalog.invalidate()
        specs = parse_plugin_specs(values, catalog, require_available=True)
        return await reconcile_plugin_states_async(states, specs, session.install_plugin, session.uninstall_plugin)

    try:
        planned = ["a_report", "z_source", {"name": "m_replacement", "enabled": False}]
        preview = preview_plugin_reconciliation(states, parse_plugin_specs(planned, catalog, require_available=True), session=session)
        assert preview and all(item["action"] != "blocked" for item in preview), preview
        assert not (tmp_path / "z_source" / "executed").exists()
        if asynchronous:
            _plugin(tmp_path, "waiting_consumer", {
                "interop": {"requires": {"z_source": {"services": ["read"]}}},
            }, mcp='''
                class Client:
                    def __init__(self, session):
                        self.session = session
                    async def register_tools(self, manager, name_prefix=None):
                        self.session.install_started.set()
                        await self.session.install_release.wait()
                        return []
                    async def close(self):
                        self.session.install_closed = True
                def build_clients(session):
                    return {"waiting": Client(session)}
            ''')
            session.install_started, session.install_release = asyncio.Event(), asyncio.Event()
            session.install_closed = False
            pending = asyncio.create_task(apply(["z_source", "waiting_consumer"]))
            await asyncio.wait_for(session.install_started.wait(), 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert session.install_closed
            assert not session.list_plugins() and not session._plugin_interop.bindings
        result = await apply(["a_report", "z_source", {"name": "m_replacement", "enabled": False}])
        assert result["ok"], result
        assert (tmp_path / "z_source" / "executed").exists()
        assert [plugin.name for plugin in session.list_plugins()] == ["z_source", "a_report"]
        result = await apply([{"name": "a_report", "enabled": False}, "z_source", "m_replacement"])
        assert result["ok"], result
        assert not session._plugin_interop.available("z_source", "services", "read")
        handles = {plugin.name: plugin for plugin in session.list_plugins()}
        preview = preview_plugin_reconciliation(states, parse_plugin_specs(
            ["a_report", "z_source", "m_replacement"], catalog, require_available=True), session=session)
        assert preview and all(item["action"] == "blocked" for item in preview), preview
        assert {plugin.name: plugin for plugin in session.list_plugins()} == handles
        result = await apply(["a_report", "z_source", "m_replacement"])
        assert not result["ok"]
        assert {plugin.name: plugin for plugin in session.list_plugins()} == handles

        result = await apply([
            {"name": "a_report", "enabled": False},
            {"name": "z_source", "capabilities": {"services": {"read": False}}},
            "m_replacement",
        ])
        assert result["ok"], result
        result = await apply([
            "a_report", {"name": "z_source", "capabilities": {"services": {"read": True}}},
            {"name": "m_replacement", "enabled": False},
        ])
        assert result["ok"], result
        assert session._plugin_interop.available("z_source", "services", "read")

        version_two = tmp_path / "v2_source"
        version_two.mkdir()
        (version_two / "meta.yaml").write_text(yaml.safe_dump({
            "name": "z_source", "version": "2", "interop": {"exports": {"services": ["renamed"]}},
        }), encoding="utf-8")
        (version_two / "services.py").write_text(textwrap.dedent(source_code).replace('service_name = "read"', 'service_name = "renamed"'), encoding="utf-8")
        result = await apply([{"name": "a_report", "enabled": False}, "z_source", "m_replacement"])
        assert not result["ok"], result
        assert result["rolled_back"] is True, result
        active = {plugin.name: plugin for plugin in session.list_plugins()}
        assert set(active) == {"z_source", "a_report"}
        assert active["z_source"].version == "1"
        assert active["z_source"].services == {"read": True}
        assert session._plugin_interop.available("z_source", "services", "read")
    finally:
        for plugin in reversed(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))
        await _await(llm.client.close())


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_skill_replacement_restores_resources_and_preserves_user_switch(tmp_path, monkeypatch, asynchronous):
    """技能替代回收旧指令和自带工具, 替代退出后仅恢复仍被用户启用的技能"""
    tool_base = "AsyncTool" if asynchronous else "Tool"
    prefix = "async " if asynchronous else ""

    def skill_plugin(plugin_name, skill_name, interop):
        return _plugin(tmp_path, plugin_name, {
            "skills": {skill_name: "公开技能"}, "interop": interop,
        }, skills=f'''
            from satrap.core.utils.TCBuilder import {tool_base}
            from satrap.core.utils.skills import Skill
            class Bundle({tool_base}):
                tool_name = {skill_name + "_bundle"!r}
                description = "技能自带工具"
                params_dict = {{}}
                {prefix}def execute(self):
                    return "ok"
            skills = [Skill(name={skill_name!r}, instructions={skill_name + " 指令"!r}, tools=[Bundle()])]
        ''')

    source = skill_plugin("skill_interop_source", "legacy_skill", {"exports": {"skills": ["legacy_skill"]}})
    replacement = skill_plugin("skill_interop_replacement", "new_skill", {
        "replaces": {"skill_interop_source": {"skills": {"legacy_skill": "new_skill"}}},
    })
    llm = AsyncLLM(api_key="test", model="test") if asynchronous else LLM(api_key="test", model="test")
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    session = session_type("skill-relations", llm, db_path=str(tmp_path / "skills.db"), enable_checkpoint=False)
    try:
        original = await _await(session.install_plugin(str(source)))
        workflow = session._wf
        await _await(workflow.ctx.add_user_message("保留已提交的用户消息"))
        assert "legacy_skill 指令" in workflow.ctx.get_context()[0]["content"]
        await _await(session.install_plugin(str(replacement)))
        assert "legacy_skill 指令" not in workflow.ctx.get_context()[0]["content"]
        assert "new_skill 指令" in workflow.ctx.get_context()[0]["content"]
        assert "legacy_skill_bundle" not in workflow.tools_manager.tools
        assert "new_skill_bundle" in workflow.tools_manager.tools
        assert original.skills["legacy_skill"] is True
        status = original.list_capabilities()["skills"][0]
        assert status["requested_enabled"] and not status["enabled"] and status["reason"] == "replaced_by"
        manager = session._skills_manager
        activate = manager.activate_async if asynchronous else manager.activate
        async def fail_restore_async(name, workflow):
            return False if name == "legacy_skill" else await activate(name, workflow)
        def fail_restore(name, workflow):
            return False if name == "legacy_skill" else activate(name, workflow)
        with monkeypatch.context() as failing:
            failing.setattr(manager, "activate_async" if asynchronous else "activate", fail_restore_async if asynchronous else fail_restore)
            with pytest.raises(RuntimeError, match="技能关系"):
                await _await(session.disable_plugin("skill_interop_replacement"))
            assert session._plugins["skill_interop_replacement"].enabled
            assert "new_skill 指令" in workflow.ctx.get_context()[0]["content"]
            assert "legacy_skill_bundle" not in workflow.tools_manager.tools
        assert await _await(session.disable_plugin("skill_interop_replacement"))
        assert "legacy_skill 指令" in workflow.ctx.get_context()[0]["content"]
        assert "new_skill 指令" not in workflow.ctx.get_context()[0]["content"]
        assert "legacy_skill_bundle" in workflow.tools_manager.tools
        assert "new_skill_bundle" not in workflow.tools_manager.tools
        assert await _await(original.disable_skill("legacy_skill"))
        assert await _await(session.enable_plugin("skill_interop_replacement"))
        assert await _await(session.disable_plugin("skill_interop_replacement"))
        assert "legacy_skill 指令" not in workflow.ctx.get_context()[0]["content"]
        assert original.skills["legacy_skill"] is False
        with monkeypatch.context() as failing:
            failing.setattr(manager, "activate_async" if asynchronous else "activate", fail_restore_async if asynchronous else fail_restore)
            with pytest.raises(RuntimeError, match="技能关系"):
                await _await(original.enable_skill("legacy_skill"))
            assert original.skills["legacy_skill"] is False
            assert "legacy_skill 指令" not in workflow.ctx.get_context()[0]["content"]
        assert [row["content"] for row in workflow.ctx.get_context() if row["role"] == "user"] == ["保留已提交的用户消息"]
    finally:
        for plugin in reversed(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))
        await _await(llm.client.close())

@pytest.mark.parametrize("asynchronous", [False, True])
async def test_metadata_mcp_and_artifact_service_flow(tmp_path, asynchronous):
    """公开资料只读查询, 真实 MCP 适配器及图片服务共用执行来源和撤销边界"""
    from dataclasses import replace
    from PIL import Image
    from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot

    base = "AsyncService" if asynchronous else "Service"
    tool_base = "AsyncTool" if asynchronous else "Tool"
    prefix, wait = ("async ", "await ") if asynchronous else ("", "")
    suffix = "async" if asynchronous else "sync"
    backend = _plugin(tmp_path, "media_backend", {
        "interop": {"exports": {"services": ["echo"], "tools": ["echo"], "handlers": ["trace"],
                                "skills": ["guide"], "mcp": ["lookup"]}},
    }, services=f'''
        from satrap.edictum import {base}
        class Echo({base}):
            service_name = "echo"
            description = "公开图片服务"
            def __init__(self, session, plugin_host):
                self.session, self.plugin_host = session, plugin_host
                self.calls = 0
            {prefix}def execute(self, mode: str = "image", payload: dict | None = None):
                self.calls += 1
                if mode == "type_error":
                    raise TypeError("body failed once")
                if mode == "count":
                    return self.calls
                if mode == "wait":
                    self.session.wait_started.set()
                    {"await " if asynchronous else ""}self.session.wait_release.wait()
                    return "released"
                if mode == "bad_return":
                    return object()
                if payload is not None:
                    payload["items"].append("provider")
                    return payload
                return {wait}self.plugin_host.artifacts.read_sandbox_image{"_async" if asynchronous else ""}("chart.png")
        def get_services(session, *, plugin_host=None):
            return [Echo(session, plugin_host)]
    ''', tools=f'''
        from satrap.core.utils.TCBuilder import {tool_base}
        class Echo({tool_base}):
            tool_name = "echo"
            description = "公开模型工具"
            params_dict = {{}}
            {prefix}def execute(self):
                return "model-tool"
    ''', handlers='''
        from satrap.edictum import SessionHandler
        def build_handlers(session, *, plugin_host=None):
            assert plugin_host is not None
            def before(text, ctx):
                session.trace_hits += 1
                return text
            return [SessionHandler("trace", priority=7, before_user_send=before)]
    ''', skills='''
        from satrap.core.utils.skills import Skill
        skills = [Skill("guide", "GUIDE CONTENT")]
    ''', mcp='''
        from types import SimpleNamespace
        from satrap.core.call_context import current_call_origin
        from satrap.core.utils.mcp.adapters import MCPToolAdapter, SyncMCPToolAdapter
        class Transport:
            async def call_tool(self, name, arguments=None):
                origin = current_call_origin()
                return SimpleNamespace(content=[SimpleNamespace(text=f"{name}:{origin.actor_id}")], isError=False)
        class Client:
            def __init__(self, session, plugin_host):
                self.session, self.plugin_host = session, plugin_host
            def adapter(self, prefix):
                tool = SimpleNamespace(name="lookup", description="离线 MCP 查询",
                                       inputSchema={"type": "object", "properties": {}})
                return MCPToolAdapter(Transport(), tool, name_prefix=prefix)
            async def register_tools(self, manager, name_prefix=None):
                adapter = self.adapter(name_prefix)
                manager.register_tool(adapter)
                return [adapter]
            def sync_register_tools(self, manager, name_prefix=None):
                adapter = SyncMCPToolAdapter(self.adapter(name_prefix), self.session.transport_loop)
                manager.register_tool(adapter)
                return [adapter]
            def sync_close(self):
                pass
            async def close(self):
                pass
        def build_clients(session, *, plugin_host=None):
            assert plugin_host is not None
            return {"lookup": Client(session, plugin_host)}
    ''')
    workflow = _plugin(tmp_path, "media_workflow", {
        "interop": {"requires": {"media_backend": {"optional": True, "services": ["echo"],
                    "tools": ["echo"], "handlers": ["trace"], "skills": ["guide"], "mcp": ["lookup"]},
                    "absent_backend": {"optional": True}}},
    }, tools=f'''
        from satrap.core.utils.TCBuilder import {tool_base}
        class Workflow({tool_base}):
            tool_name = "media_workflow"
            description = "完整插件交互链路"
            params_dict = {{"mode": ("string", "执行阶段")}}
            def __init__(self, plugin_host):
                super().__init__()
                self.plugin_host = plugin_host
                self.saved = None
                self.source = {{"items": ["consumer"]}}
            {prefix}def execute(self, mode: str):
                api = self.plugin_host.plugins
                if mode == "metadata":
                    handler = api.handlers.find("media_backend", "trace")
                    skill = api.skills.find("media_backend", "guide")
                    assert not hasattr(handler, "before_user_send")
                    assert not hasattr(skill, "tools")
                    assert api.find("absent_backend") is None
                    connection = api.mcp.find("media_backend", "lookup")
                    assert not hasattr(connection, "client")
                    assert connection.find("private") is None
                    result = {wait}connection.find("media_backend_lookup").call_{suffix}({{}})
                    return {{"handler": [handler.name, handler.priority, list(handler.stages)],
                            "skill": skill.content, "mcp": result,
                            "tool": {wait}api.tools.call_{suffix}("media_backend", "echo", {{}})}}
                if mode == "image":
                    self.saved = {wait}api.services.call_{suffix}("media_backend", "echo", {{}})
                    return len(self.saved.read())
                if mode == "data":
                    return {wait}api.services.call_{suffix}("media_backend", "echo", {{"payload": self.source}})
                if mode == "missing":
                    return {wait}api.services.call_{suffix}("media_backend", "echo", {{"unknown": True}})
                return {wait}api.services.call_{suffix}("media_backend", "echo", {{"mode": mode}})
        def get_tools(session, *, plugin_host=None):
            return [Workflow(plugin_host)]
    ''')
    llm = AsyncLLM(api_key="test", model="test") if asynchronous else LLM(api_key="test", model="test")
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    session = session_type("media-flow", llm, db_path=str(tmp_path / "media.db"), enable_checkpoint=False)
    session.transport_loop = asyncio.get_running_loop()
    session.trace_hits = 0
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    Image.new("RGB", (4, 4)).save(sandbox / "chart.png")
    bind_environment(session, replace(environment_snapshot(session), sandbox_root=sandbox))
    try:
        provider = await _await(session.install_plugin(str(backend)))
        await _await(session.install_plugin(str(workflow)))
        manager = session._wf.tools_manager
        history = list(session._wf.ctx.get_context())
        origin = CallOrigin("test", "bot", "GroupMessage", "group", "alice", "msg", "request")

        async def execute(mode):
            with bind_call_origin(origin):
                if asynchronous:
                    return await manager.execute_tool("media_workflow", {"mode": mode})
                return await asyncio.to_thread(manager.execute_tool, "media_workflow", {"mode": mode})

        result = await execute("metadata")
        assert result == {"handler": ["trace", 7, ["before_user_send"]],
                          "skill": "GUIDE CONTENT", "mcp": "lookup:alice", "tool": "model-tool"}, result
        assert session.trace_hits == 0
        assert await execute("image") == (sandbox / "chart.png").stat().st_size
        tool = manager.tools["media_workflow"]
        with pytest.raises(PluginInteractionError):
            tool.saved.read()
        assert await execute("data") == {"items": ["consumer", "provider"]}
        assert tool.source == {"items": ["consumer"]}
        before = provider._services["echo"].calls
        assert (await execute("missing"))["ok"] is False
        assert provider._services["echo"].calls == before
        assert (await execute("type_error"))["ok"] is False
        assert provider._services["echo"].calls == before + 1
        assert (await execute("bad_return"))["ok"] is False
        assert list(session._wf.ctx.get_context()) == history
        import threading
        session.wait_started = asyncio.Event() if asynchronous else threading.Event()
        session.wait_release = asyncio.Event() if asynchronous else threading.Event()
        pending = asyncio.create_task(execute("wait"))
        if asynchronous:
            await asyncio.wait_for(session.wait_started.wait(), 5)
        else:
            assert await asyncio.to_thread(session.wait_started.wait, 5)
        await _await(session.disable_plugin("media_backend"))
        await _await(session.enable_plugin("media_backend"))
        session.wait_release.set()
        assert (await asyncio.wait_for(pending, 5))["error_type"] == "capability_stale"
        assert await execute("data") == {"items": ["consumer", "provider"]}
        if asynchronous:
            session.wait_started, session.wait_release = asyncio.Event(), asyncio.Event()
            cancelled = asyncio.create_task(execute("wait"))
            await asyncio.wait_for(session.wait_started.wait(), 5)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert await execute("data") == {"items": ["consumer", "provider"]}
        assert provider.disable_mcp("lookup")
        assert tool.plugin_host.plugins.mcp.find("media_backend", "lookup") is None
        assert not any(item["function"]["name"] == "media_backend_lookup" for item in manager.get_tools_definitions())
        await _await(session.disable_plugin("media_backend"))
        assert tool.plugin_host.plugins.services.find("media_backend", "echo") is None
        await _await(session.enable_plugin("media_backend"))
        assert tool.plugin_host.plugins.services.find("media_backend", "echo") is not None
    finally:
        for plugin in reversed(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))
        await _await(llm.client.close())

@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("coding_first", [False, True])
async def test_official_plugin_replacement_and_rebindable_environment(tmp_path, monkeypatch, asynchronous, coding_first):
    """官方替代声明与安装顺序无关, 保留独立开关并复核项目改绑后的读写边界"""
    from dataclasses import replace
    from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot
    from satrap.expend.plugins.satrap_coding.state import get_plugin_state
    import satrap.core.utils.sandbox as sandbox_api
    import satrap.core.utils.sandbox.runner as runner

    llm = AsyncLLM(api_key="test", model="test") if asynchronous else LLM(api_key="test", model="test")
    session_type = AsyncSimpleSession if asynchronous else SimpleSession
    session = session_type("official-env", llm, db_path=str(tmp_path / "official.db"), enable_checkpoint=False)
    project_a, project_b = tmp_path / "project-a", tmp_path / "project-b"
    for project in (project_a, project_b):
        project.mkdir()
        (project / "document.txt").write_text(project.name, encoding="utf-8")
    sandbox_a, sandbox_b = tmp_path / "sandbox-a", tmp_path / "sandbox-b"
    bind_environment(session, replace(environment_snapshot(session), workspace_root=project_a,
                                     sandbox_root=sandbox_a, session_root=tmp_path / "session", project="a"))
    plugins = Path(__file__).resolve().parents[2] / "satrap/expend/plugins"
    try:
        order = ("satrap_coding", "base_take") if coding_first else ("base_take", "satrap_coding")
        installed = {}
        for name in order:
            installed[name] = await _await(session.install_plugin(str(plugins / name)))
        manager = session._wf.tools_manager
        def names():
            return {item["function"]["name"] for item in manager.get_tools_definitions()}
        assert {"shell", "read_document"} <= names()
        assert "code_sandbox" not in names()
        status = next(item for item in installed["base_take"].list_capabilities()["tools"] if item["name"] == "code_sandbox")
        assert status["requested_enabled"] and not status["enabled"]
        assert status["reason"] == "replaced_by" and status["replaced_by"] == "satrap_coding.tools.shell"
        rejected = await _await(manager.execute_tool("code_sandbox", {"operation": "list"}))
        assert rejected["ok"] is False
        await _await(session.disable_plugin("satrap_coding"))
        assert "code_sandbox" in names()
        await _await(session.enable_plugin("satrap_coding"))
        assert "code_sandbox" not in names()
        installed["base_take"].disable_tool("code_sandbox")
        await _await(session.disable_plugin("satrap_coding"))
        assert "code_sandbox" not in names()
        assert installed["base_take"].tools["code_sandbox"] is False
        installed["base_take"].enable_tool("code_sandbox")
        assert "code_sandbox" in names()
        await _await(session.enable_plugin("satrap_coding"))
        assert "code_sandbox" not in names()
        await _await(session.uninstall_plugin("satrap_coding"))
        assert "code_sandbox" in names()
        installed["satrap_coding"] = await _await(session.install_plugin(str(plugins / "satrap_coding")))
        assert "code_sandbox" not in names()
        installed["satrap_coding"].disable_tool("shell")
        assert "code_sandbox" in names()
        engine = get_plugin_state(session)["engine"]
        engine.set_mode("full-access", persist=False)
        engine.set_plan_mode(True)

        async def execute(name, arguments):
            if asynchronous:
                return await manager.execute_tool(name, arguments)
            return await asyncio.to_thread(manager.execute_tool, name, arguments)

        assert (await execute("code_sandbox", {"operation": "save", "path": "marker.txt", "code": "first"}))["returncode"] == 0
        assert (sandbox_a / "marker.txt").read_text(encoding="utf-8") == "first"
        assert "project-a" in await execute("read_document", {"path": "document.txt"})
        before = environment_snapshot(session)
        bind_environment(session, replace(before, workspace_root=project_b, sandbox_root=sandbox_b, project="b"))
        assert environment_snapshot(session).revision > before.revision
        assert "project-b" in await execute("read_document", {"path": "document.txt"})
        assert "错误" in await execute("read_document", {"path": str(project_a / "document.txt")})
        missing = await execute("code_sandbox", {"operation": "read", "path": "marker.txt"})
        assert missing["returncode"] != 0
        assert (await execute("code_sandbox", {"operation": "save", "path": "marker.txt", "code": "second"}))["returncode"] == 0
        assert (sandbox_b / "marker.txt").read_text(encoding="utf-8") == "second"
        blocked = await execute("code_sandbox", {"operation": "run", "code": "import socket"})
        assert blocked["returncode"] != 0
        assert blocked["error_code"] == "module_not_allowed", blocked
        original_run = runner.run if asynchronous else sandbox_api.run
        def rebind_before_worker(*args, **kwargs):
            bind_environment(session, replace(environment_snapshot(session), project="rebound-same-path"))
            return original_run(*args, **kwargs)
        monkeypatch.setattr(runner if asynchronous else sandbox_api, "run", rebind_before_worker)
        stale = await execute("code_sandbox", {"operation": "save", "path": "stale.txt", "code": "never"})
        assert stale["error_type"] == "capability_stale", stale
        assert not (sandbox_b / "stale.txt").exists()
        reader = manager.tools["read_file"]
        original_read = reader._execute
        def rebind_during_read(*args, **kwargs):
            text = original_read(*args, **kwargs)
            bind_environment(session, replace(environment_snapshot(session), project="changed-during-read"))
            return text
        monkeypatch.setattr(reader, "_execute", rebind_during_read)
        stale_read = await execute("read_file", {"path": "document.txt"})
        assert stale_read["error_type"] == "capability_stale", stale_read
        assert not hasattr(session, "coding_workspace_root") and not hasattr(session, "coding_sandbox_root")
    finally:
        for plugin in reversed(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))
        await _await(llm.client.close())
