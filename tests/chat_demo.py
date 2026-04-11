# -*- coding: utf-8 -*-
import asyncio
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import streamlit as st

# 切换到项目根目录，避免相对路径导致日志/数据库路径异常。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(str(PROJECT_ROOT))

from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.framework import AsyncModelWorkflowFramework, AsyncSession, SessionManager
from satrap.core.framework.command import AsyncCommandHandler
from satrap.core.type import SessionConfig, UserCall
from satrap.core.utils.TCBuilder import AsyncToolsManager
from satrap.expend import AsyncCodeSandboxTool, AsyncFetchPageTool, AsyncSearchTool


APP_TITLE = "Pinnacle Lite"
SESSION_TYPE = "async_my"
DEFAULT_SYSTEM_PROMPT = "你是一个有帮助的助手。"


# 全局会话管理器：
# - register_session 时分配并持久化 session_id
# - handle_call_async 时按 session_id 读取 SessionConfig 并实例化
manager = SessionManager(
    default_session_type=SESSION_TYPE,
    max_size=100,
    idle_timeout=1800,
)


def build_tools_manager() -> AsyncToolsManager:
    """构建工具管理器。"""
    tools_manager = AsyncToolsManager()
    tools_manager.register_tool(AsyncCodeSandboxTool(sandbox="sandbox"))
    tools_manager.register_tool(AsyncSearchTool())
    tools_manager.register_tool(AsyncFetchPageTool())
    return tools_manager


def extract_think_from_events(events: list[str]) -> str:
    """从回调事件中提取 <think>...</think> 内容。"""
    think_parts: list[str] = []
    for event in events:
        matched = re.findall(r"<think>\s*(.*?)\s*</think>", str(event), flags=re.S)
        if matched:
            for item in matched:
                cleaned = item.strip()
                if cleaned:
                    think_parts.append(cleaned)
    return "\n\n".join(think_parts)


def build_session_payload(
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    max_tokens: int,
    system_prompt: str,
) -> Dict[str, Any]:
    """构建可持久化到 SessionConfig.session_config 的运行配置。"""
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "system_prompt": system_prompt,
    }


class AsyncMyWF(AsyncModelWorkflowFramework):
    """异步工作流：写入用户消息 -> 调用模型 -> 处理工具调用 -> 返回助手消息。"""

    def __init__(
        self,
        llm: AsyncLLM,
        tools_manager: AsyncToolsManager | None = None,
        content_callback=None,
        context_id: str = "",
        system_prompt: str = "",
    ):
        super().__init__(
            llm=llm,
            tools_manager=tools_manager,
            content_callback=content_callback,
            context_id=context_id,
            system_prompt=system_prompt if system_prompt else None,
        )

    async def forward(self, query: str) -> str:
        await self.ctx.add_user_message(query)
        first_msg = await self.llm.call(
            self.ctx.get_context(),
            tools=self.tools_manager.get_tools_definitions(),
            tool_choice="auto",
        )

        if first_msg is False:
            return "模型调用失败，请检查 API Key / Base URL / 模型名。"

        # callback=True：会把 think/content 通过 content_callback 回传。
        context, success = await self.agent_executor(first_msg, callback=True)
        if not success:
            return "模型调用失败，请检查日志。"

        return self.get_bot_message(context)


