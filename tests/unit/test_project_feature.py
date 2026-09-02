"""
'项目'功能测试: projects 数据层 + 会话记忆隔离 + 服务绑定 + 工作区按会话解析

语义契约:
- 项目 = 登记的工作区文件夹 (任意绝对路径), 其下会话共享该工作区
- 删除项目仅解绑会话 (归入"最近"), 不动会话数据与磁盘文件
- 无项目会话使用自身私有沙箱作为工作区
- 长期记忆始终按会话隔离, 项目绑定不改变记忆作用域
"""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.storage import StorageLayout
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.display import service as service_mod
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.recorder import (
    DisplayRecorder,
    create_project,
    delete_project,
    get_conversation_meta,
    get_project,
    list_conversations,
    query_conversations,
    list_projects,
    set_conversation_project,
)
from satrap.display.service import ChatService
from satrap.expend.plugins.base_take.tools import AddMemoryTool
from satrap.expend.tools.memory_store import MemoryStore


def _created_project_id(result: dict[str, object]) -> str:
    """
    提取服务创建结果中的项目 ID

    参数:
    - result: 项目创建结果

    返回:
    - 已创建项目的字符串 ID
    """
    assert result["ok"] is True
    project = cast(dict[str, object], result["project"])
    project_id = project["project_id"]
    assert isinstance(project_id, str)
    return project_id


# ================= 数据层: projects CRUD =================


def test_project_crud_roundtrip(tmp_path: Path):
    """
    创建/列出/查询/删除项目; 删除仅解绑会话 (project_id 置 NULL)

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    proj = create_project("演示", str(tmp_path), db_path=db)
    assert proj["name"] == "演示" and proj["root_path"] == str(tmp_path)

    listed = list_projects(db_path=db)
    assert [p["project_id"] for p in listed] == [proj["project_id"]]
    fetched = get_project(proj["project_id"], db_path=db)
    assert fetched is not None and fetched["name"] == "演示"

    rec = DisplayRecorder(db, "conv-p")
    # 绑定一个会话后删除项目 -> 会话解绑但 meta 保留
    rec.save_meta("default", project_id=proj["project_id"])
    assert delete_project(proj["project_id"], db_path=db) is True
    meta = get_conversation_meta("conv-p", db_path=db)
    assert meta is not None and meta["project_id"] is None
    assert get_project(proj["project_id"], db_path=db) is None
    assert delete_project(proj["project_id"], db_path=db) is False


def test_conversation_meta_created_with_project_id(tmp_path: Path):
    """
    新库的 conversation_meta 直接包含 project_id

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    recorder = DisplayRecorder(db, "conv-new")
    recorder.save_meta("default")
    with sqlite3.connect(db) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(conversation_meta)")}
    assert "project_id" in columns


def test_list_conversations_with_project_and_empty(tmp_path: Path):
    """
    会话列表带项目归属; 新建未发言的空会话也在列表中 (meta created_at 兜底排序)

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    proj = create_project("项目A", str(tmp_path), db_path=db)

    rec = DisplayRecorder(db, "conv-talk")
    # 有轮次的会话 (绑定项目)
    rec.save_meta("default", project_id=proj["project_id"])
    rec.start_turn("第一条消息")
    rec.end_turn("答复")
    # 空会话 (只建 meta 未发言, 无项目)
    rec2 = DisplayRecorder(db, "conv-empty")
    rec2.save_meta("default")

    convs = {c["conversation_id"]: c for c in list_conversations(db)}
    assert convs["conv-talk"]["project_id"] == proj["project_id"]
    assert convs["conv-talk"]["title"] == "第一条消息"
    assert convs["conv-talk"]["turn_count"] == 1
    assert "conv-empty" in convs
    assert convs["conv-empty"]["turn_count"] == 0
    assert convs["conv-empty"]["project_id"] is None
    assert convs["conv-empty"]["title"] == "新对话"


def test_query_conversations_filters_and_paginates(tmp_path: Path):
    """
    历史查询应支持标题、模型、轮数和分页过滤

    参数:
    - tmp_path: 临时目录
    """
    db = str(tmp_path / "display.db")
    first = DisplayRecorder(db, "conv-first")
    first.save_meta("fast", think="high")
    first.start_turn("查找目标会话")
    first.end_turn("完成")
    second = DisplayRecorder(db, "conv-empty")
    second.save_meta("default")

    searched = query_conversations(db, search="目标", model="fast")
    empty = query_conversations(db, turn_count="empty", page=1, page_size=1)

    assert searched["total"] == 1
    assert searched["items"][0]["conversation_id"] == "conv-first"
    assert searched["items"][0]["think"] == "high"
    assert empty["total"] == 1
    assert empty["items"][0]["conversation_id"] == "conv-empty"


def test_set_conversation_project_rebind(tmp_path: Path):
    """
    会话改绑项目 / 移出项目 (None); 会话不存在返回 False

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    proj = create_project("项目B", str(tmp_path), db_path=db)
    rec = DisplayRecorder(db, "conv-m")
    rec.save_meta("default")

    assert set_conversation_project("conv-m", proj["project_id"], db_path=db) is True
    meta_bound = get_conversation_meta("conv-m", db_path=db)
    assert meta_bound is not None and meta_bound["project_id"] == proj["project_id"]
    assert set_conversation_project("conv-m", None, db_path=db) is True
    meta_unbound = get_conversation_meta("conv-m", db_path=db)
    assert meta_unbound is not None and meta_unbound["project_id"] is None
    assert set_conversation_project("conv-ghost", proj["project_id"], db_path=db) is False


