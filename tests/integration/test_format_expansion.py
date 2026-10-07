r"""格式扩展集成测试（F-1013/F-1014/F-1015）：新格式上传→ready→分块落库→检索命中；
OOXML 判型拒收；老格式转换编排（converter 桩）。依赖构造复用 test_ingest 工具。"""

import io

import openpyxl
from sqlalchemy import select

from app.core.llm import StubEmbedder
from app.core.storage import LocalFileStorage
from app.db.models import Chunk, Document, User
from app.services.ingest import create_document, process_document
from tests.integration.test_ingest import _make_deps, _meta

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _admin_id(session) -> int:
    return session.scalars(select(User.id).where(User.username == "admin")).one()


def _dept_id(session) -> int:
    return session.scalars(select(User.dept_id).where(User.username == "admin")).one()


def _xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _html_bytes(title: str, body: str) -> bytes:
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1><p>{body}</p></body></html>".encode()


def _upload_and_process(
    session, storage, deps, title: str, filename: str, mime: str, content: bytes
) -> Document:
    doc = create_document(
        session,
        storage,
        type("U", (), {"id": _admin_id(session)})(),
        _meta(_dept_id(session), title),
        content,
        filename,
        mime,
    )
    process_document(session, deps, doc.id)
    return doc


def test_md_ready_and_chunks_have_heading_path(session, tmp_path) -> None:
    """md：标题层级进 title_path（分块前缀），收口 ready、进度清理。"""
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, embedder=StubEmbedder())
    body = "\n".join(
        f"第{i}条 差旅费用须凭正规票据据实报销，逾期五日不予受理，请各部门遵照执行第{i}批。"
        for i in range(8)
    )
    md = f"# 差旅制度\n\n{body}\n\n## 报销标准\n\n{body}\n".encode()
    doc = _upload_and_process(
        session, storage, deps, "差旅制度", "差旅制度.md", "text/markdown", md
    )
    assert doc.status == "ready", doc.parse_error
    assert doc.parse_engine == "markdown"
    chunks = session.scalars(select(Chunk.content).where(Chunk.document_id == doc.id)).all()
    assert any("报销标准" in c for c in chunks)  # 标题路径进分块前缀
    assert doc.parse_stage is None and doc.parse_progress is None


def test_xlsx_ready_and_content_inline(session, tmp_path) -> None:
    """xlsx：sheet 分节就绪；单元格行内连接（重复短值不独立成行，过清洗）。"""
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, embedder=StubEmbedder())
    rows = [["项目", "报销标准"]] + [
        [f"差旅费第{i}档", f"凭票据据实报销且单程上限五百元第{i}档说明"] for i in range(12)
    ]
    doc = _upload_and_process(
        session,
        storage,
        deps,
        "报销标准表",
        "报销标准表.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _xlsx_bytes({"报销": rows}),
    )
    assert doc.status == "ready", doc.parse_error
    assert doc.parse_engine == "xlsx"
    contents = session.scalars(select(Chunk.content).where(Chunk.document_id == doc.id)).all()
    assert any("差旅费第1档 凭票据据实报销" in c for c in contents)  # 行内连接保留


def test_html_ready_and_script_stripped(session, tmp_path) -> None:
    """html：正文入库、script 剥离（XSS 载荷不进向量库）。"""
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, embedder=StubEmbedder())
    doc = _upload_and_process(
        session,
        storage,
        deps,
        "产品手册",
        "产品手册.html",
        "text/html",
        _html_bytes(
            "产品手册",
            "本产品支持完全离线部署，所有数据本地存储不上传不上云，适合内网与涉密环境使用。单机部署过程约需十分钟，支持国产化适配与私有化交付，提供完整的权限管理与部门隔离能力。欢迎在内网试点部门优先试用，并在使用过程中持续反馈问题，帮助我们改进产品体验与解析质量。",
        ).replace(b"</body>", b"<script>alert(1)</script></body>"),
    )
    assert doc.status == "ready", doc.parse_error
    contents = " ".join(
        session.scalars(select(Chunk.content).where(Chunk.document_id == doc.id)).all()
    )
    assert "离线部署" in contents
    assert "alert" not in contents


def test_doc_converted_via_gotenberg_stub(session, tmp_path) -> None:
    """老格式：converter 桩返回 PDF 字节（Gotenberg libreoffice 路由实测只出 PDF）
    → 转换落盘 → 按新扩展名 .pdf 走 pdf 解析链 ready。"""
    from tests.integration.test_ingest import _pdf_bytes

    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, embedder=StubEmbedder())
    # converter 桩：返回可解析 PDF（8 页各一行不同内容，避开页眉页脚跨页聚合清洗）
    pdf = _pdf_bytes(
        [
            f"Legacy converted document paragraph {i}: content flows through pipeline."
            for i in range(8)
        ]
    )

    class StubConverter:
        def convert_to_pdf(self, filename: str, content: bytes) -> tuple[bytes, str]:
            return pdf, "converted.pdf"

    deps.converter = StubConverter()
    doc = _upload_and_process(
        session,
        storage,
        deps,
        "老文档转换",
        "老文档.doc",
        "application/msword",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy-payload",  # 完整 OLE 8 字节魔数（precheck 校验）
    )
    assert doc.status == "ready", doc.parse_error
    assert doc.parse_engine == "pypdf"  # 转换产物为 PDF → 文字版走 pypdf 快速通道
    assert doc.original_filename == "老文档.doc"  # 真实来源保留
    assert doc.parse_error_detail is None


def test_doc_without_converter_fails_with_reason(session, tmp_path) -> None:
    """转换服务未配置：老格式 failed + 中文原因（引导另存），不留 parsing 僵尸。"""
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, embedder=StubEmbedder())
    doc = _upload_and_process(
        session,
        storage,
        deps,
        "无转换服务",
        "旧稿.ppt",
        "application/vnd.ms-powerpoint",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy-payload",  # 完整 OLE 8 字节魔数
    )
    assert doc.status == "failed"
    assert "转换服务未配置" in doc.parse_error


def test_ooxml_renamed_rejected_before_process(session, tmp_path) -> None:
    """改名混入（xlsx→docx）：同步段判型 422，不入库（F-1014）。"""
    import pytest

    from app.core.errors import ApiError

    storage = LocalFileStorage(tmp_path)
    renamed = _xlsx_bytes({"表": [["数据"]]})
    with pytest.raises(ApiError) as ei:
        create_document(
            session,
            storage,
            type("U", (), {"id": _admin_id(session)})(),
            _meta(_dept_id(session), "改名混入"),
            renamed,
            "改名.docx",
            DOCX_MIME,
        )
    assert ei.value.message == "文件内容与扩展名不符，请勿伪造格式"