class AsyncMySession(AsyncSession):
    """会话类。

    关键点：
    - 构造函数接收 SessionConfig（由 SessionManager 持久化并在实例化时传入）
    - 运行配置从 session_config.session_config 中读取
    - 回调事件缓存在实例内，可供 UI 在每轮请求后提取 think 内容
    """

    def __init__(self, session_config: SessionConfig):
        self._session_config_obj = session_config
        cfg = dict(session_config.session_config or {})

        sid = session_config.session_id or uuid.uuid4().hex
        self.system_prompt = str(cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)

        self.llm = AsyncLLM(
            api_key=str(cfg.get("api_key") or ""),
            model=str(cfg.get("model") or ""),
            base_url=str(cfg.get("base_url") or ""),
            max_tokens=int(cfg.get("max_tokens") or 4096),
            temperature=float(cfg.get("temperature") or 0.7),
        )
        self.tools_manager = build_tools_manager()

        self._events: list[str] = []

        async def output_callback(message: str):
            self._events.append(str(message))

        command_handler = AsyncCommandHandler(
            output_callback=output_callback,
            cmd_prefix="/",
            param_split=" ",
        )
        super().__init__(
            session_id=sid,
            content_callback=output_callback,
            command_handler=command_handler,
        )

    async def _async_init(self):
        self.wf = AsyncMyWF(
            llm=self.llm,
            tools_manager=self.tools_manager,
            content_callback=self._content_callback,
            context_id=self.session_id,
            system_prompt=self.system_prompt,
        )
        self._register_commands()

    def drain_events(self) -> list[str]:
        """提取并清空本轮累计事件。"""
        events = list(self._events)
        self._events.clear()
        return events

    def _register_commands(self):
        """注册命令控制。"""

        async def clear_cmd():
            await self.wf.ctx.del_context()
            if self.system_prompt:
                await self.wf.ctx.reset_system_prompt(self.system_prompt)
            return "已清空当前会话上下文。"

        async def status_cmd():
            msg_count = self.wf.ctx.static_message()
            return (
                f"会话ID: {self.session_id}\n"
                f"消息数: {msg_count}\n"
                f"模型: {self.llm.get_model()}\n"
                f"Base URL: {self.llm.get_base_url()}"
            )

        async def system_cmd(*args):
            if not args:
                return f"当前 system prompt:\n{self.system_prompt}"
            new_prompt = " ".join(args).strip()
            if not new_prompt:
                return "system prompt 不能为空。"

            self.system_prompt = new_prompt
            await self.wf.ctx.reset_system_prompt(new_prompt)

            # 仅更新当前 session 实例内存；持久化会在下一轮 handle_user_message 前统一写库。
            return "已更新当前会话的 system prompt。"

        self.command_handler.register_command("clear", clear_cmd, intro="清空当前会话上下文")
        self.command_handler.register_command("status", status_cmd, intro="查看当前会话状态")
        self.command_handler.register_command("system", system_cmd, intro="查看或设置 system prompt")

    async def run(self, user_input: str) -> str:
        cmd_result, is_cmd = await self.cmd_process(user_input)
        if is_cmd:
            return str(cmd_result) if cmd_result is not None else "命令已执行。"
        return await self.wf.forward(user_input)


# 明确绑定会话类型到自定义会话类，避免在“已有持久化 session_id”场景下
# 回退到 SessionManager 初始化时的默认 Session（会导致回复为空）。
manager.register_session_type(SESSION_TYPE, AsyncMySession)


def create_backend_session(payload: Dict[str, Any]) -> str:
    """注册一个新会话并返回持久化 session_id。"""
    session_cfg = manager.register_session(
        session_class=AsyncMySession,
        session_type_name=SESSION_TYPE,
        session_config=payload,
    )
    return session_cfg.session_id or uuid.uuid4().hex


def upsert_backend_session_config(session_id: str, payload: Dict[str, Any]):
    """更新指定 session_id 的持久化配置。

    这样当会话实例被淘汰后，下次恢复还能按最新配置运行。
    """
    cfg = manager.get_session_config(session_id)
    now = time.time()

    if cfg is None:
        manager.register_session(
            session_class=AsyncMySession,
            session_type_name=SESSION_TYPE,
            session_config=payload,
            session_id=session_id,
        )
        return

    cfg.session_type_name = SESSION_TYPE
    cfg.last_used_at = now
    cfg.session_config = dict(payload)
    manager.store.upsert(cfg)


async def handle_user_message(
    session_id: str,
    message: str,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    max_tokens: int,
    system_prompt: str,
) -> Dict[str, Any]:
    """把用户输入交给 SessionManager 处理，并返回 think 与 answer。"""

    payload = build_session_payload(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        system_prompt=system_prompt,
    )
    upsert_backend_session_config(session_id, payload)

    user_call = UserCall(
        session_id=session_id,
        session_type=SESSION_TYPE,
        message=message,
        img_urls=None,
    )
    answer = await manager.handle_call_async(user_call)

    # 从活跃会话实例中提取回调事件。
    events: list[str] = []
    entry = manager.pool.list_entries().get(session_id)
    if entry is not None and isinstance(entry.session, AsyncMySession):
        events = entry.session.drain_events()

    think_text = extract_think_from_events(events)
    return {
        "answer": answer,
        "think": think_text,
        "is_command": message.strip().startswith("/"),
    }


def init_state():
    """初始化 Streamlit 状态。"""
    if "api_key" not in st.session_state:
        st.session_state.api_key = ""
    if "base_url" not in st.session_state:
        st.session_state.base_url = "https://api.deepseek.com/v1"
    if "model" not in st.session_state:
        st.session_state.model = "deepseek-reasoner"
    if "temperature" not in st.session_state:
        st.session_state.temperature = 0.7
    if "max_tokens" not in st.session_state:
        st.session_state.max_tokens = 4096

    if "conversations" not in st.session_state:
        first_payload = build_session_payload(
            api_key=st.session_state.api_key,
            base_url=st.session_state.base_url,
            model=st.session_state.model,
            temperature=float(st.session_state.temperature),
            max_tokens=int(st.session_state.max_tokens),
            system_prompt=DEFAULT_SYSTEM_PROMPT,
        )
        first_id = create_backend_session(first_payload)
        st.session_state.conversations = {
            first_id: {
                "title": "新对话",
                "messages": [],
                "system_prompt": DEFAULT_SYSTEM_PROMPT,
            }
        }
        st.session_state.active_session_id = first_id


