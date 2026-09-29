r"""提问记录保留策略（F-803/§4.6）：两段式清理——归档停用 → 冷却期 → 物理清理。

§4.6 红线：数据清理两段式（先停用（软删）→ 冷却 → 物理清理），messages 是全库唯一
物理删除点；audit_logs 只增不删，被物理清理的记录靠归档时的批量审计承载可追溯性
（detail 记 archived 条数与 cutoff 边界），purge 阶段不再逐条审计——行已删，逐条
留痕只会制造悬空引用。

分批物理删除：messages 是按月分区父表，`DELETE WHERE created_at < X` 一次命中多
分区（长事务 + 锁面大），故按 `DELETE ... WHERE id IN (SELECT id ... LIMIT 1000)`
风格循环：每批 ≤1000 行、批间独立提交，删净即止；子查询与外层条件全程参数绑定。

审计 actor：系统清扫动作沿 W1 到期下线 sweep（expire_due_documents）的先例——不虚构
actor，用 seed 幂等保证的真实用户行（users.admin）留痕；专用系统行是登记过的演进项，
落地前以 admin 为锚（seed.py 只建 admin 一个用户）。
"""

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.models import Message, User
from app.services.audit import record_audit

_ARCHIVE_ACTION = "message.archive"
_PURGE_BATCH_SIZE = 1000  # 单批物理删除上限（防长事务，§4.6）
_SYSTEM_ACTOR = "admin"  # seed 幂等保证的系统用户（见模块 docstring）


def soft_archive_old_messages(db: Session, clock: Clock, retention_days: int) -> int:
    """第一阶段：created_at 早于 now-retention_days 且仍生效的提问 → is_active=false，返回条数。

    幂等：只翻 is_active（不物理删），已归档行天然跳过；空跑不写审计（对齐 W1 清扫
    「无到期 → 无审计」口径）。中断占位行（is_active=false）不受本函数影响，但同样
    按 created_at 被第二阶段管辖。审计与归档同事务提交（record_audit 契约）。
    """
    cutoff = clock.now() - timedelta(days=retention_days)
    dues = list(
        db.scalars(select(Message).where(Message.created_at < cutoff, Message.is_active.is_(True)))
    )
    if not dues:
        return 0
    now = clock.now()
    for msg in dues:
        msg.is_active = False
        msg.updated_at = now
    record_audit(
        db,
        _system_actor_id(db),
        _ARCHIVE_ACTION,
        "message",
        None,  # 批量动作无单对象 id：范围与条数记在 detail.cutoff / detail.archived
        {"archived": len(dues), "cutoff": cutoff.isoformat()},
        clock=clock,
    )
    db.commit()
    return len(dues)


def purge_expired_messages(
    db: Session, clock: Clock, retention_days: int, cooldown_days: int
) -> int:
    """第二阶段：created_at 早于 now-(retention+cooldown) 的消息物理删除（越过冷却期），返回条数。

    只删 messages（§4.6 唯一物理删除点）；不带 is_active 过滤——中断占位行同样按
    created_at 管辖；分批 ≤1000 行/批、批间提交（跨分区 DELETE 防长事务，见模块
    docstring）；条数恰为整批时多跑一轮确认删净。
    """
    cutoff = clock.now() - timedelta(days=retention_days + cooldown_days)
    total = 0
    while True:
        deleted = db.execute(
            delete(Message).where(
                Message.id.in_(
                    select(Message.id).where(Message.created_at < cutoff).limit(_PURGE_BATCH_SIZE)
                )
            )
        ).rowcount
        db.commit()  # 批间独立提交：每批都是短事务
        total += deleted or 0
        if (deleted or 0) < _PURGE_BATCH_SIZE:
            return total


def _system_actor_id(db: Session) -> int:
    """系统清扫的审计 actor：seed 保证的 admin（W1 系统动作「真实用户留痕」先例）。"""
    actor = db.scalar(select(User.id).where(User.username == _SYSTEM_ACTOR))
    if actor is None:
        raise LookupError("审计 actor 缺失：users 表无 admin（seed 未执行或被删）")
    return actor
