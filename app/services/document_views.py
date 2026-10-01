r"""文档查询视图服务（规范审查下沉项 + F-303）：管理端列表过滤分页、版本历史。

list_documents 自 api/documents.py 下沉（§1.6 api 禁直写查询）：dept/status/keyword
过滤 + 分页，created_at 降序（同刻 id 降序兜底）；keyword 的 ILIKE 通配符按字面匹配
（§22 转义口径与 qa_pairs 相同）。list_versions 为 F-303 补齐的半截：版本=同部门同
标题的 documents 多行（ingest 上传新版本递增 version、旧版归档 is_active=False 且
deleted_at 保持为空——归档≠软删），按 version 降序回全部代次。
"""

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Department, Document

_ESCAPES = (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_"))  # §22：ILIKE 通配符按字面匹配
_MSG_NOT_FOUND = "文档不存在"


def _escape_like(keyword: str) -> str:
    """§22：`%`/`_`/`\\` 逐一转义，关键字按字面匹配不展开为通配（同 qa_pairs 口径）。"""
    for raw, safe in _ESCAPES:
        keyword = keyword.replace(raw, safe)
    return keyword


def list_documents(
    db: Session,
    *,
    dept_id: int | None,
    status: str | None,
    keyword: str | None,
    page: int,
    page_size: int,
) -> tuple[list[tuple[Document, str]], int]:
    """管理端文档列表：过滤可选，返回 ((文档行, 部门 code) 当前页, 总数)。"""
    filters = []
    if dept_id is not None:
        filters.append(Document.dept_id == dept_id)
    if status is not None:
        filters.append(Document.status == status)
    if keyword:
        like = f"%{_escape_like(keyword)}%"
        filters.append(
            or_(
                Document.title.ilike(like, escape="\\"),
                Document.original_filename.ilike(like, escape="\\"),
            )
        )
    stmt = select(Document, Department.code).join(Department, Document.dept_id == Department.id)
    if filters:
        stmt = stmt.where(*filters)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(Document.created_at.desc(), Document.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def list_versions(db: Session, doc_id: int) -> list[Document]:
    """F-303 版本历史：同 (dept_id, title) 全部代次按 version 降序（含当前行）。

    寻址行不存在 → 404/3003；历史行不做存活过滤——归档版本（is_active=False）正是
    历史的主体，软删行（deleted_at 非空）属于删除语义同样在案可追溯。"""
    doc = db.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    return list(
        db.execute(
            select(Document)
            .where(Document.dept_id == doc.dept_id, Document.title == doc.title)
            .order_by(Document.version.desc(), Document.id.desc())
        )
        .scalars()
        .all()
    )
