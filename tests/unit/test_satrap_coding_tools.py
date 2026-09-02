"""satrap_coding 插件工具层单元测试 (get_tools 工厂 / 文件 / ask_user / shell)"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator
from typing import Callable, cast

import pytest

from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap.expend.plugins.satrap_coding import tools as tools_mod
from satrap.expend.plugins.satrap_coding.tools import get_tools


def _reply(value: str) -> Callable[[str], str]:
    """
    创建忽略提示文本并返回固定答案的输入函数

    参数:
    - value: 固定回答

    返回:
    - 忽略提示并返回固定回答的输入函数
    """
    def answer(_: str) -> str:
        """
        返回固定答案

        参数:
        - _: 未使用的提示文本

        返回:
        - 外层函数提供的固定回答
        """
        return value

    return answer


class _FakeAsyncLLM(AsyncLLM):
    """记录调用参数的异步 fake LLM"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        self.calls.append({"messages": messages, "tools": tools})
        return LLMCallResponse(type="answer", content="异步回复")

    async def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Any:
        self.calls.append({"messages": messages, "tools": tools})
        yield LLMCallStreamEvent(kind="content_delta", delta="流")


@pytest.fixture
def workspace(tmp_path: Any, monkeypatch: Any) -> Any:
    """
    把工作区根与插件数据目录重定向到临时目录, 使文件工具/数据落盘隔离

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具

    返回:
    - Any: 把工作区根与插件数据目录重定向到临时目录, 使文件工具/数据落盘隔离
    """
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(tools_mod, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(tools_mod, "DATA_ROOT", tmp_path / "coding")
    return root


class _FakeLLM(LLM):
    """记录调用参数的同步 fake LLM"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        self.calls.append({"messages": messages, "tools": tools})
        return LLMCallResponse(type="answer", content="回复")

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        self.calls.append({"messages": messages, "tools": tools})
        yield LLMCallStreamEvent(kind="content_delta", delta="流")


def _make_session(tmp_path: Any) -> SimpleSession:
    return SimpleSession(
        "conv-1", _FakeLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )


def _install_tools(session: SimpleSession) -> dict[str, Any]:
    """
    安装全部工具并返回名称 -> 实例映射

    参数:
    - session: 会话

    返回:
    - dict[str, Any]: 名称 -> 实例映射
    """
    tools = get_tools(session)
    names: set[str] = set()
    for tool in tools:
        name = tool.get_tool_name()
        assert name not in names, f"工具重名: {name}"
        names.add(name)
        session.add_tool(tool)
    return {t.get_tool_name(): t for t in tools}


# ================= get_tools 工厂 =================


def test_factory_sync_tool_set(tmp_path: Any):
    """
    同步会话: 11 个工具全部注册, 无重名 (search/memory 已移交 base_take)

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    expected = {
        "ask_user", "shell", "subagent", "read_file", "write_file",
        "edit_file", "search_replace", "todo_write", "list_dir", "glob_files", "grep_files",
    }
    assert set(tools) == expected
    assert set(session.list_tools()) == expected


@pytest.mark.asyncio
async def test_factory_async_tool_set(tmp_path: Any):
    """
    异步会话: 异步工具集注册

    参数:
    - tmp_path: tmp路径
    """
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    await session.initialize()
    tools = get_tools(session)
    names = [t.get_tool_name() for t in tools]
    assert len(names) == 11 and len(set(names)) == 11
    # 工具实例为 AsyncTool
    from satrap.core.utils.TCBuilder import AsyncTool

    assert all(isinstance(t, AsyncTool) for t in tools)


# ================= 文件工具 =================


def test_file_tools_read_list_glob_grep(tmp_path: Any, workspace: Any):
    """
    只读文件工具: 读/列表/glob/grep

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    target = workspace / "src" / "demo.py"
    target.parent.mkdir(parents=True)
    target.write_text("line1\nline2 demo\n", encoding="utf-8")

    out = tools["read_file"].execute(str(target))
    assert "line2 demo" in out
    out = tools["read_file"].execute(str(target), offset=1, limit=1)
    assert "line2 demo" in out and "line1" not in out

    out = tools["list_dir"].execute(str(workspace))
    assert "src" in out

    out = tools["glob_files"].execute("**/*.py")
    assert "src/demo.py" in out

    out = tools["grep_files"].execute("demo", path=str(workspace))
    assert "demo.py:2:" in out


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"offset": "bad"}, "offset 必须是整数"),
        ({"offset": -1}, "offset 必须在"),
        ({"limit": 0}, "limit 必须在"),
        ({"limit": 2001}, "limit 必须在"),
    ],
)
def test_read_file_rejects_invalid_pagination(
    tmp_path: Path,
    workspace: Path,
    kwargs: dict[str, object],
    expected: str,
):
    """
    read_file 对错误类型和越界分页参数返回稳定错误

    参数:
    - tmp_path: 临时目录
    - workspace: 隔离工作区
    - kwargs: 参数化分页参数
    - expected: 预期错误文本
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    target = workspace / "page.txt"
    target.write_text("line\n", encoding="utf-8")

    execute = cast(Callable[..., str], tools["read_file"].execute)
    out = execute(str(target), **kwargs)

    assert expected in out


def test_glob_cannot_escape_workspace(tmp_path: Any, workspace: Any):
    """
    L3: glob 的 ../ 模式不能枚举工作区外文件

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    outside = workspace.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    (workspace / "inside.txt").write_text("y", encoding="utf-8")

    out = tools["glob_files"].execute("../*.txt")
    assert "outside.txt" not in out   # ../ 越界结果被过滤
    out = tools["glob_files"].execute("*.txt")
    assert "inside.txt" in out   # 工作区内正常匹配


@pytest.mark.asyncio
async def test_grep_does_not_follow_symlink_outside_workspace(tmp_path: Path, workspace: Path):
    """
    同步和异步 grep 均不得读取工作区外的符号链接目标

    参数:
    - tmp_path: 临时目录
    - workspace: 工作区
    """
    outside = workspace.parent / "outside-secret.txt"
    outside.write_text("external-secret", encoding="utf-8")
    link = workspace / "linked-secret.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"当前环境无法创建文件符号链接: {error}")

    sync_session = _make_session(tmp_path)
    sync_tools = _install_tools(sync_session)
    assert "external-secret" not in sync_tools["grep_files"].execute("external-secret")

    async_session = await _make_async_session(tmp_path)
    async_tools = await _async_tools(async_session)
    assert "external-secret" not in await async_tools["grep_files"].execute("external-secret")


def test_file_tools_path_boundary_and_protection(tmp_path: Any, workspace: Any):
    """
    文件工具: 工作区外拒绝, 受保护路径拒绝

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    outside = workspace.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    out = tools["read_file"].execute(str(outside))
    assert "越出工作区" in out

    out = tools["read_file"].execute("no-such-file-xyz.txt")
    assert "不存在" in out

    (workspace / ".satrap").mkdir()
    out = tools["write_file"].execute(".satrap/secret.txt", "x")
    assert "受保护" in out
    out = tools["read_file"].execute(".satrap/config.yaml")
    assert "受保护" in out or "拒绝" in out
    out = tools["read_file"].execute("api-token")
    assert "受保护" in out or "不存在" in out


def test_write_file_requires_approval(tmp_path: Any, workspace: Any):
    """
    写文件: 无输入通道时拒绝, y 仅本次批准, all 本会话放行

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    target = workspace / "out.txt"

    out = tools["write_file"].execute(str(target), "hello")
    assert "需要用户批准" in out
    assert not target.exists()

    session.user_input_provider = _reply("y")
    # y 仅批准本次: 写入成功, 但下次同类操作仍需询问
    out = tools["write_file"].execute(str(target), "hello")
    assert "已写入" in out
    assert target.read_text(encoding="utf-8") == "hello"

    session.user_input_provider = _reply("n")
    # 第二次写仍需批准, 用户拒绝则拦截
    out = tools["write_file"].execute(str(target), "again", append=True)
    assert "拒绝了" in out or "拒绝" in out
    assert target.read_text(encoding="utf-8") == "hello"

    session.user_input_provider = _reply("all")
    # all = 本会话全部放行, 后续写不再询问
    out = tools["write_file"].execute(str(target), "again", append=True)
    assert "已追加" in out
    session.user_input_provider = _reply("n")
    out = tools["write_file"].execute(str(target), "third", append=True)
    assert "已追加" in out
    assert target.read_text(encoding="utf-8") == "helloagainthird"


def test_edit_file_tool(tmp_path: Any, workspace: Any):
    """
    编辑文件: 精确替换 + 未匹配报错

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = _make_session(tmp_path)
    session.user_input_provider = _reply("y")
    tools = _install_tools(session)
    target = workspace / "edit.txt"
    target.write_text("aaa bbb aaa", encoding="utf-8")

    out = tools["edit_file"].execute(str(target), "bbb", "ccc")
    assert "已编辑" in out
    assert target.read_text(encoding="utf-8") == "aaa ccc aaa"

    out = tools["edit_file"].execute(str(target), "zzz", "x")
    assert "未找到匹配" in out


def test_search_replace_tool(tmp_path: Any, workspace: Any):
    """
    批量替换: 多对 old->new, replace_all 独立控制, 全量预校验

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = _make_session(tmp_path)
    session.user_input_provider = _reply("y")
    tools = _install_tools(session)
    target = workspace / "sr.txt"
    target.write_text("x a x b x a", encoding="utf-8")

    out = tools["search_replace"].execute(
        str(target),
        [{"old": "x", "new": "X", "replace_all": True}, {"old": "b", "new": "B"}],
    )
    assert "已批量替换" in out and "'x' 3 处" in out
    assert target.read_text(encoding="utf-8") == "X a X B X a"

    out = tools["search_replace"].execute(
        str(target), [{"old": "X", "new": "Y"}, {"old": "zzz", "new": "y"}],
    )
    # 任一 old 不匹配: 全部不执行 (预校验)
    assert "未找到匹配" in out
    assert target.read_text(encoding="utf-8") == "X a X B X a"

    out = tools["search_replace"].execute(str(target), [{"new": "y"}])
    # 格式无效的替换项
    assert "格式无效" in out


def test_todo_write_tool(tmp_path: Any):
    """
    任务清单: add / list / done / clear 全链路 (会话级状态)

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    todo = tools["todo_write"]

    out = todo.execute("add", item="第一步")
    assert "任务已添加" in out
    out = todo.execute("add", item="第二步")
    assert "任务已添加 (2)" in out

    out = todo.execute("list")
    assert "1. [ ] 第一步" in out and "2. [ ] 第二步" in out

    out = todo.execute("done", index=1)
    assert "任务 1 已完成" in out
    out = todo.execute("list")
    assert "1. [x] 第一步" in out

    out = todo.execute("done", index=9)
    assert "序号无效" in out
    out = todo.execute("done", index="bad")
    assert "序号无效" in out
    out = todo.execute("clear")
    assert "已清空" in out
    out = todo.execute("list")
    assert "任务清单为空" in out


# ================= ask_user 测试 =================


def test_ask_user_tool(tmp_path: Any):
    """
    询问用户: 有通道返回回复, 无通道返回提示, 推荐选项编号展示

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    out = tools["ask_user"].execute("继续吗?")
    assert "需要用户回复" in out

    session.user_input_provider = _reply("继续")
    out = tools["ask_user"].execute("继续吗?")
    assert "继续" in out

    captured: list[str] = []

    def capture_prompt(prompt: str) -> str:
        """
        记录用户提示并返回选项

        参数:
        - prompt: 用户提示文本

        返回:
        - 固定选项序号
        """
        captured.append(prompt)
        return "2"

    session.user_input_provider = capture_prompt
    out = tools["ask_user"].execute("继续吗?", ["方案A", "方案B"])
    assert "1. 方案A" in captured[0] and "2. 方案B" in captured[0]
    assert "用户回复: 2" in out

    structured: list[tuple[str, list[str] | None]] = []

    def structured_provider(question: str, options: list[str] | None = None) -> str:
        structured.append((question, options))
        return "方案B"

    session.user_input_provider = structured_provider
    out = tools["ask_user"].execute("继续吗?", ["方案A", "方案B"])
    assert structured == [("继续吗?", ["方案A", "方案B"])]
    assert "用户回复: 方案B" in out


# ================= memory 工具 =================


# ================= shell 测试 =================


def test_shell_read_only_and_approval(tmp_path: Any, monkeypatch: Any):
    """
    shell: 工作区只读直接执行, 所有写操作和工作区外读取均审批, 黑名单拒绝

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    # 工作区指向临时目录, 避免相对路径写盘污染项目根
    monkeypatch.setattr(tools_mod, "WORKSPACE_ROOT", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    out = tools["shell"].execute("echo hello")
    assert "hello" in out

    out = tools["shell"].execute("echo written > f.txt")
    assert "需要用户批准" in out
    assert not (tmp_path / "workspace" / "f.txt").exists()

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    out = tools["shell"].execute(f'type "{outside}"')
    assert "需要用户批准" in out
    out = tools["shell"].execute("type ../outside.txt")
    assert "需要用户批准" in out
    out = tools["shell"].execute(r"type C:/Users/example/.ssh/id_rsa")
    assert "需要用户批准" in out
    out = tools["shell"].execute(r"type %USERPROFILE%\.ssh\id_rsa")
    assert "需要用户批准" in out

    out = tools["shell"].execute(r"type a\..\..\secret.txt")
    assert "需要用户批准" in out
    out = tools["shell"].execute(r'powershell -Command "type C:\Windows\win.ini"')
    assert "需要用户批准" in out
    out = tools["shell"].execute(r"type \Windows\win.ini")
    assert "需要用户批准" in out

    out = tools["shell"].execute("powershell -e ZQBjAGgAbwAgAHgA")
    assert "黑名单" in out

    out = tools["shell"].execute("cmd /c dir")
    assert "需要用户批准" not in out
    out = tools["shell"].execute("cd ..")
    assert "需要用户批准" not in out

    out = tools["shell"].execute("git push")
    assert "需要用户批准" in out

    out = tools["shell"].execute("copy \\\\server\\share\\a.txt \\\\server\\share\\b.txt")
    # 引用工作区外绝对路径 (UNC, 不在任何盘符下): 走审批 (无输入通道时拒绝)
    assert "需要用户批准" in out

    out = tools["shell"].execute("rm -rf /")
    assert "黑名单" in out

    session.user_input_provider = _reply("y")
    out = tools["shell"].execute("echo written > f.txt")
    assert "需要用户批准" not in out
    assert (tmp_path / "workspace" / "f.txt").is_file()
    out = tools["shell"].execute("copy \\\\server\\share\\a.txt \\\\server\\share\\b.txt")
    assert "需要用户批准" not in out
    # 工作区外路径经用户批准后走执行流程, 源不存在仅验证流程且无副作用
    out = tools["shell"].execute("echo written", shell="cmd")
    assert "written" in out


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [
        ("bad", "timeout 必须是整数"),
        (0, "timeout 必须在"),
        (3601, "timeout 必须在"),
    ],
)
def test_shell_rejects_invalid_timeout(
    tmp_path: Path,
    workspace: Path,
    timeout: int | str,
    expected: str,
) -> None:
    """
    shell 对错误类型和越界超时返回稳定错误

    参数:
    - tmp_path: 临时目录
    - workspace: 隔离工作区
    - timeout: 待验证的超时参数
    - expected: 预期错误文本
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    output = tools["shell"].execute("echo never-runs", timeout=timeout)

    assert expected in output


# ================= subagent 测试 =================


def test_subagent_tool_constructs(tmp_path: Any):
    """
    子代理工具: 可构造, 独立 manager 白名单过滤

    参数:
    - tmp_path: tmp路径
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)
    sub = tools["subagent"]
    assert sub.llm is session.llm

    from satrap.expend.plugins.satrap_coding.tools import _CodingSubAgent

    agent = _CodingSubAgent(session.llm, session.tools_manager, "你是子代理", ["read_file"])
    assert agent.tools_manager.tools.keys() == {"read_file"}
    assert agent.context_id.startswith("coding_sub_")


