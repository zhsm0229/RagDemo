r"""停机补偿与 running 幽灵 run 清扫测试（商用就绪工单 06，评审 P1-1）。

停机跨周日场景：进程停机跨过周日 04:00/05:00 触发窗口，重启后补跑当周合成流水与
三视角跑分各一轮——补跑幂等（同周标记/在册 run 跳过），重放不重复。合成幂等键走
app_settings（key=eval_auto_synth_week，"ISO年-W周" 口径与 _weekly_run_name 同源）；
跑分幂等天然由 [自动] run 在册判定（工单 05 唯一索引兜底）。running 幽灵 run 清扫
复用 agent_runs 幽灵清扫先例（scheduler.py sweep_stale_agent_runs 条件 UPDATE）。
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.core.config import Settings
from app.db.models import AppSetting, EvalRun, User

# 周一 04:00（ISO 2026-W43）：停机跨过周日触发点后，周一启动即应补跑
_CLOCK = FakeClock("2026-10-19T04:00:00+00:00")


def _admin_id(session: Session) -> int:
    return session.scalar(select(User.id).where(User.username == "admin"))


def _save_enabled(session: Session) -> None:
    from app.services.eval_auto_config import save_eval_auto_config

    save_eval_auto_config(
        session,
        _CLOCK,
        actor_id=_admin_id(session),
        enabled=True,
        synth_per_seed=2,
        doc_sample_per_dept=5,
        weekly_max_questions=50,
        regression_pp=5.0,
    )
    session.commit()


def test_停机跨周日启动补跑合成与跑分各一轮(session: Session) -> None:
    """停机跨过周日触发窗口后启动：catchup 恰补跑一轮（合成标记+三视角 run 落库），
    重放幂等（第二次调用零新增）。"""
    from app.services.eval_catchup import catchup_missed_weekly

    _save_enabled(session)
    settings = Settings(jwt_secret="catchup-test", test_mode=True)

    summary = catchup_missed_weekly(
        session, _CLOCK, settings, deps=None, llm=None, executor=lambda *a: None
    )
    assert summary["synth"] is True
    assert summary["eval"] == 3  # 三视角各一个 run（executor 为记录型 lambda）
    row = session.get(AppSetting, "eval_auto_synth_week")
    assert row is not None and json.loads(row.value) == "2026-W43"

    summary2 = catchup_missed_weekly(
        session, _CLOCK, settings, deps=None, llm=None, executor=lambda *a: None
    )
    assert summary2["synth"] is False
    assert summary2["eval"] == 0
    assert len(list(session.scalars(select(EvalRun)))) == 3


def test_停机补偿总闸关零副作用(session: Session) -> None:
    """总闸关闭：catchup 零副作用（不落标记不建 run）——与周任务同口径，未开启
    客户/测试环境不受启动补偿影响。"""
    from app.services.eval_catchup import catchup_missed_weekly

    settings = Settings(jwt_secret="catchup-test", test_mode=True)
    summary = catchup_missed_weekly(
        session, _CLOCK, settings, deps=None, llm=None, executor=lambda *a: None
    )
    assert summary == {"synth": False, "eval": 0}
    assert session.get(AppSetting, "eval_auto_synth_week") is None
    assert len(list(session.scalars(select(EvalRun)))) == 0


def test_停机补偿同周标记在但run缺失时仍补跑分(session: Session) -> None:
    """合成标记已在（同周已合成）但 [自动] run 缺失（如合成成功后崩溃）：只补跑分
    不重复合成——两条通道幂等键独立。"""
    from app.services.eval_catchup import catchup_missed_weekly

    _save_enabled(session)
    settings = Settings(jwt_secret="catchup-test", test_mode=True)
    session.add(
        AppSetting(
            key="eval_auto_synth_week",
            value=json.dumps("2026-W43"),
            updated_by=_admin_id(session),
        )
    )
    session.commit()

    summary = catchup_missed_weekly(
        session, _CLOCK, settings, deps=None, llm=None, executor=lambda *a: None
    )
    assert summary["synth"] is False
    assert summary["eval"] == 3


def test_running幽灵run超时清扫置failed(session: Session) -> None:
    """崩溃遗留 running run 超 eval_run_stale_minutes → 条件 UPDATE 置 failed
    （detail 留清扫标记）；不触碰正常在跑行。复用 agent_runs 幽灵清扫口径。"""
    from app.services.scheduler import sweep_stale_eval_runs

    stale_cutoff = FakeClock("2026-10-19T04:00:00+00:00")
    ghost = EvalRun(
        name="[自动]2026-W42-hr",
        config={"perspective": "hr"},
        status="running",
        total=0,
        correct=0,
        accuracy=0,
        detail={"items": []},
        triggered_by=None,
        created_at=stale_cutoff.now() - __import__("datetime").timedelta(minutes=130),
    )
    session.add(ghost)
    session.commit()

    swept = sweep_stale_eval_runs(
        session, FakeClock("2026-10-19T06:00:00+00:00"), stale_minutes=120
    )
    assert swept == 1
    session.expire_all()
    row = session.scalar(select(EvalRun).where(EvalRun.id == ghost.id))
    assert row is not None and row.status == "failed"
    assert row.detail is not None and row.detail.get("error") == "运行超时自动收口（幽灵清扫）"
