"""base_take 插件测试: 安装 / 工具组成 / 配置注入 / 文档解析"""
from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import pytest
from typing import Any, Callable, Iterator, Protocol, cast

from satrap.expend.plugins.base_take.core.docread import extract_text
from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import SimpleSession

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "base_take"
CODING_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "satrap" / "expend" / "plugins" / "satrap_coding"


class _ReportlabCanvas(Protocol):
    """reportlab Canvas 的最小调用面 (可选依赖, 无类型声明)"""

    def drawString(self, x: float, y: float, text: str) -> None: ...
    def save(self) -> None: ...


class _ReportlabCanvasModule(Protocol):
    """reportlab.pdfgen.canvas 模块的最小调用面"""

    Canvas: Callable[[str], _ReportlabCanvas]


class _FakeLLM(LLM):
    """最小同步 fake LLM"""

    def __init__(self) -> None:
        pass

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content="回复")

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        yield LLMCallStreamEvent(kind="content_delta", delta="流")


@pytest.fixture
def session(tmp_path: Any, monkeypatch: Any) -> SimpleSession:
    """
    隔离数据目录的会话 + 安装 base_take 插件

    参数:
    - tmp_path: tmp路径
    - monkeypatch: pytest monkeypatch 夹具

    返回:
    - SimpleSession: 隔离数据目录的会话 + 安装 base_take 插件
    """
    from satrap.edictum import simple_session as ss_mod
    from satrap.edictum.plugin_config import PluginConfigManager

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))
    # 隔离: 插件配置目录 + 记忆 db + 沙箱根

    s = SimpleSession("conv-1", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(PLUGIN_DIR), config={
        "sandbox_root": str(tmp_path / "sandbox"),
        "workspace_root": str(tmp_path / "workspace"),
    })
    (tmp_path / "workspace").mkdir(exist_ok=True)
    return s


def test_plugin_install_capabilities(session: SimpleSession):
    """
    安装: 4 工具, 无记忆处理器和命令

    参数:
    - session: 会话
    """
    plugin = session.list_plugins()[0]
    assert plugin.name == "base_take"
    assert set(plugin.tools) == {
        "search", "fetch_page", "code_sandbox", "read_document",
    }
    assert not plugin.handlers
    assert not plugin.commands
    assert len(session.list_tools()) == 4


def test_config_schema_on_plugin(session: SimpleSession):
    """
    config_schema 随插件返回 (供前端渲染表单)

    参数:
    - session: 会话
    """
    plugin = session.list_plugins()[0]
    assert plugin.config_schema["sandbox_root"]["type"] == "path"
    assert plugin.config_schema["search_timeout"]["default"] == 10
    assert "memory_scope" not in plugin.config_schema
    assert "memory_mode" not in plugin.config_schema


# ================= 文档解析 =================

def test_extract_text_txt(tmp_path: Path):
    """
    纯文本直接读取

    参数:
    - tmp_path: tmp路径
    """
    f = tmp_path / "a.txt"
    f.write_text("你好世界", encoding="utf-8")
    assert extract_text(f) == "你好世界"


def test_extract_text_xlsx(tmp_path: Path):
    """
    xlsx 解析为 TSV 文本

    参数:
    - tmp_path: tmp路径
    """
    from openpyxl import Workbook
    wb: Any = Workbook()   # openpyxl 无类型声明
    ws: Any = wb.active
    assert ws is not None
    ws.title = "数据"
    ws.append(["姓名", "年龄"])
    ws.append(["张三", 30])
    f = tmp_path / "t.xlsx"
    wb.save(str(f))
    wb.close()
    text = extract_text(f)
    assert "Sheet: 数据" in text
    assert "姓名" in text and "张三" in text


def test_extract_text_docx(tmp_path: Path):
    """
    docx 解析段落为纯文本

    参数:
    - tmp_path: tmp路径
    """
    import docx
    doc = docx.Document()
    doc.add_paragraph("第一段内容")
    doc.add_paragraph("第二段内容")
    f = tmp_path / "t.docx"
    doc.save(str(f))
    text = extract_text(f)
    assert "第一段内容" in text and "第二段内容" in text


