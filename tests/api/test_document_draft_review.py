r"""草稿审核端点测试（agent-evolution #05）：POST /api/documents/{id}/draft/approve|reject。

覆盖：通过触发既有解析入库管道（test_mode 同步段 monkeypatch 断言 mock 被调 +
全链路真实收口 ready）、驳回原因留档、非草稿/重复审核 422、空原因 422、
member 403/2004、未登录 401/2003、列表 sourceType/reviewStatus 筛选。
"""

import os
from datetime import date
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.core.assembly import build_storage
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import AuditLog, Department, Document, User
from app.services import draft_review
from tests.conftest import login_as


@pytest.fixture()
def sandbox_storage(tmp_path, monkeypatch):
    """STORAGE_DIR 指向 tmp_path 并清 get_settings 缓存（口径同 test_document_delete）。"""
    saved = os.environ.get("STORAGE_DIR")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    if saved is None:
        monkeypatch.delenv("STORAGE_DIR", raising=False)
    else:
        monkeypatch.setenv("STORAGE_DIR", saved)
    get_settings.cache_clear()


def _make_user(session, username: str, password: str, role: str) -> User:
    """直插测试用户（口径同 test_document_delete），挂在测试事务内。"""
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


_DRAFT_BODY = (
    "草稿接口验收第一条 内容说明：适用于全体员工并遵照执行。\n"
    "草稿接口验收第二条 内容说明：适用于全体员工并遵照执行。\n"
    "草稿接口验收第三条 内容说明：适用于全体员工并遵照执行。\n"
    "草稿接口验收第四条 内容说明：适用于全体员工并遵照执行。"
).encode()


def _create_draft_via_service(session, unique: str) -> Document:
    """service 直建草稿（创建入口属 agent 侧，本文件只测审核面）：md 白名单内容。"""
    dept = session.scalars(select(Department).where(Department.code == "all")).one()
    admin = session.scalars(select(User).where(User.username == "admin")).one()
    return draft_review.create_draft(
        session,
        build_storage(get_settings()),
        admin,
        {
            "dept_id": dept.id,
            "title": f"all_草稿接口验收{unique}",
            "effective_date": date(2026, 10, 1),
            "owner_name": "制度责任人",
        },
        _DRAFT_BODY,
        f"草稿接口验收{unique}.md",
        "text/markdown",
    )


# ---------------------------------------------------------------------------
# 通过路径：管道触发断言 + 全链路收口
# ---------------------------------------------------------------------------


def test_approve_triggers_ingest_pipeline(client, session, test_mode, sandbox_storage, monkeypatch):
    """通过 → review_status=已通过（读回）+ 既有入库管道被触发（process_document mock
    断言：test_mode 同步段同 retry 编排）+ 审计留痕。"""
    _make_user(session, "dadmin", "DraftAdmin#2026", role="admin")
    assert login_as(client, "dadmin", "DraftAdmin#2026").status_code == 200
    doc = _create_draft_via_service(session, "管道触发")
    mocked = MagicMock()
    monkeypatch.setattr("app.api.document_admin.process_document", mocked)

    resp = client.post(f"/api/documents/{doc.id}/draft/approve")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["reviewStatus"] == "已通过"

    session.expire_all()
    row = session.get(Document, doc.id)
    assert row.review_status == "已通过"
    mocked.assert_called_once()
    assert mocked.call_args.args[2] == doc.id  # (db, deps, doc_id)
    audits = session.scalars(
        select(AuditLog).where(
            AuditLog.object_type == "document",
            AuditLog.object_id == doc.id,
            AuditLog.action == "document.draft_approve",
        )
    ).all()
    assert len(audits) == 1


def test_approve_full_pipeline_ready(client, session, test_mode, sandbox_storage):
    """全链路（不 mock）：通过 → test_mode 同步走真实解析管道 → status=ready（可演示
    "通过→入库"路径；检索命中方向由 tests/integration/test_draft_review.py 读回断言）。"""
    _make_user(session, "dadmin", "DraftAdmin#2026", role="admin")
    assert login_as(client, "dadmin", "DraftAdmin#2026").status_code == 200
    doc = _create_draft_via_service(session, "全链路")

    resp = client.post(f"/api/documents/{doc.id}/draft/approve")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["reviewStatus"] == "已通过"
    assert body["status"] == "ready"

    session.expire_all()
    row = session.get(Document, doc.id)
    assert row.status == "ready"
    assert row.review_status == "已通过"


# ---------------------------------------------------------------------------
# 驳回路径
# ---------------------------------------------------------------------------