def test_save_meta_upsert_preserves_project(tmp_path: Path):
    """
    save_meta 冲突更新 (模型/think 变更) 不清空已有 project_id

    参数:
    - tmp_path: tmp路径
    """
    db = str(tmp_path / "display.db")
    proj = create_project("项目C", str(tmp_path), db_path=db)
    rec = DisplayRecorder(db, "conv-u")
    rec.save_meta("default", project_id=proj["project_id"])
    # 再次 save_meta 不带 project_id (如运行时刷新), 项目归属应保留
    rec.save_meta("fast", "high")
    meta = get_conversation_meta("conv-u", db_path=db)
    assert meta is not None
    assert meta["model"] == "fast" and meta["think"] == "high"
    assert meta["project_id"] == proj["project_id"]


# ================= 会话记忆隔离 =================


def test_memory_requires_concrete_session_scope(tmp_path: Path):
    """
    记忆库不允许空作用域, 防止意外读写整个平台数据

    参数:
    - tmp_path: tmp路径
    """
    with pytest.raises(ValueError, match="scope"):
        MemoryStore(db_path=tmp_path / "memory.db", scope="")


def test_memory_isolated_between_sessions(tmp_path: Path):
    """
    同一平台库中的不同会话不能查看、删除或清空对方记忆

    参数:
    - tmp_path: tmp路径
    """
    first = MemoryStore(db_path=tmp_path / "platform.db", scope="session:first")
    second = MemoryStore(db_path=tmp_path / "platform.db", scope="session:second")
    first.add("第一会话", "first")
    second.add("第二会话", "second")

    first_id = first.list_all()[0]["id"]
    assert [item["title"] for item in first.list_all()] == ["第一会话"]
    assert [item["title"] for item in second.list_all()] == ["第二会话"]
    assert second.get(first_id)["ok"] is False
    assert second.delete(first_id)["ok"] is False
    assert second.clear() == 1
    assert first.count() == 1


def test_add_memory_tool_writes_only_current_session(tmp_path: Path):
    """
    add_memory 不暴露层级参数, 并始终写入当前会话

    参数:
    - tmp_path: tmp路径
    """
    store = MemoryStore(db_path=tmp_path / "platform.db", scope="session:current")
    tool = AddMemoryTool(store)

    assert "level" not in tool.params_dict
    assert "已添加" in tool.execute(title="t1", content="c1")
    assert {item["scope"] for item in store.list_all()} == {"session:current"}


# ================= 服务层: 项目绑定 =================


class _FakeAsyncLLM(AsyncLLM):
    """流式返回固定内容的 fake LLM"""

    def __init__(self) -> None:
        pass

    async def call(self, messages: list[dict[str, Any]], **kw: Any) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content="答复")

    async def stream_call(self, messages: list[dict[str, Any]], **kw: Any) -> AsyncIterator[LLMCallStreamEvent]:
        yield LLMCallStreamEvent(kind="content_delta", delta="答复")
        yield LLMCallStreamEvent(kind="done", response=LLMCallResponse(type="answer", content="答复"))

    def set_parameters(self, **kwargs: Any) -> None:
        pass


class _FakeModelConfig:
    """最小 ModelConfigManager 替身"""

    def list_llm_configs(self, mask_api_key: bool = False) -> dict[str, Any]:
        return {"default": {}}

    def get_llm_config(self, name: str = "default") -> Any:
        from satrap.core.type import LLMConfig
        return LLMConfig(name=name, model="m", api_key="k", base_url="https://x")