@pytest.mark.parametrize(
    ("max_turns", "expected"),
    [
        ("bad", "max_turns 必须是整数"),
        (0, "max_turns 必须在"),
        (101, "max_turns 必须在"),
    ],
)
def test_subagent_rejects_invalid_max_turns(
    tmp_path: Path,
    max_turns: int | str,
    expected: str,
) -> None:
    """
    子代理对错误类型和越界轮数返回稳定错误

    参数:
    - tmp_path: 临时目录
    - max_turns: 待验证的最大轮数
    - expected: 预期错误文本
    """
    session = _make_session(tmp_path)
    tools = _install_tools(session)

    output = tools["subagent"].execute("never-runs", max_turns=max_turns)

    assert expected in output


# ================= 异步工具执行 =================


async def _make_async_session(tmp_path: Any) -> AsyncSimpleSession:
    """
    构造异步会话并安装全部异步工具

    参数:
    - tmp_path: tmp路径

    返回:
    - AsyncSimpleSession: 构造异步会话并安装全部异步工具
    """
    session = AsyncSimpleSession(
        "conv-a", _FakeAsyncLLM(), db_path=str(tmp_path / "chat.db"), enable_checkpoint=True,
    )
    await session.initialize()
    for tool in get_tools(session):
        session.add_tool(tool)
    return session


