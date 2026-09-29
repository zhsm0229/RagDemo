r"""引用正文端点（T8 评审裁决 Rider，T9 前端按此消费）：GET /api/citations/content
?refType=doc|qa&refId=<id>，前端"查看原文"按引用定位取正文原文。

登录用户即可（ReadyUser 门，非 admin）：引用产自问答链路，读者即提问者。部门
隔离 0 容忍（红线①）：``dept_id IN (用户部门, 全员部门)``，与检索 F-502 同口径；
存活闸亦与检索面一致（块 is_active / 问答 enabled+is_active / 文档 is_active+ready
——检索给出的引用必在面内，文档撤回后不得再经引用读出正文）。隔离面外与不存在
一律 404/3003 统一文案"引用不存在或无权访问"（不区分两种情形，不给探测空间，同
会话归属口径）。qa 分支 answer/question 映射 content/title，page/titlePath 恒空
（问答对无页码与标题路径概念）；doc 分支联 documents 取规范标题。查询逻辑按任务
裁决内联在本薄控制器（单查询无跨模块副作用）；chunk ≤800 字、answer ≤10000 字
由库级 CHECK 兜底，响应体积天然安全。refType 枚举外 / refId 非正整数由 FastAPI
参数校验 422/1003。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DbSession, ReadyUser
from app.core.errors import ERR_INTERNAL, ERR_NOT_FOUND, ApiError
from app.db.models import Chunk, Department, Document, QaPair

router = APIRouter(prefix="/citations", tags=["citations"])

_ALL_DEPT_CODE = "all"  # 种子全员部门：F-502 隔离面恒含它
_MSG_NOT_FOUND = "引用不存在或无权访问"


@router.get("/content")
def citation_content(
    user: ReadyUser,
    db: DbSession,
    refType: Annotated[Literal["doc", "qa"], Query()],
    refId: Annotated[int, Query(gt=0)],
) -> dict:
    """引用正文（T8 裁决契约）：两分支各一条主键查询，越权与不存在统一 404/3003。"""
    dept_ids = [user.dept_id, _all_dept_id(db)]
    if refType == "doc":
        data = _doc_content(db, refId, dept_ids)
    else:
        data = _qa_content(db, refId, dept_ids)
    return {"code": 0, "message": "ok", "data": data}


def _all_dept_id(db: Session) -> int:
    """全员部门 id（F-502）：种子幂等存在，缺失属环境装配错误按 500 兜底拒绝。"""
    dept_id = db.scalar(select(Department.id).where(Department.code == _ALL_DEPT_CODE))
    if dept_id is None:
        raise ApiError(ERR_INTERNAL, "部门种子缺失，请联系管理员", 500)
    return dept_id


def _doc_content(db: Session, ref_id: int, dept_ids: list[int]) -> dict:
    """doc 分支：块 + 文档规范标题；存活闸与检索 _chunk_search_statement 同口径
    （块 is_active、文档 is_active+ready——撤回/未就绪文档不因引用泄漏正文）。"""
    row = db.execute(
        select(Chunk, Document.title)
        .join(Document, Chunk.document_id == Document.id)
        .where(
            Chunk.id == ref_id,
            Chunk.dept_id.in_(dept_ids),  # F-502 部门隔离：dept_id IN (用户部门, 全员)
            Chunk.is_active.is_(True),
            Document.is_active.is_(True),
            Document.status == "ready",
        )
    ).first()
    if row is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    chunk, title = row
    return {
        "refType": "doc",
        "refId": ref_id,
        "content": chunk.content,
        "title": title,
        "page": chunk.page_no,
        "titlePath": chunk.title_path,
    }


def _qa_content(db: Session, ref_id: int, dept_ids: list[int]) -> dict:
    """qa 分支：问答对按同隔离面查询；存活闸与检索 match_qa 同口径（QA-05/07）。"""
    qa = db.scalars(
        select(QaPair).where(
            QaPair.id == ref_id,
            QaPair.dept_id.in_(dept_ids),  # F-502 部门隔离：dept_id IN (用户部门, 全员)
            QaPair.status == "enabled",
            QaPair.is_active.is_(True),
        )
    ).first()
    if qa is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    return {
        "refType": "qa",
        "refId": ref_id,
        "content": qa.answer,
        "title": qa.question,
        "page": None,
        "titlePath": None,
    }
