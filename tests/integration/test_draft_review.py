r"""草稿审核状态机集成测试（agent-evolution #05，真 pg + StubEmbedder）。

覆盖：① 创建（draft+待审核登记、FileStorage 落盘读回、审计留痕）；② 双路径状态
迁移（通过→已通过且既有管道收口 ready 后可被检索命中 / 驳回→已驳回+原因留档）；
③ 红线读回断言——未审核草稿即使被恶意塞入 chunk 行也不被检索命中（检索侧
"仅 ready 且 is_active"过滤，agent-evolution §五：无审核不落地）；④ 非法迁移
（对 upload 语料审核 / 重复审核 / 空原因驳回 / 格式预检 / 精确查重）逐一 4xx。
"""

import hashlib
import os
import threading
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.clock import FakeClock, SystemClock
from app.core.config import Settings, get_settings
from app.core.errors import (
    ERR_DUPLICATE_DOC,
    ERR_PARAM_INVALID,
    ERR_UNSUPPORTED_FORMAT,
    ApiError,
)
from app.core.llm import StubEmbedder
from app.core.storage import LocalFileStorage
from app.db.models import AuditLog, Chunk, Department, Document, User
from app.services.draft_review import (
    REVIEW_APPROVED,
    REVIEW_PENDING,
    REVIEW_REJECTED,
    approve_draft,
    create_draft,
    reject_draft,
)
from app.services.ingest import IngestDeps, process_document
from app.services.retrieval import search_chunks

MD_MIME = "text/markdown"

_DRAFT_BODY = "\n".join(
    f"草稿验收第{i}条 内容说明{i}：适用于全体员工并遵照执行。" for i in range(12)
)


@pytest.fixture()
def sandbox_storage(tmp_path, monkeypatch):
    """STORAGE_DIR 指向 tmp_path 并清 get_settings 缓存（口径同 tests/api/test_document_delete）。"""
    saved = os.environ.get("STORAGE_DIR")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    if saved is None:
        monkeypatch.delenv("STORAGE_DIR", raising=False)
    else:
        monkeypatch.setenv("STORAGE_DIR", saved)
    get_settings.cache_clear()


def _admin(session: Session) -> User:
    return session.scalars(select(User).where(User.username == "admin")).one()


def _dept_id(session: Session) -> int:
    return session.scalars(select(Department.id).where(Department.code == "service")).one()


def _draft_meta(session: Session) -> dict:
    return {
        "dept_id": _dept_id(session),
        "title": "客服_草稿验收制度",
        "effective_date": date(2026, 10, 1),
        "owner_name": "制度责任人",
    }


def _make_draft(session: Session, storage: LocalFileStorage, unique: str = "") -> Document:
    return create_draft(
        session,
        storage,
        _admin(session),
        _draft_meta(session),
        _DRAFT_BODY.encode("utf-8"),
        f"草稿验收{unique}.md",
        MD_MIME,
    )


def _deps(storage: LocalFileStorage, embedder: StubEmbedder) -> IngestDeps:
    """与 test_ingest._make_deps 同口径：parser_settings 与落盘根对齐，嵌入走 Stub。"""
    return IngestDeps(
        parser_settings=Settings(jwt_secret="draft-test", storage_dir=str(storage.root_dir)),
        embedder=embedder,
        clock=FakeClock(),
        storage=storage,
        converter=None,
    )


# ---------------------------------------------------------------------------
# ① 创建：登记 + 落盘 + 审计读回
# ---------------------------------------------------------------------------


def test_create_draft_registers_pending_review(session, sandbox_storage):
    """创建草稿：source_type=draft、review_status=待审核、status=pending、is_active=False，
    文件经 FileStorage 落盘可读回，审计 document.draft_submit 留痕（写库必读回断言）。"""
    doc = _make_draft(session, LocalFileStorage(sandbox_storage))
    session.expire_all()  # 强制从库重读（事务内可见）
    row = session.get(Document, doc.id)
    assert row is not None
    assert row.source_type == "draft"
    assert row.review_status == REVIEW_PENDING
    assert row.status == "pending"
    assert row.is_active is False
    assert row.reject_reason is None
    stored = sandbox_storage / row.file_path
    assert stored.is_file()
    assert stored.read_bytes() == _DRAFT_BODY.encode("utf-8")
    audit = session.scalars(
        select(AuditLog).where(
            AuditLog.object_type == "document",
            AuditLog.object_id == row.id,
            AuditLog.action == "document.draft_submit",
        )
    ).all()
    assert len(audit) == 1


