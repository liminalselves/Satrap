# -*- coding: utf-8 -*-
"""satrap_coding 插件 TUI Demo: 终端交互展示插件全部能力

运行:
    python tests/manual/tui_demo.py                # 真实模型 (自动读取 .toolkit/apikey.txt)
    python tests/manual/tui_demo.py --demo         # 离线模式 (无 API key, 模拟工具调用)
    python tests/manual/tui_demo.py --key-file 路径  # 指定模型配置文件
    python tests/manual/tui_demo.py --workspace 路径  # 指定模型可读工作区 (默认沙箱根)

默认工作区 = 沙箱根 (.satrap/coding/sandbox/): 一个目录, 写文件免审批 (沙箱=免审批区);
传 --workspace 其他目录可恢复工作区/沙箱分离, 工作区写走审批。

注: 插件 sandbox 工具已移除 (与 shell/文件工具重复), 执行统一走 shell, 文件读写走文件工具。

界面: 顶部状态面板 (审批策略/计划模式/记忆/目标) + 消息区 + 底部输入。

输入:
- 普通文本: 走 Agent 流程 (模型 + 工具调用)
- /new: 清空上下文历史, 开始新会话 (记忆/目标/审批规则保留)
- /goal <描述>: 设置目标并自动进入 yolo 推进 (模型连续执行直到完成或达轮数上限)
- /plan /memory /approve: 插件命令
- 离线模式额外支持 @write <路径> <内容> / @read <路径> / @shell <命令> ... 模拟工具调用
- exit / quit / q: 退出
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))


def _reconfigure_utf8(stream: Any) -> None:
    """stdin/stdout 切到 UTF-8 (typeshed 未标注 reconfigure, 用 Any 兼容)"""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


_reconfigure_utf8(sys.stdout)
if not sys.stdin.isatty():
    # 管道/CI 场景: stdin 统一按 UTF-8 读取 (tty 下由 prompt_toolkit 自行处理)
    _reconfigure_utf8(sys.stdin)

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory

from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import SimpleSession
from satrap.expend.plugins.satrap_coding.state import get_plugin_state

PLUGIN_DIR = PROJECT_ROOT / "satrap" / "expend" / "plugins" / "satrap_coding"
KEY_FILE = PROJECT_ROOT / ".toolkit" / "apikey.txt"
SESSION_ID = "tui-demo"
CHAT_DB = PROJECT_ROOT / ".satrap" / "tui_demo" / "chat.db"
DEFAULT_WORKSPACE = PROJECT_ROOT / ".satrap" / "coding" / "sandbox"
# 默认工作区 = 沙箱根: 与插件 DEFAULT_SANDBOX_ROOT 一致, 一个目录双重身份 (可被 --workspace 覆盖)

AUTO_MAX_ROUNDS = 8
"""yolo 自动推进轮数上限"""
AUTO_PROMPT = "继续推进当前目标。若目标已全部完成, 请以「已完成」开头总结成果, 不要再调用工具。"

console = Console(highlight=False, soft_wrap=True)

# 流式内容/思考转发: 回调在会话构造时传入, 转发到当前 TuiApp
_content_sink: list[Any] = []
_thinking_sink: list[Any] = []


def _content_forward(delta: str) -> None:
    if _content_sink:
        _content_sink[0](delta)


def _thinking_forward(delta: str) -> None:
    if _thinking_sink:
        _thinking_sink[0](delta)


# 非 tty (管道/CI) 时退化用内置 input, prompt_toolkit 需要真实终端
_input: PromptSession[str] | None
if sys.stdin.isatty():
    _input = PromptSession(history=InMemoryHistory())
else:
    _input = None


def _ask(prompt: str) -> str:
    """统一输入入口 (tty 用 prompt_toolkit, 否则内置 input)"""
    if _input is not None:
        return str(_input.prompt(prompt))
    return input(prompt)


# ================= 模型构建 =================


def _normalize_base_url(url: str) -> str:
    """apikey.txt 中的地址可能含完整端点, 去掉 /chat/completions 尾缀"""
    for suffix in ("/chat/completions", "/chat/completions/"):
        if url.rstrip().endswith(suffix):
            return url.rstrip()[: -len(suffix)]
    return url.rstrip()


def build_real_llm(key_file: Path) -> LLM:
    """从 apikey.txt 解析第一组 (base url / model / api key) 构造真实 LLM"""
    if not key_file.is_file():
        console.print(
            f"[yellow]未找到 {key_file}, 请使用 --demo 离线模式或 --key-file 指定配置文件[/yellow]"
        )
        sys.exit(1)
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in key_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        match = re.match(r"^(base url|model|api key)\s*:\s*(.+)$", line, re.IGNORECASE)
        if match:
            current[match.group(1).lower()] = match.group(2).strip()
    if current:
        entries.append(current)
    if not entries:
        console.print(f"[red]{escape(str(key_file))} 中未解析到模型配置[/red]")
        sys.exit(1)
    conf = entries[0]
    if not conf.get("api key") or not conf.get("base url"):
        console.print(f"[red]{escape(str(key_file))} 第一组配置缺少 api key / base url[/red]")
        sys.exit(1)
    console.print(
        f"[dim]使用模型: {conf.get('model') or '(默认)'} @ {conf['base url']}[/dim]"
    )
    return LLM(
        api_key=conf["api key"],
        base_url=_normalize_base_url(conf["base url"]),
        model=conf.get("model") or "put-your-model-name-here",
    )


class DemoLLM(LLM):
    """离线演示 LLM: 按关键词模拟 Agent 工具调用, 走真实审批链路"""

    def __init__(self) -> None:
        super().__init__(api_key="demo")
        self.session: SimpleSession | None = None

    def bind(self, session: SimpleSession) -> None:
        self.session = session

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content=self._simulate(messages))

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: bool = False, temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        text = self._simulate(messages)
        if thinking:
            yield LLMCallStreamEvent(
                kind="thinking_delta", delta="分析指令并选择合适工具…",
            )
        for ch in text:
            yield LLMCallStreamEvent(kind="content_delta", delta=ch)

    def _simulate(self, messages: list[dict[str, Any]]) -> str:
        """模拟 Agent: @write/@read/@shell 触发真实工具 (含审批)

        用户消息可能被注入器加记忆/目标头, 用正则提取 @ 指令
        """
        session = self.session
        assert session is not None, "DemoLLM 未绑定会话"
        user_msgs = [m for m in messages if m.get("role") == "user"]
        text = str(user_msgs[-1]["content"]) if user_msgs else ""
        stripped = text.strip()

        match = re.search(r"@(write|read|shell)\s+(.+)$", stripped, re.MULTILINE)
        if match:
            cmd, args = match.group(1), match.group(2).strip()
            tool = session.tools_manager.tools.get(
                {"write": "write_file", "read": "read_file"}.get(cmd, cmd)
            )
            if tool is None:
                return f"未知工具: {cmd}"
            if cmd == "write":
                path, _, content = args.partition(" ")
                if not path:
                    return "用法: @write <路径> <内容>"
                return str(tool.execute(path, content))
            return str(tool.execute(args))
        if stripped in ("help", "帮助", "?"):
            return (
                "[离线演示模式] 可用指令:\n"
                "  @write <路径> <内容>   模拟调用 write_file (沙箱内免审批)\n"
                "  @read <路径>           模拟调用 read_file\n"
                "  @shell <命令>          模拟调用 shell\n"
                "  /goal /plan /memory /approve  插件命令"
            )
        return f"[离线演示] 收到: {text[:60]}\n输入 help 查看演示指令"


# ================= TUI 应用 =================


class TuiApp:
    """状态面板 + 消息区 + 输入循环"""

    def __init__(self, session: SimpleSession, demo: bool) -> None:
        self.session = session
        self.demo = demo
        self.messages: list[tuple[str, str]] = []
        self._thinking_active = False
        _content_sink.append(self._on_content)
        _thinking_sink.append(self._on_thinking)

    # ---------------- 状态读取 ----------------

    def _state(self) -> dict[str, Any]:
        return get_plugin_state(self.session)

    def _status_table(self) -> Table:
        state = self._state()
        engine = state["engine"]
        store = state["store"]
        goals = state["goals"]
        goal = goals.get_goal(self.session.session_id)

        table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
        table.add_column(justify="left")
        table.add_column(justify="left")
        table.add_row(
            f"[bold]审批[/bold] {engine.mode}",
            f"[bold]计划[/bold] {'[red]ON[/red]' if engine.plan_mode else '[green]off[/green]'}",
        )
        table.add_row(
            f"[bold]记忆[/bold] {store.mode} ({store.count()} 条)",
            f"[bold]目标[/bold] {escape(goal['text'][:22]) if goal else '(无)'}"
            + (f" [{goal['status']}]" if goal else ""),
        )
        table.add_row(
            f"[bold]工具[/bold] {len(self.session.list_tools())} 个",
            f"[bold]命令[/bold] {len(self.session.list_commands())} 个",
        )
        return table

    # ---------------- 渲染 ----------------

    def render(self) -> None:
        console.clear()
        header = Panel.fit(
            "[bold cyan]satrap_coding TUI Demo[/bold cyan]"
            + (" [yellow](离线模式)[/yellow]" if self.demo else "")
            + f"\n[dim]会话 {SESSION_ID} · 输入 exit 退出 · /help 查看命令[/dim]",
            border_style="cyan",
        )
        console.print(header)
        console.print(Panel(self._status_table(), title="插件状态", border_style="blue"))
        console.print(self._messages_panel())

    def _messages_panel(self) -> Panel:
        """消息区: 文本按字面渲染 (Text 不解析 markup, 防 [x] 片段被误吞)"""
        style_map = {"用户": "cyan", "助手": "green", "命令": "yellow", "系统": "magenta"}
        lines: list[Text] = []
        for role, text in self.messages:
            style = style_map.get(role, "white")
            body = Text(text.replace("\n", "\n  "), style=style)
            lines.append(Text(f"[{role}] ", style=style) + body)
        if not lines:
            lines.append(Text("还没有消息, 输入一句话开始, 或试试 /goal 设置目标", style="dim"))
        return Panel(Group(*lines), title="对话", border_style="green")

    # ---------------- 交互 ----------------

    def ask_user(self, question: str) -> str:
        """询问入口: 区分模型询问 (ask_user 工具) 与审批询问

        ask_user 工具带推荐选项 (格式: 可选: 1. A  2. B): 提示输入序号或文本, 序号映射为选项;
        审批询问 (是否允许执行等): 提示 y/n/all
        """
        if "可选: " in question:
            console.print(
                Panel(Text(question), title="[bold yellow]模型询问[/bold yellow]", border_style="yellow")
            )
            ans = _ask("输入序号或文本 > ").strip()
            m = re.match(r"^(\d+)$", ans)
            if m:
                opts = question.split("可选: ", 1)[1].split("  ")
                parts = [o.split(". ", 1)[1] for o in opts if ". " in o]
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(parts):
                    return parts[idx]
            return ans
        console.print(Panel(Text(question), title="[bold red]审批请求[/bold red]", border_style="red"))
        return _ask("y 仅本次 / n 拒绝 / all 会话放行 > ").strip()

    def _on_content(self, delta: str) -> None:
        """流式内容回调 (Text 字面渲染, 防模型输出含 [x] 触发 markup 解析)"""
        if self._thinking_active:
            console.print()  # 思考结束, 换行进入正文
            self._thinking_active = False
        console.print(Text(delta), end="", soft_wrap=True)

    def _on_thinking(self, delta: str) -> None:
        """流式思考回调 (斜体灰显)"""
        if not self._thinking_active:
            console.print(Text("💭 "), style="dim italic", end="")
            self._thinking_active = True
        console.print(Text(delta), style="dim italic", end="", soft_wrap=True)

    def _new_session(self) -> None:
        """/new: 清空上下文历史, 开始新会话 (记忆/目标/审批规则等持久数据保留)

        ContextManager 无公开清空 API, 利用增量保存机制: _saved_count 置 -1
        触发全量重写 (先 DELETE 再 INSERT), 空消息列表落库即清空历史
        """
        ctx = self.session._wf.ctx
        try:
            ctx._messages = []
            ctx._saved_count = -1
            ctx.save_context()
        except Exception as e:
            self.messages.append(("系统", f"清空上下文失败: {e}"))
            return
        self.messages = []
        self.messages.append(("系统", "已开始新会话 (上下文历史已清空, 记忆/目标/审批规则保留)"))

    def _auto_progress(self) -> None:
        """yolo 模式: 目标设置后自动连续推进, 直到模型报告完成或达轮数上限

        每轮驱动消息带目标块 (注入器拼接), 模型自主调用工具推进;
        回复以「已完成」开头即停止, 工具审批照常询问 (可干预)
        """
        console.print(f"[dim]… 目标自动推进 (yolo) 已启动, 完成或达 {AUTO_MAX_ROUNDS} 轮上限自动停止 …[/dim]")
        for round_no in range(1, AUTO_MAX_ROUNDS + 1):
            console.print(f"[dim]── 第 {round_no}/{AUTO_MAX_ROUNDS} 轮 ──[/dim]")
            try:
                reply = self.session.run(AUTO_PROMPT, thinking=True)
            except Exception as e:
                self.messages.append(("系统", f"执行出错: {e}"))
                break
            self.messages.append(("助手", reply))
            self.render()
            if reply.strip().startswith("已完成"):
                console.print("[dim]✓ 目标推进完成, 自动停止[/dim]")
                break
        else:
            console.print(
                f"[dim]达 {AUTO_MAX_ROUNDS} 轮上限自动停止 (可用 /goal status 查看进度, /goal done 结束)[/dim]"
            )

    def _process(self, text: str) -> None:
        if text in ("/new", "new", "新会话"):
            self._new_session()
            return
        result, is_cmd = self.session.cmd_handler.process_message(text)
        if is_cmd:
            self.messages.append(("命令", str(result)))
            if str(result).startswith("目标已设置"):
                self.render()
                self._auto_progress()
            return
        self.messages.append(("用户", text))
        self.render()
        console.print("[dim]… 思考中 (工具调用需批准时会在下方询问) …[/dim]")
        try:
            reply = self.session.run(text, thinking=True)
        except Exception as e:
            self.messages.append(("系统", f"执行出错: {e}"))
            console.print()
            return
        console.print()
        if reply:
            self.messages.append(("助手", reply))

    def run_loop(self) -> None:
        while True:
            self.render()
            try:
                text = _ask("输入 > ")
            except (KeyboardInterrupt, EOFError):
                console.print("[yellow]再见[/yellow]")
                return
            stripped = text.strip()
            if not stripped:
                continue
            if stripped in ("exit", "quit", "q", "退出"):
                console.print("[yellow]再见[/yellow]")
                return
            self._process(stripped)


# ================= 入口 =================


def main() -> None:
    parser = argparse.ArgumentParser(description="satrap_coding 插件 TUI Demo")
    parser.add_argument("--demo", action="store_true", help="离线模式 (无需 API key)")
    parser.add_argument("--key-file", type=Path, default=KEY_FILE, help="模型配置文件路径")
    parser.add_argument(
        "--workspace", type=Path, default=DEFAULT_WORKSPACE,
        help="模型可读工作区 (默认沙箱根, 写文件免审批; 传其他目录则工作区写走审批)",
    )
    args = parser.parse_args()

    # 隔离模型工作区: 默认即沙箱根 (一个目录, 写文件免审批); 只读白名单指向该目录
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if not any(workspace.iterdir()):
        (workspace / "README.md").write_text(
            "这是 TUI demo 的工作区/沙箱: 模型在这里读写文件无需审批。\n", encoding="utf-8",
        )
    import satrap.expend.plugins.satrap_coding.tools as tools_mod

    tools_mod.WORKSPACE_ROOT = workspace

    llm: LLM | DemoLLM
    if args.demo:
        llm = DemoLLM()
    else:
        llm = build_real_llm(args.key_file)

    session = SimpleSession(
        SESSION_ID,
        llm,
        db_path=str(CHAT_DB),
        enable_checkpoint=False,
        stream=True,
        return_thinking=True,
        content_callback=_content_forward,
        thinking_callback=_thinking_forward,
    )
    CHAT_DB.parent.mkdir(parents=True, exist_ok=True)
    session.install_plugin(str(PLUGIN_DIR))
    if args.demo:
        demo_llm = llm
        assert isinstance(demo_llm, DemoLLM), "离线模式必须使用 DemoLLM"
        demo_llm.bind(session)

    app = TuiApp(session, demo=args.demo)
    session.user_input_provider = app.ask_user
    app.messages.append(("系统", f"插件已安装: {len(session.list_tools())} 工具 / {len(session.list_commands())} 命令"))
    app.messages.append(("系统", f"工作区 = 沙箱根: {workspace} (沙箱内写文件免审批)"))
    app.run_loop()


if __name__ == "__main__":
    main()
