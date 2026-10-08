r"""文档分型服务（metrics-balance 工单 01）：存量回填适配器 / 人工修正 / 批量回填。

回填适配器（chunks→detect 三参）在本层（spec §3.1）：拼接全文前剥离每块的
`title_path｜` 前缀、title_path 由存储列还原——与 ingest 期适配器（ParsedDoc→三参，
在 ingest_pipeline）口径逐字一致，否则 <1500 字信号在存量文档上漂移。
分型只影响查询期 top_n 档位与报告分组，不改可检索事实：修正与回填均不清问答缓存
（spec §3.4 落锤），编辑不限文档状态（傻瓜式，改错可改回，审计留痕）。
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.errors import ERR_NOT_FOUND, ERR_PARAM_INVALID, ApiError
from app.db.models import Chunk, Document
from app.rag.doc_profile import DocProfile, detect_from_text
from app.services.ingest_commit import audit_document

_PATH_PREFIX_SEP = "｜"  # chunker.OutChunk 的 title_path 前缀分隔符（同源，勿单方修改）
_MSG_NOT_FOUND = "文档不存在"


def chunks_detect_inputs(rows: Sequence[tuple[str, str]]) -> tuple[list[str], str]:
    """存量回填适配器：chunks 行（title_path, content）→（title_paths, 全文）。

    逐块剥离 `title_path｜` 前缀后按 position 序以换行拼接——重组结果与 ingest 期
    检测输入（非空节正文换行拼接）逐字一致（tests/unit/test_doc_profile.py 对拍）。
    """
    title_paths = [title_path for title_path, _content in rows]
    full_text = "\n".join(
        content.removeprefix(f"{title_path}{_PATH_PREFIX_SEP}") for title_path, content in rows
    )
    return title_paths, full_text


def set_doc_type(
    session: Session, clock: Clock, doc_id: int, doc_type: str, actor_user_id: int
) -> Document:
    """人工修正分型（PATCH /doc-type 后端）：仅此一字段，404/422 校验后落库留痕并提交。

    doc_type 必须是五档 code 之一（枚举外 422/1003）；编辑不限文档状态（§3.4）。"""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    valid_codes = {profile.value for profile in DocProfile}
    if doc_type not in valid_codes:
        raise ApiError(ERR_PARAM_INVALID, f"分型取值无效：{doc_type}", 422)
    before = doc.doc_type
    doc.doc_type = doc_type
    doc.updated_at = clock.now()
    audit_document(
        session,
        actor_user_id,
        "document.set_doctype",
        doc.id,
        {"before": {"docType": before}, "after": {"docType": doc_type}},
        clock=clock,
    )
    session.commit()
    return doc


def backfill_doc_types(session: Session, clock: Clock, actor_user_id: int) -> dict:
    """全量重算分型（POST /backfill-doc-type 后端）：对全部 is_active 且 ready 文档
    从 chunks 构造重检（ingest 进行中未 ready 的文档天然不参与），覆盖现有值（含
    手工修正，§3.4 覆盖语义落锤）。返回 {updated, byType}：updated=分型发生变化的
    文档数，byType=其中按新分型的计数分布。整批一条审计 document.backfill_doctype。"""
    docs = list(
        session.scalars(
            select(Document).where(Document.is_active.is_(True), Document.status == "ready")
        ).all()
    )
    updated = 0
    by_type: dict[str, int] = {}
    for doc in docs:
        rows = session.execute(
            select(Chunk.title_path, Chunk.content)
            .where(Chunk.document_id == doc.id)
            .order_by(Chunk.position)
        ).all()
        title_paths, full_text = chunks_detect_inputs(rows)
        profile = detect_from_text(doc.title, title_paths, full_text)
        if profile.value != doc.doc_type:
            doc.doc_type = profile.value
            doc.updated_at = clock.now()
            updated += 1
            by_type[profile.value] = by_type.get(profile.value, 0) + 1
    audit_document(
        session,
        actor_user_id,
        "document.backfill_doctype",
        None,  # 批量动作无单文档对象；范围与结果在 detail（updated/byType）留痕
        {"after": {"updated": updated, "byType": by_type}},
        clock=clock,
    )
    session.commit()
    return {"updated": updated, "byType": by_type}
