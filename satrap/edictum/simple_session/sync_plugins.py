from __future__ import annotations
from pathlib import Path
from .recovery import plugin_fingerprint
from satrap.edictum.plugin_compatibility import check_plugin_compatibility
from typing import Any
from satrap.edictum.plugin_config import parse_config_schema, schema_to_payload
from satrap.edictum.plugin_settings import (
    EffectivePluginConfig,
    resolve_session_plugin_config,
)
from satrap.edictum.plugin_resources import PluginResources, MODEL_TYPES
from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.utils.TCBuilder import Tool
from satrap.edictum.plugin import (
    Plugin,
    collect_cleanup,
    collect_commands,
    collect_handlers,
    collect_mcp_clients,
    collect_skills,
    collect_tools,
    load_plugin_meta,
    parse_capability_descriptions,
)
from satrap.core.type import safe_getattr_callable
from satrap.core.log import logger
from .utils import (
    _assert_sync_callbacks,
    _command_intro,
    _add_plugin_sys_path,
    _remove_plugin_sys_path,
    _warn_undeclared_capabilities,
    SessionHandler,
)
from .utils import _plugin_config_manager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sync import SimpleSession


def install_plugin(
    self: SimpleSession, path: str, config: dict[str, Any] | None = None
) -> Plugin:
    """
    安装目录插件 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py)

    参数:
    - path: 路径
    - config: 配置信息

    mcp.py 同样支持: 远端工具经后台事件循环线程桥接为同步适配器注册;
    任一步失败时回滚已注册能力与 sys.path, 不留孤儿

    config: 会话级插件配置覆盖, 与全局配置合成后注入 get_tools 工厂

    返回:
    - Plugin: 安装目录插件 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py)
    """
    plugin_dir = Path(path)
    meta = load_plugin_meta(plugin_dir)
    check_plugin_compatibility(meta, self.plugin_environment).require()
    _add_plugin_sys_path(plugin_dir)
    tool_states: dict[str, bool] = {}
    skill_states: dict[str, bool] = {}
    handler_states: dict[str, bool] = {}
    command_states: dict[str, bool] = {}
    mcp_states: dict[str, bool] = {}
    mcp_clients: dict[str, tuple[Any, list[Any]]] = {}
    resources = None
    try:
        name = str(meta.get("name") or "").strip()
        if not name:
            raise ValueError(f"插件 {path} 的 meta.yaml 缺少 name")
        with self._registry_lock:
            if name in self._plugins:
                raise ValueError(f"插件 {name} 已安装")

        config_schema = parse_config_schema(meta)
        # 合成插件配置: schema.default < 全局 json < 会话覆盖
        if isinstance(config, EffectivePluginConfig):
            plugin_config = dict(config)
        else:
            plugin_config = _plugin_config_manager().resolve(
                name, config_schema, config
            )
            plugin_config = resolve_session_plugin_config(
                self, name, config_schema, plugin_config
            )
        model_manager = getattr(self, "plugin_model_manager", None)
        if model_manager is None and any(
            item.type in MODEL_TYPES for item in config_schema.values()
        ):
            model_manager = ModelConfigManager(auto_create=False)
        resources = PluginResources(
            model_manager, config_schema, plugin_config, async_=False
        )

        for t in collect_tools(plugin_dir, name, Tool, self, plugin_config, resources):
            tname = t.get_tool_name()
            if tname in self._wf.tools_manager.tools or tname in tool_states:
                raise ValueError(f"插件 {name} 的工具 {tname} 与已注册工具冲突")
            t.owner_plugin = name
            self._wf.tools_manager.register_tool(t)
            tool_states[tname] = True

        mgr = self._get_skills_manager()
        for s in collect_skills(plugin_dir, name):
            key = s.skill_id or s.name
            if key in mgr.skills or key in skill_states:
                raise ValueError(f"插件 {name} 的技能 {key} 与已加载技能冲突")
            mgr._register_skill(s)
            skill_states[key] = True

        for h in collect_handlers(
            plugin_dir, name, self, SessionHandler, plugin_config, resources
        ):
            self._validate_handler(h)
            _assert_sync_callbacks(h)
            with self._registry_lock:
                if h.name in self._handlers or h.name in handler_states:
                    raise ValueError(
                        f"插件 {name} 的处理器 {h.name} 与已注册处理器冲突"
                    )
                h.owner_plugin = name
                self._handler_seq += 1
                h._seq = self._handler_seq
                self._handlers[h.name] = h
            handler_states[h.name] = True

        sync_commands, _async_commands = collect_commands(
            plugin_dir, name, self, plugin_config, resources
        )
        for cname, chandler in sync_commands.items():
            if cname in self.cmd_handler.commands or cname in command_states:
                raise ValueError(f"插件 {name} 的命令 {cname} 与已注册命令冲突")
            self.cmd_handler.register_command(
                cname, chandler, intro=_command_intro(chandler)
            )
            command_states[cname] = True

        for mcp_name, client in collect_mcp_clients(plugin_dir, name).items():
            if mcp_name in self._mcp_clients or mcp_name in mcp_states:
                raise ValueError(f"插件 {name} 的 MCP 连接 {mcp_name} 与已接入连接冲突")
            try:
                adapters = client.sync_register_tools(
                    self._wf.tools_manager, name_prefix=name
                )
            except Exception:
                close = safe_getattr_callable(client, "sync_close")
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
                raise
            mcp_clients[mcp_name] = (client, list(adapters))
            mcp_states[mcp_name] = True

        plugin = Plugin(
            recovery_fingerprint=plugin_fingerprint(plugin_dir, plugin_config, model_manager, config_schema),
            name=name,
            version=str(meta.get("version") or ""),
            author=str(meta.get("author") or ""),
            repo=str(meta.get("repo") or ""),
            description=str(meta.get("description") or ""),
            path=str(plugin_dir),
        )
        plugin.resources = resources
        plugin._session = self
        plugin._cleanup = collect_cleanup(plugin_dir, name, self)
        plugin.tools = tool_states
        plugin.skills = skill_states
        plugin.mcp = mcp_states
        plugin._mcp_clients = mcp_clients
        plugin.handlers = handler_states
        plugin.commands = command_states
        # meta.yaml 能力声明: 读入描述并校验未匹配项 (仅警告, 不改变安装行为)
        plugin.capability_descriptions = parse_capability_descriptions(meta)
        plugin.config_schema = schema_to_payload(config_schema)
        _warn_undeclared_capabilities(
            name,
            plugin.capability_descriptions,
            {
                "tools": tool_states,
                "skills": skill_states,
                "mcp": mcp_states,
                "handlers": handler_states,
                "commands": command_states,
            },
        )
        with self._registry_lock:
            self._plugins[name] = plugin
        logger.info(f"[edictum] 插件 {name} 已安装")
        return plugin
    except Exception:
        if resources is not None:
            resources.close()
        _remove_plugin_sys_path(plugin_dir)
        for tname in tool_states:
            self._wf.tools_manager.unregister_tool(tname)
        mgr = self._skills_manager
        if mgr is not None:
            for key in skill_states:
                mgr.unregister_skill(key)
        for hname in handler_states:
            with self._registry_lock:
                handler = self._take_handler_locked(hname)
            if handler is not None:
                self._close_handler(hname, handler)
        for cname in command_states:
            self.cmd_handler.unregister_command(cname)
        for mcp_name, (client, adapters) in mcp_clients.items():
            for adapter in adapters:
                self._wf.tools_manager.unregister_tool(adapter.get_tool_name())
            close = safe_getattr_callable(client, "sync_close")
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        raise


