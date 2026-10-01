r"""重建索引服务（F-304 补齐）：文档级/全库级重刷块集，standards 红线 8 的功能化入口。

复用入库状态机（process_document）：按**现行设置**重走 解析→红线→清洗→分块→嵌入，
收口单事务内"删本 doc 旧块 → 写新块 → ready"（spec 语境的"先建新块集、事务内切换"：
原子切换，任何一步失败整体回滚为 failed，不存在无索引窗口）。版本号不变——重建是
同内容重分块，不产生新版本（F-303 版本语义归上传路径）。审计双痕：状态机自身落
document.ready/document.failed（actor=uploaded_by，先例口径），本模块补
document.rebuild（actor=发起重建的管理员），前后块数可核对。

批量口径：仅 status='ready' 且未软删的文档可重建（failed 走 DOC-11 retry；
parsing/pending 属状态冲突 422）；全库重建逐文档独立事务，单文档失败落 failed 不
中断批次（与 retry 幂等语义一致）。缓存失效归 api 层（同 retry/confirm 口径）。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.db.models import Chunk, Document
from app.services.audit import record_audit
from app.services.ingest import process_document
from app.services.ingest_pipeline import IngestDeps

_MSG_NOT_FOUND = "文档不存在"
_MSG_NOT_READY = "仅就绪状态的文档可以重建索引"
_MSG_DELETED = "文档已删除，不能重建索引"


def _chunk_count(session: Session, doc_id: int) -> int:
    """块数读回（审计 before/after 用）：重建不必然改变块数（同参数重刷等量）。"""
    return (
        session.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == doc_id))
        or 0
    )


def ensure_rebuildable(doc: Document | None) -> Document:
    """受理校验（api 受理段与执行段共用，错误文案单一事实来源）：存在、未软删、ready。"""
    if doc is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_FOUND, 404)
    if doc.deleted_at is not None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_DELETED, 422)
    if doc.status != "ready":
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_READY, 422)
    return doc


def rebuild_document_index(
    session: Session, deps: IngestDeps, doc_id: int, actor_user_id: int
) -> Document:
    """文档级重建：校验可重建态 → 现行设置重跑状态机 → 补 rebuild 审计（含前后块数）。"""
    doc = ensure_rebuildable(session.get(Document, doc_id))
    before = _chunk_count(session, doc_id)
    process_document(session, deps, doc_id)  # 内部自提交：成功 ready / 失败 failed+中文原因
    after = _chunk_count(session, doc_id)
    record_audit(
        session,
        actor_user_id,
        "document.rebuild",
        "document",
        doc.id,
        {
            "before": {"chunks": before},
            "after": {"chunks": after, "status": doc.status, "isActive": doc.is_active},
        },
    )
    session.commit()
    return doc


def rebuild_ready_indexes(session: Session, deps: IngestDeps, actor_user_id: int) -> list[int]:
    """全库级重建：全部就绪且未软删文档逐个重建，返回受理的文档 id 列表。

    单文档分析失败由 process_document 落 failed 不抛出，批次不中断（重试幂等语义）；
    唯一会抛的 ApiError(404) 在逐个 get 前已被排除，实际不可达。"""
    doc_ids = list(
        session.scalars(
            select(Document.id).where(Document.status == "ready", Document.deleted_at.is_(None))
        )
    )
    for doc_id in doc_ids:
        rebuild_document_index(session, deps, doc_id, actor_user_id)
    return doc_ids