def test_create_draft_rejects_non_whitelist_format(session, sandbox_storage):
    """白名单外格式（.exe）创建草稿 → 422/1001：预检三闸与上传同源，无旁路。"""
    with pytest.raises(ApiError) as exc_info:
        create_draft(
            session,
            LocalFileStorage(sandbox_storage),
            _admin(session),
            _draft_meta(session),
            b"MZ binary",
            "木马.exe",
            "application/octet-stream",
        )
    assert exc_info.value.code == ERR_UNSUPPORTED_FORMAT
    assert exc_info.value.http_status == 422


def test_create_draft_rejects_exact_duplicate(session, sandbox_storage):
    """与库内已有文档完全重复的草稿 → 409/3001（F-1001 查重闸对草稿同样生效）。"""
    storage = LocalFileStorage(sandbox_storage)
    _make_draft(session, storage)
    with pytest.raises(ApiError) as exc_info:
        create_draft(
            session,
            storage,
            _admin(session),
            _draft_meta(session),
            _DRAFT_BODY.encode("utf-8"),
            "草稿验收.md",
            MD_MIME,
        )
    assert exc_info.value.code == ERR_DUPLICATE_DOC
    assert exc_info.value.http_status == 409


# ---------------------------------------------------------------------------
# ② 红线：未审核草稿不可被检索命中（含最坏情形：库内存在其 chunk 行）
# ---------------------------------------------------------------------------


def _insert_worst_case_chunk(session: Session, doc: Document, embedder: StubEmbedder) -> None:
    """最坏情形种子：假设草稿不知何故带上了 chunk 行（未来回归的防线），验证检索
    过滤（status='ready' AND is_active 强制 JOIN）兜底不命中——红线不依赖登记纪律。"""
    content = _DRAFT_BODY.splitlines()[0]
    session.add(
        Chunk(
            document_id=doc.id,
            dept_id=doc.dept_id,
            title_path=doc.title,
            content=content,
            content_chars=len(content),
            position=0,
            embedding=embedder.embed([content])[0],
        )
    )
    session.flush()


def _search(session: Session, embedder: StubEmbedder, dept_id: int, query: str) -> list:
    return search_chunks(session, embedder.embed([query])[0], [dept_id], top_k=5, ef_search=40)


def test_draft_not_searchable_until_approved(session, sandbox_storage):
    """红线读回断言：待审核草稿（即使带 chunk 行）检索 0 命中；审核通过 + 既有管道
    收口 ready 后同查询命中——隔离由检索过滤保证，入库走既有管道无旁路。"""
    storage = LocalFileStorage(sandbox_storage)
    embedder = StubEmbedder()
    doc = _make_draft(session, storage)
    _insert_worst_case_chunk(session, doc, embedder)

    hits = _search(session, embedder, doc.dept_id, "草稿验收")
    assert hits == [], "待审核草稿被检索命中：违反无审核不落地红线"

    approve_draft(session, FakeClock(), doc.id, actor_user_id=_admin(session).id)
    session.expire_all()
    assert session.get(Document, doc.id).review_status == REVIEW_APPROVED
    # 审核通过本身仍不可检索：入库要等既有管道收口（status 才变 ready）
    assert session.get(Document, doc.id).status == "pending"
    assert _search(session, embedder, doc.dept_id, "草稿验收") == []

    process_document(session, _deps(storage, embedder), doc.id)
    session.expire_all()
    row = session.get(Document, doc.id)
    assert row.status == "ready"
    assert row.is_active is True
    hits = _search(session, embedder, doc.dept_id, "草稿验收")
    assert len(hits) == 1
    assert hits[0].document_id == doc.id


