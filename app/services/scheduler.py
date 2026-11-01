r"""调度器门面（工单 20/27）：装配 APScheduler + advisory lock 抢主恰一 leader。

实现拆分至三模块（各自 ≤300 行红线达标，语义零变化——test_scheduler/
test_scheduler_leader/test_hit_counter_flush 全绿即回归钉）：
- scheduler_leader：try_acquire_leader / release_leader（NullPool 独立持锁连接）
- scheduler_impl：幽灵清扫 / 到期下线 / 月分区共享实现（cron/runbook/测试共用入口）
- scheduler_jobs：cron 任务体（独立短会话 + 实时读 settings）与召回计数 flush 挂载
调用方（main / tests）导入路径不变（本门面 re-export）。
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings
from app.services.scheduler_impl import (
    ensure_month_partitions,
    run_due_sweeps,
    sweep_stale_agent_runs,
    sweep_stale_eval_runs,
)
from app.services.scheduler_jobs import (
    _run_agent_stale_job,
    _run_eval_auto_job,
    _run_eval_run_stale_job,
    _run_eval_synth_job,
    _run_partitions_job,
    _run_retention_job,
    _run_sweeps_job,
    add_hit_flush_job,
    run_hit_flush,
)
from app.services.scheduler_leader import release_leader, try_acquire_leader

__all__ = [
    "add_hit_flush_job",
    "build_scheduler",
    "ensure_month_partitions",
    "release_leader",
    "run_due_sweeps",
    "run_hit_flush",
    "sweep_stale_agent_runs",
    "sweep_stale_eval_runs",
    "try_acquire_leader",
]

logger = logging.getLogger(__name__)

# ---- 装配常量（job id 门面自用；_HIT_FLUSH_JOB_ID 在 scheduler_jobs） ----
_SWEEPS_JOB_ID = "run_due_sweeps"
_PARTITIONS_JOB_ID = "ensure_month_partitions"
_RETENTION_JOB_ID = "message_retention_cleanup"
_AGENT_STALE_JOB_ID = "agent_run_stale_sweep"
_EVAL_SYNTH_JOB_ID = "eval_auto_synth"
_EVAL_RUN_JOB_ID = "eval_auto_run"
_EVAL_RUN_STALE_JOB_ID = "eval_run_stale_sweep"


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
