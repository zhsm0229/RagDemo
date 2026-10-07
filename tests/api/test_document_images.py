r"""图片回显与原文件端点测试（F-1017/F-1018）：test_mode 同步解析 + OCR 桩。

覆盖：登录闸 401、字节一致（图片 png / 原文件 docx 与上传一致、
Content-Disposition 用 original_filename）、不存在序号 404。
"""

import io

import pytest
from docx import Document as DocxDocument
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, User
from tests.conftest import login_as


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (100, 60), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _unique_docx(unique: str) -> bytes:
    document = DocxDocument()
    document.add_paragraph(
        f"差旅费用报销制度正文{unique}：交通费凭票据据实报销，住宿费按城市分档执行。" * 3
    )
    document.add_picture(io.BytesIO(_png_bytes()))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
def sandbox_storage(tmp_path, monkeypatch):
    """STORAGE_DIR 指向 tmp_path（口径同 test_documents/test_zip_import 的本地夹具）。"""
    import os

    saved = os.environ.get("STORAGE_DIR")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    if saved is None:
        monkeypatch.delenv("STORAGE_DIR", raising=False)
    else:
        monkeypatch.setenv("STORAGE_DIR", saved)
    get_settings.cache_clear()


@pytest.fixture()
def ocr_mode(monkeypatch):
    """开启图片 OCR + 指向不可达 MinerU + OCR 桩返回固定文字（端到端走真管线）。"""
    from app.services import ocr as ocr_module

    monkeypatch.setenv("IMAGE_OCR_ENABLED", "true")
    monkeypatch.setenv("MINERU_BASE_URL", "http://mineru.invalid")
    monkeypatch.setenv("MINERU_TIMEOUT_S", "5")
    get_settings.cache_clear()
    monkeypatch.setattr(
        ocr_module.ImageOcrClient,
        "ocr",
        lambda self, image_bytes, filename: "流程示意图：申请、审批、报销、审核、到账五个步骤。",
    )
    yield
    get_settings.cache_clear()


def _upload_docx(client, unique: str, content: bytes) -> dict:
    resp = client.post(
        "/api/documents",
        files={
            "file": (
                f"{unique}.docx",
                content,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={
            "title": f"图片端点验证{unique}",
            "dept": "all",
            "owner": "admin",
            "effectiveDate": "2026-10-01",
            "complianceConfirmed": "true",
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]


def _make_admin(session):
    from app.core.security import hash_password
    from app.db.models import Department

    dept = session.scalars(select(Department).where(Department.code == "all")).one()
    user = User(
        username="imgadmin",
        password_hash=hash_password("ImgAdmin#2026"),
        dept_id=dept.id,
        role="admin",
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def test_image_and_file_endpoints(client, session, sandbox_storage, test_mode, ocr_mode) -> None:
    """回显/原文件端点：字节一致、原文件用 original_filename 命名；401/404 闸。"""
    _make_admin(session)
    assert login_as(client, "imgadmin", "ImgAdmin#2026").status_code == 200

    unique = "全链"
    content = _unique_docx(unique)
    data = _upload_docx(client, unique, content)
    doc_id = data["id"]
    assert data["status"] in ("ready", "parsing")  # test_mode 同步解析 → ready
    assert data["status"] == "ready"

    # 图片回显：字节与上传内嵌图一致
    img = client.get(f"/api/documents/{doc_id}/images/1")
    assert img.status_code == 200
    assert img.content == _png_bytes()
    assert img.headers["content-type"].startswith("image/png")

    # 原文件：字节一致 + Content-Disposition 用 original_filename
    file_resp = client.get(f"/api/documents/{doc_id}/file")
    assert file_resp.status_code == 200
    assert file_resp.content == content
    from urllib.parse import unquote

    # RFC 5987：中文文件名 URL 编码（filename*=UTF-8''…），全链→%E5%85%A8%E9%93%BE
    disposition = file_resp.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=UTF-8" in disposition  # RFC 5987 透传（原文件名中文）
    assert unquote(disposition.split("''")[-1]) == "全链.docx"  # original_filename=上传文件名

    # 不存在的图片序号 → 404；未登录 → 401
    assert client.get(f"/api/documents/{doc_id}/images/99").status_code == 404
    client.cookies.clear()
    assert client.get(f"/api/documents/{doc_id}/images/1").status_code == 401
    assert client.get(f"/api/documents/{doc_id}/file").status_code == 401


def test_image_endpoint_inactive_doc_404(
    client, session, sandbox_storage, test_mode, ocr_mode
) -> None:
    """非 ready/未激活文档不泄漏图片（与 citations 存活闸同口径）。"""
    _make_admin(session)
    assert login_as(client, "imgadmin", "ImgAdmin#2026").status_code == 200
    unique = "待确认"
    data = _upload_docx(client, unique, _unique_docx(unique))
    doc_id = data["id"]
    # 强制置为未激活（模拟敏感预检待确认态）
    doc = session.get(Document, doc_id)
    doc.is_active = False
    session.commit()
    assert client.get(f"/api/documents/{doc_id}/images/1").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/file").status_code == 404