def _make_service(tmp_path: Path, monkeypatch: Any) -> ChatService:
    """
    构造 ChatService, build_llm 替换为 fake

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具

    返回:
    - ChatService: 构造 ChatService, build_llm 替换为 fake
    """
    def _fake_build_llm(cfg: Any) -> AsyncLLM:
        return _FakeAsyncLLM()

    monkeypatch.setattr(service_mod, "build_llm", _fake_build_llm)
    reg = ChatPluginRegistry(state_path=tmp_path / "plugins.json")
    return ChatService(
        _FakeModelConfig(),   # type: ignore[arg-type]
        reg,
        chat_db_path=str(tmp_path / "chat.db"),
        display_db_path=str(tmp_path / "display.db"),
        storage_layout=StorageLayout(tmp_path / "data"),
        workspace_roots=[tmp_path],
        denied_workspace_roots=[tmp_path / ".satrap"],
    )


def test_service_project_crud_validation(tmp_path: Path, monkeypatch: Any):
    """
    项目创建校验 (空名/路径不存在/非目录) + 删除解绑

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    svc = _make_service(tmp_path, monkeypatch)
    assert svc.create_project("", str(tmp_path))["ok"] is False
    assert svc.create_project("x", str(tmp_path / "ghost"))["ok"] is False
    fpath = tmp_path / "file.txt"
    fpath.write_text("x", encoding="utf-8")
    assert svc.create_project("x", str(fpath))["ok"] is False

    result = svc.create_project("工作区", str(tmp_path))
    assert result["ok"] is True
    pid = _created_project_id(result)
    assert [p["project_id"] for p in svc.list_projects()] == [pid]
    assert svc.delete_project(pid)["ok"] is True
    assert svc.list_projects() == []
    assert svc.delete_project(pid)["ok"] is False


def test_service_create_conversation_with_project(tmp_path: Path, monkeypatch: Any):
    """
    项目会话: 工作区/沙箱鸭子属性绑定 + meta 持久化; 无效项目抛 ValueError

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    svc = _make_service(tmp_path, monkeypatch)
    ws = tmp_path / "proj_ws"
    ws.mkdir()
    pid = _created_project_id(svc.create_project("绑定", str(ws)))

    async def _run() -> tuple[str, str]:
        cid = await svc.create_conversation(model="default", project_id=pid)
        plain = await svc.create_conversation(model="default")
        return cid, plain

    cid, plain = asyncio.run(_run())

    conv = svc.get_conversation(cid)
    assert conv is not None and conv.project_id == pid
    session = conv.session
    assert getattr(session, "coding_workspace_root") == str(ws.resolve())
    expected_sandbox = svc._storage.session_sandbox("chat", cid)
    assert getattr(session, "coding_sandbox_root") == str(expected_sandbox)
    assert not expected_sandbox.is_relative_to(ws.resolve())

    plain_conv = svc.get_conversation(plain)
    # 无项目会话直接使用自己的私有沙箱作为工作区
    assert plain_conv is not None
    plain_sandbox = svc._storage.session_sandbox("chat", plain)
    assert getattr(plain_conv.session, "coding_workspace_root") == str(plain_sandbox)
    assert getattr(plain_conv.session, "coding_sandbox_root") == str(plain_sandbox)

    meta = get_conversation_meta(cid, db_path=str(svc._display_db_path))
    # meta 持久化项目归属
    assert meta is not None and meta["project_id"] == pid

    async def _bad() -> None:
        """验证创建会话时拒绝无效项目"""
        try:
            await svc.create_conversation(model="default", project_id="ghost")
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass

    asyncio.run(_bad())


