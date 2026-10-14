"""
ContextManager 编辑 API 单元测试 (add_chat / tool / system / 删除 / 导出 / 截断)

覆盖:
- add_chat / add_tool_message / add_tool_call_flow 消息写入与持久化
- add_at_system_start / add_at_system_end 系统消息拼接 (有/无系统消息)
- del_system_message / del_message / del_last_message / del_last_chat 删除语义
- export_json 导出文件内容
- estimate_token 两种估算方法 + 图片 token 成本
- get_model_context 截断: sliding / mid_truncate / 未知策略
- _group_messages_by_turns 轮次分组
- 异步版核心编辑 API
"""
from pathlib import Path
import pytest
from typing import Any
import json

from satrap.core.utils.context import AsyncContextManager, ContextManager

TOOL_CALLS: list[dict[str, Any]] = [
    {
        "id": "call_1",
        "type": "function",
        "function": {"name": "sum", "arguments": "{\"a\": 1, \"b\": 2}"},
    }
]


# ================= 消息写入 =================

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_add_chat_and_tool_flow(tmp_path: Path, asynchronous: bool):
    """add_chat 追加 user+assistant, add_tool_call_flow 追加模型消息与工具返回 (同步/异步一致)"""
    db = str(tmp_path / "chat_history.db")
    if asynchronous:
        ctx = AsyncContextManager("conv-chat", db_path=db)
        await ctx.initialize()
        await ctx.add_chat("你好", "回复")
        await ctx.add_tool_call_flow("计算", TOOL_CALLS, [{"r": 3}])
    else:
        ctx = ContextManager("conv-chat", db_path=db)
        ctx.add_chat("你好", "回复")
        ctx.add_tool_call_flow("我来计算", TOOL_CALLS, [{"result": 3}])
    messages = ctx.get_context()
    assert [m.get("role") for m in messages] == ["user", "assistant", "assistant", "tool"]
    assert messages[2]["tool_calls"] == TOOL_CALLS
    assert messages[3]["role"] == "tool" and messages[3]["tool_call_id"] == "call_1"
    assert ctx.static_message() == 4


def test_add_tool_message_dict_serialized_to_json(tmp_path: Path):
    """dict 结果转 JSON 字符串, str 结果原样保存"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-tool", db_path=db)
    ctx.add_tool_message("call_1", {"result": 3, "ok": True})
    ctx.add_tool_message("call_2", "纯文本结果")
    messages = ctx.get_context()
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "call_1"
    assert json.loads(str(messages[0]["content"])) == {"result": 3, "ok": True}
    assert messages[1]["content"] == "纯文本结果"


# ================= 系统消息编辑 =================

@pytest.mark.parametrize("scenario, expected", [
    ("start_with_system", "前缀： 原提示"),
    ("start_without_system", "新系统提示"),
    ("end_with_system", "原提示 补充"),
])
def test_add_at_system_positions(tmp_path: Path, scenario: str, expected: str):
    """system 消息按位置拼接: 有系统消息时前后缀, 无系统消息时新建插入"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-sys", db_path=db)
    if scenario == "start_with_system":
        ctx.reset_system_prompt("原提示")
        ctx.add_at_system_start("前缀：", separator=" ")
        assert ctx.get_context()[0]["content"] == expected
    elif scenario == "start_without_system":
        ctx.add_user_message("你好")
        ctx.add_at_system_start("新系统提示")
        messages = ctx.get_context()
        assert messages[0]["role"] == "system" and messages[0]["content"] == expected
        assert messages[1]["role"] == "user"
    else:
        ctx.reset_system_prompt("原提示")
        ctx.add_at_system_end("补充", separator=" ")
        assert ctx.get_context()[0]["content"] == expected


# ================= 删除语义 =================

def test_del_system_message_removes_all_system_messages(tmp_path: Path):
    """del_system_message 删除全部系统消息, 保留其余"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-del-sys", db_path=db)
    ctx.reset_system_prompt("系统提示")
    ctx.add_chat("你好", "回复")
    ctx.del_system_message()
    roles = [m.get("role") for m in ctx.get_context()]
    assert roles == ["user", "assistant"]


def test_del_message_by_index_and_out_of_range(tmp_path: Path):
    """del_message 按索引删除, 越界索引不报错且不删除"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-del-idx", db_path=db)
    ctx.add_chat("一", "A")
    ctx.add_chat("二", "B")
    ctx.del_message(1)   # 删除 assistant A
    assert ctx.static_message() == 3
    ctx.del_message(-1)   # 删除最后一条
    assert ctx.static_message() == 2
    ctx.del_message(99)   # 越界, 保持 2 条
    assert ctx.static_message() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_del_last_message_removes_n_tail_messages(tmp_path: Path, asynchronous: bool):
    """del_last_message 删除末尾 n 条 (同步/异步一致)"""
    db = str(tmp_path / "chat_history.db")
    if asynchronous:
        ctx = AsyncContextManager("conv-del-tail", db_path=db)
        await ctx.initialize()
        await ctx.add_chat("一", "A")
        await ctx.add_chat("二", "B")
        await ctx.del_last_message(2)
    else:
        ctx = ContextManager("conv-del-tail", db_path=db)
        ctx.add_chat("一", "A")
        ctx.add_chat("二", "B")
        ctx.del_last_message(2)
    assert ctx.static_message() == 2
    assert ctx.get_context()[1]["content"] == "A"


