r"""定时任务集成测试：run_due_sweeps 到期清扫 + ensure_month_partitions 分区补建（真 pg + FakeClock）。

brief 用例 4 条：①FakeClock 拨到 expiry_date 之后 → run_due_sweeps → is_active=False +
审计读回 + 返回 1；②无到期文档 → 返回 0 且无审计；③分区补建幂等（二次执行 0 新增）；
④未来 3 个月分区读回存在。测试不依赖真实等待：任务体是接收 session/clock 的纯函数，
可手动调用；cron 装配另设断言（注册任务与表达式、test_mode 不自启）。
"""

import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.clock import FakeClock, SystemClock, set_clock
from app.core.config import Settings
from app.db.models import AuditLog, Document, User
from app.services.scheduler import build_scheduler, ensure_month_partitions, run_due_sweeps

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture()
def pinned_clock() -> Iterator[FakeClock]:
    """全局时钟钉在 2026-10-15：ensure_month_partitions 的「当前月」确定可断言，测后还原。"""
    clock = FakeClock("2026-10-15T08:00:00+00:00")
    set_clock(clock)
    yield clock
    set_clock(SystemClock())


def _dept_id(session: Session) -> int:
    return session.scalar(select(User.dept_id).where(User.username == "admin"))


def _admin_id(session: Session) -> int:
    return session.scalar(select(User.id).where(User.username == "admin"))


def _make_doc(
    session: Session, title: str, *, expiry: date | None = None, is_active: bool = True
) -> Document:
    """直插文档行（绕开解析管线）：清扫测试只关心 is_active / expiry_date 两个字段。"""
    title_digest = hashlib.sha256(title.encode()).hexdigest()
    doc = Document(
        title=title,
        dept_id=_dept_id(session),
        original_filename=f"{title}.docx",
        file_path=f"uploads/{title_digest}.docx",
        mime=DOCX_MIME,
        size_bytes=1024,
        content_hash=title_digest,  # 标题派生 hash：行间不撞 uq_doc_content_hash
        version=1,
        effective_date=date(2026, 9, 1),
        expiry_date=expiry,
        owner_name="制度责任人",
        status="ready",
        is_active=is_active,
        uploaded_by=_admin_id(session),
    )
    session.add(doc)
    session.flush()
    return doc


def _audits(session: Session, doc_id: int) -> list[AuditLog]:
    """按对象精确读回该文档的审计（同 test_ingest 口径，不依赖表空前提）。"""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.object_type == "document", AuditLog.object_id == doc_id)
            .order_by(AuditLog.id)
        ).all()
    )


def _partition_names(session: Session) -> set[str]:
    """读回 messages 全部分区子表名（pg_inherits，同 test_migrations 口径）。"""
    return set(
        session.execute(
            text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = to_regclass(:parent)"
            ),
            {"parent": "messages"},
        ).scalars()
    )


# ---------- ① 到期清扫：下线 + 审计读回 + 返回条数 ----------


def test_run_due_sweeps_expires_due_documents_and_audits(session: Session) -> None:
    """① FakeClock 拨到 expiry_date 之后：到期件 is_active=False，审计 document.expire 读回，返回 1。"""
    expired = _make_doc(session, "客服_已到期_20260901", expiry=date(2026, 9, 15))
    future = _make_doc(session, "客服_未到期_20260901", expiry=date(2026, 10, 2))

    count = run_due_sweeps(session, FakeClock("2026-10-01T08:00:00+00:00"))

    assert count == 1
    session.refresh(expired)
    session.refresh(future)
    assert expired.is_active is False
    assert future.is_active is True
    audits = _audits(session, expired.id)
    assert [audit.action for audit in audits] == ["document.expire"]
    assert audits[0].object_type == "document"
    assert audits[0].object_id == expired.id
    assert audits[0].detail == {"after": {"isActive": False}, "expiryDate": "2026-09-15"}
    assert audits[0].created_at == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)  # 注入时钟贯穿审计
    assert _audits(session, future.id) == []


# ---------- ② 无到期：返回 0、无审计 ----------


def test_run_due_sweeps_without_due_documents_is_noop(session: Session) -> None:
    """② 无到期文档：返回 0，全程不产生任何 document.expire 审计，未到期件保持生效。"""
    future = _make_doc(session, "客服_未到期_20260901", expiry=date(2026, 10, 2))
    perpetual = _make_doc(session, "客服_无期限_20260901")

    count = run_due_sweeps(session, FakeClock("2026-10-01T08:00:00+00:00"))

    assert count == 0
    session.refresh(future)
    session.refresh(perpetual)
    assert future.is_active is True
    assert perpetual.is_active is True
    assert (
        session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "document.expire")
        )
        == 0
    )


# ---------- ③ 分区补建幂等 ----------


def test_ensure_month_partitions_idempotent(session: Session, pinned_clock: FakeClock) -> None:
    """③ 从当前月（2026-10）起补 3 个月：首跑补 2026_11/12（迁移示例分区 2026_10 已存在），
    二次执行 0 新增（幂等），DEFAULT 兜底分区不重复创建。"""
    assert _partition_names(session) == {"messages_default", "messages_2026_10"}

    created_first = ensure_month_partitions(session)
    created_second = ensure_month_partitions(session)

    assert created_first == 2
    assert created_second == 0
    assert _partition_names(session) == {
        "messages_default",
        "messages_2026_10",
        "messages_2026_11",
        "messages_2026_12",
    }


# ---------- ④ 未来 3 个月分区读回存在 ----------


def test_ensure_month_partitions_creates_next_three_months(
    session: Session, pinned_clock: FakeClock
) -> None:
    """④ 未来 3 个月分区存在读回：2026_10/11/12 齐备，DEFAULT 兜底恰一份。"""
    created = ensure_month_partitions(session, n=3)

    assert created == 2
    assert _partition_names(session) == {
        "messages_default",
        "messages_2026_10",
        "messages_2026_11",
        "messages_2026_12",
    }


# ---------- cron 装配：任务注册与 test_mode 不自启 ----------


def test_build_scheduler_registers_cron_jobs_without_starting() -> None:
    """装配断言：三任务按 cron 注册（每日 02:00 清扫、每日 03:00 提问记录清理、每月 1 日
    02:30 分区）；test_mode 不自启。"""
    scheduler = build_scheduler(Settings(jwt_secret="scheduler-test", test_mode=True))
    assert scheduler.running is False
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {
        "run_due_sweeps",
        "message_retention_cleanup",
        "ensure_month_partitions",
    }
    sweeps = {field.name: str(field) for field in jobs["run_due_sweeps"].trigger.fields}
    assert (sweeps["hour"], sweeps["minute"], sweeps["day"]) == ("2", "0", "*")
    retention = {
        field.name: str(field) for field in jobs["message_retention_cleanup"].trigger.fields
    }
    assert (retention["hour"], retention["minute"], retention["day"]) == ("3", "0", "*")
    parts = {field.name: str(field) for field in jobs["ensure_month_partitions"].trigger.fields}
    assert (parts["day"], parts["hour"], parts["minute"]) == ("1", "2", "30")


def test_build_scheduler_starts_in_normal_mode() -> None:
    """test_mode=False：调度器自启且任务保持注册（测试内即启即停，不等待真实触发）。"""
    scheduler = build_scheduler(Settings(jwt_secret="scheduler-test", test_mode=False))
    try:
        assert scheduler.running is True
        assert {job.id for job in scheduler.get_jobs()} == {
            "run_due_sweeps",
            "message_retention_cleanup",
            "ensure_month_partitions",
        }
    finally:
        scheduler.shutdown(wait=False)