def test_rejected_draft_never_enters_pipeline(session, sandbox_storage):
    """驳回路径：已驳回 + 原因留档 + 审计；即使再塞 chunk 行，检索仍 0 命中（永不入库）。"""
    storage = LocalFileStorage(sandbox_storage)
    embedder = StubEmbedder()
    doc = _make_draft(session, storage)
    clock = FakeClock()
    reject_draft(session, clock, doc.id, actor_user_id=_admin(session).id, reason="数据来源未核实")
    session.expire_all()
    row = session.get(Document, doc.id)
    assert row.review_status == REVIEW_REJECTED
    assert row.reject_reason == "数据来源未核实"
    assert row.status == "pending"
    audit = session.scalars(
        select(AuditLog).where(
            AuditLog.object_type == "document",
            AuditLog.object_id == row.id,
            AuditLog.action == "document.draft_reject",
        )
    ).all()
    assert len(audit) == 1
    _insert_worst_case_chunk(session, row, embedder)
    assert _search(session, embedder, row.dept_id, "草稿验收") == []


# ---------------------------------------------------------------------------
# ③ 非法迁移逐一 4xx
# ---------------------------------------------------------------------------


def test_approve_rejects_blank_reason(session, sandbox_storage):
    """空原因驳回 → 422/1003（驳回必须留档原因）。"""
    doc = _make_draft(session, LocalFileStorage(sandbox_storage))
    with pytest.raises(ApiError) as exc_info:
        reject_draft(session, FakeClock(), doc.id, actor_user_id=1, reason="   ")
    assert exc_info.value.code == ERR_PARAM_INVALID
    assert exc_info.value.http_status == 422


def test_review_rejects_non_draft_document(session, sandbox_storage):
    """对 upload 语料执行审核 → 422/1003（非草稿不可审核）。"""
    doc = Document(
        title="客服_普通上传",
        dept_id=_dept_id(session),
        original_filename="普通上传.md",
        file_path="seed/x.md",
        mime=MD_MIME,
        size_bytes=16,
        content_hash=hashlib.sha256(b"plain-upload").hexdigest(),
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        uploaded_by=_admin(session).id,
    )
    session.add(doc)
    session.flush()
    with pytest.raises(ApiError) as exc_info:
        approve_draft(session, FakeClock(), doc.id, actor_user_id=1)
    assert exc_info.value.code == ERR_PARAM_INVALID
    with pytest.raises(ApiError) as exc_info:
        reject_draft(session, FakeClock(), doc.id, actor_user_id=1, reason="驳回")
    assert exc_info.value.code == ERR_PARAM_INVALID


def test_double_approve_rejected(session, sandbox_storage):
    """重复审核（已通过后再通过）→ 422/1003：非法迁移 fail-fast，不给幂等假象。"""
    doc = _make_draft(session, LocalFileStorage(sandbox_storage))
    approve_draft(session, FakeClock(), doc.id, actor_user_id=1)
    with pytest.raises(ApiError) as exc_info:
        approve_draft(session, FakeClock(), doc.id, actor_user_id=1)
    assert exc_info.value.code == ERR_PARAM_INVALID
    with pytest.raises(ApiError) as exc_info:
        reject_draft(session, FakeClock(), doc.id, actor_user_id=1, reason="再驳")
    assert exc_info.value.code == ERR_PARAM_INVALID


# ---------------------------------------------------------------------------
# ④ B3 TOCTOU：条件 UPDATE——并发审核恰一次迁移、恰一次触发管道
# ---------------------------------------------------------------------------


