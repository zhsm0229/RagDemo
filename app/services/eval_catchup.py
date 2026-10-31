r"""停机补偿任务体（商用就绪工单 06，评审 P1-1）：启动时检查"当周应跑未跑"。

口径：合成与跑分两条通道幂等键独立——
- 合成：app_settings key=eval_auto_synth_week（JSON 字符串 "ISO年-W周序"），由
  run_synth_pipeline 成功路径落（cron 与 catchup 共享，同周互斥）；缺失即补跑。
- 跑分：[自动] run 在册判定天然幂等（run_weekly_eval 同周在册跳过 + 0017 唯一
  索引兜底），缺失即补跑。
- 总闸关（enabled=False）零副作用：不落标记不建 run——与周任务同口径。

调用时机：app lifespan 启动时（非 test_mode 且未关闭总闸）；另有 scheduler 周日
cron 触发。进程被 kill -9 丢任务后，重启即自愈——"每周必有一轮"承诺在无人值守
场景成立。
"""

import json

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import AppSetting
from app.services.eval_auto_config import load_eval_auto_config

# 合成幂等标记的存储键（app_settings）：值为 JSON 字符串 "ISO年-W周序"
SYNTH_WEEK_KEY: str = "eval_auto_synth_week"


def _iso_week_key(clock: Clock) -> str:
    """当周幂等键（与 eval_auto_run._weekly_run_name 同一 ISO 周口径）。"""
    iso = clock.now().isocalendar()
    return str(iso.year) + "-W" + str(iso.week).zfill(2)


def _synth_week_done(session: Session, week_key: str) -> bool:
    row = session.get(AppSetting, SYNTH_WEEK_KEY)
    return row is not None and json.loads(row.value) == week_key


def catchup_missed_weekly(
    session: Session,
    clock: Clock,
    settings: Settings,
    *,
    deps: object | None,
    llm: object | None,
    executor: object | None = None,
) -> dict:
    """启动补偿入口（lifespan/cron/测试共用）：补跑当周缺失的合成与跑分各一轮。

    返回摘要 {"synth": bool（本轮是否执行了合成）, "eval": int（本轮新建 run 数）}；
    总闸关返回 {"synth": False, "eval": 0} 零副作用。deps/llm/executor 全部透传
    下游任务体（测试缝；cron/lifespan 侧传真实现）。
    """
    from app.services.eval_auto import run_synth_pipeline
    from app.services.eval_auto_run import run_weekly_eval

    config = load_eval_auto_config(session, settings)
    if config.enabled is False:
        return {"synth": False, "eval": 0}
    summary: dict = {"synth": False, "eval": 0}
    if not _synth_week_done(session, _iso_week_key(clock)):
        run_synth_pipeline(session, clock, settings, llm=llm)
        summary["synth"] = True
    created = run_weekly_eval(session, clock, settings, deps=deps, executor=executor)
    summary["eval"] = len(created)
    return summary


def load_config(session: Session, settings: Settings):
    """总闸读取（防御性包装）：enabled=False 返回 None（保留给 lifespan 调用点用）。"""
    from app.services.eval_auto_config import load_eval_auto_config

    config = load_eval_auto_config(session, settings)
    if config.enabled is False:
        return None
    return config
