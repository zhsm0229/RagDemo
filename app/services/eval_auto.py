r"""评测自动流水任务体（05 票）：采集 → 难例合成 → 文档造题 的周任务编排。

编排口径：
- 总闸先行：load_eval_auto_config(session, settings).enabled=False → 直接返回
  {"enabled": False}，零副作用（不采集不出题不留审计）——配置经管理端 PUT 落
  app_settings，运行时读取（改配置无需重装调度器）。
- 顺序固定：采集（拒答+点踩入池）→ 难例合成（点踩种子变体）→ 文档造题（零召回
  优先抽样）；合成预算取配置，文档造题的全局余量 = weekly_max_questions − 种子
  通道已产题数（两通道共享总量闸）。
- 系统审计：流水结果落 eval.autosynth（trigger="weekly"，actor=种子 admin——
  audit_logs.actor_user_id 非空约束，同 W1 系统清扫先例）；与业务写入同事务提交。
- LLM 由调用方注入（测试缝；cron 包装侧 build_llm(settings, 0.0) 第二实例，与
  人工评测判分同款 temperature=0 口径）。
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import AppSetting, User
from app.services import eval_synth, hardcase_mine
from app.services.audit import record_audit
from app.services.eval_auto_config import load_eval_auto_config


def run_synth_pipeline(session: Session, clock: Clock, settings: Settings, *, llm: Any) -> dict:
    """合成流水任务体（cron/runbook/测试共用入口）：返回合并摘要（enabled=False 时零副作用）。

    同周幂等（工单 06）：当周已成功合成（app_settings eval_auto_synth_week）则零副
    作用返回——cron 与启动补偿共享幂等键，停机补跑不重复合成。"""
    config = load_eval_auto_config(session, settings)
    if not config.enabled:
        return {"enabled": False}
    from app.services.eval_catchup import SYNTH_WEEK_KEY, _iso_week_key

    week_key = _iso_week_key(clock)
    done_row = session.get(AppSetting, SYNTH_WEEK_KEY)
    if done_row is not None and json.loads(done_row.value) == week_key:
        return {"enabled": True, "skipped": True}
    mined = hardcase_mine.collect_hardcase_seeds(session, clock)
    seed_summary = eval_synth.synthesize_from_seeds(
        session,
        clock,
        llm=llm,
        max_per_seed=config.synth_per_seed,
        max_total=config.weekly_max_questions,
    )
    remaining = config.weekly_max_questions - seed_summary["synthesized"]
    chunk_summary = eval_synth.synthesize_from_chunks(
        session,
        clock,
        llm=llm,
        sample_per_dept=config.doc_sample_per_dept,
        max_total=max(remaining, 0),
    )
    summary: dict = {
        "enabled": True,
        "mined": mined,
        "seedsProcessed": seed_summary["seedsProcessed"],
        "noGround": seed_summary["noGround"],
        "docChunksSampled": chunk_summary["chunksSampled"],
        "synthesized": seed_summary["synthesized"] + chunk_summary["synthesized"],
        "skipped": seed_summary["skipped"] + chunk_summary["skipped"],
    }
    actor_id = _system_actor_id(session)
    # 合成同周幂等标记（工单 06）：cron 与启动补偿共享同一幂等键——同周重入直接
    # 跳过（run_synth_pipeline 开头检查），停机补跑不重复合成（LLM 预算双花防线）
    _mark_synth_week(session, clock, actor_id)
    record_audit(
        session,
        actor_id,
        "eval.autosynth",
        "eval",
        None,
        {"trigger": "weekly", **summary},
    )
    session.commit()
    return summary


def _mark_synth_week(session: Session, clock: Clock, actor_id: int | None) -> None:
    """写 app_settings eval_auto_synth_week = "ISO年-W周序"（JSON 字符串）。"""
    from app.services.eval_catchup import SYNTH_WEEK_KEY, _iso_week_key

    week_key = _iso_week_key(clock)
    row = session.get(AppSetting, SYNTH_WEEK_KEY)
    if row is None:
        row = AppSetting(key=SYNTH_WEEK_KEY, value=json.dumps(week_key), updated_by=actor_id)
    else:
        row.value = json.dumps(week_key)  # 重新赋值触发 JSONB 变更检测
    session.add(row)


# 开头幂等检查的存储键同 eval_catchup.SYNTH_WEEK_KEY（延迟 import 防循环）


def _system_actor_id(session: Session) -> int | None:
    """系统动作审计人：种子 admin（audit_logs.actor_user_id 非空约束，W1 系统清扫先例）。"""
    stmt = select(User.id).where(User.username.in_(["admin"]))
    return session.scalar(stmt)
