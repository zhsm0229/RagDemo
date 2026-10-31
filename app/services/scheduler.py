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
- PG 工具语句（CREATE TABLE ... FOR VALUES）不接受服务端绑定参数（psycopg 实测
  IndeterminateDatatype），故 DDL 不走 bind param：由 psycopg.sql 组合——分区名经
  sql.Identifier、月界经 sql.Literal 在客户端安全转义（见 ensure_month_partitions），
  红线 §4.3「禁止 f-string 拼 SQL」字面达成；值为注入时钟 date 算术产物，非请求输入。
- 调度任务一律用独立 SessionLocal 会话（调度线程无请求会话可依附）；
  settings.test_mode=True 时 build_scheduler 只装配不自启（测试进程不起后台线程）。
- 调度独立单副本（工单 20）：scheduler_enabled 开关 + PG 会话级 advisory lock 抢主
  （try_acquire_leader，NullPool 独立引擎持锁连接随进程存活），多副本部署恰一
  leader；catchup 补偿线程随 leader（main.lifespan 读 is_leader）。
- agent run 幽灵清扫（A4）：running 且 started_at 超过 agent_run_stale_minutes 的
  agent_runs 置 failed（error="运行超时被清扫"）——正常路径收尾必达终态（services/
  agent_runs finally 兜底），此任务兜的是进程崩溃/断电等"finally 也没跑成"的遗留行，
  防幽灵 running 永久占用 A3 并发名额；单条条件 UPDATE 判定与迁移同语句（无 TOCTOU），
  每小时 45 分跑一次（阈值 15 分钟，最长滞后 ≈1h，远小于占位行产生业务影响的量级；
  错开 02:00/02:30/03:00 的既有任务窗口）。
- chat 召回计数 flush（Task 7/F-1011）：HitCounter 进程内聚合经 run_hit_flush
  独立会话刷库，周期由调用方传（Task 8 装配传 settings.cache_ttl_s，与问答缓存
  同生命周期）；pg 异常被 logger.exception 吞掉、聚合保留滚入下一轮。任务注册走
  add_hit_flush_job（时钟注入的 HitCounter 由装配方持有，build_scheduler 不造缓存实例）。
  调度时区统一 UTC（东八区口径为已登记遗留，同下）。
- 提问记录保留清理（Task 12/F-803）：先归档后物理清理两段式（services/retention.py，
  保留/冷却天数读 settings），独立会话每日一次，结果留日志；审计留痕在归档函数内部
  完成（actor=seed 的 admin，同 W1 系统清扫先例）。
