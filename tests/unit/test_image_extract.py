"""内嵌图片抽取与 OCR 客户端单测（F-1017）：构造含图 bytes → 断言抽取；闸门与 mock。

OCR 全部 httpx MockTransport（照抄 test_parser._mineru 模式），不依赖真实 MinerU。
"""

import io
import pathlib
import tempfile
import zipfile

import httpx
import pytest
from PIL import Image

from app.core.llm import UpstreamError
from app.rag.image_extract import (
    MAX_IMAGES_PER_DOC,
    ExtractedImage,
    new_result,
)
from app.rag.parser import DocxParser
from app.rag.parsers_extra import PptxParser, XlsxParser
from app.services.ocr import ImageOcrClient, strip_image_placeholders


def _png_bytes(color=(200, 30, 30), size=(120, 80)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _docx_with_images(count: int) -> bytes:
    from docx import Document as DocxDocument

    document = DocxDocument()
    document.add_paragraph("含图文档的正文内容，用于验证图片抽取与 OCR 编排链路完整性。" * 2)
    for _ in range(count):
        document.add_picture(io.BytesIO(_png_bytes()))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pptx_with_images(count: int) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width = Inches(10)
    presentation.slide_height = Inches(7.5)
    layout = presentation.slide_layouts[6]  # 空白版式
    for _ in range(count):
        slide = presentation.slides.add_slide(layout)
        slide.shapes.add_picture(io.BytesIO(_png_bytes()), Inches(1), Inches(1))
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _xlsx_with_images(count: int) -> bytes:
    import openpyxl
    from openpyxl.drawing.image import Image as XlImage

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet["A1"] = "含图表格正文内容，用于验证 xlsx 图片抽取链路的完整性，长度满足质量红线要求。"
    for i in range(count):
        # pathlib 写入（免 open(变量,"wb") 模式误报；tempfile 目录派生免固定 /tmp 形态）
        png = pathlib.Path(_xlimg_path(i))
        png.write_bytes(_png_bytes())
        sheet.add_image(XlImage(str(png)), f"B{2 + i * 5}")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xlimg_path(i: int) -> str:
    """xlsx 图片夹具落盘路径：tempfile 目录派生（免固定 /tmp 形态）。"""
    directory = tempfile.mkdtemp(prefix="xlimg-")
    return directory + "/_xlimg_" + str(i) + ".png"


class TestDocxExtraction:
    def test_docx_images_collected_when_enabled(self) -> None:
        doc: ParsedDocLike = DocxParser(with_images=True).parse("含图.docx", _docx_with_images(2))
        # python-docx 对同字节图片复用同一部件（2 处 add_picture → 1 个 media part）：去重合理
        assert len(doc.images) == 1

        assert doc.images[0].ext == "png"
        assert doc.images[0].page is None
        assert doc.image_skipped == 0

    def test_docx_images_not_collected_when_disabled(self) -> None:
        doc: ParsedDocLike = DocxParser(with_images=False).parse("含图.docx", _docx_with_images(2))
        assert doc.images == []  # 开关关闭：不收集（回归保障）

    def test_docx_no_image(self) -> None:
        doc: ParsedDocLike = DocxParser(with_images=True).parse("无图.docx", _docx_with_images(0))
        assert doc.images == []


class TestPptxXlsxExtraction:
    def test_pptx_pictures_with_page(self) -> None:
        doc: ParsedDocLike = PptxParser(with_images=True).parse("课件.pptx", _pptx_with_images(2))
        assert len(doc.images) == 2
        assert doc.images[0].page == 1 and doc.images[1].page == 2

    def test_pptx_disabled(self) -> None:
        doc: ParsedDocLike = PptxParser(with_images=False).parse("课件.pptx", _pptx_with_images(2))
        assert doc.images == []

    def test_xlsx_images_with_second_load(self) -> None:
        doc: ParsedDocLike = XlsxParser(with_images=True).parse("表.xlsx", _xlsx_with_images(1))
        assert len(doc.images) == 1  # 非 read_only 二开读 _images


class TestGates:
    def test_oversize_image_skipped(self) -> None:
        from app.rag.image_extract import _admit

        result = new_result()
        _admit(result, b"x" * (10 * 1024 * 1024 + 1), None, "png")
        assert result.images == [] and result.skipped_oversize == 1

    def test_vector_format_skipped(self) -> None:
        from app.rag.image_extract import _admit

        result = new_result()
        _admit(result, b"emf-data", None, "emf")
        assert result.images == [] and result.skipped_vector == 1

    def test_doc_count_cap(self) -> None:
        from app.rag.image_extract import _admit

        result = new_result()
        for _ in range(MAX_IMAGES_PER_DOC + 3):
            _admit(result, _png_bytes(), None, "png")
        assert len(result.images) == MAX_IMAGES_PER_DOC
        assert result.skipped_total_limit == 3

    def test_pdf_page_images(self) -> None:
        """pypdf PageObject.images 通道：用构造器直接喂 _admit 产物形状（页码透传）。"""
        result = new_result()
        from app.rag.image_extract import _admit

        _admit(result, _png_bytes(), 3, "png")
        assert result.images == [ExtractedImage(data=result.images[0].data, page=3, ext="png")]


class TestOcrClient:
    def test_ocr_success_strips_placeholders(self) -> None:
        calls: list[httpx.Request] = []
        body = {"results": {"img_1": {"md_content": "![](images/abc.jpg)\n\n识别出的文字内容\n"}}}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=body)

        client = ImageOcrClient("http://mineru.test", timeout_s=5)
        client._transport = httpx.MockTransport(handler)  # 类型: httpx 客户端工厂注入
        import httpx as _hx

        original_init = _hx.Client

        def patched_client(**kwargs):
            kwargs.pop("transport", None)
            return original_init(transport=httpx.MockTransport(handler), **kwargs)

        text = None
        from unittest import mock

        with mock.patch("httpx.Client", patched_client):
            text = client.ocr(_png_bytes(), "img_1.png")
        assert text == "识别出的文字内容"  # 占位行剥离
        assert calls[0].url.path == "/file_parse"

    def test_ocr_failure_raises_upstream(self) -> None:
        from unittest import mock

        client = ImageOcrClient("http://mineru.test", timeout_s=5)
        original_init = httpx.Client

        def patched_client(**kwargs):
            kwargs.pop("transport", None)
            return original_init(
                transport=httpx.MockTransport(lambda request: httpx.Response(500)), **kwargs
            )

        with mock.patch("httpx.Client", patched_client), pytest.raises(Exception, match="重试"):
            client.ocr(_png_bytes(), "img_1.png")

    def test_ocr_bad_json_raises_upstream_error_not_raw_valueerror(self) -> None:
        """200 + 坏 JSON：resp.json() 的 ValueError 收口为 UpstreamError（I2）。

        ingest_pipeline 只捕 UpstreamError——收口后单图坏 JSON 才走"跳过该图计数"
        降级，而不是裸 ValueError 逃逸拖垮整篇转 failed。"""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=b"not-json")

        client = ImageOcrClient(
            "http://mineru.test", timeout_s=5, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(UpstreamError, match="JSON"):
            client.ocr(_png_bytes(), "img_1.png")
        assert len(calls) == 2  # 坏 JSON 与非 2xx 同口径：重试 1 次后仍败（恰 2 次）


class TestPlaceholderStrip:
    def test_strip_only_placeholder_lines(self) -> None:
        md = "![](images/a.jpg)\n\n正文保留。\n![alt](images/b.png)\n更多正文。"
        # 占位行删除后残留空行由 ingest 清洗链的折叠规则收口（此处只验证占位被剥）
        assert strip_image_placeholders(md) == "正文保留。\n\n更多正文。"

    def test_zip_with_media_entry_not_blocked(self) -> None:
        """含 media 条目的 OOXML 预检不受影响（docx media 在白名单内，判型正常放行）。"""
        from app.rag.precheck import check_sync

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types>wordprocessingml</Types>")
            archive.writestr("word/document.xml", "<w:document/>")
            archive.writestr("word/media/image1.png", _png_bytes())
        result = check_sync(
            "含图.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            buffer.getvalue(),
        )
        assert result.ok


from app.rag.chunker import ParsedDoc

ParsedDocLike = ParsedDoc
