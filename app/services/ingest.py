r"""入库状态机（F1 编排核心）：同步段 create_document、异步段 process_document、confirm/expire。

同步段（上传请求内毫秒级）：check_sync 白名单 → SHA-256 精确查重（409 指向已有 id，
软删不占 hash 可重传）→ 落盘 → 登记 parsing 且 is_active=False（不占"单一生效版本"
部分索引）→ 审计。异步段编排（任何一步失败 → failed+中文原因，绝不落半成品块）：
analysis（解析/红线/清洗/乱码/敏感/simhash/分块/embed，见 ingest_pipeline.py）→
单事务写 chunks（先删本 doc 旧块，重试幂等）+ 同标题旧版归档 → ready；敏感/近似
命中 → is_active=False 待确认。confirm 激活；expire 到期下线（F-1004）。
"""

from pathlib import PureWindowsPath

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.errors import (
    ERR_DUPLICATE_DOC,
    ERR_PARAM_INVALID,
    ERR_UNSUPPORTED_FORMAT,
    ApiError,
)
from app.core.llm import UpstreamError
from app.core.storage import FileStorage
from app.db.models import Chunk, Document, User
from app.rag.precheck import check_sync as check_sync_gate
from app.services.audit import record_audit
from app.services.ingest_pipeline import (
    IngestDeps,
    PipelineOutcome,
    analyze_document,
    to_signed64,
)

_MSG_NOT_FOUND = "文档不存在"
_MSG_EMBED_FAIL = "向量化服务调用失败，请稍后重试"
_MSG_GENERIC_FAIL = "文档处理失败，请稍后重试"
_META_REQUIRED: tuple[str, ...] = ("dept_id", "title", "owner_name", "effective_date")
_SYNC_REJECTS: dict[str, str] = {
    "ext_not_allowed": "仅支持 pdf/docx 格式",
    "mime_not_allowed": "文件 MIME 类型不在白名单内",
    "too_large": "文件大小超过 50MB 上限",
    "magic_mismatch": "文件内容与扩展名不符，请勿伪造格式",
}


def create_document(
    session: Session,
    storage: FileStorage,
    uploader: User,
    meta: dict,
    content: bytes,
    filename: str,
    mime: str,
) -> Document:
    """同步段：元数据必填 → 白名单 → 查重 → 落盘 → 登记 parsing → 审计并提交。"""
    missing = [key for key in _META_REQUIRED if not meta.get(key)]
    if missing:
        raise ApiError(ERR_PARAM_INVALID, f"元数据缺失：{'、'.join(missing)}", 422)
    gate = check_sync_gate(filename, mime, content)
    if not gate.ok:
        raise _sync_rejected(gate.reason)
    _reject_duplicate(session, gate.sha256)
    stored = storage.save(content, PureWindowsPath(filename).suffix.lower())
    doc = Document(
        title=meta["title"],
        dept_id=meta["dept_id"],
        original_filename=filename,
        file_path=stored.rel_path,
        mime=mime,
        size_bytes=stored.size,
        content_hash=gate.sha256,
        version=_next_version(session, meta["dept_id"], meta["title"]),
        effective_date=meta["effective_date"],
        expiry_date=meta.get("expiry_date"),
        owner_name=meta["owner_name"],
        status="parsing",
        is_active=False,  # parsing 期不生效：不占 uq_doc_active，检索不露半成品
        uploaded_by=uploader.id,
    )
    session.add(doc)
    session.flush()  # 先取 id 供审计外键
    detail = {"after": {"title": doc.title, "filename": filename, "sha256": gate.sha256}}
    _audit(session, uploader.id, "document.upload", doc.id, detail)
    session.commit()
    return doc


def process_document(session: Session, deps: IngestDeps, doc_id: int) -> None:
    """异步段编排：先回 parsing（DOC-11 重试语义），失败转 failed+中文原因，成功收口 ready。"""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_FOUND, 404)
    doc.status = "parsing"
    session.flush()
    try:
        outcome = analyze_document(session, deps, doc)
    except Exception as exc:  # noqa: BLE001 — 状态机兜底：任何一步失败都必须落到 failed 态
        _commit_failed(session, deps.clock, doc, exc)
        return
    _commit_ready(session, deps, doc, outcome)


def confirm_document(session: Session, doc_id: int) -> Document:
    """待确认激活：管理员确认敏感/近似文档 → is_active=True，审计并提交。"""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_FOUND, 404)
    doc.is_active = True
    _audit(
        session,
        doc.uploaded_by,
        "document.confirm",
        doc.id,
        {"before": {"isActive": False}, "after": {"isActive": True}},
    )
    session.commit()
    return doc


def expire_due_documents(session: Session, clock: Clock) -> int:
    """F-1004 到期下线：expiry_date < 时钟当日的生效未删文档逐个下线并审计，返回条数。"""
    today = clock.now().date()
    dues = list(
        session.scalars(
            select(Document).where(
                Document.expiry_date.is_not(None),
                Document.expiry_date < today,
                Document.is_active.is_(True),
                Document.deleted_at.is_(None),
            )
        )
    )
    for doc in dues:
        doc.is_active = False
        doc.updated_at = clock.now()
        _audit(
            session,
            doc.uploaded_by,
            "document.expire",
            doc.id,
            {"after": {"isActive": False}, "expiryDate": doc.expiry_date.isoformat()},
            clock=clock,
        )
    session.commit()
    return len(dues)