"""

import logging
import re
from datetime import date, timedelta
from functools import partial

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from psycopg import sql
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.clock import Clock, get_clock
from app.core.config import Settings, get_settings
from app.db.base import SessionLocal
from app.db.models import AgentRun
from app.services.chat_cache import HitCounter
from app.services.ingest import expire_due_documents
from app.services.retention import purge_expired_messages, soft_archive_old_messages

logger = logging.getLogger(__name__)

_PARENT_TABLE = "messages"
_PARTITION_NAME = re.compile(r"messages_\d{4}_\d{2}\Z")
_SWEEPS_JOB_ID = "run_due_sweeps"
_PARTITIONS_JOB_ID = "ensure_month_partitions"
_RETENTION_JOB_ID = "message_retention_cleanup"
_HIT_FLUSH_JOB_ID = "flush_hit_counter"
_AGENT_STALE_JOB_ID = "agent_run_stale_sweep"
_EVAL_SYNTH_JOB_ID = "eval_auto_synth"
_EVAL_RUN_JOB_ID = "eval_auto_run"
_EVAL_RUN_STALE_JOB_ID = "eval_run_stale_sweep"
_MSG_AGENT_STALE = "运行超时被清扫"  # 落 agent_runs.error 的对外脱敏话术（技术细节只进日志）

# 调度独立单副本（工单 20）：锁键字面量入 docstring/日志便于运维 pg_locks 排查——
# SELECT * FROM pg_locks WHERE locktype='advisory' 配合 hashtext(key) 对账
_LEADER_KEY = "ragdemo:scheduler:leader"
_leader_engine: Engine | None = None  # 持锁连接的独立 NullPool 引擎（生命周期=进程）
_leader_conn: Connection | None = None  # 连接必须显式引用：NullPool.dispose 是 no-op（实测源码）


def try_acquire_leader(settings: Settings) -> bool:
    """进程级抢主：pg_try_advisory_lock(hashtext(key))，持锁连接随进程生命周期。

    为何不用 SessionLocal 池连接：池连接会被 recycle/归还，锁随连接回收静默丢失 →
    出现"第二个调度器"；NullPool 独立引擎的连接生命周期=进程，锁不可漂移（宁多一条
    常驻连接换确定性，design/20 §9.2）。失败路径连接即关、返回 False。"""
    global _leader_engine, _leader_conn
    _leader_engine = create_engine(settings.database_url, poolclass=NullPool)
    _leader_conn = _leader_engine.connect()
    got = _leader_conn.exec_driver_sql(
        "SELECT pg_try_advisory_lock(hashtext(%s))",  # 参数绑定，禁拼接（红线 2）
        (_LEADER_KEY,),
    ).scalar()
    if not got:
        _leader_conn.close()  # 会话级锁随连接关闭释放
        _leader_conn = None
        return False
    return True  # 连接保持打开：锁随连接存活；进程退出 TCP 断开 PG 自动释放


def release_leader() -> None:
    """释放调度主锁：关持锁连接并 dispose 引擎（lifespan finally 先 shutdown 后调用）。

    连接必须显式 close——NullPool.dispose() 是 no-op（实测源码），不关连接锁不释放
    （design/20 §3"关 NullPool 连接并 dispose 引擎"的字面落实）。异常吞掉留日志——
    进程退出时 TCP 断开为兜底释放路径；未抢锁过为 no-op（test_mode/开关关进程安全可调）。"""
    global _leader_engine, _leader_conn
    conn, _leader_conn = _leader_conn, None
    engine, _leader_engine = _leader_engine, None
    if conn is None and engine is None:
        return
    try:
        if conn is not None:
            conn.close()
        if engine is not None:
            engine.dispose()
    except Exception:
        logger.exception("调度主锁释放失败（进程退出兜底释放）")


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


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """装配 APScheduler（UTC）：每日 02:00 到期清扫、每日 03:00 提问记录清理、
    每月 1 日 02:30 分区补建、每小时 45 分 agent run 幽灵清扫。

    分区排在清扫之后，同窗口内先收口到期再补建下月分区；提问记录清理再后一小时
    （03:00），避免与 02:00 清扫同窗口抢库；agent 幽灵清扫错开上述窗口（:45），
    阈值 15 分钟下最长清扫滞后约 1 小时。settings.test_mode=True 只装配不自启
    （短路顺序不变，测试进程永不起后台线程）；生产启动闸=scheduler_enabled 开关 +
    advisory lock 抢主（try_acquire_leader，锁键 'ragdemo:scheduler:leader'），多副本
    部署恰一 leader——未抢到的进程 running=False，本进程不启动调度（design/20 §3）。
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    # misfire_grace_time=1h + coalesce（商用就绪工单 06）：运行中错峰错过（GC 停顿/
    # 前序任务超时占线程）的触发在 1h 内补跑且多轮合并为一轮——与"同周幂等"互补，
    # 前者管运行中错过、后者管停机跨窗口（catchup）
    _MISFIRE = {"misfire_grace_time": 3600, "coalesce": True}
    scheduler.add_job(
        _run_sweeps_job,
        CronTrigger(day="*", hour="2", minute="0", timezone="UTC"),
        id=_SWEEPS_JOB_ID,
        name="到期文档下线清扫",
        replace_existing=True,
        **_MISFIRE,
    )
    scheduler.add_job(
        _run_partitions_job,
        CronTrigger(day="1", hour="2", minute="30", timezone="UTC"),
        id=_PARTITIONS_JOB_ID,
        name="messages 月分区补建",
        replace_existing=True,
        **_MISFIRE,
    )
    scheduler.add_job(
        _run_retention_job,
        CronTrigger(day="*", hour="3", minute="0", timezone="UTC"),
        id=_RETENTION_JOB_ID,
        name="提问记录两段式清理",
        replace_existing=True,
        **_MISFIRE,
    )
    scheduler.add_job(
        _run_agent_stale_job,
        CronTrigger(day="*", hour="*", minute="45", timezone="UTC"),
        id=_AGENT_STALE_JOB_ID,
        name="agent 运行幽灵清扫",
        replace_existing=True,
        **_MISFIRE,
    )
    # 评测集自动更新（05 票）：周日 04:00 合成流水（采集→难例合成→文档造题），
    # 错峰既有 02:00/02:30/03:00 窗口；总闸 app_settings.eval_auto.enabled 关时零副作用
    scheduler.add_job(
        _run_eval_synth_job,
        CronTrigger(day_of_week="sun", hour="4", minute="0", timezone="UTC"),
        id=_EVAL_SYNTH_JOB_ID,
        name="评测自动合成流水",
        replace_existing=True,
        **_MISFIRE,
    )
    # 评测集自动更新（06 票）：周日 05:00 三视角自动跑分（排在合成流水之后，新题
    # 当轮即被跑分覆盖）；同一总闸，任务体幂等（同周同视角 run 在册即跳过）
    scheduler.add_job(
        _run_eval_auto_job,
        CronTrigger(day_of_week="sun", hour="5", minute="0", timezone="UTC"),
        id=_EVAL_RUN_JOB_ID,
        name="周评测自动跑分",
        replace_existing=True,
        **_MISFIRE,
    )
    # 评测 run 幽灵清扫（工单 06）：每小时 15 分（错开 agent 清扫的 :45），崩溃遗留
    # running 置 failed 释放 [自动] 幂等键；阈值 eval_run_stale_minutes
    scheduler.add_job(
        _run_eval_run_stale_job,
        CronTrigger(day="*", hour="*", minute="15", timezone="UTC"),
        id=_EVAL_RUN_STALE_JOB_ID,
        name="评测 run 幽灵清扫",
        replace_existing=True,
        **_MISFIRE,
    )
    # 启动闸（工单 20）：test_mode 短路顺序不变（测试进程永不起后台线程）+ 开关 +
    # advisory lock 抢主。抢主放本函数内而非 lifespan：签名/返回类型零破坏，装配与
    # 选举同处一函数便于审计（design/20 §9.1）；选举失败不拖垮 API 装配（misfire +
    # catchup 兜底）。is_leader 挂调度器对象供 lifespan 消费（catchup 归属）。
    is_leader = False
    if not settings.test_mode and settings.scheduler_enabled:
        try:
            is_leader = try_acquire_leader(settings)
        except Exception:
            logger.exception("调度主锁获取失败（本进程不启动调度器，misfire+catchup 兜底）")
        if is_leader:
            scheduler.start()
        else:
            logger.info("调度主锁已被其他实例持有，本进程不启动调度")
    scheduler.is_leader = is_leader  # type: ignore[attr-defined]  # BackgroundScheduler 可挂属性
    return scheduler


