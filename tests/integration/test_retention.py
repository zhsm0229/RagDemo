r"""提问记录保留策略集成测试（F-803/§4.6）：软删归档 → 冷却 → 物理清理（真 pg + FakeClock）。

brief 用例 ①-⑤：
① 归档边界：3 条消息（now / 2h 前 / 25h 前）retention=1 天 → 仅 25h 前（早于
   09-30 08:00 cutoff）is_active=false，其余保持生效；
③ 归档审计读回：恰一条 message.archive，actor=seed 的 admin，detail={archived,
   cutoff=ISO 时刻}，created_at 走注入时钟；空跑不追加审计；
② 两段式闭环：①之后前拨 31h，purge（retention=1+cooldown=1，cutoff=09-30 15:00）
   → 25h 前消息物理消失、now / 2h 前消息仍在（cutoff 恰夹在两者之间）；
④ 分批物理删除：1201 条过期消息 → 恰两批（1000+201，单批 ≤1000）删净，返回 1201、
   表内 0 行；
⑤ scheduler 挂载断言在 tests/integration/test_scheduler.py（任务名存在 + cron 表达式）。

短 TTL 口径：保留/冷却天数粒度下，同时满足 ①「仅 25h 前归档」与 ②「31h 后仅 25h 前
被清理」的最小取值是 retention=1、cooldown=1（边界推算见各用例注释）；purge 另用
retention=0/cooldown=0 验证「到期即清」。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Delete, Executable, func, select
from sqlalchemy.engine import Result
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.db.models import AuditLog, Conversation, Message, User
from app.services.retention import purge_expired_messages, soft_archive_old_messages

_T0 = "2026-10-01T08:00:00+00:00"  # FakeClock 起点：与 test_scheduler 同月（2026_10 分区在）


def _admin_id(session: Session) -> int:
    return session.scalar(select(User.id).where(User.username == "admin"))


def _dept_id(session: Session) -> int:
    return session.scalar(select(User.dept_id).where(User.username == "admin"))


def _make_conversation(session: Session) -> Conversation:
    """直插会话行：消息外键（conversation_id/user_id/dept_id）的挂靠点。"""
    conv = Conversation(
        user_id=_admin_id(session),
        dept_id=_dept_id(session),
        title="保留策略测试会话",
    )
    session.add(conv)
    session.flush()
    return conv


def _make_message(session: Session, conv: Conversation, *, created_at: datetime) -> Message:
    """直插消息行（绕开问答链路）：保留策略只关心 created_at / is_active。"""
    msg = Message(
        conversation_id=conv.id,
        user_id=conv.user_id,
        dept_id=conv.dept_id,
        role="user",
        content="知识库里报销标准是什么",
        is_active=True,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(msg)
    session.flush()
    return msg


# ---------- ① 归档边界：仅超保留期的生效消息被停用 ----------


def test_soft_archive_expires_only_messages_older_than_retention(session: Session) -> None:
    """① 3 条消息（now / 2h 前 / 25h 前），retention=1 天（cutoff=09-30 08:00）：
    仅 25h 前（09-30 07:00）is_active=false，2h 前与 now 在界内保持生效，返回 1。"""
    clock = FakeClock(_T0)
    conv = _make_conversation(session)
    m_now = _make_message(session, conv, created_at=clock.now())
    m_2h = _make_message(session, conv, created_at=clock.now() - timedelta(hours=2))
    m_25h = _make_message(session, conv, created_at=clock.now() - timedelta(hours=25))

    archived = soft_archive_old_messages(session, clock, retention_days=1)

    assert archived == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.id == m_25h.id, Message.is_active.is_(False))
        )
        == 1
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.id == m_2h.id, Message.is_active.is_(True))
        )
        == 1
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.id == m_now.id, Message.is_active.is_(True))
        )
        == 1
    )


# ---------- ③ 归档审计读回 + 空跑无审计 ----------


def test_soft_archive_writes_summary_audit_once(session: Session) -> None:
    """③ 归档审计读回：恰一条 message.archive（批量动作 object_id 为空），actor=seed 的
    admin（W1 系统清扫同款真实用户留痕），detail={archived, cutoff=ISO 时刻}，created_at
    走注入时钟；对空库重复执行返回 0 且不追加审计。"""
    clock = FakeClock(_T0)
    conv = _make_conversation(session)
    _make_message(session, conv, created_at=clock.now() - timedelta(hours=25))

    assert soft_archive_old_messages(session, clock, retention_days=1) == 1

    audit = session.scalar(select(AuditLog).where(AuditLog.action == "message.archive"))
    assert audit is not None
    assert audit.actor_user_id == _admin_id(session)
    assert audit.object_type == "message"
    assert audit.object_id is None
    assert audit.detail == {"archived": 1, "cutoff": "2026-09-30T08:00:00+00:00"}
    assert audit.created_at == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

    assert soft_archive_old_messages(session, clock, retention_days=1) == 0  # 空跑
    assert (
        session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "message.archive")
        )
        == 1
    )


# ---------- ② 两段式：冷却期满物理清理、界内消息保留 ----------


def test_purge_after_cooldown_removes_only_expired_messages(session: Session) -> None:
    """② 先归档（retention=1）再前拨 31h，purge（retention=1+cooldown=1，cutoff=09-30
    15:00）：25h 前消息物理消失，now（10-01 08:00）与 2h 前（10-01 06:00）仍在，返回 1。"""
    clock = FakeClock(_T0)
    conv = _make_conversation(session)
    m_now = _make_message(session, conv, created_at=clock.now())
    m_2h = _make_message(session, conv, created_at=clock.now() - timedelta(hours=2))
    m_25h = _make_message(session, conv, created_at=clock.now() - timedelta(hours=25))
    soft_archive_old_messages(session, clock, retention_days=1)

    clock.advance(hours=31)
    purged = purge_expired_messages(session, clock, retention_days=1, cooldown_days=1)

    assert purged == 1
    assert (
        session.scalar(select(func.count()).select_from(Message).where(Message.id == m_25h.id)) == 0
    )
    assert (
        session.scalar(select(func.count()).select_from(Message).where(Message.id == m_now.id)) == 1
    )
    assert (
        session.scalar(select(func.count()).select_from(Message).where(Message.id == m_2h.id)) == 1
    )


# ---------- ④ 大批量分批物理删除：1201 条恰两批删净 ----------


def test_purge_deletes_backlog_in_batches_capped_at_1000(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """④ 1201 条过期消息（retention=0/cooldown=0 到期即清）：物理删除按 ≤1000 行/批
    分批执行（1201 → 恰两批 DELETE 语句），全部删净返回 1201、表内 0 行。"""
    clock = FakeClock(_T0)
    conv = _make_conversation(session)
    expired = clock.now() - timedelta(hours=25)
    session.bulk_insert_mappings(
        Message,
        [
            {
                "conversation_id": conv.id,
                "user_id": conv.user_id,
                "dept_id": conv.dept_id,
                "role": "user",
                "content": f"过期提问-{i}",
                "created_at": expired,
                "updated_at": expired,
                "is_active": True,
            }
            for i in range(1201)
        ],
    )
    session.flush()
    assert session.scalar(select(func.count()).select_from(Message)) == 1201

    deletes: list[Delete] = []
    real_execute = session.execute

    def _spy(statement: Executable, *args: Any, **kwargs: Any) -> Result[Any]:
        if isinstance(statement, Delete):
            deletes.append(statement)
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", _spy)

    purged = purge_expired_messages(session, clock, retention_days=0, cooldown_days=0)

    assert purged == 1201
    assert len(deletes) == 2  # 1000 + 201：单批 ≤1000 行，两批删净
    assert session.scalar(select(func.count()).select_from(Message)) == 0