def test_reject_stores_reason_and_audits(client, session, test_mode, sandbox_storage):
    """驳回 → 已驳回 + 原因留档（读回）+ 审计；status 停在 pending（永不入库）。"""
    _make_user(session, "dadmin", "DraftAdmin#2026", role="admin")
    assert login_as(client, "dadmin", "DraftAdmin#2026").status_code == 200
    doc = _create_draft_via_service(session, "驳回")

    resp = client.post(
        f"/api/documents/{doc.id}/draft/reject", json={"reason": "数据来源未核实，请补充依据"}
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["reviewStatus"] == "已驳回"
    assert body["rejectReason"] == "数据来源未核实，请补充依据"

    session.expire_all()
    row = session.get(Document, doc.id)
    assert row.review_status == "已驳回"
    assert row.reject_reason == "数据来源未核实，请补充依据"
    assert row.status == "pending"
    audits = session.scalars(
        select(AuditLog).where(
            AuditLog.object_type == "document",
            AuditLog.object_id == doc.id,
            AuditLog.action == "document.draft_reject",
        )
    ).all()
    assert len(audits) == 1


def test_reject_blank_reason_422(client, session, test_mode, sandbox_storage):
    """空原因（min_length=1 空串被 Pydantic 拦）→ 422/1003。"""
    _make_user(session, "dadmin", "DraftAdmin#2026", role="admin")
    assert login_as(client, "dadmin", "DraftAdmin#2026").status_code == 200
    doc = _create_draft_via_service(session, "空原因")
    resp = client.post(f"/api/documents/{doc.id}/draft/reject", json={"reason": ""})
    assert resp.status_code == 422
    assert resp.json()["code"] == 1003


def test_approve_non_draft_422(client, session, test_mode, sandbox_storage):
    """对 upload 语料审核 → 422/1003（仅 Agent 草稿可审核）。"""
    _make_user(session, "dadmin", "DraftAdmin#2026", role="admin")
    assert login_as(client, "dadmin", "DraftAdmin#2026").status_code == 200
    doc = _create_draft_via_service(session, "非草稿")
    doc.source_type = "upload"
    doc.review_status = None
    session.commit()

    resp = client.post(f"/api/documents/{doc.id}/draft/approve")
    assert resp.status_code == 422
    assert "草稿" in resp.json()["message"]


def test_double_review_422(client, session, test_mode, sandbox_storage):
    """重复审核 → 第二次 422/1003（非法迁移 fail-fast）。"""
    _make_user(session, "dadmin", "DraftAdmin#2026", role="admin")
    assert login_as(client, "dadmin", "DraftAdmin#2026").status_code == 200
    doc = _create_draft_via_service(session, "重复")
    assert client.post(f"/api/documents/{doc.id}/draft/approve").status_code == 200
    resp = client.post(f"/api/documents/{doc.id}/draft/approve")
    assert resp.status_code == 422
    assert "待审核" in resp.json()["message"]


# ---------------------------------------------------------------------------
# 权限门与筛选
# ---------------------------------------------------------------------------


def test_review_member_403(client, session, test_mode, sandbox_storage):
    """member 审核 → 403/2004（语料治理 admin-only）。"""
    _make_user(session, "dmember", "DraftMember#2026", role="member")
    assert login_as(client, "dmember", "DraftMember#2026").status_code == 200
    doc = _create_draft_via_service(session, "权限")
    resp = client.post(f"/api/documents/{doc.id}/draft/approve")
    assert resp.status_code == 403
    assert resp.json()["code"] == 2004
    resp = client.post(f"/api/documents/{doc.id}/draft/reject", json={"reason": "x"})
    assert resp.status_code == 403


def test_review_unauthenticated_401(client, test_mode, sandbox_storage):
    """未登录审核 → 401/2003。"""
    resp = client.post("/api/documents/999999/draft/approve")
    assert resp.status_code == 401
    assert resp.json()["code"] == 2003


def test_list_filter_by_source_and_review_status(client, session, test_mode, sandbox_storage):
    """列表 sourceType/reviewStatus 筛选：草稿审核入口取"draft+待审核"清单；
    非法枚举 422；响应携带 sourceType/reviewStatus 字段。"""
    _make_user(session, "dadmin", "DraftAdmin#2026", role="admin")
    assert login_as(client, "dadmin", "DraftAdmin#2026").status_code == 200
    doc = _create_draft_via_service(session, "筛选")

    rows = client.get("/api/documents?sourceType=draft&reviewStatus=待审核").json()["data"]["items"]
    assert [r["id"] for r in rows] == [doc.id]
    assert rows[0]["sourceType"] == "draft"
    assert rows[0]["reviewStatus"] == "待审核"
    assert rows[0]["rejectReason"] is None

    empty = client.get("/api/documents?sourceType=upload&reviewStatus=待审核").json()["data"]
    assert empty["items"] == []

    bad = client.get("/api/documents?reviewStatus=不存在")
    assert bad.status_code == 422


def test_review_404_for_missing_doc(client, session, test_mode, sandbox_storage):
    """寻址不存在的草稿 → 404/3003（I13 同判口径）。"""
    _make_user(session, "dadmin", "DraftAdmin#2026", role="admin")
    assert login_as(client, "dadmin", "DraftAdmin#2026").status_code == 200
    resp = client.post("/api/documents/999999/draft/approve")
    assert resp.status_code == 404
    assert resp.json()["code"] == 3003
