r"""入库状态机收口段（自 ingest.py 拆出，行数红线 §1.4）：ready/failed 终态落库与失败归因。

_commit_ready 单事务：删本 doc 旧块（重试幂等）→ 写新块 → 图片资产落库 → 旧版归档 →
ready/待确认 → 审计 → 提交，绝不落半成品块；_commit_failed 失败收口：failed+中文原因
+技术错误摘要双轨，绝不触碰 chunks（DOC-26 旧块集保留，重试成功后整体替换）。
审计留痕薄封装 audit_document（object_type 恒为 document，commit 归调用方或收口函数）。
"""

import traceback

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.errors import ApiError
from app.core.llm import UpstreamError
from app.db.models import Chunk, Document, DocumentImage
from app.services.audit import record_audit
from app.services.ingest_pipeline import IngestDeps, PipelineOutcome, to_signed64

_MSG_EMBED_FAIL = "向量化服务调用失败，请稍后重试"
_MSG_GENERIC_FAIL = "文档处理失败，请稍后重试"


def audit_document(
    session: Session,
    actor: int,
    action: str,
    doc_id: int,
    detail: dict,
    clock: Clock | None = None,
) -> None:
    """document 对象审计留痕薄封装（object_type 恒为 document，commit 归调用方）。

    状态机各段（ingest.py 与本模块）共用，避免两处各包一层（单一事实来源）。"""
    record_audit(session, actor, action, "document", doc_id, detail, clock=clock)


def commit_ready(
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
                page_no=chunk.page,  # 引用定位（task-6b）：pdf 块所在页，无页码来源 NULL
                embedding=vector,
            )
        )
    _add_image_assets(session, doc.id, outcome.image_assets)  # F-1017：图片与 chunks 同事务
    _archive_old_versions(session, deps, doc)
    doc.status = "ready"
    doc.parse_engine = outcome.engine
    doc.parse_error = outcome.degrade_note
    doc.parse_stage = None
    doc.parse_progress = None
    doc.parse_error_detail = None
    doc.simhash = to_signed64(outcome.simhash)
    doc.doc_type = outcome.doc_type  # 分型随收口落库（metrics-balance §3.4；重试=重检覆盖）
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
            "imageCount": outcome.image_count,
            "ocrChars": outcome.ocr_chars,
        }
    }
    audit_document(session, doc.uploaded_by, "document.ready", doc.id, detail, clock=deps.clock)
    session.commit()


def _add_image_assets(session: Session, doc_id: int, image_assets: list) -> None:
    """图片资产落库（F-1017）：与 chunks 同事务写 document_images（引用"图片N："回显用）。"""
    for asset in image_assets:
        session.add(
            DocumentImage(
                document_id=doc_id,
                image_index=asset.index,
                file_path=asset.rel_path,
                mime=asset.mime,
                page_no=asset.page,
                ocr_chars=asset.ocr_chars,
            )
        )


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


def commit_failed(session: Session, clock: Clock, doc: Document, exc: Exception) -> None:
    """失败收口：failed+中文原因（覆写 parsing 中间态）+技术错误摘要双轨 → 审计 → 提交；
    绝不触碰 chunks（DOC-26 旧块集保留，重试成功后整体替换）。不做 rollback：分析段为
    只读（进度落库点已各自 commit，失败时无未提交写）+ failed 直接覆写；且 rollback 会
    拆掉会话与外部事务（测试夹具 join-external）的关联，导致后续读不到本事务的行。"""
    doc.status = "failed"
    doc.parse_error = _failure_reason(exc)
    doc.parse_error_detail = _failure_detail(exc)
    doc.parse_stage = None
    doc.parse_progress = None
    doc.updated_at = clock.now()
    audit_document(
        session,
        doc.uploaded_by,
        "document.failed",
        doc.id,
        {
            "after": {
                "status": "failed",
                "reason": doc.parse_error,
                "detail": (doc.parse_error_detail or "")[:200],
            }
        },
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


def _failure_detail(exc: Exception) -> str:
    """技术错误摘要（管理端定位用，parse_error_detail 列）：保留 UpstreamError 原文
    （如 "tei /embed 传输失败: ReadTimeout"——脱敏文案丢失的关键定位信息），未知异常
    给 类型+消息+末栈帧；截 500 字防日志型异常刷库。"""
    if isinstance(exc, UpstreamError):
        return str(exc)[:500]
    frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
    origin = f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}" if frame else "unknown"
    return f"{type(exc).__name__}: {exc} ({origin})"[:500]