async def _async_tools(session: AsyncSimpleSession) -> dict[str, Any]:
    return {t.get_tool_name(): t for t in session.tools_manager.tools.values()}


@pytest.mark.asyncio
async def test_async_file_tools(tmp_path: Any, workspace: Any):
    """
    异步文件工具: 读写/编辑/列表/glob/grep 主路径

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = await _make_async_session(tmp_path)
    session.user_input_provider = _reply("y")
    tools = await _async_tools(session)

    target = workspace / "a.txt"
    out = await tools["write_file"].execute(str(target), "hello")
    assert "已写入" in out
    out = await tools["read_file"].execute(str(target))
    assert "hello" in out
    assert "offset 必须是整数" in await tools["read_file"].execute(str(target), offset="bad")
    assert "limit 必须在" in await tools["read_file"].execute(str(target), limit=0)

    out = await tools["edit_file"].execute(str(target), "hello", "world")
    assert "已编辑" in out
    assert (await tools["read_file"].execute(str(target))).endswith("world")
    out = await tools["edit_file"].execute(str(target), "zzz", "x")
    assert "未找到匹配" in out

    out = await tools["list_dir"].execute(str(workspace))
    assert "a.txt" in out
    out = await tools["glob_files"].execute("**/*.txt")
    assert "a.txt" in out
    out = await tools["grep_files"].execute("world", path=str(workspace))
    assert "a.txt:1:" in out


@pytest.mark.asyncio
async def test_async_search_replace_and_todo(tmp_path: Any, workspace: Any):
    """
    异步 search_replace / todo_write 主路径

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = await _make_async_session(tmp_path)
    session.user_input_provider = _reply("y")
    tools = await _async_tools(session)

    target = workspace / "a.txt"
    target.write_text("x x y", encoding="utf-8")
    out = await tools["search_replace"].execute(
        str(target), [{"old": "x", "new": "X", "replace_all": True}],
    )
    assert "已批量替换" in out
    assert target.read_text(encoding="utf-8") == "X X y"

    out = await tools["todo_write"].execute("add", item="异步任务")
    assert "任务已添加" in out
    out = await tools["todo_write"].execute("list")
    assert "异步任务" in out
    out = await tools["todo_write"].execute("done", index="bad")
    assert "序号无效" in out


