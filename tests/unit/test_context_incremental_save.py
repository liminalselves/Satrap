"""
消息增量保存 (save_context 增量追加) 单元测试

覆盖:
- 纯追加走增量 INSERT, 行 id 稳定, 无重复
- 编辑操作 (reset_system_prompt / del_context / del_message / del_last_chat) 触发全量重写
- 外部直接修改 _messages 后 _mark_dirty 持久化
- 外部 append 消息随下次保存落库
- keep_in_memory 手动 save_context
- 异步版增量保存
- 写失败后水位恢复, 重试不产生重复行
"""
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from satrap.core.utils.context import AsyncContextManager, ContextManager


def _row_ids(db_path: str, conversation_id: str) -> list[int]:
    """
    读取库中指定对话的消息行 id

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID

    返回:
    - list[int]: 读取库中指定对话的消息行 id
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM chat_history WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]
    finally:
        conn.close()


def _row_contents(db_path: str, conversation_id: str) -> list[dict[str, Any]]:
    """
    读取库中指定对话的消息内容

    参数:
    - db_path: 数据库路径
    - conversation_id: 会话 ID

    返回:
    - list[dict[str, Any]]: 读取库中指定对话的消息内容
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [dict[str, Any](row) for row in rows]
    finally:
        conn.close()


def test_incremental_append_stable_ids_no_duplicates(tmp_path: Path):
    """
    纯追加只 INSERT 尾部新消息, 行 id 稳定且不重复

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-inc", db_path=db)
    ctx.add_user_message("一")
    ctx.add_bot_message("回复一")
    ids_after_2 = _row_ids(db, "conv-inc")
    assert ids_after_2 == [1, 2]

    ctx.add_user_message("二")
    ctx.add_bot_message("回复二")
    ids_after_4 = _row_ids(db, "conv-inc")
    assert ids_after_4 == [1, 2, 3, 4]   # 追加, 原行 id 不变

    loaded = ContextManager("conv-inc", db_path=db)
    assert len(loaded.get_context()) == 4
    assert loaded.get_context()[0]["content"] == "一"


def test_incremental_append_then_reload_roundtrip(tmp_path: Path):
    """
    增量写入后重新加载内容与顺序完整

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-r", db_path=db)
    for i in range(10):
        ctx.add_user_message(f"用户{i}")
        ctx.add_bot_message(f"回复{i}")

    loaded = ContextManager("conv-r", db_path=db)
    contents = [m["content"] for m in loaded.get_context()]
    expected = [
        f"用户{i}" if j == 0 else f"回复{i}" for i in range(10) for j in range(2)
    ]
    assert contents == expected


def test_reset_system_prompt_triggers_full_rewrite(tmp_path: Path):
    """
    编辑操作 (reset_system_prompt) 走全量重写, 库内容与内存一致

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-e", db_path=db)
    ctx.add_user_message("对话一")
    ctx.reset_system_prompt("新系统提示")

    loaded = ContextManager("conv-e", db_path=db)
    roles = [m["role"] for m in loaded.get_context()]
    assert roles == ["system", "user"]
    assert loaded.get_context()[0]["content"] == "新系统提示"


def test_del_context_and_del_message_persist(tmp_path: Path):
    """
    del_context / del_message 后保存, 重新加载与内存一致

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-d", db_path=db)
    ctx.add_user_message("一")
    ctx.add_user_message("二")
    ctx.add_user_message("三")

    ctx.del_message(1)   # 删除"二"
    ctx.add_user_message("四")
    ctx.del_last_chat(1)   # 删除最后一组 (四)

    loaded = ContextManager("conv-d", db_path=db)
    assert [m["content"] for m in loaded.get_context()] == ["一", "三"]


def test_external_append_persisted_on_next_save(tmp_path: Path):
    """
    外部直接 append 消息, 下次保存时尾部插入落库

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-x", db_path=db)
    ctx.add_user_message("一")

    ctx.get_context().append({"role": "user", "content": "外部追加"})
    ctx.save_context()

    loaded = ContextManager("conv-x", db_path=db)
    assert [m["content"] for m in loaded.get_context()] == ["一", "外部追加"]


def test_external_edit_with_mark_dirty_persists(tmp_path: Path):
    """
    外部修改已保存消息内容 + _mark_dirty, 全量重写落库

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-m", db_path=db)
    ctx.add_user_message("旧内容")

    ctx.get_context()[0]["content"] = "新内容"
    ctx._mark_dirty()
    ctx.save_context()

    loaded = ContextManager("conv-m", db_path=db)
    assert loaded.get_context()[0]["content"] == "新内容"


