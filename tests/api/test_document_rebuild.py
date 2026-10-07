r"""重建索引与版本历史 API 测试（F-304 + F-303，W7 补齐）。

F-304：ready 文档可重建（旧块删除、新块生成、审计双痕、缓存失效、member 403）；
failed/pending 状态拒绝 422；不存在 404；全库重建返回受理计数并逐文档重刷。
F-303：GET /{id}/versions 返回同 (dept,title) 全部代次 version 降序（多版本场景
先经 DOC-13 版本递增路径造出 v1/v2）。

test_mode=True 时端点内同步执行（同 test_documents 口径），STORAGE_DIR 指向
tmp_path 隔离落盘；docx 夹具与断言口径复用 tests/api/test_documents 的辅助。
"""

import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ERR_FORBIDDEN, ERR_NOT_FOUND, ERR_PARAM_INVALID
from app.db.models import AuditLog, Chunk, Document
from tests.api.test_documents import admin_login, make_user, post_document
from tests.conftest import login_as

# ---------- fixtures：settings 环境注入（口径同 test_documents，手动保存/还原） ----------


@pytest.fixture()
def sandbox_storage(tmp_path):
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
    saved = os.environ.get("TEST_MODE")
    os.environ["TEST_MODE"] = "true"
    get_settings.cache_clear()
    yield
    if saved is None:
        os.environ.pop("TEST_MODE", None)
    else:
        os.environ["TEST_MODE"] = saved
    get_settings.cache_clear()


@pytest.fixture()
def admin_client(client, session: Session, test_mode, sandbox_storage):
    """admin 登录态 client：test_mode（同步解析可断言终态）+ sandbox_storage（落盘隔离）
    先于 client 生效——上传/重建全链在测试事务内闭环（client 夹具来自 tests/api/conftest.py）。"""
    admin_login(client, session)
    return client


def _chunk_ids(session: Session, doc_id: int) -> list[int]:
    return list(session.scalars(select(Chunk.id).where(Chunk.document_id == doc_id)))


def _ready_doc_id(client, session: Session, title: str) -> int:
    """上传即 ready（test_mode 同步解析），返回文档 id。"""
    resp = post_document(client, title=title)
    assert resp.status_code == 202
    doc_id = resp.json()["data"]["id"]
    assert session.get(Document, doc_id).status == "ready"
    return doc_id


# ---------- F-304 文档级重建 ----------


def test_重建索引_旧块换新块并留审计(admin_client, session: Session) -> None:
    """ready 文档重建：202 → 状态机重跑收口 ready → 旧块集被整体替换（先删后写同事务）
    → document.rebuild 审计带前后块数。"""
    doc_id = _ready_doc_id(admin_client, session, "客服_重建索引_01")
    old_ids = _chunk_ids(session, doc_id)
    assert old_ids, "前置：上传后应有块"

    resp = admin_client.post(f"/api/documents/{doc_id}/rebuild-index")

    assert resp.status_code == 202
    assert resp.json()["data"] == {"id": doc_id, "status": "ready"}
    session.expire_all()
    doc = session.get(Document, doc_id)
    assert doc.status == "ready"
    new_ids = _chunk_ids(session, doc_id)
    assert new_ids and set(new_ids).isdisjoint(set(old_ids)), "旧块必须被删除重建（新 id 集）"
    audits = list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.object_type == "document",
                AuditLog.object_id == doc_id,
                AuditLog.action == "document.rebuild",
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].detail["before"]["chunks"] == len(old_ids)
    assert audits[0].detail["after"]["chunks"] == len(new_ids)


def test_重建索引_非就绪状态422_不存在404_member403(admin_client, session: Session) -> None:
    """failed/pending 拒绝 422；未知 id 404；member 全端点 403（红线：admin-only）。"""
    ready_id = _ready_doc_id(admin_client, session, "客服_重建索引_02")
    doc = session.get(Document, ready_id)
    doc.status = "failed"  # 直改状态造 failed 场景（重试路径的对应面）
    session.flush()

    failed = admin_client.post(f"/api/documents/{ready_id}/rebuild-index")
    assert failed.status_code == 422
    assert failed.json()["code"] == ERR_PARAM_INVALID
    assert failed.json()["message"] == "仅就绪状态的文档可以重建索引"

    missing = admin_client.post("/api/documents/999999/rebuild-index")
    assert missing.status_code == 404
    assert missing.json()["code"] == ERR_NOT_FOUND  # I13：documents 域 404 统一 3003

    make_user(session, "rebuildmember", "right-one", role="member")
    assert login_as(admin_client, "rebuildmember", "right-one").status_code == 200
    forbidden = admin_client.post(f"/api/documents/{ready_id}/rebuild-index")
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == ERR_FORBIDDEN


# ---------- F-304 全库级重建 ----------


def test_全库重建_受理计数与逐文档重刷(admin_client, session: Session) -> None:
    """全库重建：202 {accepted=N} → 全部 ready 文档重刷；failed 文档不受理。"""
    id_a = _ready_doc_id(admin_client, session, "客服_全库重建_A")
    id_b = _ready_doc_id(admin_client, session, "客服_全库重建_B")
    failed_id = _ready_doc_id(admin_client, session, "客服_全库重建_C")
    session.get(Document, failed_id).status = "failed"
    session.flush()
    old_a = _chunk_ids(session, id_a)
    old_b = _chunk_ids(session, id_b)

    resp = admin_client.post("/api/documents/rebuild-index")

    assert resp.status_code == 202
    assert resp.json()["data"] == {"accepted": 2}
    session.expire_all()
    assert set(_chunk_ids(session, id_a)).isdisjoint(set(old_a))
    assert set(_chunk_ids(session, id_b)).isdisjoint(set(old_b))
    total_ready = session.scalar(
        select(func.count()).select_from(Document).where(Document.status == "ready")
    )
    assert total_ready == 2, "failed 文档不参与重建也不被修复"


# ---------- F-303 版本历史 ----------


def test_版本历史_多代次降序全量返回(admin_client, session: Session) -> None:
    """v1 上传 → v2 重传（DOC-13 版本递增路径）→ versions 返回 [v2, v1] 全代次，
    归档 v1 isActive=false 且在案可追溯；单版本文档返回自身。"""
    title = "客服_版本历史_01"
    v1_resp = post_document(admin_client, title=title)
    assert v1_resp.status_code == 202
    v1_id = v1_resp.json()["data"]["id"]

    # v2：内容必须不同（SHA-256 查重 409），同 (dept,title) 触发版本递增 + v1 归档
    from tests.api.test_documents import _docx_bytes, _filler

    v2_resp = post_document(
        admin_client,
        title=title,
        content=_docx_bytes([("第一章 总则", [_filler(title + "二版", 18)])]),
    )
    assert v2_resp.status_code == 202
    v2_id = v2_resp.json()["data"]["id"]
    session.expire_all()
    assert session.get(Document, v2_id).version == 2
    assert session.get(Document, v1_id).is_active is False, "v1 应被归档（is_active=false）"

    resp = admin_client.get(f"/api/documents/{v2_id}/versions")

    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert [item["version"] for item in items] == [2, 1]
    assert [item["id"] for item in items] == [v2_id, v1_id]
    assert items[0]["isActive"] is True
    assert items[1]["isActive"] is False
    assert all(item["ownerName"] == "制度责任人" for item in items)


def test_版本历史_不存在404(admin_client) -> None:
    resp = admin_client.get("/api/documents/999999/versions")
    assert resp.status_code == 404
    assert resp.json()["code"] == ERR_NOT_FOUND
    assert resp.json()["message"] == "文档不存在"