def run_hit_flush(counter: HitCounter) -> None:
    """flush 任务体：独立会话执行召回计数聚合刷库（调度线程无请求会话可依附）。

    异常经 logger.exception 记录后吞掉：刷库失败不打垮调度线程；HitCounter 聚合
    仅在提交成功后清空（chat_cache.HitCounter.flush 契约），失败轮次的计数自动
    滚入下一周期重试。
    """
    try:
        with SessionLocal() as session:
            counter.flush(session)
    except Exception:
        logger.exception("chunk 召回计数刷库失败（聚合保留，待下一周期重试）")


def add_hit_flush_job(scheduler: BackgroundScheduler, counter: HitCounter, interval_s: int) -> None:
    """挂召回计数 flush 任务：IntervalTrigger 每 interval_s 秒（调用方传
    settings.cache_ttl_s，与问答缓存同周期）。时区沿用调度器统一 UTC（已登记遗留）。
    HitCounter 实例由装配方持有并注入，保证调度刷的与请求侧 add 的是同一份聚合。
    """
    scheduler.add_job(
        partial(run_hit_flush, counter),
        IntervalTrigger(seconds=interval_s, timezone="UTC"),
        id=_HIT_FLUSH_JOB_ID,
        name="chunk 召回计数刷库",
        replace_existing=True,
    )


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


