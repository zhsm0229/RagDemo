r"""评测题库管理服务（02 票，评测集自动更新）：列表筛选 + 层级一键切换。

口径：
- 列表：created_at DESC, id DESC 兜底排序（行次序确定，分页可复现）；五维筛选
  （origin/tier/dept/qtype/keyword）全部参数绑定，keyword 用 contains(autoescape)
  ——用户可控的 LIKE 模式字符（%/_）按字面匹配，不充当通配符。部门表恒为种子
  三行，整表预载 code 映射（免 join 取 code，走实体 scalars 路径）。
- 层级切换：official↔extended 人工一键（自动题默认 extended，升格才进发版门禁
  口径）；审计 action=eval.tier、object_type=eval、detail={from,to}，与业务写入
  同事务提交（record_audit 不 commit 契约）。
- 非法 tier 值由 API 层 Literal DTO 拦截（422/1003）；未知题 404/3003（ERR_NOT_FOUND，
  IDOR 防线口径：admin 视角全域可及，不存在与无权同话术）。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Department, EvalQuestion
from app.services.audit import record_audit

_PAGE_SIZE_MAX: int = 100  # 列表响应上限（admin_views/qa 列表同口径）


def list_questions(
    session: Session,
    *,
    page: int,
    page_size: int,
    origin: str | None = None,
    tier: str | None = None,
    dept_code: str | None = None,
    qtype: str | None = None,
    keyword: str | None = None,
) -> tuple[int, list[tuple[EvalQuestion, str]]]:
    """题库分页列表：返回 (total, [(question, dept_code)])，时间倒序稳定排序。"""
    stmt = (
        select(EvalQuestion)
        .join(Department, Department.id == EvalQuestion.dept_id)
        .order_by(EvalQuestion.created_at.desc(), EvalQuestion.id.desc())
    )
    conditions = []
    if origin is not None:
        conditions.append(EvalQuestion.origin == origin)
    if tier is not None:
        conditions.append(EvalQuestion.tier == tier)
    if qtype is not None:
        conditions.append(EvalQuestion.qtype == qtype)
    if dept_code is not None:
        conditions.append(Department.code == dept_code)
    if keyword:
        conditions.append(EvalQuestion.question.contains(keyword, autoescape=True))
    if conditions:
        stmt = stmt.where(*conditions)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = session.scalar(count_stmt) or 0
    offset = (max(page, 1) - 1) * page_size
    questions = session.scalars(stmt.limit(page_size).offset(offset)).all()
    codes = {dept.id: dept.code for dept in session.scalars(select(Department)).all()}
    return int(total), [(question, codes[question.dept_id]) for question in questions]


def switch_tier(
    session: Session, clock: Clock, *, question_id: int, tier: str, actor_id: int
) -> EvalQuestion:
    """层级一键切换并留审计：eval.tier {from,to}；未知题 404/3003。"""
    question = session.get(EvalQuestion, question_id)
    if question is None:
        raise ApiError(ERR_NOT_FOUND, "评测题不存在", 404)
    previous = question.tier
    question.tier = tier
    record_audit(
        session,
        actor_id,
        "eval.tier",
        "eval",
        question_id,
        {"from": previous, "to": tier},
    )
    session.commit()
    return question
