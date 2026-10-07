r"""文档在线预览端点测试：统一 PDF inline。覆盖 pdf/老格式转换产物直通、
docx 经转换桩 + content_hash 缓存（二次请求只转一次）、文本类 422、
未配置 422、401/404/软删 404 闸、删除后预览缓存清理。"""

import io

import pytest
from docx import Document as DocxDocument
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, User
from app.services import document_preview
from tests.conftest import login_as
from tests.integration.test_ingest import _pdf_bytes


def _unique_docx(unique: str) -> bytes:
    document = DocxDocument()
    document.add_paragraph(
        f"差旅费用报销制度正文{unique}：交通费凭票据据实报销，住宿费按城市分档执行。" * 3
    )
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
def sandbox_storage(tmp_path, monkeypatch):
    """STORAGE_DIR 指向 tmp_path + MINERU_BASE_URL 钉空（口径同 test_document_images 的
    本地夹具；不清 MinerU 会吃到 .env 的真容器——极简 PDF 文字层 <200 字符被判扫描件，
    走真 OCR 数分钟且版面简化后过不了质量红线）。"""
    import os

    saved_dir = os.environ.get("STORAGE_DIR")
    saved_mineru = os.environ.get("MINERU_BASE_URL")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("MINERU_BASE_URL", "")
    get_settings.cache_clear()
    yield tmp_path
    if saved_dir is None:
        monkeypatch.delenv("STORAGE_DIR", raising=False)
    else:
        monkeypatch.setenv("STORAGE_DIR", saved_dir)
    if saved_mineru is None:
        monkeypatch.delenv("MINERU_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("MINERU_BASE_URL", saved_mineru)
    get_settings.cache_clear()


@pytest.fixture()
def convert_env(monkeypatch):
    """指向不可达 Gotenberg + 预览侧装配引用换桩（findings I5 后 converter 只经
    document_preview.build_converter 取得，桩打在该名字上，记录调用次数）。"""

    monkeypatch.setenv("CONVERT_BASE_URL", "http://gotenberg.invalid")
    get_settings.cache_clear()
    calls: list[int] = []

    class StubConverter:
        def convert_to_pdf(self, filename: str, content: bytes) -> tuple[bytes, str]:
            calls.append(1)
            return b"%PDF-1.4\n%stub-preview-bytes\n", "stub.pdf"

    monkeypatch.setattr(document_preview, "build_converter", lambda settings: StubConverter())
    yield calls
    get_settings.cache_clear()


def _make_admin(session):
    """幂等建号：gates 用例清除 cookie 重登会二次调用，重复 INSERT 撞 users_username_key。"""
    from app.core.security import hash_password
    from app.db.models import Department

    existing = session.scalars(select(User).where(User.username == "previewadmin")).first()
    if existing is not None:
        return existing
    dept = session.scalars(select(Department).where(Department.code == "all")).one()
    user = User(
        username="previewadmin",
        password_hash=hash_password("PreviewAdmin#2026"),
        dept_id=dept.id,
        role="admin",
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _upload(client, filename: str, mime: str, content: bytes, title: str) -> dict:
    resp = client.post(
        "/api/documents",
        files={"file": (filename, content, mime)},
        data={
            "title": title,
            "dept": "all",
            "owner": "admin",
            "effectiveDate": "2026-10-01",
            "complianceConfirmed": "true",
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]


def _login_admin(client, session) -> None:
    _make_admin(session)
    assert login_as(client, "previewadmin", "PreviewAdmin#2026").status_code == 200


def test_preview_pdf_passthrough_inline(client, session, sandbox_storage, test_mode) -> None:
    """pdf 直通：inline 头 + application/pdf + 字节一致 + 中文名 RFC 5987 可解码。"""
    _login_admin(client, session)
    pdf = _pdf_bytes(
        [f"Preview passthrough document paragraph {i} keeps text searchable." for i in range(8)]
    )
    data = _upload(client, "预览直通.pdf", "application/pdf", pdf, "预览直通")
    resp = client.get(f"/api/documents/{data['id']}/preview")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == pdf
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("inline")
    from urllib.parse import unquote

    assert unquote(disposition.split("''")[-1]) == "预览直通.pdf"


def test_preview_docx_converts_and_caches(
    client, session, sandbox_storage, test_mode, convert_env
) -> None:
    """docx：转换桩产 PDF + 缓存落盘；第二次请求命中缓存（桩只调一次）。"""
    _login_admin(client, session)
    data = _upload(
        client,
        "转换预览.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        _unique_docx("缓存链"),
        "转换预览",
    )
    doc_id = data["id"]
    first = client.get(f"/api/documents/{doc_id}/preview")
    assert first.status_code == 200, first.text
    assert first.content.startswith(b"%PDF-")
    second = client.get(f"/api/documents/{doc_id}/preview")
    assert second.status_code == 200
    assert second.content == first.content
    assert sum(convert_env) == 1  # 缓存命中：转换只发生一次

    doc = session.get(Document, doc_id)
    cache = sandbox_storage / "previews" / str(doc_id) / f"{doc.content_hash}.pdf"
    assert cache.is_file() and cache.read_bytes() == first.content  # 读回：缓存路径/内容

    # 删除文档 → 预览缓存目录随档清理
    assert client.delete(f"/api/documents/{doc_id}").status_code == 200
    assert not (sandbox_storage / "previews" / str(doc_id)).exists()


def test_preview_text_format_rejected(client, session, sandbox_storage, test_mode) -> None:
    """md（文本类）：预览端点 422 明确提示，前端走 /file 文本渲染路径。"""
    _login_admin(client, session)
    md = "\n".join(
        f"# 预览文本\n\n第{i}条 Markdown 文档内容用于验证文本类格式不走 PDF 预览端点。"
        for i in range(4)
    )
    data = _upload(client, "预览文本.md", "text/markdown", md.encode(), "预览文本")
    resp = client.get(f"/api/documents/{data['id']}/preview")
    assert resp.status_code == 422
    assert resp.json()["message"] == "该文档为文本格式，请直接查看内容或下载原文件"


def test_preview_converter_unconfigured(
    client, session, sandbox_storage, test_mode, monkeypatch
) -> None:
    """转换服务未配置：docx 预览 422 中文降级（pdf 直通不受影响）。"""
    import os

    saved = os.environ.get("CONVERT_BASE_URL")
    monkeypatch.setenv("CONVERT_BASE_URL", "")
    get_settings.cache_clear()
    try:
        _login_admin(client, session)
        data = _upload(
            client,
            "未配置预览.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            _unique_docx("未配置"),
            "未配置预览",
        )
        resp = client.get(f"/api/documents/{data['id']}/preview")
        assert resp.status_code == 422
        assert resp.json()["message"] == "在线预览服务未配置，请下载原文件查看"
    finally:
        if saved is None:
            monkeypatch.delenv("CONVERT_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("CONVERT_BASE_URL", saved)
        get_settings.cache_clear()


def test_preview_gates_401_404(client, session, sandbox_storage, test_mode) -> None:
    """未登录 401；不存在 404；软删后 404（与 /file 同口径的存活+隔离闸）。"""
    _login_admin(client, session)
    pdf = _pdf_bytes(
        [f"Preview gates document paragraph {i} keeps text searchable here." for i in range(8)]
    )
    data = _upload(client, "闸门预览.pdf", "application/pdf", pdf, "闸门预览")
    doc_id = data["id"]
    assert client.get(f"/api/documents/{doc_id}/preview").status_code == 200

    client.cookies.clear()
    assert client.get(f"/api/documents/{doc_id}/preview").status_code == 401

    _login_admin(client, session)
    assert client.get("/api/documents/999999/preview").status_code == 404

    doc = session.get(Document, doc_id)
    doc.deleted_at = doc.updated_at
    session.commit()
    assert client.get(f"/api/documents/{doc_id}/preview").status_code == 404
