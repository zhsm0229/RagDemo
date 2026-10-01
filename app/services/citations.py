r"""引用正文服务（T8 裁决契约的查询侧）：GET /api/citations/content 两分支查询。

部门隔离 0 容忍（红线①）：``dept_id IN (用户部门, 全员部门)``，与检索 F-502 同
口径；存活闸与检索面一致（块 is_active / 问答 enabled+is_active / 文档 is_active+ready
——检索给出的引用必在面内，文档撤回后不得再经引用读出正文）。隔离面外与不存在
一律 ApiError 404/3003 统一文案（不区分两种情形，不给探测空间，同会话归属口径）。
chunk ≤800 字、answer ≤10000 字由库级 CHECK 兜底，响应体积天然安全。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Chunk, Document, QaPair
from app.services.departments import all_dept_id

MSG_CITATION_NOT_FOUND = "引用不存在或无权访问"


def doc_content(db: Session, ref_id: int, user_dept_id: int) -> tuple[Chunk, str]:
    """doc 分支：块 + 文档规范标题；存活闸与检索召回语句同口径
    （块 is_active、文档 is_active+ready——撤回/未就绪文档不因引用泄漏正文）。"""
    row = db.execute(
        select(Chunk, Document.title)
        .join(Document, Chunk.document_id == Document.id)
        .where(
            Chunk.id == ref_id,
            Chunk.dept_id.in_([user_dept_id, all_dept_id(db)]),  # F-502 部门隔离
            Chunk.is_active.is_(True),
            Document.is_active.is_(True),
            Document.status == "ready",
        )
    ).first()
    if row is None:
        raise ApiError(ERR_NOT_FOUND, MSG_CITATION_NOT_FOUND, 404)
    chunk, title = row
    return chunk, title


def qa_content(db: Session, ref_id: int, user_dept_id: int) -> QaPair:
    """qa 分支：问答对行，存活闸与检索 match_qa 同口径（QA-05/07）。"""
    qa = db.scalars(
        select(QaPair).where(
            QaPair.id == ref_id,
            QaPair.dept_id.in_([user_dept_id, all_dept_id(db)]),  # F-502 部门隔离
            QaPair.status == "enabled",
            QaPair.is_active.is_(True),
        )
    ).first()
    if qa is None:
        raise ApiError(ERR_NOT_FOUND, MSG_CITATION_NOT_FOUND, 404)
    return qa