@pytest.mark.asyncio
async def test_async_ask_shell(tmp_path: Any, workspace: Any):
    """
    异步 ask_user / shell 主路径

    参数:
    - tmp_path: tmp路径
    - workspace: 工作区
    """
    session = await _make_async_session(tmp_path)
    session.user_input_provider = _reply("y")
    tools = await _async_tools(session)

    out = await tools["ask_user"].execute("继续?")
    assert "用户回复: y" in out

    structured: list[tuple[str, list[str] | None]] = []

    async def structured_provider(question: str, options: list[str] | None = None) -> str:
        structured.append((question, options))
        return "继续"

    session.user_input_provider = structured_provider
    out = await tools["ask_user"].execute("下一步?", ["继续", "停止"])
    assert structured == [("下一步?", ["继续", "停止"])]
    assert "用户回复: 继续" in out

    out = await tools["shell"].execute("echo async-ok")
    assert "async-ok" in out
    out = await tools["shell"].execute("rm -rf /")
    assert "黑名单" in out

    session.user_input_provider = None
    out = await tools["shell"].execute("git push")
    assert "需要用户批准" in out
    out = await tools["shell"].execute("type ../outside.txt")
    assert "需要用户批准" in out


@pytest.mark.asyncio
async def test_async_shell_and_subagent_reject_invalid_integer_arguments(
    tmp_path: Path,
) -> None:
    """
    异步 shell 和子代理在执行前拒绝非法整数参数

    参数:
    - tmp_path: 临时目录
    """
    session = await _make_async_session(tmp_path)
    tools = await _async_tools(session)

    timeout_error = await tools["shell"].execute("echo never-runs", timeout="bad")
    turns_error = await tools["subagent"].execute("never-runs", max_turns="bad")

    assert "timeout 必须是整数" in timeout_error
    assert "max_turns 必须是整数" in turns_error