def test_del_last_chat_removes_n_groups(tmp_path: Path):
    """del_last_chat 按组删除 (user+assistant 为一组)"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-del-group", db_path=db)
    ctx.add_chat("一", "A")
    ctx.add_chat("二", "B")
    ctx.add_chat("三", "C")
    ctx.del_last_chat(2)
    assert ctx.static_message() == 2
    assert ctx.get_context()[1]["content"] == "A"


def test_del_last_chat_stops_at_system_message(tmp_path: Path):
    """del_last_chat 遇到系统消息停止删除"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-del-group2", db_path=db)
    ctx.reset_system_prompt("系统")
    ctx.add_chat("一", "A")
    ctx.del_last_chat(5)
    assert ctx.static_message() == 1
    assert ctx.get_context()[0]["role"] == "system"


# ================= 导出 =================

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_export_json_writes_conversation_file(tmp_path: Path, asynchronous: bool):
    """export_json 导出 id + messages 到 JSON 文件 (同步/异步一致)"""
    db = str(tmp_path / "chat_history.db")
    out = str(tmp_path / "export.json")
    if asynchronous:
        ctx = AsyncContextManager("conv-export", db_path=db)
        await ctx.initialize()
        await ctx.add_chat("你好", "回复")
        await ctx.export_json(out)
    else:
        ctx = ContextManager("conv-export", db_path=db)
        ctx.add_chat("你好", "回复")
        ctx.export_json(out)
    with open(out, encoding="utf-8") as f:
        data = json.load(f)
    assert data["id"] == "conv-export"
    assert len(data["messages"]) == 2
    assert data["messages"][0]["role"] == "user"


# ================= token 估算 =================

def test_estimate_token_counts_image_cost(tmp_path: Path):
    """带图片内容的消息计入图片 token 成本"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-token-img", db_path=db)
    ctx.add_user_message("看图", img_urls=["/tmp/a.png"])
    base = ctx.estimate_token(method="experience")
    ctx.add_user_message("纯文本")
    with_img = ctx.estimate_token(method="experience")
    assert with_img > base


# ================= 截断 =================

def _build_long_context(db: str, exceed_process: str, rounds: int = 12) -> ContextManager:
    """构造超长上下文 (max_context 缩小, 写入多轮消息)"""
    ctx = ContextManager(
        "conv-trunc",
        db_path=db,
        max_context=2000,
        context_threshold=0.9,
        exceed_process=exceed_process,
    )
    ctx.reset_system_prompt("系统提示词")
    for i in range(rounds):
        ctx.add_chat(f"第{i}轮用户消息", f"第{i}轮模型回复" * 50)
    return ctx


def test_get_model_context_truncates_with_sliding(tmp_path: Path):
    """sliding 截断: 超限时保留系统消息和最近轮次, 返回副本"""
    db = str(tmp_path / "chat_history.db")
    ctx = _build_long_context(db, "sliding")
    raw = ctx.get_context()
    truncated = ctx.get_model_context()
    assert truncated[0]["role"] == "system"
    assert truncated[-1] == raw[-1]   # 最近一条保留
    assert len(truncated) < len(raw)   # 发生了截断
    assert truncated is not raw   # 不修改原列表


def test_get_model_context_within_threshold_returns_copy(tmp_path: Path):
    """未超限时返回原列表副本"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-trunc2", db_path=db)
    ctx.add_chat("你好", "回复")
    result = ctx.get_model_context()
    assert result == ctx.get_context()
    assert result is not ctx.get_context()


def test_mid_truncate_keeps_head_and_tail(tmp_path: Path):
    """mid_truncate: 轮次较多时从中间删除, 保留首尾"""
    db = str(tmp_path / "chat_history.db")
    ctx = _build_long_context(db, "mid_truncate")
    truncated = ctx.get_model_context(method="experience")
    assert truncated[0]["role"] == "system"
    assert truncated[-1] == ctx.get_context()[-1]


def test_mid_truncate_falls_back_to_sliding_when_few_turns(tmp_path: Path):
    """mid_truncate 轮次过少时退化为滑动窗口 (可全部删除)"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager(
        "conv-trunc3",
        db_path=db,
        max_context=100,
        context_threshold=0.5,
        exceed_process="mid_truncate",
    )
    ctx.add_chat("长消息" * 100, "长回复" * 100)
    result = ctx.get_model_context()
    assert len(result) < 2   # 退化不报错, 可能仅剩系统消息或全部删除


def test_unknown_exceed_process_returns_original(tmp_path: Path):
    """未知截断策略记录错误并返回原列表副本"""
    db = str(tmp_path / "chat_history.db")
    ctx = _build_long_context(db, "unknown_strategy")
    raw = ctx.get_context()
    result = ctx.get_model_context()
    assert result == raw


def test_group_messages_by_turns(tmp_path: Path):
    """轮次分组: 系统消息独立成组, 每组以 user 开头"""
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-groups", db_path=db)
    ctx.reset_system_prompt("系统")
    ctx.add_chat("一", "A")
    ctx.add_tool_message("call_1", {"r": 1})
    ctx.add_chat("二", "B")
    turns = ctx._group_messages_by_turns(ctx.get_context())
    assert len(turns) == 3
    assert [m["role"] for m in turns[0]] == ["system"]
    assert [m["role"] for m in turns[1]] == ["user", "assistant", "tool"]
    assert [m["role"] for m in turns[2]] == ["user", "assistant"]