def test_keep_in_memory_manual_save(tmp_path: Path):
    """
    keep_in_memory=True 时手动 save_context 落库, 增量/全量均正确

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-k", db_path=db, keep_in_memory=True)
    ctx.add_user_message("一")
    ctx.add_user_message("二")
    ctx.save_context()

    loaded = ContextManager("conv-k", db_path=db)
    assert len(loaded.get_context()) == 2

    ctx.del_context()
    # 编辑后手动保存
    ctx.save_context()
    loaded2 = ContextManager("conv-k", db_path=db)
    assert loaded2.get_context() == []


def test_checkpoint_pointer_still_works_with_incremental_save(tmp_path: Path):
    """
    增量保存下指针检查点截断恢复仍准确 (行 id 稳定, 水位=COUNT)

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-c", db_path=db, enable_checkpoint=True, auto_checkpoint=False)
    ctx.add_user_message("一")
    ctx.add_user_message("二")
    cp = ctx.create_checkpoint(name="到二")
    assert cp.position == 2

    ctx.add_user_message("三")
    ctx.add_user_message("四")
    ctx.rollback(cp.checkpoint_id)

    assert [m["content"] for m in ctx.get_context()] == ["一", "二"]


@pytest.mark.asyncio
async def test_async_incremental_append(tmp_path: Path):
    """
    异步版增量保存: 追加落库 + 编辑全量重写

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "chat_history.db")
    ctx = AsyncContextManager("conv-a", db_path=db)
    await ctx.initialize()
    await ctx.add_user_message("一")
    await ctx.add_bot_message("回复一")
    await ctx.add_user_message("二")

    loaded = AsyncContextManager("conv-a", db_path=db)
    await loaded.initialize()
    assert [m["content"] for m in loaded.get_context()] == ["一", "回复一", "二"]

    await loaded.reset_system_prompt("异步系统")
    assert [m["role"] for m in loaded.get_context()] == ["system", "user", "assistant", "user"]

    loaded2 = AsyncContextManager("conv-a", db_path=db)
    await loaded2.initialize()
    assert loaded2.get_context()[0]["content"] == "异步系统"


def test_full_rewrite_failure_restores_dirty_watermark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    全量重写失败后水位恢复为 dirty, 下次保存重试不产生重复行

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-fw", db_path=db)
    ctx.add_user_message("一")
    ctx.add_bot_message("回复一")

    ctx._mark_dirty()   # 模拟编辑, 下次保存走全量重写
    ctx._messages.append({"role": "user", "content": "二"})   # 外部修改

    fake = MagicMock()
    fake.commit.side_effect = RuntimeError("disk full")
    monkeypatch.setattr(ctx, "_get_conn", lambda: fake)
    ctx.save_context()
    assert ctx._saved_count == -1   # 水位恢复到 dirty, 下次仍走全量重写

    monkeypatch.undo()
    ctx.save_context()
    contents = _row_contents(db, "conv-fw")
    assert [m["content"] for m in contents] == ["一", "回复一", "二"]   # 无重复


def test_incremental_failure_keeps_watermark_no_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    增量 INSERT 失败后水位不推进, 下次保存重试同批消息无重复

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    db = str(tmp_path / "chat_history.db")
    ctx = ContextManager("conv-iw", db_path=db)
    ctx.add_user_message("一")
    ctx.add_bot_message("回复一")

    ctx._messages.append({"role": "user", "content": "二"})   # 外部追加, 未落库

    fake = MagicMock()
    fake.commit.side_effect = RuntimeError("disk full")
    monkeypatch.setattr(ctx, "_get_conn", lambda: fake)
    ctx.save_context()
    assert ctx._saved_count == 2   # 水位未推进

    monkeypatch.undo()
    ctx.save_context()
    contents = _row_contents(db, "conv-iw")
    assert [m["content"] for m in contents] == ["一", "回复一", "二"]
