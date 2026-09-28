r"""定时任务（W1 Task 15）：到期文档下线清扫 + messages 月分区补建的 APScheduler 装配。

设计要点（brief「关键设计」）：
- 测试与 runbook 不依赖真实等待：任务体拆成接收 session/clock 的纯函数
  （run_due_sweeps / ensure_month_partitions），可手动调用；cron 只负责按时刻调起。
- 到期下线的状态迁移与审计只有一份实现（services/ingest.expire_due_documents，
  F-1004，动作 document.expire、object=文档 id）：定时清扫与人工下线同一条路，口径
  天然一致，本模块不重复写审计。
- 分区补建 DDL 与 alembic/versions/0001_init.py 的建分区语句同源（同名约定
  messages_YYYY_MM、上开下闭月界），幂等：已存在的分区读 pg_inherits 跳过；DEFAULT
  兜底分区由迁移建成后永不重建。
- PG 工具语句（CREATE TABLE ... FOR VALUES）不接受绑定参数（psycopg 服务端绑定实测
  IndeterminateDatatype），故月界只能内联：值为注入时钟 date 算术的 isoformat 产物、
  分区名经正则校验，均非请求输入，与迁移内字面 DDL 同一信任域（红线 §2 的让步点）。
- 调度任务一律用独立 SessionLocal 会话（调度线程无请求会话可依附）；
  settings.test_mode=True 时 build_scheduler 只装配不自启（测试进程不起后台线程）。
"""

import re
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.config import Settings
from app.db.base import SessionLocal
from app.services.ingest import expire_due_documents

_PARENT_TABLE = "messages"
_PARTITION_NAME = re.compile(r"messages_\d{4}_\d{2}\Z")
_SWEEPS_JOB_ID = "run_due_sweeps"
_PARTITIONS_JOB_ID = "ensure_month_partitions"


def run_due_sweeps(session: Session, clock: Clock) -> int:
    """清扫任务体：到期文档下线并留审计，返回处理条数（cron/runbook/测试共用入口）。

    审计与状态迁移只有一份实现（expire_due_documents：动作 document.expire、
    object=文档 id），定时与人工口径一致；提交由其内部完成。
    """
    return expire_due_documents(session, clock)


def ensure_month_partitions(session: Session, n: int = 3) -> int:
    """从当前月起补建 messages 未来 n 个月的 RANGE 分区（幂等），返回新建分区数。

    「当前月」取注入时钟（get_clock()，测试可换 FakeClock 钉住月份）；已存在的分区
    （含迁移建的示例月分区）跳过；DEFAULT 兜底分区不属于月分区序列、永不重建。
    DDL 在传入会话的事务内执行并由本函数提交；月界/分区名见模块 docstring 的
    参数绑定让步说明。
    """
    today = get_clock().now().date()
    existing = set(
        session.execute(
            text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = to_regclass(:parent)"
            ),
            {"parent": _PARENT_TABLE},
        ).scalars()
    )
    created = 0
    for month_start, month_end in _month_bounds(today, n):
        name = f"messages_{month_start.year:04d}_{month_start.month:02d}"
        if not _PARTITION_NAME.fullmatch(name) or name in existing:
            continue  # 名字即防线：非约定格式不执行；已存在的月分区跳过（幂等）
        session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {_PARENT_TABLE} "
                f"FOR VALUES FROM ('{month_start.isoformat()}') "
                f"TO ('{month_end.isoformat()}')"
            )
        )
        created += 1
    session.commit()
    return created


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """装配 APScheduler（UTC）：每日 02:00 到期清扫、每月 1 日 02:30 分区补建。

    分区排在清扫之后，同窗口内先收口到期再补建下月分区。settings.test_mode=True
    只装配不自启；生产按单实例语义（多 worker 部署时各进程各持一份调度器，属
    W4 部署关切）。
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _run_sweeps_job,
        CronTrigger(day="*", hour="2", minute="0", timezone="UTC"),
        id=_SWEEPS_JOB_ID,
        name="到期文档下线清扫",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_partitions_job,
        CronTrigger(day="1", hour="2", minute="30", timezone="UTC"),
        id=_PARTITIONS_JOB_ID,
        name="messages 月分区补建",
        replace_existing=True,
    )
    if not settings.test_mode:
        scheduler.start()
    return scheduler


def _month_bounds(anchor: date, n: int) -> list[tuple[date, date]]:
    """自 anchor 所在月起 n 个连续月的 [月首, 次月首) 边界（12 月跨年由进位处理）。"""
    bounds: list[tuple[date, date]] = []
    year, month = anchor.year, anchor.month
    for _ in range(n):
        start = date(year, month, 1)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        bounds.append((start, date(year, month, 1)))
    return bounds


def _run_sweeps_job() -> None:
    """cron 任务体：独立会话执行到期清扫（调度线程无请求会话可依附）。"""
    with SessionLocal() as session:
        run_due_sweeps(session, get_clock())


def _run_partitions_job() -> None:
    """cron 任务体：独立会话执行月分区补建（失败由 DEFAULT 分区兜底，见迁移注释）。"""
    with SessionLocal() as session:
        ensure_month_partitions(session)
