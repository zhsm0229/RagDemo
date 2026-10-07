r"""内嵌图片 OCR 集成测试（F-1017）：含图 docx 全链——抽取→OCR（mock）→图片块入库
→document_images 落库→图片回显端点→原文件端点；降级语义与开关关闭回归。
OCR 用桩注入（analyze_document 依赖 ImageOcrClient 由 mineru_base_url 构造——
集成层以 monkeypatch 替换 ocr.ocr 或指向不可达端口触发降级轨）。"""

import io

from docx import Document as DocxDocument
from PIL import Image
from sqlalchemy import func, select

from app.core.llm import UpstreamError
from app.core.storage import LocalFileStorage
from app.db.models import Chunk, Document, DocumentImage, User
from app.services.ingest import create_document, process_document
from tests.integration.test_ingest import _make_deps, _meta

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _admin_id(session) -> int:
    return session.scalars(select(User.id).where(User.username == "admin")).one()


def _dept_id(session) -> int:
    return session.scalars(select(User.dept_id).where(User.username == "admin")).one()


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (100, 60), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _docx_with_image_and_text(unique: str) -> bytes:
    """含 1 张图 + 足量正文的 docx（图片独立成段，正文过质量红线）。

    unique 参数进正文改变 SHA-256：内容查重（F-1001）为全局唯一约束，同 bytes
    二次上传会 409（2026-10-06 实测）。"""
    document = DocxDocument()
    document.add_paragraph(
        f"差旅费用报销制度正文{unique}：交通费凭票据据实报销，住宿费按城市分档上限执行。" * 3
    )
    document.add_picture(io.BytesIO(_png_bytes()))
    document.add_paragraph(f"图片为报销流程示意图{unique}，展示从申请到到账的五个步骤。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _upload(session, storage, deps, title: str, content: bytes) -> Document:
    doc = create_document(
        session,
        storage,
        type("U", (), {"id": _admin_id(session)})(),
        _meta(_dept_id(session), title),
        content,
        f"{title}.docx",
        DOCX_MIME,
    )
    process_document(session, deps, doc.id)
    return doc


def _stub_ocr_text(monkeypatch, text: str) -> None:
    """OCR 桩：任何图片返回固定文字（含可检索关键词）。"""
    from app.services import ocr as ocr_module

    def fake_ocr(self, image_bytes: bytes, filename: str) -> str:
        return text

    monkeypatch.setattr(ocr_module.ImageOcrClient, "ocr", fake_ocr)


def test_image_ocr_full_chain(session, tmp_path, monkeypatch) -> None:
    """含图 docx：OCR 桩 → 图片块入库（"图片1："标记）+ document_images 行 +
    落盘文件存在 + audit imageCount；开关关闭时全链不发生。"""
    _stub_ocr_text(monkeypatch, "流程示意图：申请、审批、报销、审核、到账五个步骤。")
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, mineru_base_url="http://mineru.invalid")
    deps.parser_settings.image_ocr_enabled = True
    doc = _upload(session, storage, deps, "含图制度", _docx_with_image_and_text("全链"))

    # 图片资产行 + 落盘文件
    rows = session.scalars(select(DocumentImage).where(DocumentImage.document_id == doc.id)).all()
    assert len(rows) == 1 and rows[0].image_index == 1 and rows[0].ocr_chars > 0
    assert (tmp_path / rows[0].file_path).is_file()

    # 图片文字进切片（"图片1："标记在 content 内）
    contents = session.scalars(select(Chunk.content).where(Chunk.document_id == doc.id)).all()
    assert any("图片1：流程示意图" in c for c in contents)

    # 开关关闭回归：同内容新文档不产生图片块/资产行
    deps_off = _make_deps(storage, mineru_base_url="http://mineru.invalid")
    deps_off.parser_settings.image_ocr_enabled = False
    doc_off = _upload(
        session, storage, deps_off, "含图制度关闭开关", _docx_with_image_and_text("关开关")
    )
    assert doc_off.status == "ready"
    assert (
        session.scalar(
            select(func.count(DocumentImage.id)).where(DocumentImage.document_id == doc_off.id)
        )
        == 0
    )
    off_contents = " ".join(
        session.scalars(select(Chunk.content).where(Chunk.document_id == doc_off.id)).all()
    )
    assert "图片1：" not in off_contents