def test_extract_text_pdf(tmp_path: Path):
    """
    pdf 解析 (用 pdfplumber 生成最小 pdf 较复杂, 改用 reportlab 若可用否则跳过)

    参数:
    - tmp_path: tmp路径
    """
    # reportlab 为可选依赖, 未安装时 importorskip 跳过; 调用面以下方 Protocol 静态声明
    canvas_mod = cast(_ReportlabCanvasModule, pytest.importorskip("reportlab.pdfgen.canvas"))
    f = tmp_path / "t.pdf"
    c = canvas_mod.Canvas(str(f))
    c.drawString(100, 750, "Hello PDF")
    c.save()
    text = extract_text(f)
    assert "Hello PDF" in text


def test_pdfminer_fontbbox_warning_filtered():
    """缺 FontBBox 的良性警告被过滤, 其他 pdfminer 警告不受影响, 挂载幂等"""
    import logging

    from satrap.core.utils.documents import _mute_pdfminer_fontbbox_warning

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    logger = logging.getLogger("pdfminer.pdffont")
    _mute_pdfminer_fontbbox_warning()
    _mute_pdfminer_fontbbox_warning()   # 重复调用不叠加
    filters = [f for f in logger.filters if f.__class__.__name__ == "_FontBBoxWarningFilter"]
    assert len(filters) == 1

    capture = _Capture()
    logger.addHandler(capture)
    try:
        logger.warning("Could not get FontBBox from font descriptor because %r cannot be parsed as 4 floats", None)
        logger.warning("pdfminer 其他警告")
        assert capture.messages == ["pdfminer 其他警告"]
    finally:
        logger.removeHandler(capture)


def test_extract_text_unsupported(tmp_path: Path):
    """
    不支持的类型返回明确错误

    参数:
    - tmp_path: tmp路径
    """
    f = tmp_path / "t.bin"
    f.write_bytes(b"\x00\x01")
    with pytest.raises(ValueError, match="不支持"):
        extract_text(f)


def test_extract_text_missing(tmp_path: Path):
    """
    文件不存在返回明确错误

    参数:
    - tmp_path: tmp路径
    """
    with pytest.raises(ValueError, match="不存在"):
        extract_text(tmp_path / "ghost.txt")


def test_extract_text_enforces_file_and_output_limits(tmp_path: Path):
    """
    文本读取在解析前限制文件大小, 并在解析中限制输出长度

    参数:
    - tmp_path: 临时目录
    """
    path = tmp_path / "large.txt"
    path.write_text("abcdefghij", encoding="utf-8")
    assert extract_text(path, max_length=4) == "abcd"
    with pytest.raises(ValueError, match="文件过大"):
        extract_text(path, max_file_size=4)


def test_extract_text_rejects_extreme_zip_expansion(tmp_path: Path):
    """
    OOXML 在交给解析器前拒绝异常展开大小

    参数:
    - tmp_path: 临时目录
    """
    path = tmp_path / "bomb.docx"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 10_000)
    with pytest.raises(ValueError, match="展开后过大"):
        extract_text(path, max_expanded_size=100)


def test_read_document_tool(session: SimpleSession, tmp_path: Path):
    """
    read_document 工具: 工作区内解析 + 越界拒绝

    参数:
    - session: 会话
    - tmp_path: tmp路径
    """
    workspace = tmp_path / "workspace"
    (workspace / "note.txt").write_text("工作区笔记", encoding="utf-8")
    tool = session._wf.tools_manager.tools["read_document"]
    assert "工作区笔记" in tool.execute(path="note.txt")
    assert tool.execute(path="note.txt", max_length="bad") == "错误: max_length 必须是整数"
    # 越界拒绝
    result = tool.execute(path="../outside.txt")
    assert "越出工作区" in result or "错误" in result