def ensure_active_session():
    """确保当前激活会话有效。"""
    conversations = st.session_state.conversations
    if st.session_state.active_session_id not in conversations:
        if conversations:
            st.session_state.active_session_id = next(iter(conversations.keys()))
        else:
            payload = build_session_payload(
                api_key=st.session_state.api_key,
                base_url=st.session_state.base_url,
                model=st.session_state.model,
                temperature=float(st.session_state.temperature),
                max_tokens=int(st.session_state.max_tokens),
                system_prompt=DEFAULT_SYSTEM_PROMPT,
            )
            sid = create_backend_session(payload)
            conversations[sid] = {
                "title": "新对话",
                "messages": [],
                "system_prompt": DEFAULT_SYSTEM_PROMPT,
            }
            st.session_state.active_session_id = sid


def render_sidebar():
    """渲染侧边栏配置。"""
    with st.sidebar:
        st.subheader("模型配置")
        st.session_state.api_key = st.text_input("API Key", value=st.session_state.api_key, type="password")
        st.session_state.base_url = st.text_input("Base URL", value=st.session_state.base_url)
        st.session_state.model = st.text_input("Model", value=st.session_state.model)
        st.session_state.temperature = st.slider("Temperature", 0.0, 1.5, float(st.session_state.temperature), 0.1)
        st.session_state.max_tokens = st.number_input(
            "Max Tokens",
            min_value=128,
            max_value=32768,
            value=int(st.session_state.max_tokens),
            step=128,
        )

        st.caption("命令控制: `/help`、`/clear`、`/status`、`/system 新提示词`")

        st.divider()
        st.subheader("历史会话")

        if st.button("新建对话", use_container_width=True):
            payload = build_session_payload(
                api_key=st.session_state.api_key,
                base_url=st.session_state.base_url,
                model=st.session_state.model,
                temperature=float(st.session_state.temperature),
                max_tokens=int(st.session_state.max_tokens),
                system_prompt=DEFAULT_SYSTEM_PROMPT,
            )
            sid = create_backend_session(payload)
            st.session_state.conversations[sid] = {
                "title": "新对话",
                "messages": [],
                "system_prompt": DEFAULT_SYSTEM_PROMPT,
            }
            st.session_state.active_session_id = sid
            st.rerun()

        session_ids = list(st.session_state.conversations.keys())
        current_idx = session_ids.index(st.session_state.active_session_id)
        selected_id = st.selectbox(
            "选择会话",
            options=session_ids,
            index=current_idx,
            format_func=lambda sid: st.session_state.conversations[sid]["title"],
        )
        st.session_state.active_session_id = selected_id

        if st.button("删除当前会话", use_container_width=True):
            sid = st.session_state.active_session_id
            st.session_state.conversations.pop(sid, None)
            manager.remove_session(sid, remove_config=True)
            ensure_active_session()
            st.rerun()


def main():
    st.set_page_config(page_title=APP_TITLE, page_icon=":speech_balloon:", layout="wide")
    st.title(APP_TITLE)

    init_state()
    ensure_active_session()
    render_sidebar()

    session_id = st.session_state.active_session_id
    conversation = st.session_state.conversations[session_id]

    conversation["system_prompt"] = st.text_area(
        "System Prompt",
        value=conversation.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
        height=100,
        help="当前会话专属 System Prompt",
    )

    for msg in conversation["messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and msg.get("think"):
                with st.expander("Think", expanded=False):
                    st.markdown(msg["think"])

    user_input = st.chat_input("输入消息...")
    if not user_input:
        return

    if not st.session_state.api_key.strip():
        st.warning("请先在左侧输入 API Key。")
        return

    conversation["messages"].append({"role": "user", "content": user_input})
    if len(conversation["messages"]) == 1:
        conversation["title"] = user_input[:20] if user_input.strip() else "新对话"

    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("思考中...")
        think_text = ""
        try:
            result = asyncio.run(
                handle_user_message(
                    session_id=session_id,
                    message=user_input,
                    api_key=st.session_state.api_key.strip(),
                    base_url=st.session_state.base_url.strip(),
                    model=st.session_state.model.strip(),
                    temperature=float(st.session_state.temperature),
                    max_tokens=int(st.session_state.max_tokens),
                    system_prompt=conversation["system_prompt"],
                )
            )
            answer = str(result.get("answer", ""))
            think_text = str(result.get("think", "") or "")

            if result.get("is_command") and user_input.strip().startswith("/clear"):
                conversation["messages"] = []
                conversation["title"] = "新对话"
        except Exception as e:
            answer = f"请求失败：{e}"

        placeholder.markdown(answer)
        if think_text:
            with st.expander("Think", expanded=False):
                st.markdown(think_text)

    conversation["messages"].append({"role": "assistant", "content": answer, "think": think_text})


if __name__ == "__main__":
    main()
