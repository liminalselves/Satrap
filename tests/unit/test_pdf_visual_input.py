"""PDF 分页视觉结果, 能力选择和同步异步一致性回归"""
from types import SimpleNamespace
from pathlib import Path
import base64
import io

from PIL import Image
import pypdfium2 as pdfium
import pytest

from satrap.expend.plugins.base_take.tools.async_ import AsyncReadDocumentTool
from satrap.expend.plugins.base_take.tools.sync import ReadDocumentTool
from satrap.core.utils.pdf_pages import read_pdf_pages


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "scan.pdf"
    with pdfium.PdfDocument.new() as document:
        for _ in range(3):
            page = document.new_page(1200, 1800)
            page.close()
        document.save(str(path))
    return path


def test_pdf_visual_pages_are_real_images_with_page_metadata(pdf_path):
    result = read_pdf_pages(pdf_path, visual=True, start_page=2, page_count=1)
    assert isinstance(result, dict)
    assert result["metadata"] == {
        "total_pages": 3, "start_page": 2, "end_page": 2,
        "next_page": 3, "text_truncated": False,
    }
    assert "没有可提取的文本层" in result["text"]
    assert "第 2 页" in result["media"][0]["text"]
    data = base64.b64decode(result["media"][1]["image_url"]["url"].split(",")[1])
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 1600
        image.verify()


@pytest.mark.parametrize("start,count", [(0, 1), (4, 1), (1, 0), (1, 6), (True, 1), (1, 1.5)])
def test_pdf_invalid_page_selection_fails(pdf_path, start, count):
    with pytest.raises(ValueError):
        read_pdf_pages(pdf_path, visual=True, start_page=start, page_count=count)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
async def test_document_modes_follow_current_model(pdf_path, asynchronous, enabled, monkeypatch):
    cls = AsyncReadDocumentTool if asynchronous else ReadDocumentTool
    tool = cls(pdf_path.parent)
    monkeypatch.setattr(tool, "_session", SimpleNamespace(llm=SimpleNamespace(supports_visual_input=enabled)), raising=False)

    async def execute(mode):
        if isinstance(tool, AsyncReadDocumentTool):
            return await tool.execute(str(pdf_path), mode=mode, start_page=1, page_count=1)
        return tool.execute(str(pdf_path), mode=mode, start_page=1, page_count=1)

    automatic = await execute("auto")
    assert isinstance(automatic, dict) is enabled
    text = await execute("text")
    assert isinstance(text, str)
    assert "没有可提取的文本层" in text
    visual = await execute("visual")
    if enabled:
        assert isinstance(visual, dict)
    else:
        assert "未启用图像与视频输入" in visual


def test_pdf_text_mode_does_not_render_pages(pdf_path, monkeypatch):
    def fail_render(*args, **kwargs):
        raise AssertionError("文本模式不应渲染图片")

    monkeypatch.setattr(pdfium.PdfPage, "render", fail_render)
    result = read_pdf_pages(pdf_path, visual=False, start_page=3)
    assert isinstance(result, str)
    assert "本次读取 3-3 页" in result
    assert "下一页" not in result
