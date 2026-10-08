r"""文档分型端点测试（metrics-balance 工单 01）：入库自动分型读回、docType 透出、
PATCH 合法值/枚举外/404/成员 403、backfill 范围（is_active 且 ready）/覆盖手工修正/
updated+byType/审计读回/成员 403。

入库链路用 test_mode 同步收口（同 test_documents 口径）走真解析管道；回填与 PATCH
用直插 ORM 行构造确定性信号（chunks 内容即检测输入，embedding 桩零向量）。
写库断言一律带读回；分型不改可检索事实：PATCH/回填按 §3.4 落锤不清问答缓存。
"""

import io
import os
from datetime import date

import pytest
from docx import Document as DocxDocument
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ERR_FORBIDDEN, ERR_NOT_FOUND, ERR_PARAM_INVALID
from app.db.models import AuditLog, Chunk, Department, Document, User
from tests.conftest import login_as

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ---------- fixtures（口径同 tests/api/test_documents.py） ----------


@pytest.fixture()
def sandbox_storage(tmp_path):
    """STORAGE_DIR 指向 tmp_path 并清 get_settings 缓存：上传落盘与解析读盘全隔离。"""
    saved = os.environ.get("STORAGE_DIR")
    os.environ["STORAGE_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    if saved is None:
        os.environ.pop("STORAGE_DIR", None)
    else:
        os.environ["STORAGE_DIR"] = saved
    get_settings.cache_clear()


@pytest.fixture()
def test_mode():
    """开启 settings.test_mode：上传端点同步执行 process_document（解析收口即终态）。"""
    saved = os.environ.get("TEST_MODE")
    os.environ["TEST_MODE"] = "true"
    get_settings.cache_clear()
    yield
    if saved is None:
        os.environ.pop("TEST_MODE", None)
    else:
        os.environ["TEST_MODE"] = saved
    get_settings.cache_clear()


# ---------- 构造辅助 ----------


def make_user(session: Session, username: str, password: str, role: str) -> User:
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
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


def admin_login(client, session: Session) -> None:
    make_user(session, "docadmin", "right-one", role="admin")
    assert login_as(client, "docadmin", "right-one").status_code == 200


def _clause_docx() -> bytes:
    """六处条款编号 docx：过质量红线（≈120 有效字）且命中条款信号（≥5 处）。"""
    document = DocxDocument()
    for i in range(1, 7):
        document.add_paragraph(f"第{i}条 员工应当遵守本条规定并妥善执行。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _flat_docx(lines: int = 80) -> bytes:
    """无标题平文 docx：不含条款编号/表格信号，≥1500 字 → 兜底 general。"""
    document = DocxDocument()
    for i in range(lines):
        document.add_paragraph(f"内容说明{i}：适用于全体员工并遵照执行。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def post_document(client, *, title: str, content: bytes) -> dict:
    resp = client.post(
        "/api/documents",
        data={
            "title": title,
            "dept": "service",
            "owner": "制度责任人",
            "effectiveDate": "2026-09-01",
            "complianceConfirmed": "true",
        },
        files={"file": (f"{title}.docx", content, DOCX_MIME)},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]


def make_doc(
    session: Session,
    *,
    title: str,
    status: str = "ready",
    is_active: bool = True,
    doc_type: str = "general",
) -> Document:
    """直插 ready 文档行（content_hash 随标题唯一，避开 uq_doc_content_hash）。"""
    dept = session.scalars(select(Department).where(Department.code == "service")).first()
    admin = session.scalars(select(User).where(User.username == "docadmin")).first()
    doc = Document(
        title=title,
        dept_id=dept.id,
        original_filename=f"{title}.docx",
        file_path=f"docs/{title}.docx",
        mime=DOCX_MIME,
        size_bytes=1024,
        content_hash=f"hash-{title}",
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status=status,
        doc_type=doc_type,
        is_active=is_active,
        uploaded_by=admin.id,
    )
    session.add(doc)
    session.flush()
    return doc


def add_chunks(session: Session, doc: Document, contents: list[str]) -> None:
    """直插桩块（embedding 零向量即可满足 NOT NULL；检测只读 title_path/content）。"""
    for position, content in enumerate(contents):
        session.add(
            Chunk(
                document_id=doc.id,
                dept_id=doc.dept_id,
                title_path=content.partition("｜")[0],
                content=content,
                content_chars=len(content),
                position=position,
                embedding=[0.0] * 1024,
            )
        )
    session.flush()


def patch_doc_type(client, doc_id: int, doc_type: str):
    return client.patch(f"/api/admin/documents/{doc_id}/doc-type", json={"docType": doc_type})


# ---------- 入库自动分型（真解析管道，读回断言） ----------


def test_上传入库自动分型_条款文档落policy_clause(
    client, session, sandbox_storage, test_mode
) -> None:
    admin_login(client, session)
    data = post_document(client, title="客服_考勤制度_20260901", content=_clause_docx())
    doc = session.get(Document, data["id"])
    assert doc is not None and doc.status == "ready"
    assert doc.doc_type == "policy_clause"


def test_上传入库自动分型_长平文落general兜底(client, session, sandbox_storage, test_mode) -> None:
    admin_login(client, session)
    data = post_document(client, title="客服_杂项说明_20260901", content=_flat_docx())
    doc = session.get(Document, data["id"])
    assert doc is not None and doc.status == "ready"
    assert doc.doc_type == "general"


def test_文档列表透出docType(client, session, sandbox_storage, test_mode) -> None:
    admin_login(client, session)
    data = post_document(client, title="客服_考勤制度_20260902", content=_clause_docx())
    resp = client.get("/api/documents", params={"keyword": "考勤制度_20260902"})
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == data["id"]
    assert items[0]["docType"] == "policy_clause"


# ---------- PATCH /api/admin/documents/{id}/doc-type ----------


def test_PATCH合法值读回_落库并留审计(client, session) -> None:
    admin_login(client, session)
    doc = make_doc(session, title="客服_通知A")
    resp = patch_doc_type(client, doc.id, "notice")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": doc.id, "docType": "notice"}
    session.expire_all()  # 读回库内真相（防会话镜像）
    reloaded = session.get(Document, doc.id)
    assert reloaded.doc_type == "notice"
    audits = list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.object_type == "document",
                AuditLog.action == "document.set_doctype",
                AuditLog.object_id == doc.id,
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].detail == {"before": {"docType": "general"}, "after": {"docType": "notice"}}


def test_PATCH枚举外422_库值不变(client, session) -> None:
    admin_login(client, session)
    doc = make_doc(session, title="客服_通知B")
    resp = patch_doc_type(client, doc.id, "moon")
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert "分型取值无效" in body["message"]
    session.expire_all()
    assert session.get(Document, doc.id).doc_type == "general"


def test_PATCH不存在文档404(client, session) -> None:
    admin_login(client, session)
    resp = patch_doc_type(client, 99999, "notice")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == ERR_NOT_FOUND
    assert body["message"] == "文档不存在"


def test_PATCH成员403(client, session) -> None:
    make_user(session, "docmember", "right-one", role="member")
    assert login_as(client, "docmember", "right-one").status_code == 200
    resp = patch_doc_type(client, 1, "notice")
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN


# ---------- POST /api/admin/documents/backfill-doc-type ----------


def test_回填_重算覆盖手工修正并返回updated与byType(client, session) -> None:
    admin_login(client, session)
    doc_a = make_doc(session, title="客服_手册A", doc_type="notice")  # 手工值被重算覆盖
    add_chunks(session, doc_a, ["第一章｜" + "第一条 制度内容说明。\n" for _ in range(6)])
    doc_b = make_doc(session, title="客服_通知B", doc_type="notice")  # 与重算一致 → 不计 updated
    add_chunks(session, doc_b, ["关于放假的通知｜" + "正文说明。" * 10])
    resp = client.post("/api/admin/documents/backfill-doc-type")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"updated": 1, "byType": {"policy_clause": 1}}
    session.expire_all()
    assert session.get(Document, doc_a.id).doc_type == "policy_clause"
    assert session.get(Document, doc_b.id).doc_type == "notice"
    audits = list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.object_type == "document",
                AuditLog.action == "document.backfill_doctype",
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].detail == {"after": {"updated": 1, "byType": {"policy_clause": 1}}}


def test_回填_非active或非ready文档不参与(client, session) -> None:
    admin_login(client, session)
    inactive = make_doc(session, title="客服_停用C", is_active=False)
    add_chunks(session, inactive, ["｜" + "第一条 制度内容说明。\n" for _ in range(6)])
    pending = make_doc(session, title="客服_解析中D", status="parsing")
    resp = client.post("/api/admin/documents/backfill-doc-type")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"updated": 0, "byType": {}}
    session.expire_all()
    assert session.get(Document, inactive.id).doc_type == "general"
    assert session.get(Document, pending.id).doc_type == "general"


def test_回填_无分型变化时updated为零(client, session) -> None:
    admin_login(client, session)
    make_doc(session, title="客服_一致E", doc_type="notice")
    resp = client.post("/api/admin/documents/backfill-doc-type")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"updated": 0, "byType": {}}


def test_回填成员403(client, session) -> None:
    make_user(session, "docmember2", "right-one", role="member")
    assert login_as(client, "docmember2", "right-one").status_code == 200
    resp = client.post("/api/admin/documents/backfill-doc-type")
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