def _run_retention_job() -> None:
    """cron 任务体：独立会话执行提问记录两段式清理（先归档后物理清理），结果留日志。

    保留/冷却天数从 settings 实时读取（改配置无需重装调度器）；F-803 要求清理任务
    有日志与审计——审计由 soft_archive_old_messages 内部落 audit_logs，这里补运行日志。
    """
    settings = get_settings()
    with SessionLocal() as session:
        archived = soft_archive_old_messages(session, get_clock(), settings.message_retention_days)
        purged = purge_expired_messages(
            session,
            get_clock(),
            settings.message_retention_days,
            settings.message_cleanup_cooldown_days,
        )
    logger.info("提问记录保留清理完成：归档 %d 条，物理清理 %d 条", archived, purged)


def _run_agent_stale_job() -> None:
    """cron 任务体（A4）：独立会话执行 agent run 幽灵清扫；阈值从 settings 实时读取。
    清扫 >0 才留运行日志（常态空转不刷屏），清扫失败不吞也不拖垮调度线程。"""
    with SessionLocal() as session:
        swept = sweep_stale_agent_runs(session, get_clock(), get_settings().agent_run_stale_minutes)
    if swept:
        logger.info("agent run 幽灵清扫：%d 行 running 超龄置 failed", swept)


def _run_eval_synth_job() -> None:
    """cron 任务体（05 票）：独立会话执行评测自动合成流水；LLM 用 temperature=0
    第二实例（与人工评测判分同款口径）。总闸关时任务体零副作用直接返回；
    失败经 logger.exception 吞掉不拖垮调度线程（下轮周任务重试）。"""
    settings = get_settings()
    try:
        from app.core.assembly import build_llm
        from app.services.eval_auto import run_synth_pipeline

        with SessionLocal() as session:
            summary = run_synth_pipeline(
                session, get_clock(), settings, llm=build_llm(settings, 0.0)
            )
    except Exception:
        logger.exception("评测自动合成流水执行失败（下轮周任务重试）")
        return
    if summary.get("enabled"):
        logger.info("评测自动合成流水完成：%s", summary)


def _run_eval_auto_job() -> None:
    """cron 任务体（06 票）：独立会话建三视角 `[自动]` run 并逐一执行引擎；deps 由
    main._build_chat_deps 装配（延迟导入防循环）。总闸关时任务体返回空列表零副作用；
    失败经 logger.exception 吞掉（失败视角已在任务体内置 failed，见 eval_auto_run）。"""
    settings = get_settings()
    try:
        from app.main import _build_chat_deps
        from app.services.eval_auto_run import run_weekly_eval

        with SessionLocal() as session:
            run_ids = run_weekly_eval(
                session, get_clock(), settings, deps=_build_chat_deps(settings)
            )
    except Exception:
        logger.exception("周评测自动跑分执行失败（下轮周任务重试）")
        return
    if run_ids:
        logger.info("周评测自动跑分：本轮新建 run %s", run_ids)


def _run_eval_run_stale_job() -> None:
    """评测 run 幽灵清扫 wrapper：阈值实时读 settings（改配置无需重装调度器）。"""
    settings = get_settings()
    try:
        with SessionLocal() as session:
            swept = sweep_stale_eval_runs(session, get_clock(), settings.eval_run_stale_minutes)
    except Exception:
        logger.exception("评测 run 幽灵清扫执行失败")
        return
    if swept:
        logger.info("评测 run 幽灵清扫：置 failed %s 条", swept)