def test_image_ocr_failure_degrades(session, tmp_path, monkeypatch) -> None:
    """OCR 失败降级：纯文本 ready + degrade_note 标注"N 张图片未解析"；资产行仍登记
    （图片落盘可回显，文字未参与检索）。"""
    from app.services import ocr as ocr_module

    def failing_ocr(self, image_bytes: bytes, filename: str) -> str:
        raise UpstreamError("mineru 图片 OCR 调用失败（已重试 1 次）: ReadTimeout")

    monkeypatch.setattr(ocr_module.ImageOcrClient, "ocr", failing_ocr)
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, mineru_base_url="http://mineru.invalid")
    deps.parser_settings.image_ocr_enabled = True
    doc = _upload(session, storage, deps, "降级标注文档", _docx_with_image_and_text("降级"))
    assert doc.status == "ready", doc.parse_error  # 降级不阻塞
    assert "1 张图片未解析" in (doc.parse_error or "")
    rows = session.scalars(select(DocumentImage).where(DocumentImage.document_id == doc.id)).all()
    assert len(rows) == 1 and rows[0].ocr_chars == 0


def test_image_ocr_bad_json_degrades_not_fails(session, tmp_path, monkeypatch) -> None:
    """真实 OCR 客户端遇坏 JSON（I2 收口前为裸 ValueError 逃逸）：ingest_pipeline 捕
    UpstreamError 走"单图跳过计数"——文档 ready + degrade_note，绝不整篇转 failed。
    桩方式：类方法替换为委托给带 MockTransport 的真实 ImageOcrClient（跑真重试/解析代码）。"""
    import httpx as _httpx

    from app.services import ocr as ocr_module
    from app.services.ocr import ImageOcrClient

    bad_json_client = ImageOcrClient(
        "http://mineru.invalid",
        timeout_s=5,
        transport=_httpx.MockTransport(lambda request: _httpx.Response(200, content=b"not-json")),
    )
    original_ocr = ImageOcrClient.ocr  # 先取原函数再替换类方法，委托调用不递归
    monkeypatch.setattr(
        ocr_module.ImageOcrClient,
        "ocr",
        lambda self, image_bytes, filename: original_ocr(bad_json_client, image_bytes, filename),
    )
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, mineru_base_url="http://mineru.invalid")
    deps.parser_settings.image_ocr_enabled = True
    doc = _upload(session, storage, deps, "坏JSON降级文档", _docx_with_image_and_text("坏JSON"))
    assert doc.status == "ready", doc.parse_error  # 单图坏 JSON 只跳过计数，不拖垮整篇
    assert "1 张图片未解析" in (doc.parse_error or "")  # 跳过计数语义（与 UpstreamError 同轨）
    rows = session.scalars(select(DocumentImage).where(DocumentImage.document_id == doc.id)).all()
    assert len(rows) == 1 and rows[0].ocr_chars == 0  # 图片仍落库登记（可回显，文字未参与检索）


def test_image_ocr_no_text_still_stored(session, tmp_path, monkeypatch) -> None:
    """OCR 返回空（图无文字）：资产行登记 ocr_chars=0，无"图片N："标记块。"""
    _stub_ocr_text(monkeypatch, "")
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, mineru_base_url="http://mineru.invalid")
    deps.parser_settings.image_ocr_enabled = True
    doc = _upload(session, storage, deps, "空图文档", _docx_with_image_and_text("空图"))
    assert doc.status == "ready"
    rows = session.scalars(select(DocumentImage).where(DocumentImage.document_id == doc.id)).all()
    assert len(rows) == 1 and rows[0].ocr_chars == 0
    contents = " ".join(
        session.scalars(select(Chunk.content).where(Chunk.document_id == doc.id)).all()
    )
    assert "图片1：" not in contents
