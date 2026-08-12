from __future__ import annotations

from typing import Any

import streamlit as st
from satrap.core.framework.UserManager import UserInfoStore
from satrap.core.type import safe_getattr_str

st.set_page_config(page_title="用户管理", page_icon="", layout="wide")


def _db_path() -> str:
    """用户信息库路径: 显式配置 > 默认"""
    config = st.session_state.config
    return str(safe_getattr_str(config, "user_db_path") or ".satrap/user_info.db")


def _store() -> UserInfoStore:
    return UserInfoStore(db_path=_db_path())


@st.dialog("新建用户")
def _create_dialog():
    user_id = st.text_input("user_id", placeholder="如 misskey:xxxxxxxx")
    platform = st.text_input("平台", placeholder="如 misskey / onebot, 可选")
    nickname = st.text_input("昵称", placeholder="可选")
    if st.button("创建"):
        if not user_id.strip():
            st.error("user_id 不能为空")
            return
        store = _store()
        existed = store.get(user_id.strip())
        if existed is not None:
            st.error(f"用户已存在: {user_id.strip()}")
            return
        store.upsert(_mk_user(user_id.strip(), platform, nickname))
        st.success(f"已创建用户: `{user_id.strip()}`")
        st.rerun()


@st.dialog("编辑用户")
def _edit_dialog(user_id: str, nickname: str, platform: str):
    new_nickname = st.text_input("昵称", value=nickname)
    new_platform = st.text_input("平台", value=platform)
    if st.button("保存"):
        store = _store()
        info = store.get(user_id)
        if info is None:
            st.error("用户不存在")
            return
        info.user_nickname = new_nickname.strip()
        info.user_platform = new_platform.strip()
        store.upsert(info)
        st.success("已保存")
        st.rerun()


@st.dialog("绑定会话")
def _bind_dialog(user_id: str):
    session_id = st.text_input("session_id", placeholder="如 sr7dws")
    if st.button("绑定"):
        if not session_id.strip():
            st.error("session_id 不能为空")
            return
        store = _store()
        store.add_session(user_id, session_id.strip())
        st.success(f"已绑定: `{session_id.strip()}`")
        st.rerun()


@st.dialog("删除用户")
def _delete_dialog(user_id: str):
    st.warning(f"将删除用户 `{user_id}` 的信息记录, 绑定的会话本身不受影响")
    if st.button("确认删除", type="primary"):
        store = _store()
        store.delete(user_id)
        st.success("已删除")
        st.rerun()


def _mk_user(user_id: str, platform: str, nickname: str):
    """构造 UserInfo (避免直接 import type 的样板)"""
    from satrap.core.type import UserInfo

    return UserInfo(
        user_id=user_id,
        user_platform=platform.strip(),
        user_nickname=nickname.strip(),
        user_session=[],
    )


st.title("用户管理")
st.caption("直接操作用户信息库 (与运行时 Session 解耦), 支持新建 / 编辑 / 绑定会话 / 删除")

store = _store()
users = store.list(limit=500)

with st.container(border=True):
    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("新建用户", type="primary"):
            _create_dialog()
    with col2:
        st.caption(f"共 {len(users)} 个用户")

if not users:
    st.info("暂无用户, 点击上方按钮新建")
    st.stop()

rows: list[dict[str, Any]] = []
for u in users:
    rows.append(
        {
            "user_id": u.user_id,
            "平台": u.user_platform or "-",
            "昵称": u.user_nickname or "-",
            "会话数": len(u.user_session or []),
        }
    )
st.dataframe(rows, use_container_width=True, hide_index=True)  # pyright: ignore[reportUnknownMemberType]

st.divider()
st.subheader("用户详情")

user_ids = [u.user_id for u in users]
selected = st.selectbox("选择用户", user_ids, key="user_select")
if not selected:
    st.stop()
info = store.get(selected)
if info is None or info.user_id is None:
    st.error("用户不存在")
    st.stop()

meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
meta_col1.metric("user_id", info.user_id)
meta_col2.metric("平台", info.user_platform or "-")
meta_col3.metric("昵称", info.user_nickname or "-")
meta_col4.metric("会话数", len(info.user_session or []))

st.write("**绑定的会话**")
if info.user_session:
    for sid in list(info.user_session or []):
        b1, b2 = st.columns([4, 1])
        b1.write(f"- `{sid}`")
        if b2.button("解绑", key=f"unbind_{sid}"):
            store.remove_session(info.user_id, sid)
            st.rerun()
else:
    st.caption("未绑定任何会话")

btn1, btn2, btn3, btn4 = st.columns(4)
if btn1.button("编辑信息", key="edit_user"):
    _edit_dialog(info.user_id, info.user_nickname or "", info.user_platform or "")
if btn2.button("绑定会话", key="bind_user"):
    _bind_dialog(info.user_id)
if btn3.button("删除用户", key="del_user"):
    _delete_dialog(info.user_id)
btn4.caption("删除仅移除用户记录, 不影响会话与聊天数据")
