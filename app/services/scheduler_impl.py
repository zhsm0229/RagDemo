r"""调度任务共享实现（工单 27 自 scheduler.py 拆分）：幽灵清扫 / 到期下线 / 月分区。

cron 任务体（scheduler_jobs）与 runbook/测试共用入口；AgentRun/EvalRun 幽灵清扫为
单条条件 UPDATE（判定与迁移同语句，PG 行锁 + WHERE 复评与并发收尾互斥不互覆，恰一次
落终态）；月分区 DDL 由 psycopg.sql 组合（Identifier/Literal 客户端转义），不经
f-string 拼接；「当前月」取注入时钟（测试可 FakeClock 钉住月份）。
"""

import re
from datetime import date, timedelta

from psycopg import sql
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.db.models import AgentRun
from app.services.ingest import expire_due_documents

_PARENT_TABLE = "messages"
_PARTITION_NAME = re.compile(r"messages_\d{4}_\d{2}\Z")
_MSG_AGENT_STALE = "运行超时被清扫"  # 落 agent_runs.error 的对外脱敏话术（技术细节只进日志)


def sweep_stale_agent_runs(session: Session, clock: Clock, stale_minutes: int) -> int:
    """agent run 幽灵清扫任务体（cron/runbook/测试共用入口）：running 且 started_at
    早于时钟现在时刻减阈值 → failed。单条条件 UPDATE：判定与迁移同语句，PG 行锁 +
    WHERE 复评保证与并发收尾（_fail_run）互斥不互覆，恰一次落终态；返回清扫条数。
    阈值经调用方传入（cron 体从 settings 实时读取，改配置无需重装调度器）。"""
    cutoff = clock.now() - timedelta(minutes=stale_minutes)
    result = session.execute(
        update(AgentRun)
        .where(AgentRun.status == "running", AgentRun.started_at < cutoff)
        .values(
            status="failed",
            error=_MSG_AGENT_STALE,
            finished_at=clock.now(),
            updated_at=clock.now(),
        )
    )
    session.commit()
    return int(result.rowcount or 0)


_MSG_EVAL_RUN_STALE = "运行超时自动收口（幽灵清扫）"  # 落 eval_runs.detail.error 的脱敏话术


def sweep_stale_eval_runs(session: Session, clock: Clock, stale_minutes: int) -> int:
    """评测 run 幽灵清扫任务体（商用就绪工单 06）：running 且 created_at 超过阈值
    → failed。崩溃遗留的 running 占住 [自动] 幂等键（同周在册即跳过），本周三视角
    跑分静默缺失——清扫后 catchup 补跑可达。单条条件 UPDATE（agent 幽灵清扫同款），
    返回清扫条数。"""
    from app.db.models import EvalRun

    cutoff = clock.now() - timedelta(minutes=stale_minutes)
    result = session.execute(
        update(EvalRun)
        .where(EvalRun.status == "running", EvalRun.created_at < cutoff)
        .values(
            status="failed",
            detail={"error": _MSG_EVAL_RUN_STALE},
        )
    )
    session.commit()
    return int(result.rowcount or 0)


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
    DDL 在传入会话的事务内执行并由本函数提交；建表语句由 psycopg.sql 组合
    （Identifier/Literal 客户端转义，见模块 docstring），不经 f-string 拼接。
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
        # DDL 用 psycopg.sql 组合（Identifier/Literal 客户端转义，§4.3）：
        # PG 工具语句不接受服务端绑定参数，月界以 Literal 转义字面进语句。
        ddl = sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})"
        ).format(
            sql.Identifier(name),
            sql.Identifier(_PARENT_TABLE),
            sql.Literal(month_start),
            sql.Literal(month_end),
        )
        connection = session.connection()
        connection.exec_driver_sql(ddl.as_string(connection.connection.dbapi_connection))
        created += 1
    session.commit()
    return created


def _month_bounds(anchor: date, n: int) -> list[tuple[date, date]]:
    """自 anchor 所在月起 n 个连续月的 [月首, 次月首) 边界（12 月跨年由进位处理）。"""
    bounds: list[tuple[date, date]] = []
    year, month = anchor.year, anchor.month
    for _ in range(n):
        start = date(year, month, 1)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        bounds.append((start, date(year, month, 1)))
    return bounds
