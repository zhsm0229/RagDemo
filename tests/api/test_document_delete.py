"""文档删除端点测试（F-305，2026-10-06 立项实施）：软删+级联停用 chunks+审计读回。

覆盖：admin 删除成功（软删字段/级联/审计读回）、解析中 422、
member 403、未登录 401、删除后列表与检索不再命中。
"""

import io
import os

import pytest
from docx import Document as DocxDocument
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import AuditLog, Chunk, Department, Document, User
from tests.conftest import login_as

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture()
def sandbox_storage(tmp_path, monkeypatch):
    """STORAGE_DIR 指向 tmp_path 并清 get_settings 缓存（口径同 test_zip_import）。"""
    saved = os.environ.get("STORAGE_DIR")
    os.environ["STORAGE_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    if saved is None:
        os.environ.pop("STORAGE_DIR", None)
    else:
        os.environ["STORAGE_DIR"] = saved
    get_settings.cache_clear()


def _make_user(session, username: str, password: str, role: str) -> User:
    """直插测试用户（口径同 test_zip_import），挂在测试事务内。"""
    dept = session.scalars(select(Department).where(Department.code == "all")).one()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _make_admin(session):
    return _make_user(session, "deladmin", "DelAdmin#2026", role="admin")


def _make_member(session):
    return _make_user(session, "delmember", "DelMember#2026", role="member")


def _upload_docx(client, session, unique: str) -> dict:
    """test_mode 同步解析上传：返回 data（含 id/status=ready）。"""

    buffer = io.BytesIO()
    document = DocxDocument()
    document.add_paragraph(
        f"删除测试正文{unique}：交通费凭票据据实报销，住宿费按城市分档上限执行。" * 3
    )
    buffer.seek(0)
    document.save(buffer)
    resp = client.post(
        "/api/documents",
        files={"file": (f"{unique}.docx", buffer.getvalue(), DOCX_MIME)},
        data={
            "title": f"删除测试{unique}",
            "dept": "all",
            "owner": "admin",
            "effectiveDate": "2026-10-01",
            "complianceConfirmed": "true",
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]


def test_delete_soft_deletes_and_cascades_chunks(client, session, test_mode, sandbox_storage):
    """删除：软删字段（deleted_at/is_active=false）+ chunks 级联停用 + 审计读回；列表不再返回。"""
    _make_admin(session)
    assert login_as(client, "deladmin", "DelAdmin#2026").status_code == 200
    data = _upload_docx(client, session, "删除全链")
    doc_id = data["id"]
    assert data["status"] == "ready"  # test_mode 同步解析

    resp = client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["id"] == doc_id and body["deleted"] is True

    # 读回：软删字段
    doc = session.get(Document, doc_id)
    assert doc.is_active is False
    assert doc.deleted_at is not None

    # chunks 级联停用
    active_chunks = session.scalars(
        select(Chunk.is_active).where(Chunk.document_id == doc_id)
    ).all()
    assert active_chunks and set(active_chunks) == {False}

    # 审计留痕
    audits = session.scalars(
        select(AuditLog).where(
            AuditLog.object_type == "document",
            AuditLog.object_id == doc_id,
            AuditLog.action == "document.delete",
        )
    ).all()
    assert len(audits) == 1

    # 列表不再返回
    rows = client.get("/api/documents?page=1&pageSize=50").json()["data"]["items"]
    assert all(r["id"] != doc_id for r in rows)


def test_delete_parsing_state_422(client, session, test_mode, sandbox_storage):
    """解析中（parsing）删除 → 422（防与后台任务竞态）。"""
    _make_admin(session)
    assert login_as(client, "deladmin", "DelAdmin#2026").status_code == 200
    data = _upload_docx(client, session, "删除parsing")
    doc_id = data["id"]
    # 强制置 parsing（模拟异步解析进行中）
    doc = session.get(Document, doc_id)
    doc.status = "parsing"
    session.commit()
    resp = client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 422
    assert "解析中" in resp.json()["message"]


def test_delete_member_403(client, session, test_mode, sandbox_storage):
    """member 删除 → 403/2004（语料治理 admin-only）。"""
    _make_member(session)
    assert login_as(client, "delmember", "DelMember#2026").status_code == 200
    resp = client.delete("/api/documents/999999")
    assert resp.status_code == 403
    assert resp.json()["code"] == 2004


def test_delete_unauthenticated_401(client):
    """未登录 → 401/2003。"""
    resp = client.delete("/api/documents/999999")
    assert resp.status_code == 401
    assert resp.json()["code"] == 2003
