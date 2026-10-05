"""Coding 配置经真实工厂与安装生命周期生效的回归测试"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from typing import Any

import pytest

from satrap.expend.plugins.satrap_coding import tools as coding
from satrap.expend.plugins.satrap_coding.commands import build_commands
from satrap.expend.plugins.satrap_coding.handlers import build_handlers
from satrap.expend.plugins.satrap_coding.state import get_plugin_state, reset_plugin_state
from satrap.expend.plugins.satrap_coding.tools import async_interaction, sync_interaction
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.edictum import AsyncSimpleSession, HandlerConfig, HandlerContext, SimpleSession

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap/expend/plugins/satrap_coding"


async def _execute(tool: Any, *args: Any, **kwargs: Any) -> str:
    """
    执行同步或异步测试工具

    参数:
    - tool: 真实工具实例
    - args: 工具位置参数
    - kwargs: 工具关键字参数

    返回:
    - str: 工具执行结果
    """
    result = tool.execute(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_factory_configuration_isolated_without_host_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    """
    无宿主路径时, 交错构造和修改原配置均不改变工具路径与默认超时

    参数:
    - tmp_path: 文件与状态的隔离目录
    - monkeypatch: 替换 Shell 进程入口
    - asynchronous: 是否构造异步工具
    """
    def embedded_session(name: str) -> Any:
        session = object.__new__(AsyncSimpleSession if asynchronous else SimpleSession)
        session.session_id = name
        setattr(session, "_wf", SimpleNamespace(llm=None, tools_manager=None))
        setattr(session, "session_ctx", SimpleNamespace(db_path=str(tmp_path / "chat.db")))
        return session

    sessions = [embedded_session(name) for name in ("a", "b", "empty", "none")]
    configs: list[dict[str, Any] | None] = [
        {"workspace_root": str(tmp_path / "a"), "sandbox_root": str(tmp_path / "a/private"),
         "data_root": str(tmp_path / "state-a"), "shell_timeout": 7},
        {"workspace_root": str(tmp_path / "b"), "sandbox_root": str(tmp_path / "b/private"),
         "data_root": str(tmp_path / "state-b"), "shell_timeout": 11},
        {}, None,
    ]
    calls: list[tuple[Path, int]] = []

    def run_shell(args: Any, cwd: Path, timeout: int, allow: Any = ()) -> str:
        calls.append((cwd, timeout))
        return "executed"

    monkeypatch.setattr(coding, "_run_shell", run_shell)
    module = async_interaction if asynchronous else sync_interaction
    monkeypatch.setattr(module, "_prepare_shell", lambda command, shell, root, cwd: ([], 0, "test"))
    try:
        maps = []
        for session, config in zip(sessions, configs):
            session.user_input_provider = lambda *args: "y"
            maps.append({tool.tool_name: tool for tool in coding.get_tools(session, config)})
        first_config = configs[0]
        assert first_config is not None
        first_config["workspace_root"] = str(tmp_path / "b")
        for index in (0, 1, 2, 3, 0):
            expected = (tmp_path / ("a" if index == 0 else "b")).resolve() if index < 2 else coding.WORKSPACE_ROOT.resolve()
            tool = maps[index]["shell"]
            assert coding._tool_root(tool) == expected
            assert await _execute(tool, "test") == "executed"
            assert calls[-1] == (expected, (7, 11, 120, 120)[index])
            assert f"默认 {(7, 11, 120, 120)[index]}" in tool.params_dict["timeout"][1]
            definition = tool.get_tool_defined()["function"]["parameters"]
            assert definition["required"] == ["command"]
            assert definition["properties"]["timeout"]["default"] == (7, 11, 120, 120)[index]
            assert definition["properties"]["timeout"]["type"] == "integer"
            before = len(calls)
            assert "错误" in await _execute(tool, "test", timeout=0)
            assert len(calls) == before
            assert await _execute(tool, "test", timeout=19) == "executed"
            assert calls[-1][1] == 19
        for index in (0, 1, 0):
            root = tmp_path / ("a" if index == 0 else "b")
            (root / "private").mkdir(parents=True, exist_ok=True)
            (root / "source.txt").write_text(root.name, encoding="utf-8")
            assert root.name in await _execute(maps[index]["read_file"], "source.txt")
            sessions[index].user_input_provider = None
            assert "已写入" in await _execute(maps[index]["write_file"], "private/result.txt", root.name)
            assert "需要用户批准" in await _execute(maps[index]["write_file"], "outside.txt", "blocked")
            assert not (root / "outside.txt").exists()
            assert (root / "private/result.txt").read_text(encoding="utf-8") == root.name
        assert Path(get_plugin_state(sessions[0])["_data_root"]) == (tmp_path / "state-a").resolve()
        assert Path(get_plugin_state(sessions[1])["_data_root"]) == (tmp_path / "state-b").resolve()
    finally:
        for session in sessions:
            reset_plugin_state(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_real_install_state_roots_commands_plan_and_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    """
    真实安装保持状态根, 命令和处理器一致, 卸载后可切换数据目录

    参数:
    - tmp_path: 文件与数据库的隔离目录
    - monkeypatch: 隔离全局插件配置文件
    - asynchronous: 是否使用异步会话
    """
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    base, llm_type = (AsyncSimpleSession, AsyncLLM) if asynchronous else (SimpleSession, LLM)
    session = base("same-id", MagicMock(spec=llm_type), db_path=str(tmp_path / "chat.db"))
    workspace = tmp_path / "workspace"
    (workspace / "private").mkdir(parents=True)
    config = {"workspace_root": str(workspace), "sandbox_root": str(workspace / "private"),
              "data_root": str(tmp_path / "state"), "shell_timeout": 7}

    async def install(cfg: dict[str, Any]) -> None:
        result = session.install_plugin(str(PLUGIN_DIR), config=cfg)
        if inspect.isawaitable(result):
            await result

    async def uninstall() -> None:
        result = session.uninstall_plugin("satrap_coding")
        assert (await result if inspect.isawaitable(result) else result) is True

    async def command(text: str) -> str:
        manager = session.command_handler if isinstance(session, AsyncSimpleSession) else session.cmd_handler
        result = manager.process_message(text)
        value, is_command = await result if inspect.isawaitable(result) else result
        assert is_command
        return str(value)

    await install(config)
    try:
        state = get_plugin_state(session)
        assert Path(state["_data_root"]) == (tmp_path / "state").resolve()
        tool = session.tools_manager.tools["write_file"]
        assert isinstance(tool, (coding.WriteFileTool, coding.AsyncWriteFileTool))
        assert tool.engine is state["engine"]
        assert "目标已设置" in await command("/goal shared-goal")
        assert (tmp_path / "state/goal.json").is_file()
        handler = session._handlers["satrap_coding.inject"]
        callback = handler.before_user_send
        assert callback is not None
        context = HandlerContext(HandlerConfig("message", None, "off", 1, "test"), "message")
        injected = callback("message", context)
        assert isinstance(injected, str) and "shared-goal" in injected
        assert "已切换" in await command("/approve mode full")
        assert (tmp_path / "state/permissions.json").is_file()
        assert "计划模式" in await command("/plan on")
        assert "计划模式" in await _execute(tool, "private/new.txt", "blocked")
        assert not (workspace / "private/new.txt").exists()
        assert "退出" in await command("/plan off")
        assert "已写入" in await _execute(tool, "outside.txt", "approved")
        assert (tmp_path / "state/approval_log.jsonl").is_file()
        await uninstall()
        await install({**config, "data_root": str(tmp_path / "new-state")})
        assert get_plugin_state(session) is not state
        replacement = session.tools_manager.tools["write_file"]
        assert isinstance(replacement, (coding.WriteFileTool, coding.AsyncWriteFileTool))
        assert not replacement.engine.plan_mode
        assert "shared-goal" not in await command("/goal status")
        assert not get_plugin_state(session)["goals"].to_context_block(session.session_id)
        assert Path(get_plugin_state(session)["_data_root"]) == (tmp_path / "new-state").resolve()
    finally:
        if session.list_plugins():
            await uninstall()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("host_paths", [False, True], ids=["configured", "host"])
@pytest.mark.parametrize("order", [("tools", "commands", "handlers"), ("commands", "handlers", "tools"), ("handlers", "tools", "commands")])
async def test_factory_order_and_host_path_priority(
    tmp_path: Path, order: tuple[str, ...], asynchronous: bool, host_paths: bool,
) -> None:
    """
    工厂顺序不改变共享状态, 宿主路径优先级在两类会话中一致

    参数:
    - tmp_path: 路径与数据库的隔离目录
    - order: 工具, 命令和处理器工厂的调用顺序
    - asynchronous: 是否使用异步会话
    - host_paths: 是否注入宿主路径
    """
    base, llm_type = (AsyncSimpleSession, AsyncLLM) if asynchronous else (SimpleSession, LLM)
    session = base("order", MagicMock(spec=llm_type), db_path=str(tmp_path / "chat.db"))
    if isinstance(session, AsyncSimpleSession):
        await session.initialize()
    if host_paths:
        setattr(session, "coding_workspace_root", str(tmp_path / "host-workspace"))
        setattr(session, "coding_sandbox_root", str(tmp_path / "host-sandbox"))
        setattr(session, "coding_cache_root", str(tmp_path / "host-cache"))
    config = {"workspace_root": str(tmp_path / "configured-workspace"), "sandbox_root": str(tmp_path / "configured-sandbox"), "data_root": str(tmp_path / "configured-data")}
    builders = {"tools": coding.get_tools, "commands": build_commands, "handlers": build_handlers}
    try:
        results = {name: builders[name](session, config) for name in order}
        state = get_plugin_state(session)
        expected_data = tmp_path / ("host-cache/satrap_coding" if host_paths else "configured-data")
        expected_workspace = tmp_path / ("host-workspace" if host_paths else "configured-workspace")
        assert Path(state["_data_root"]) == expected_data.resolve()
        tool = next(tool for tool in results["tools"] if tool.tool_name == "shell")
        assert coding._tool_root(tool) == expected_workspace.resolve()
        assert tool.engine is state["engine"]
        if host_paths:
            expected_workspace.mkdir()
            setattr(session, "coding_sandbox_root", str(expected_workspace))
            write_tool = next(tool for tool in results["tools"] if tool.tool_name == "write_file")
            assert "已写入" in await _execute(write_tool, "host.txt", "host")
            assert (expected_workspace / "host.txt").read_text(encoding="utf-8") == "host"
        sync_commands, async_commands = results["commands"]
        if asynchronous:
            await async_commands["plan"]("on")
        else:
            sync_commands["plan"]("on")
        assert tool.engine.plan_mode
        with pytest.raises(ValueError, match="卸载"):
            get_plugin_state(session, tmp_path / "other-data")
        assert tool.engine is get_plugin_state(session)["engine"]
    finally:
        reset_plugin_state(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_failed_install_clears_state_and_duplicate_install_preserves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    """
    失败安装释放状态, 重复安装不破坏已加载插件

    参数:
    - tmp_path: 插件数据与数据库的隔离目录
    - monkeypatch: 隔离全局插件配置
    - asynchronous: 是否使用异步安装
    """
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager
    from satrap.expend.plugins.satrap_coding import state as state_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    base, llm_type = (AsyncSimpleSession, AsyncLLM) if asynchronous else (SimpleSession, LLM)
    session = base("retry", MagicMock(spec=llm_type), db_path=str(tmp_path / "chat.db"))
    manager = session.command_handler if isinstance(session, AsyncSimpleSession) else session.cmd_handler
    manager.register_command("goal", lambda *args: "existing")

    async def install(root: str) -> None:
        result = session.install_plugin(str(PLUGIN_DIR), config={"data_root": str(tmp_path / root)})
        if inspect.isawaitable(result):
            await result

    with pytest.raises(ValueError, match="命令.*冲突"):
        await install("failed-state")
    assert session.list_tools() == []
    assert session.list_plugins() == []
    assert id(session) not in state_mod._registry
    manager.unregister_command("goal")
    await install("retry-state")
    try:
        state = get_plugin_state(session)
        state["engine"].set_plan_mode(True)
        with pytest.raises(ValueError, match="已安装"):
            await install("different-state")
        assert get_plugin_state(session) is state
        assert state["engine"].plan_mode
        assert Path(state["_data_root"]) == (tmp_path / "retry-state").resolve()
    finally:
        result = session.uninstall_plugin("satrap_coding")
        if inspect.isawaitable(result):
            await result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("tool_name,args", [
    ("write_file", ("file.txt", "changed")),
    ("edit_file", ("file.txt", "original", "changed")),
    ("search_replace", ("file.txt", [{"old": "original", "new": "changed"}])),
])
@pytest.mark.parametrize("change", ["workspace", "plan"])
async def test_file_approval_rechecks_execution_context(
    tmp_path: Path, asynchronous: bool, tool_name: str, args: tuple[Any, ...], change: str,
) -> None:
    """
    审批期间改绑工作区或进入计划模式时, 所有文件写工具取消执行

    参数:
    - tmp_path: 目标文件与数据库的隔离目录
    - asynchronous: 是否使用异步工具
    - tool_name: 文件写工具名称
    - args: 对应的执行参数
    - change: 审批回调修改的上下文
    """
    base, llm_type = (AsyncSimpleSession, AsyncLLM) if asynchronous else (SimpleSession, LLM)
    session = base("approval", MagicMock(spec=llm_type), db_path=str(tmp_path / "chat.db"))
    if isinstance(session, AsyncSimpleSession):
        await session.initialize()
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "file.txt"
    target.write_text("original", encoding="utf-8")
    tools = {tool.tool_name: tool for tool in coding.get_tools(session, {"workspace_root": str(root)})}
    engine = get_plugin_state(session)["engine"]

    def approve(*args: Any) -> str:
        if change == "workspace":
            setattr(session, "coding_workspace_root", str(tmp_path / "other"))
        else:
            engine.set_plan_mode(True)
        return "y"

    session.user_input_provider = approve
    try:
        assert "执行已取消" in await _execute(tools[tool_name], *args)
        assert target.read_text(encoding="utf-8") == "original"
    finally:
        reset_plugin_state(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("tool_name,args", [
    ("write_file", ("file.txt", "changed")),
    ("edit_file", ("file.txt", "original", "changed")),
    ("search_replace", ("file.txt", [{"old": "original", "new": "changed"}])),
])
async def test_plan_mode_blocks_all_sandbox_file_writes(
    tmp_path: Path, asynchronous: bool, tool_name: str, args: tuple[Any, ...],
) -> None:
    """
    计划模式优先于沙箱免审批, 同步与异步文件写入均被拒绝

    参数:
    - tmp_path: 沙箱目标与数据库的隔离目录
    - asynchronous: 是否使用异步工具
    - tool_name: 文件写工具名称
    - args: 对应的执行参数
    """
    base, llm_type = (AsyncSimpleSession, AsyncLLM) if asynchronous else (SimpleSession, LLM)
    session = base("sandbox-plan", MagicMock(spec=llm_type), db_path=str(tmp_path / "chat.db"))
    if isinstance(session, AsyncSimpleSession):
        await session.initialize()
    target = tmp_path / "file.txt"
    target.write_text("original", encoding="utf-8")
    config = {"workspace_root": str(tmp_path), "sandbox_root": str(tmp_path)}
    tools = {tool.tool_name: tool for tool in coding.get_tools(session, config)}
    get_plugin_state(session)["engine"].set_plan_mode(True)
    try:
        assert "计划模式" in await _execute(tools[tool_name], *args)
        assert target.read_text(encoding="utf-8") == "original"
    finally:
        reset_plugin_state(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_host_sandbox_inside_system_data_remains_usable_and_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    """
    平台默认数据目录内的当前会话沙箱可用, 其他运行数据仍受保护

    参数:
    - tmp_path: 模拟系统数据根与数据库的隔离目录
    - monkeypatch: 将系统保护根定向到模拟存储布局
    - asynchronous: 是否使用异步文件工具
    """
    base, llm_type = (AsyncSimpleSession, AsyncLLM) if asynchronous else (SimpleSession, LLM)
    session = base("platform", MagicMock(spec=llm_type), db_path=str(tmp_path / "chat.db"))
    if isinstance(session, AsyncSimpleSession):
        await session.initialize()
    system_root = tmp_path / ".satrap"
    sandbox = system_root / "data/platforms/p/sessions/current/sandbox"
    sandbox.mkdir(parents=True)
    monkeypatch.setattr(coding, "_SYSTEM_PROTECTED_ROOT", system_root.resolve())
    setattr(session, "coding_workspace_root", str(sandbox))
    setattr(session, "coding_sandbox_root", str(sandbox))
    tools = {tool.tool_name: tool for tool in coding.get_tools(session)}
    try:
        assert "已写入" in await _execute(tools["write_file"], "normal.txt", "original")
        assert "original" in await _execute(tools["read_file"], "normal.txt")
        assert "已编辑" in await _execute(tools["edit_file"], "normal.txt", "original", "changed")
        assert "已批量替换" in await _execute(tools["search_replace"], "normal.txt", [{"old": "changed", "new": "updated"}])
        assert "normal.txt" in await _execute(tools["glob_files"], "*.txt")
        assert "updated" in await _execute(tools["grep_files"], "updated")
        for protected in (".env", ".satrap/private.txt", ".git/private.txt", "node_modules/private.txt"):
            assert "拒绝" in await _execute(tools["write_file"], protected, "blocked")
        other = system_root / "data/platforms/p/sessions/other/sandbox/private.txt"
        assert "越出工作区" in await _execute(tools["write_file"], str(other), "blocked")
        setattr(session, "coding_workspace_root", str(system_root))
        assert "拒绝" in await _execute(tools["write_file"], "credentials.txt", "blocked")
        assert not (system_root / "credentials.txt").exists()
        setattr(session, "coding_workspace_root", str(sandbox))
        delattr(session, "coding_sandbox_root")
        assert "拒绝" in await _execute(tools["read_file"], "normal.txt")
    finally:
        reset_plugin_state(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("embedded", [False, True], ids=["ordinary", "embedded"])
@pytest.mark.parametrize("source", ["default", "configured", "host"])
@pytest.mark.parametrize("relation", ["same", "ancestor", "descendant"])
async def test_effective_sandbox_protection_matches_file_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
    embedded: bool, source: str, relation: str,
) -> None:
    """
    实际沙箱的普通文件可用, 祖先工作区不会放开其他运行数据或敏感目录

    参数:
    - tmp_path: 模拟项目, 沙箱和状态数据的隔离目录
    - monkeypatch: 隔离固定默认路径与系统保护根
    - asynchronous: 是否使用异步工具
    - embedded: 是否使用无宿主路径的轻量嵌入会话
    - source: 沙箱来自默认值, 配置或优先的宿主属性
    - relation: 工作区与沙箱相同, 包含沙箱或位于沙箱内
    """
    project = tmp_path / "project"
    system = project / ".satrap"
    default = system / "sandbox"
    configured = system / "coding/sandbox"
    sandbox = {"default": default, "configured": configured, "host": system / "data/sessions/current/sandbox"}[source]
    workspace = {"same": sandbox, "ancestor": project, "descendant": sandbox / "nested"}[relation]
    target_dir = workspace if relation == "descendant" else sandbox
    target_dir.mkdir(parents=True)
    monkeypatch.setattr(coding, "WORKSPACE_ROOT", project)
    monkeypatch.setattr(coding, "DEFAULT_SANDBOX_ROOT", default)
    monkeypatch.setattr(coding, "_SYSTEM_PROTECTED_ROOT", system)
    base, llm_type = (AsyncSimpleSession, AsyncLLM) if asynchronous else (SimpleSession, LLM)
    if embedded:
        session: Any = object.__new__(base)
        session.session_id = "embedded-sandbox"
        session._wf = SimpleNamespace(llm=None, tools_manager=None)
        session.session_ctx = SimpleNamespace(db_path=str(tmp_path / "chat.db"))
    else:
        session = base("ordinary-sandbox", MagicMock(spec=llm_type), db_path=str(tmp_path / "chat.db"))
        if isinstance(session, AsyncSimpleSession):
            await session.initialize()
    if source == "host":
        setattr(session, "coding_sandbox_root", str(sandbox))
    config = {"workspace_root": str(workspace), "protected_dirs": "private"}
    if source != "default":
        config["sandbox_root"] = str(configured)
    tools = {tool.tool_name: tool for tool in coding.get_tools(session, config)}
    target = target_dir / "normal.txt"
    relative = str(target.relative_to(workspace))
    try:
        assert "已写入" in await _execute(tools["write_file"], relative, "original")
        assert "original" in await _execute(tools["read_file"], relative)
        assert "已编辑" in await _execute(tools["edit_file"], relative, "original", "changed")
        assert "已批量替换" in await _execute(tools["search_replace"], relative, [{"old": "changed", "new": "updated"}])
        assert "normal.txt" in await _execute(tools["glob_files"], str(target_dir.relative_to(workspace) / "*.txt"))
        assert "updated" in await _execute(tools["grep_files"], "updated", str(target_dir.relative_to(workspace)))
        for protected in (".env", ".satrap/secret.txt", ".git/secret.txt", "node_modules/secret.txt", "private/secret.txt"):
            path = target_dir / protected
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("protected-marker", encoding="utf-8")
            name = str(path.relative_to(workspace))
            assert "拒绝" in await _execute(tools["write_file"], name, "blocked")
            assert "拒绝" in await _execute(tools["read_file"], name)
            assert path.read_text(encoding="utf-8") == "protected-marker"
        assert "protected-marker" not in await _execute(tools["grep_files"], "protected-marker")
        assert "secret.txt" not in await _execute(tools["glob_files"], "**/*.txt")
        get_plugin_state(session)["engine"].set_plan_mode(True)
        assert "计划模式" in await _execute(tools["write_file"], relative, "blocked")
        assert target.read_text(encoding="utf-8") == "updated"
        setattr(session, "coding_workspace_root", str(project))
        for sibling in (system / "credentials.txt", system / "data/sessions/other/sandbox/secret.txt", configured / "unused.txt" if source == "host" else system / "unused/secret.txt"):
            sibling.parent.mkdir(parents=True, exist_ok=True)
            sibling.write_text("outside-marker", encoding="utf-8")
            name = str(sibling.relative_to(project))
            assert "拒绝" in await _execute(tools["read_file"], name)
            assert "拒绝" in await _execute(tools["write_file"], name, "blocked")
            assert sibling.read_text(encoding="utf-8") == "outside-marker"
        assert "outside-marker" not in await _execute(tools["grep_files"], "outside-marker")
        for ancestor in (".git", "node_modules", "private"):
            blocked_sandbox = system / ancestor / "sandbox"
            blocked_sandbox.mkdir(parents=True)
            secret = blocked_sandbox / "secret.txt"
            secret.write_text("protected-ancestor", encoding="utf-8")
            setattr(session, "coding_sandbox_root", str(blocked_sandbox))
            assert "拒绝" in await _execute(tools["read_file"], str(secret.relative_to(project)))
    finally:
        reset_plugin_state(session)


@pytest.mark.parametrize("value", [0, -1, 3601, 1.5, True, "bad", float("inf"), float("nan")])
def test_factory_rejects_invalid_default_timeout(tmp_path: Path, value: object) -> None:
    """
    非法默认超时不能被截断或回退成可执行配置

    参数:
    - tmp_path: 测试数据库的隔离目录
    - value: 非法超时配置值
    """
    session = SimpleSession("invalid", MagicMock(spec=LLM), db_path=str(tmp_path / "chat.db"))
    try:
        with pytest.raises(ValueError, match="shell_timeout"):
            coding.get_tools(session, {"shell_timeout": value})
    finally:
        reset_plugin_state(session)