def test_service_set_conversation_project_rebind(tmp_path: Path, monkeypatch: Any):
    """
    会话改绑: 绑定 -> 换绑 -> 移出, 活动会话鸭子属性同步刷新

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    svc = _make_service(tmp_path, monkeypatch)
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()
    pid_a = _created_project_id(svc.create_project("A", str(ws_a)))
    pid_b = _created_project_id(svc.create_project("B", str(ws_b)))

    cid = asyncio.run(svc.create_conversation(model="default"))

    assert svc.set_conversation_project(cid, pid_a)["ok"] is True
    # 绑定 A
    conv = svc.get_conversation(cid)
    assert conv is not None and conv.project_id == pid_a
    assert getattr(conv.session, "coding_workspace_root") == str(ws_a.resolve())

    assert svc.set_conversation_project(cid, pid_b)["ok"] is True
    # 换绑 B
    assert getattr(conv.session, "coding_workspace_root") == str(ws_b.resolve())

    assert svc.set_conversation_project(cid, None)["ok"] is True
    # 移出项目后使用会话私有沙箱作为工作区
    assert conv.project_id is None
    private_sandbox = svc._storage.session_sandbox("chat", cid)
    assert getattr(conv.session, "coding_workspace_root") == str(private_sandbox)

    meta = get_conversation_meta(cid, db_path=str(svc._display_db_path))
    # 持久化同步
    assert meta is not None and meta["project_id"] is None

    assert svc.set_conversation_project("ghost", pid_a)["ok"] is False
    # 错误路径: 会话/项目不存在
    assert svc.set_conversation_project(cid, "ghost")["ok"] is False


def test_service_delete_project_unbinds_active(tmp_path: Path, monkeypatch: Any):
    """
    删除项目: 活动会话解绑并回到私有沙箱, 会话本身保留

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    svc = _make_service(tmp_path, monkeypatch)
    ws = tmp_path / "ws_del"
    ws.mkdir()
    pid = _created_project_id(svc.create_project("待删", str(ws)))
    cid = asyncio.run(svc.create_conversation(model="default", project_id=pid))

    conv = svc.get_conversation(cid)
    assert conv is not None and hasattr(conv.session, "coding_workspace_root")

    assert svc.delete_project(pid)["ok"] is True
    assert conv.project_id is None
    private_sandbox = svc._storage.session_sandbox("chat", cid)
    assert getattr(conv.session, "coding_workspace_root") == str(private_sandbox)
    # 会话数据保留
    assert get_conversation_meta(cid, db_path=str(svc._display_db_path)) is not None


def test_service_resume_conversation_rebinds_project(tmp_path: Path, monkeypatch: Any):
    """
    会话恢复 (内存未命中): 从 meta 读项目归属并重新绑定工作区

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    svc = _make_service(tmp_path, monkeypatch)
    ws = tmp_path / "ws_resume"
    ws.mkdir()
    pid = _created_project_id(svc.create_project("恢复", str(ws)))
    cid = asyncio.run(svc.create_conversation(model="default", project_id=pid))

    svc._conversations.clear()
    # 模拟重启: 清掉内存态

    resumed = asyncio.run(svc._resume_conversation(cid))
    assert resumed is not None and resumed.project_id == pid
    assert getattr(resumed.session, "coding_workspace_root") == str(ws.resolve())


def test_service_upload_project_scoped(tmp_path: Path, monkeypatch: Any):
    """
    上传落盘: 项目会话和普通会话分别写入自己的私有 uploads 目录

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    svc = _make_service(tmp_path, monkeypatch)
    ws = tmp_path / "ws_upload"
    ws.mkdir()
    pid = _created_project_id(svc.create_project("上传", str(ws)))

    async def _run() -> tuple[str, str]:
        return (
            await svc.create_conversation(model="default", project_id=pid),
            await svc.create_conversation(model="default"),
        )

    proj_cid, plain_cid = asyncio.run(_run())

    result = svc.save_upload(plain_cid, "a.txt", b"hello")
    assert result["ok"] is True
    plain_upload_dir = svc._storage.session_uploads("chat", plain_cid)
    assert plain_upload_dir.is_dir()

    result = svc.save_upload(proj_cid, "b.txt", b"world")
    assert result["ok"] is True
    upload_dir = svc._storage.session_uploads("chat", proj_cid)
    assert upload_dir.is_dir()
    assert upload_dir != plain_upload_dir
    assert not (ws.resolve() / ".satrap" / "uploads").exists()
    files = list(upload_dir.iterdir())
    assert len(files) == 1 and files[0].name.endswith("_b.txt")


# ================= 目录浏览 (新建项目选择工作区) =================


def test_browse_directories_lists_dirs_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    只列子目录不列文件, 按名排序 (忽略大小写), parent 指向父目录

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    (tmp_path / "beta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "beta" / "sub").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")

    service = _make_service(tmp_path, monkeypatch)
    result = service.browse_directories(str(tmp_path))
    assert result["ok"] is True
    assert result["path"] == str(tmp_path.resolve())
    dirs = cast(list[dict[str, str]], result["dirs"])
    assert [directory["name"] for directory in dirs] == ["Alpha", "beta", "data"]
    assert result["parent"] == ""


