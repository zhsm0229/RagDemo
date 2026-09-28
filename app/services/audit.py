"""审计服务：record_audit 向 audit_logs 追加一条操作留痕（只增不改，无 updated_at）。

约定：本函数只 add+flush，**commit 由调用方控制**——调用方把审计写入与业务写入
放在同一事务里（要么一起落库，要么一起回滚，保证留痕与事实一致）；created_at
统一经注入的 Clock 取值（生产缺省 SystemClock，测试可换 FakeClock 拨动）。
"""

from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.db.models import AuditLog


def record_audit(
    session: Session,
    actor_user_id: int,
    action: str,
    object_type: str,
    object_id: int | None,
    detail: dict,
    ip: str | None = None,
    clock: Clock | None = None,
) -> None:
    """追加一条审计记录并 flush 落入当前事务；不 commit（事务边界归调用方）。

    created_at 属复合主键 (id, created_at)（按月分区前置），必须显式赋值：
    取 clock.now()（None 时缺省 get_clock() 的 SystemClock）。
    """
    effective_clock = clock if clock is not None else get_clock()
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail=detail,
            ip=ip,
            created_at=effective_clock.now(),
        )
    )
    session.flush()  # INSERT 进当前事务：调用方 commit 前同事务 SELECT 可见