def test_concurrent_approve_transition_exactly_once(pg):
    """双管理员并发审核（两线程独立连接，同刻起跑放大竞态）→ 恰一次迁移成功（另一方
    422/1003），document.draft_approve 审计恰一条。api 层按"审核成功才触发既有入库
    管道"编排（api/document_admin.approve_draft），审计唯一即管道恰触发一次的等价证据
    ——旧"前置检查 + UPDATE"两步形态下两个线程都能读到待审核而双双放行。"""
    digest = uuid4().hex  # uuid 内容防与库内残留行撞唯一约束
    with Session(pg) as setup:
        admin_id = setup.scalars(select(User.id).where(User.username == "admin")).one()
        dept_id = setup.scalars(select(Department.id).where(Department.code == "service")).one()
        doc = Document(
            title=f"客服_并发审核_{digest[:8]}",
            dept_id=dept_id,
            original_filename=f"{digest[:8]}.md",
            file_path=f"seed/{digest[:8]}.md",
            mime=MD_MIME,
            size_bytes=16,
            content_hash=digest,
            version=1,
            effective_date=date(2026, 10, 1),
            owner_name="制度责任人",
            status="pending",
            is_active=False,
            source_type="draft",
            review_status=REVIEW_PENDING,
            uploaded_by=admin_id,
        )
        setup.add(doc)
        setup.commit()
        doc_id = doc.id

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def _approve() -> None:
        with Session(pg) as worker:  # 独立连接：真行锁竞态，非同事务假并发
            barrier.wait()
            try:
                approve_draft(worker, SystemClock(), doc_id, actor_user_id=admin_id)
                outcomes.append("approved")
            except ApiError as exc:
                outcomes.append(f"{exc.http_status}/{exc.code}")

    threads = [threading.Thread(target=_approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert sorted(outcomes) == ["422/1003", "approved"]  # 恰一次成功，另一方 422
        with Session(pg) as verify:
            assert verify.get(Document, doc_id).review_status == REVIEW_APPROVED
            audits = verify.scalars(
                select(AuditLog).where(
                    AuditLog.object_type == "document",
                    AuditLog.object_id == doc_id,
                    AuditLog.action == "document.draft_approve",
                )
            ).all()
            assert len(audits) == 1  # 恰一次成功路径：审计唯一 → 管道恰触发一次
    finally:
        with Session(pg) as cleanup:  # 本测试独立连接提交的行自清，不污染其他用例
            cleanup.execute(
                delete(AuditLog).where(
                    AuditLog.object_type == "document", AuditLog.object_id == doc_id
                )
            )
            cleanup.execute(delete(Document).where(Document.id == doc_id))
            cleanup.commit()


def test_approve_with_stale_pending_read_rejected(pg):
    """确定性 TOCTOU（不依赖线程时序）：本会话先持"待审核"陈旧读数 → 对手在另一连接
    完成迁移并提交 → 本会话仍按陈旧读数发起迁移 → 条件 UPDATE 0 行 → 422/1003。
    旧"前置检查 + UPDATE"两步形态在此剧本下会按缓存读数放行并留下第二份审计
    （= api 层第二次触发管道）；条件 UPDATE 把判定与迁移合并为一条语句后不可能。"""
    digest = uuid4().hex
    with Session(pg) as setup:
        admin_id = setup.scalars(select(User.id).where(User.username == "admin")).one()
        dept_id = setup.scalars(select(Department.id).where(Department.code == "service")).one()
        doc = Document(
            title=f"客服_陈旧读数_{digest[:8]}",
            dept_id=dept_id,
            original_filename=f"{digest[:8]}.md",
            file_path=f"seed/{digest[:8]}.md",
            mime=MD_MIME,
            size_bytes=16,
            content_hash=digest,
            version=1,
            effective_date=date(2026, 10, 1),
            owner_name="制度责任人",
            status="pending",
            is_active=False,
            source_type="draft",
            review_status=REVIEW_PENDING,
            uploaded_by=admin_id,
        )
        setup.add(doc)
        setup.commit()
        doc_id = doc.id

    try:
        with Session(pg) as mine:
            stale_view = mine.get(Document, doc_id)  # 陈旧读数：对手提交前取得
            assert stale_view.review_status == REVIEW_PENDING
            with Session(pg) as rival:  # 对手抢先完成迁移并提交
                approve_draft(rival, SystemClock(), doc_id, actor_user_id=admin_id)
            with pytest.raises(ApiError) as exc_info:
                approve_draft(mine, SystemClock(), doc_id, actor_user_id=admin_id)
            assert (exc_info.value.http_status, exc_info.value.code) == (422, ERR_PARAM_INVALID)
        with Session(pg) as verify:  # 终态唯一：迁移恰一次，审计恰一条
            assert verify.get(Document, doc_id).review_status == REVIEW_APPROVED
            audits = verify.scalars(
                select(AuditLog).where(
                    AuditLog.object_type == "document",
                    AuditLog.object_id == doc_id,
                    AuditLog.action == "document.draft_approve",
                )
            ).all()
            assert len(audits) == 1
    finally:
        with Session(pg) as cleanup:
            cleanup.execute(
                delete(AuditLog).where(
                    AuditLog.object_type == "document", AuditLog.object_id == doc_id
                )
            )
            cleanup.execute(delete(Document).where(Document.id == doc_id))
            cleanup.commit()