def test_browse_directories_invalid_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    路径不存在/不是目录 -> ok=False

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    service = _make_service(tmp_path, monkeypatch)
    ghost = service.browse_directories(str(tmp_path / "ghost"))
    assert ghost["ok"] is False and "不存在" in str(ghost["error"])
    fpath = tmp_path / "f.txt"
    fpath.write_text("x", encoding="utf-8")
    not_dir = service.browse_directories(str(fpath))
    assert not_dir["ok"] is False


def test_browse_directories_root_and_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    空 path 只返回配置的工作区根目录

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    service = _make_service(tmp_path, monkeypatch)
    empty = service.browse_directories("")
    assert empty["ok"] is True
    assert empty["path"] == "" and empty["parent"] is None
    assert empty["dirs"] == [{"name": tmp_path.name, "path": str(tmp_path.resolve())}]


def test_project_and_browse_reject_paths_outside_workspace_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    项目创建和目录浏览均不得越出服务端允许根目录

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    service = _make_service(tmp_path, monkeypatch)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)

    created = service.create_project("越界", str(outside))
    browsed = service.browse_directories(str(outside))

    assert created["ok"] is False and "越出" in str(created["error"])
    assert browsed["ok"] is False and "越出" in str(browsed["error"])


def test_project_and_browse_reject_satrap_data_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    项目创建和目录浏览均拒绝 .satrap 本身及其子目录

    参数:
    - tmp_path: 临时目录
    - monkeypatch: pytest monkeypatch 夹具
    """
    protected = tmp_path / ".satrap"
    nested = protected / "data"
    nested.mkdir(parents=True)
    (protected / "api-token").write_text("secret", encoding="utf-8")
    service = _make_service(tmp_path, monkeypatch)

    assert service.create_project("敏感目录", str(protected))["ok"] is False
    assert service.create_project("敏感子目录", str(nested))["ok"] is False
    assert service.browse_directories(str(protected))["ok"] is False

    root = service.browse_directories(str(tmp_path))
    assert root["ok"] is True
    root_dirs = cast(list[dict[str, str]], root["dirs"])
    assert ".satrap" not in {item["name"] for item in root_dirs}


# ================= satrap_coding: 工作区按会话解析 =================


def test_coding_tools_per_session_workspace(tmp_path: Path, monkeypatch: Any):
    """
    两个会话不同工作区根并行互不干扰; 无鸭子属性会话回落全局

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具
    """
    import satrap.expend.plugins.satrap_coding.tools as tools_mod
    from satrap.edictum import SimpleSession
    from satrap.expend.plugins.satrap_coding.tools import get_tools

    global_ws = tmp_path / "global_ws"
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    for d in (global_ws, ws_a, ws_b):
        d.mkdir()
    (global_ws / "g.txt").write_text("global", encoding="utf-8")
    (ws_a / "a.txt").write_text("aaa", encoding="utf-8")
    (ws_b / "b.txt").write_text("bbb", encoding="utf-8")
    monkeypatch.setattr(tools_mod, "WORKSPACE_ROOT", global_ws)
    monkeypatch.setattr(tools_mod, "DATA_ROOT", tmp_path / "coding")

    class _StubLLM:
        def call(self, *a: Any, **kw: Any) -> Any:
            raise NotImplementedError

        def stream_call(self, *a: Any, **kw: Any) -> Any:
            raise NotImplementedError

        def set_parameters(self, **kwargs: Any) -> None:
            pass

    def _session(sid: str, ws: Path | None) -> SimpleSession:
        s = SimpleSession(sid, _StubLLM(), db_path=str(tmp_path / f"{sid}.db"), enable_checkpoint=False)   # type: ignore[arg-type]
        if ws is not None:
            setattr(s, "coding_workspace_root", str(ws))
            # 鸭子属性: 与 ChatService._apply_project 同款注入方式
        return s

    def _read_tool(s: SimpleSession) -> Any:
        tools = get_tools(s)
        return next(t for t in tools if t.get_tool_name() == "read_file")

    sess_a = _session("sess-a", ws_a)
    sess_b = _session("sess-b", ws_b)
    sess_g = _session("sess-g", None)

    assert "aaa" in _read_tool(sess_a).execute(path="a.txt")
    # 同一份全局配置下, 三个会话各自解析到自己的工作区
    assert "bbb" in _read_tool(sess_b).execute(path="b.txt")
    assert "global" in _read_tool(sess_g).execute(path="g.txt")

    assert "错误" in _read_tool(sess_a).execute(path="b.txt")
    # 互不干扰: A 看不到 B 的文件 (B 的文件不在 A 的工作区内)
    # 无项目会话行为与全局一致 (相对路径基于全局工作区)
    assert "错误" in _read_tool(sess_g).execute(path="a.txt")
