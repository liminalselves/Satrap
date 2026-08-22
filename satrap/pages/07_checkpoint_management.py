from __future__ import annotations

import datetime
from typing import Any

import streamlit as st
from satrap.core.state import StateStore
from satrap.core.type import StateScope, safe_getattr_str
from satrap.core.utils.context import ContextManager
from satrap.core.utils.paths import get_db_path

st.set_page_config(page_title="检查点管理", page_icon="", layout="wide")


def _db_path() -> str:
    """
    检查点/上下文库路径: 显式配置 > 会话库 > 默认

    返回:
    - str: 检查结果
    """
    config = st.session_state.config
    return str(
        safe_getattr_str(config, "session_checkpoint_db")
        or safe_getattr_str(config, "session_db_path")
        or get_db_path("chat_history.db")
    )


def _fmt_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _rows(items: list[Any], extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """
    检查点对象 -> 展示行

    参数:
    - items: 条目列表
    - extra: 附加数据

    返回:
    - list[dict[str, Any]]: 检查结果
    """
    rows: list[dict[str, Any]] = []
    for cp in items:
        row: dict[str, Any] = {
            "类型": cp.checkpoint_kind,
            "名称": cp.name or "-",
            "水位": cp.position,
            "版本": cp.state_revision,
            "批次": cp.batch_id or "-",
            "来源": cp.source,
            "原因": cp.reason or "-",
            "时间": _fmt_time(cp.created_at),
            "ID": cp.checkpoint_id,
        }
        if extra:
            row.update(extra)
        rows.append(row)
    return rows


@st.dialog("创建检查点")
def _create_dialog(db: str, conv: str):
    st.write(f"对话: `{conv}`")
    name = st.text_input("名称", placeholder="可选, 默认自动命名")
    description = st.text_area("说明", placeholder="可选")
    if st.button("创建"):
        ctx = ContextManager(conv, db_path=db, enable_checkpoint=True)
        try:
            cp = ctx.create_checkpoint(name=name, description=description)
            st.success(f"已创建: `{cp.checkpoint_id}`")
        except Exception as e:
            st.error(f"创建失败: {e}")
        finally:
            ctx.close()
        st.rerun()


@st.dialog("回滚 / 重试")
def _revert_dialog(db: str, conv: str, checkpoints: list[Any]):
    st.write(f"对话: `{conv}`")
    options = [cp.checkpoint_id for cp in checkpoints]
    cid = st.selectbox("选择检查点", options, format_func=lambda c: f"{c}  [{next(cp.checkpoint_kind for cp in checkpoints if cp.checkpoint_id == c)}]") or ""
    mode = st.radio("方式", ["回滚 (删除未来检查点)", "重试 (保留未来检查点)"])
    if st.button("执行"):
        ctx = ContextManager(conv, db_path=db, enable_checkpoint=True)
        try:
            if mode.startswith("回滚"):
                ctx.rollback(cid)
            else:
                ctx.retry(cid)
            st.success(f"已{mode.split(' ')[0]}: `{cid}`")
        except Exception as e:
            st.error(f"操作失败: {e}")
        finally:
            ctx.close()
        st.rerun()


@st.dialog("Fork 分支")
def _fork_dialog(db: str, conv: str, checkpoints: list[Any]):
    st.write(f"对话: `{conv}`")
    branch_name = st.text_input("分支名", placeholder="如 retry_v2")
    options = [cp.checkpoint_id for cp in checkpoints]
    cid = st.selectbox("从检查点 (默认最新)", options, format_func=lambda c: c)
    if st.button("Fork"):
        ctx = ContextManager(conv, db_path=db, enable_checkpoint=True)
        try:
            new_ctx = ctx.fork(branch_name, checkpoint_id=cid)
            st.success(f"已分支: `{new_ctx.conversation_id}`")
            st.info("在左侧输入新对话 ID 即可查看该分支")
            new_ctx.close()
        except Exception as e:
            st.error(f"Fork 失败: {e}")
        finally:
            ctx.close()
        st.rerun()


st.title("检查点管理")
st.caption("直接操作上下文库 (与运行时 Session 解耦), 支持创建 / 回滚 / 重试 / 分支 / 血缘 / 审计")

conv = st.text_input(
    "对话 ID",
    placeholder="如 conv-xxx, 或 fork 出的分支 ID",
    key="ckpt_conv",
)

if not conv:
    st.info("输入对话 ID 后查看其检查点, 分支与变更记录")
    st.stop()

db = _db_path()
try:
    store = StateStore(db_path=db)
    ctx = ContextManager(conv, db_path=db, enable_checkpoint=True)
    try:
        checkpoints = ctx.list_checkpoints()
        branches = store.list_branches(f"{conv}:fork:")
        mutations = store.list_mutations(StateScope("conversation", conv))
    finally:
        ctx.close()
except Exception as e:
    st.error(f"读取检查点数据失败: {e}")
    st.stop()

col_ops, col_summary = st.columns([1, 3])
with col_ops:
    st.subheader("操作")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("创建检查点", use_container_width=True):
            _create_dialog(db, conv)
    with c2:
        if st.button("回滚 / 重试", use_container_width=True):
            if checkpoints:
                _revert_dialog(db, conv, checkpoints)
            else:
                st.warning("没有可回滚的检查点")
    with c3:
        if st.button("Fork 分支", use_container_width=True):
            if checkpoints:
                _fork_dialog(db, conv, checkpoints)
            else:
                st.warning("没有可分支的检查点")
with col_summary:
    st.subheader("概览")
    s1, s2, s3 = st.columns(3)
    s1.metric("检查点", len(checkpoints))
    s2.metric("分支", len(branches))
    s3.metric("变更记录", len(mutations))

st.divider()

st.subheader(f"检查点列表 ({len(checkpoints)})")
if checkpoints:
    st.dataframe(   # pyright: ignore[reportUnknownMemberType]
        _rows(checkpoints),
        use_container_width=True,
        hide_index=True,
        column_config={"ID": st.column_config.TextColumn(width="large")},
    )   # pyright: ignore[reportUnknownMemberType]
else:
    st.info("还没有检查点, 可通过上方按钮或运行时操作创建")

st.subheader(f"分支 ({len(branches)})")
if branches:
    rows: list[dict[str, Any]] = []
    for b in branches:
        rows.append(
            {
                "分支 ID": b.scope_id,
                "名称": b.name or "-",
                "水位": b.position,
                "父检查点": b.parent_checkpoint_id or "-",
                "时间": _fmt_time(b.created_at),
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)   # pyright: ignore[reportUnknownMemberType]
else:
    st.info("该对话尚未 fork 出分支")

st.subheader(f"变更记录 ({len(mutations)})")
if mutations:
    st.dataframe(   # pyright: ignore[reportUnknownMemberType]
        _rows(mutations),
        use_container_width=True,
        hide_index=True,
        column_config={"ID": st.column_config.TextColumn(width="large")},
    )   # pyright: ignore[reportUnknownMemberType]
else:
    st.info("暂无变更记录")
