r"""评测题库管理路由（02 票，评测集自动更新）：列表筛选 + 层级一键切换。

独立于 admin_eval.py（该文件已近 300 行红线，且导入/模板/跑分与本路由同域不同
职责）：GET /admin/eval-questions（题库分页列表，五维筛选）与 PATCH
/admin/eval-questions/{id}/tier（official↔extended 人工一键切换，审计 eval.tier）。
非法 tier 由 Literal DTO 拦截（Pydantic → 422/1003 统一处理器）；member 403 走
AdminUser 依赖链。响应键 camelCase（统一响应体口径）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import AdminUser, DbSession
from app.core.clock import get_clock
from app.services import eval_bank

router = APIRouter(prefix="/admin/eval-questions", tags=["admin-eval"])

_Tier = Literal["official", "extended"]


class TierPatchRequest(BaseModel):
    """层级切换请求体：非法值 → 422/1003（RequestValidationError 统一处理）。"""

    tier: _Tier


def _page(page: Annotated[int, Query(ge=1)] = 1) -> int:
    return page


def _page_size(pageSize: Annotated[int, Query(ge=1, le=100)] = 20) -> int:
    return pageSize


@router.get("")
def list_questions(
    user: AdminUser,
    db: DbSession,
    page: Annotated[int, Depends(_page)],
    pageSize: Annotated[int, Depends(_page_size)],
    origin: Annotated[str | None, Query()] = None,
    tier: Annotated[str | None, Query()] = None,
    dept: Annotated[str | None, Query()] = None,
    qtype: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
) -> dict:
    """题库分页列表（admin）：origin/tier/dept/qtype/keyword 五维筛选。"""
    total, rows = eval_bank.list_questions(
        db,
        page=page,
        page_size=pageSize,
        origin=origin,
        tier=tier,
        dept_code=dept,
        qtype=qtype,
        keyword=keyword,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [
                {
                    "id": question.id,
                    "deptCode": dept_code,
                    "question": question.question,
                    "expectedAnswer": question.expected_answer,
                    "sourceRef": question.source_ref,
                    "sourceDocId": question.source_doc_id,
                    "qtype": question.qtype,
                    "origin": question.origin,
                    "tier": question.tier,
                    "isActive": question.is_active,
                    "createdAt": question.created_at.isoformat(),
                }
                for question, dept_code in rows
            ],
            "total": total,
            "page": page,
            "pageSize": pageSize,
        },
    }


@router.patch("/{question_id}/tier")
def switch_tier(
    user: AdminUser,
    db: DbSession,
    question_id: int,
    payload: TierPatchRequest,
) -> dict:
    """层级一键切换（official↔extended）：升格才进发版门禁口径，写审计 eval.tier。"""
    question = eval_bank.switch_tier(
        db,
        get_clock(),
        question_id=question_id,
        tier=payload.tier,
        actor_id=user.id,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {"id": question.id, "tier": question.tier},
    }