def _audit(
    session: Session,
    actor: int,
    action: str,
    doc_id: int,
    detail: dict,
    clock: Clock | None = None,
) -> None:
    """document 对象审计留痕薄封装（object_type 恒为 document，commit 归调用方）。"""
    record_audit(session, actor, action, "document", doc_id, detail, clock=clock)


def _sync_rejected(reason: str | None) -> ApiError:
    """check_sync 失败码 → ApiError：超限走 1003，其余格式类走 1001，一律 422。"""
    code = ERR_PARAM_INVALID if reason == "too_large" else ERR_UNSUPPORTED_FORMAT
    return ApiError(code, _SYNC_REJECTS.get(reason or "", "文件未通过同步预检"), 422)


def _reject_duplicate(session: Session, sha256: str) -> None:
    """F-1001 精确查重：命中未软删的同 hash 文档即 409，消息指向已有 id（并发由唯一索引兜底）。"""
    existing = session.scalars(
        select(Document).where(Document.content_hash == sha256, Document.deleted_at.is_(None))
    ).first()
    if existing is not None:
        raise ApiError(
            ERR_DUPLICATE_DOC,
            f"文件与已有文档完全重复（已有文档 ID {existing.id}），如需更新请上传新版本",
            409,
        )


def _next_version(session: Session, dept_id: int, title: str) -> int:
    """同部门同标题最大版本号 +1：不过滤软删——uq_doc_title_dept_ver 对全部行生效，
    软删行仍占其版本号，新行必须越过它（部分索引只豁免 content_hash 查重）。"""
    current = session.scalar(
        select(func.max(Document.version)).where(
            Document.dept_id == dept_id, Document.title == title
        )
    )
    return (current or 0) + 1


def _commit_ready(
    session: Session, deps: IngestDeps, doc: Document, outcome: PipelineOutcome
) -> None:
    """单事务收口：删本 doc 旧块（重试幂等）→ 写新块 → 旧版归档 → ready/待确认 → 审计 → 提交。"""
    session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    for chunk, vector in zip(outcome.chunks, outcome.embeddings, strict=True):
        session.add(
            Chunk(
                document_id=doc.id,
                dept_id=doc.dept_id,
                title_path=chunk.title_path,
                content=chunk.content,
                content_chars=len(chunk.content),
                position=chunk.position,
                embedding=vector,
            )
        )
    _archive_old_versions(session, deps, doc)
    doc.status = "ready"
    doc.parse_engine = outcome.engine
    doc.parse_error = outcome.degrade_note
    doc.simhash = to_signed64(outcome.simhash)
    doc.updated_at = deps.clock.now()
    doc.is_active = not (outcome.sensitive_hits or outcome.near_dup)  # 命中→待确认
    detail = {
        "after": {
            "status": "ready",
            "parseEngine": outcome.engine,
            "degraded": outcome.degrade_note is not None,
            "sensitiveHits": outcome.sensitive_hits,
            "nearDup": outcome.near_dup,
            "isActive": doc.is_active,
        }
    }
    _audit(session, doc.uploaded_by, "document.ready", doc.id, detail, clock=deps.clock)
    session.commit()


def _archive_old_versions(session: Session, deps: IngestDeps, doc: Document) -> None:
    """同部门同标题旧生效版本归档：is_active=False 且 deleted_at 保持为空（归档≠软删）。"""
    olds = list(
        session.scalars(
            select(Document).where(
                Document.dept_id == doc.dept_id,
                Document.title == doc.title,
                Document.id != doc.id,
                Document.is_active.is_(True),
            )
        )
    )
    for old in olds:
        old.is_active = False
        old.updated_at = deps.clock.now()


def _commit_failed(session: Session, clock: Clock, doc: Document, exc: Exception) -> None:
    """失败收口：failed+中文原因（覆写 parsing 中间态）→ 审计 → 提交；绝不触碰 chunks
    （DOC-26 旧块集保留，重试成功后整体替换）。不做 rollback：分析段为只读（不变量），
    失败时未提交写只有 parsing 状态、直接被 failed 覆写；且 rollback 会拆掉会话与
    外部事务（测试夹具 join-external）的关联，导致后续读不到本事务的行。"""
    doc.status = "failed"
    doc.parse_error = _failure_reason(exc)
    doc.updated_at = clock.now()
    _audit(
        session,
        doc.uploaded_by,
        "document.failed",
        doc.id,
        {"after": {"status": "failed", "reason": doc.parse_error}},
        clock=clock,
    )
    session.commit()


def _failure_reason(exc: Exception) -> str:
    """异常 → 中文失败原因：业务错误透传，上游故障与未知异常给通用文案（脱敏 §16.4）。"""
    if isinstance(exc, ApiError):
        return exc.message
    if isinstance(exc, UpstreamError):
        return _MSG_EMBED_FAIL
    return _MSG_GENERIC_FAIL
