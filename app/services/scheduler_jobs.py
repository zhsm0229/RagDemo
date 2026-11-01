r"""调度 cron 任务体（工单 27 自 scheduler.py 拆分）：独立短会话执行实现并留运行日志。

调度线程无请求会话可依附——每个任务体 with SessionLocal() 自管短会话；阈值/开关从
settings 实时读取（改配置无需重装调度器）；评测两任务延迟 import 防循环（eval_auto/
main._build_chat_deps）；任务体异常经 logger.exception 吞掉不拖垮调度线程（下轮重试）。
"""

import logging
from functools import partial

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.clock import get_clock
from app.core.config import get_settings
from app.db.base import SessionLocal
from app.services.chat_cache import HitCounter
from app.services.retention import purge_expired_messages, soft_archive_old_messages
from app.services.scheduler_impl import (
    ensure_month_partitions,
    run_due_sweeps,
    sweep_stale_agent_runs,
    sweep_stale_eval_runs,
)

logger = logging.getLogger(__name__)

_HIT_FLUSH_JOB_ID = "flush_hit_counter"


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
