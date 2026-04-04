import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional, Callable
from satrap.core.APICall.LLMCall import LLM
from satrap.core.framework import ModelWorkflowFramework
from satrap.core.log import logger
from satrap.core.utils.TCBuilder import ToolsManager
from satrap.core.utils.sandbox import CodeSandbox
from satrap.expend.sandbox_tools import CodeSandboxTool


@dataclass
class DemoConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    sandbox_path: str = "./sandbox/coding_agent_demo"
    python_executable: str = sys.executable
    temperature: float = 0.2
    max_tokens: int = 32768
    max_tool_rounds: int = 300


class CodingAgentWorkflow(ModelWorkflowFramework):
    """基于 Satrap 框架的简单编码代理工作流，使用父类的 agent_executor 处理工具调用。"""

    def __init__(
        self,
        llm: LLM,
        context_id: str,
        tools_manager: ToolsManager,
        system_prompt: str,
        max_tool_rounds: int = 999,
        content_callback: Optional[Callable[[str], None]] = None,
        return_thinking: bool = True,
    ):
        super().__init__(llm, context_id, tools_manager, system_prompt, content_callback=content_callback, return_thinking=return_thinking)
        self.max_tool_rounds = max_tool_rounds
        self.return_thinking = return_thinking

    def forward(self, user_input: str) -> str:
        self.ctx.add_user_message(user_input)

        initial_response = self.llm.call(
            self.ctx.get_context(),
            tools=self.tools_manager.get_tools_definitions(),
            tool_choice="auto",
        )

        if isinstance(initial_response, bool):
            error_msg = "模型调用失败。请检查 API 配置和日志。"
            if self.content_callback:
                self.content_callback(error_msg)
            return error_msg

        # 使用父类的 agent_executor 继续处理可能的工具调用
        context= self.agent_executor(
            initial_response,
            callback=True,
            max_iterations=self.max_tool_rounds
        )

        return "未找到助手响应。"


def build_system_prompt() -> str:
    return (
        "你是一个编码代理。"
        "使用 code_sandbox 工具来读取、写入、列出和运行代码。"
        "不要编造执行结果。"
        "简要规划，然后执行，最后总结改变了什么以及为什么。"
        "你可以使用 tools_call 调用 code_sandbox 工具。"
        "code_sandbox有以下操作:"
        "run（执行代码字符串）、run_file（执行文件）、save（保存代码到文件）、read（读取文件内容）、delete（删除文件）、delete_dir（删除目录）、list（列出文件）。"
        "参数:"
            "operation(string): 要执行的操作，可选值：'run', 'run_file', 'save', 'read', 'delete', 'delete_dir', 'list'。"
            "code(string): 当操作为'run'或'save'时，需要提供的代码字符串。"
            "path(string): 当操作为'save','run_file','read','delete','delete_dir','list'时，需要的文件或目录路径（相对于沙箱根目录）。"
    )


def build_workflow(cfg: DemoConfig, content_callback: Optional[Callable[[str], None]] = None) -> CodingAgentWorkflow:
    llm = LLM(
        api_key=cfg.api_key,
        base_url=cfg.base_url or None,
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )

    sandbox = CodeSandbox(cfg.sandbox_path, cfg.python_executable)
    tools_manager = ToolsManager()
    tools_manager.register_tool(CodeSandboxTool(sandbox))

    context_id = f"coding_agent_demo_{int(time.time())}"
    return CodingAgentWorkflow(
        llm=llm,
        context_id=context_id,
        tools_manager=tools_manager,
        system_prompt=build_system_prompt(),
        max_tool_rounds=cfg.max_tool_rounds,
        content_callback=content_callback,
    )


def run_once(cfg: DemoConfig, message: str) -> str:
    wf = build_workflow(cfg)  # 单次运行不需要回调
    return wf(message)


def run_repl(cfg: DemoConfig):
    # 定义实时回调：逐块打印模型输出（不换行，实现流式效果）
    def on_content(content: str):
        print(content, flush=True)

    wf = build_workflow(cfg, content_callback=on_content)
    print("编码代理演示已启动。输入 'quit' 退出。")

    while True:
        user_input = input("\n你> ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit", "q"}:
            print("再见。")
            break

        # 工作流内部会通过回调实时输出内容，此处不再重复打印
        wf(user_input)
        # 输出换行，使下一轮提示符出现在新行
        print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Satrap 编码代理演示")
    parser.add_argument("--message", type=str, default="", help="单轮用户任务")
    parser.add_argument("--api-key", type=str, default="", help="模型 API 密钥")
    parser.add_argument("--base-url", type=str, default="", help="兼容 OpenAI 的基础 URL")
    parser.add_argument("--model", type=str, default="", help="模型名称")
    parser.add_argument(
        "--sandbox-path",
        type=str,
        default="./sandbox/coding_agent_demo",
        help="沙箱目录",
    )
    parser.add_argument(
        "--python-executable",
        type=str,
        default=sys.executable,
        help="用于沙箱代码执行的 Python 解释器",
    )
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> DemoConfig:
    api_key = ''
    base_url = "https://api.deepseek.com/v1"
    model = "deepseek-reasoner"

    return DemoConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        sandbox_path=args.sandbox_path,
        python_executable=args.python_executable,
    )


def validate_config(cfg: DemoConfig) -> None:
    missing = []
    if not cfg.api_key:
        missing.append("api_key")
    if not cfg.model:
        missing.append("model")

    if missing:
        raise ValueError(
            "缺少模型配置："
            + "，".join(missing)
            + "。请在 examples/coding_agent_demo.py 中填写默认值，"
            + "或通过命令行参数 / 环境变量传入。"
        )


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args)

    try:
        validate_config(config)
    except ValueError as exc:
        logger.error(str(exc))
        print(str(exc))
        raise SystemExit(1)

    if args.message:
        print(run_once(config, args.message))
    else:
        run_repl(config)

# 帮我写一个投骰子程序,支持4/6/8/12/20/100面骰,并做好测试