def uninstall_plugin(self: SimpleSession, name: str) -> bool:
    """
    卸载插件: 回收其全部能力 (含清理回调), 不留孤儿

    参数:
    - name: 名称

    返回:
    - bool: 卸载插件: 回收其全部能力 (含清理回调), 不留孤儿
    """
    with self._registry_lock:
        plugin = self._plugins.pop(name, None)
    if plugin is None:
        return False
    _remove_plugin_sys_path(Path(plugin.path))
    for tname in plugin.tools:
        self._wf.tools_manager.unregister_tool(tname)
    mgr = self._skills_manager
    if mgr is not None:
        for sname in plugin.skills:
            mgr.deactivate(sname, self._wf)
            mgr.unregister_skill(sname)
    for hname in list(plugin.handlers):
        with self._registry_lock:
            handler = self._take_handler_locked(hname)
        if handler is not None:
            self._close_handler(hname, handler)
    for cname in plugin.commands:
        self.cmd_handler.unregister_command(cname)
    for mcp_name, (client, adapters) in plugin._mcp_clients.items():
        for adapter in adapters:
            self._wf.tools_manager.unregister_tool(adapter.get_tool_name())
        close = safe_getattr_callable(client, "sync_close")
        if close is not None:
            try:
                close()
            except Exception as e:
                logger.warning(f"[edictum] 插件 {name} MCP 断开失败: {e}")
    cleanup = plugin._cleanup
    if cleanup is not None:
        try:
            cleanup(self)
        except Exception as e:
            logger.warning(f"[edictum] 插件 {name} 清理回调失败: {e}")
    if plugin.resources is not None:
        plugin.resources.close()
    logger.info(f"[edictum] 插件 {name} 已卸载")
    return True


def enable_plugin(self: SimpleSession, name: str) -> bool:
    """
    启用插件 (按独立状态恢复名下能力, 独立停用的保持停用)

    参数:
    - name: 名称

    返回:
    - bool: 启用插件 (按独立状态恢复名下能力, 独立停用的保持停用)
    """
    with self._registry_lock:
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        check_plugin_compatibility(load_plugin_meta(Path(plugin.path)), self.plugin_environment).require()
        if plugin.enabled:
            return True
        plugin.enabled = True
    # 工具/处理器由执行路径合成承担 (effectiveness_guard / _enabled_handlers), 无需恢复循环
    mgr = self._skills_manager
    if mgr is not None:
        for sname, st in plugin.skills.items():
            if st:
                mgr.activate(sname, self._wf)
    for cname, st in plugin.commands.items():
        if st:
            self.cmd_handler.enable_command(cname)
    return True


def disable_plugin(self: SimpleSession, name: str) -> bool:
    """
    停用插件 (压制名下全部能力, 不改独立状态; 工具/处理器由执行路径合成压制)

    参数:
    - name: 名称

    返回:
    - bool: 停用插件 (压制名下全部能力, 不改独立状态; 工具/处理器由执行路径合成压制)
    """
    with self._registry_lock:
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if not plugin.enabled:
            return True
        plugin.enabled = False
    mgr = self._skills_manager
    if mgr is not None:
        for sname in plugin.skills:
            mgr.deactivate(sname, self._wf)
    for cname in plugin.commands:
        self.cmd_handler.disable_command(cname)
    return True


def _activate_plugin_skill(self: SimpleSession, name: str) -> bool:
    """
    插件技能独立启用钩子 (同步版)

    参数:
    - name: 名称

    返回:
    - bool: 插件技能独立启用钩子 (同步版)
    """
    return self._get_skills_manager().activate(name, self._wf)


def _deactivate_plugin_skill(self: SimpleSession, name: str) -> bool:
    """
    插件技能独立停用钩子 (同步版)

    参数:
    - name: 名称

    返回:
    - bool: 插件技能独立停用钩子 (同步版)
    """
    return self._get_skills_manager().deactivate(name, self._wf)
