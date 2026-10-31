r"""难例池管理服务（03 票，评测集自动更新）：种子列表 + 置忽略终态。

口径：
- 列表：status/dept_code 可选筛选，created_at DESC, id DESC 稳定排序（与题库列表
  同款排序纪律）；部门表恒为种子三行整表预载 code 映射（免 join 取 code）。
- 置忽略：仅 ignored 一个终态开放人工 API——pending/synthesized/no_ground 的流转
  由合成任务体驱动（03 票），人工只补"这条种子没价值"的否决动作；审计
  action=hardcase.ignore、object_type=hardcase_seed、detail={from}，与业务写入
  同事务提交（record_audit 不 commit 契约）。未知种子 404/3003。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Department, HardcaseSeed
from app.services.audit import record_audit

_PAGE_SIZE_MAX: int = 100  # 列表响应上限（题库/提问记录列表同口径）


def list_seeds(
    session: Session,
    *,
    page: int,
    page_size: int,
    status: str | None = None,
    dept_code: str | None = None,
) -> tuple[int, list[tuple[HardcaseSeed, str]]]:
    """难例池分页列表：返回 (total, [(seed, dept_code)])，时间倒序稳定排序。"""
    stmt = (
        select(HardcaseSeed)
        .join(Department, Department.id == HardcaseSeed.dept_id)
        .order_by(HardcaseSeed.created_at.desc(), HardcaseSeed.id.desc())
    )
    conditions = []
    if status is not None:
        conditions.append(HardcaseSeed.status == status)
    if dept_code is not None:
        conditions.append(Department.code == dept_code)
    if conditions:
        stmt = stmt.where(*conditions)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = session.scalar(count_stmt) or 0
    offset = (max(page, 1) - 1) * min(page_size, _PAGE_SIZE_MAX)
    seeds = session.scalars(stmt.limit(page_size).offset(offset)).all()
    codes = {dept.id: dept.code for dept in session.scalars(select(Department)).all()}
    return int(total), [(seed, codes[seed.dept_id]) for seed in seeds]


def ignore_seed(session: Session, clock: Clock, *, seed_id: int, actor_id: int) -> HardcaseSeed:
    """置 ignored 终态并留审计：hardcase.ignore {from}；未知种子 404/3003。"""
    seed = session.get(HardcaseSeed, seed_id)
    if seed is None:
        raise ApiError(ERR_NOT_FOUND, "难例种子不存在", 404)
    previous = seed.status
    seed.status = "ignored"
    seed.updated_at = clock.now()
    record_audit(
        session,
        actor_id,
        "hardcase.ignore",
        "hardcase_seed",
        seed_id,
        {"from": previous},
    )
    session.commit()
    return seed
