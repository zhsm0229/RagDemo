r"""周评测自动跑分任务体集成测试（06 票，真 pg + FakeClock + 注入 executor 测试缝）。

用例：
① 总闸关 → 返回空列表，零 run 创建；
② 总闸开 → service/hr/all 三视角各建一个 `[自动]YYYY-Www-{视角}` run
   （triggered_by=NULL 系统来源，config 快照含 perspective），executor 逐一被调；
③ 幂等：同周二次调用不重建不重跑（name 在册即跳过）；
④ 失败隔离：executor 对某视角抛异常 → 该 run 置 failed（detail.error），其余视角照常。

engine 执行以 executor 参数注入（缺省 = 真 run_eval_run + SessionLocal；测试注入
记录型替身，避免组装 ChatDeps 三件套——引擎自身行为已由 test_eval_engine 覆盖）。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.core.config import Settings
from app.db.models import Department, EvalRun, User
from app.services.eval_auto_run import run_weekly_eval

_CLOCK = FakeClock("2026-10-19T04:00:00+00:00")  # 周一 → ISO 周 2026-W43


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


def _recording_executor(calls: list[tuple[int, int]], fail_dept_id: int | None = None):
    """记录型 executor：成功路径仅记录 (run_id, dept_id)；fail_dept_id 抛异常。"""

    def _exec(session: Any, run_id: int, deps: Any, dept_id: int) -> None:
        if fail_dept_id is not None and dept_id == fail_dept_id:
            raise RuntimeError("模拟引擎故障")
        calls.append((run_id, dept_id))

    return _exec


def _runs(session: Session) -> list[EvalRun]:
    return list(session.scalars(select(EvalRun).order_by(EvalRun.id)).all())


def test_总闸关返回空(session: Session) -> None:
    settings = Settings(jwt_secret="eval-auto-run-test")
    calls: list[tuple[int, int]] = []
    result = run_weekly_eval(
        session, _CLOCK, settings, deps=None, executor=_recording_executor(calls)
    )
    assert result == []
    assert _runs(session) == []


def test_三视角建run并执行(session: Session) -> None:
    _save_enabled(session)
    settings = Settings(jwt_secret="eval-auto-run-test")
    calls: list[tuple[int, int]] = []

    created = run_weekly_eval(
        session, _CLOCK, settings, deps=None, executor=_recording_executor(calls)
    )

    assert len(created) == 3
    assert len(calls) == 3
    runs = _runs(session)
    assert len(runs) == 3
    names = {run.name for run in runs}
    assert names == {"[自动]2026-W43-service", "[自动]2026-W43-hr", "[自动]2026-W43-all"}
    for run in runs:
        assert run.status == "running"  # executor 为记录替身：状态留给引擎语义
        assert run.triggered_by is None  # 系统来源
        assert run.config["perspective"] in ("service", "hr", "all")
        assert run.created_at == _CLOCK.now()


def test_同周幂等(session: Session) -> None:
    _save_enabled(session)
    settings = Settings(jwt_secret="eval-auto-run-test")
    calls: list[tuple[int, int]] = []
    exec_fn = _recording_executor(calls)

    first = run_weekly_eval(session, _CLOCK, settings, deps=None, executor=exec_fn)
    second = run_weekly_eval(session, _CLOCK, settings, deps=None, executor=exec_fn)

    assert len(first) == 3
    assert second == []
    assert len(calls) == 3  # 二次调用不重跑
    assert len(_runs(session)) == 3


def test_单视角失败隔离(session: Session) -> None:
    _save_enabled(session)
    settings = Settings(jwt_secret="eval-auto-run-test")
    hr_dept = session.scalar(select(Department.id).where(Department.code == "hr"))
    calls: list[tuple[int, int]] = []

    created = run_weekly_eval(
        session,
        _CLOCK,
        settings,
        deps=None,
        executor=_recording_executor(calls, fail_dept_id=hr_dept),
    )

    assert len(created) == 3
    assert len(calls) == 2  # hr 视角抛异常，service/all 照常执行
    runs = _runs(session)
    by_name = {run.name: run for run in runs}
    failed = by_name["[自动]2026-W43-hr"]
    assert failed.status == "failed"
    assert failed.detail is not None and "error" in failed.detail
    for code in ("service", "all"):
        assert by_name[f"[自动]2026-W43-{code}"].status == "running"


# ---------------------------------------------------------------------------
# 防双跑唯一闸与失败日志（2026-10-31 评审 P1-13/14，工单 05——修复前两用例均红）
# ---------------------------------------------------------------------------


def test_自动run名称唯一闸_同周同名双插被数据库拒绝(session: Session) -> None:
    """[自动] 前缀 name 部分唯一索引：并发入口（两个进程同时过 _run_exists 查-插
    间隙）兜底由数据库约束完成——同周同名双插必须 IntegrityError。修复前 name 无
    任何约束，TOCTOU 双跑（双份三视角 LLM 账单）无防线。"""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    session.add(
        EvalRun(
            name="[自动]2026-W43-hr",
            config={"perspective": "hr"},
            status="running",
            total=0,
            correct=0,
            accuracy=0,
            detail={"items": []},
            triggered_by=None,
        )
    )
    session.add(
        EvalRun(
            name="[自动]2026-W43-hr",
            config={"perspective": "hr"},
            status="running",
            total=0,
            correct=0,
            accuracy=0,
            detail={"items": []},
            triggered_by=None,
        )
    )
    with _pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_自动run名称唯一闸_人工run不受限(session: Session) -> None:
    """部分唯一索引只约束 [自动] 前缀：人工 run 同名不受限（既有行为不回退——人工
    触发同名属运营自查范畴，历史上存在同名 run）。"""
    admin = _admin_id(session)
    for _ in range(2):
        session.add(
            EvalRun(
                name="上线前回归",
                config={},
                status="done",
                total=0,
                correct=0,
                accuracy=0,
                detail={"items": []},
                triggered_by=admin,
            )
        )
    session.commit()
    assert len([r for r in _runs(session) if r.name == "上线前回归"]) == 2


def test_引擎外异常有日志_失败run置failed(session: Session, caplog) -> None:  # type: ignore[no-untyped-def]
    """executor 抛异常时异常分支必须 logger.exception（运维排障盲区消除，评审 P1-14）：
    caplog 抓到 ERROR 级记录含 run_id 与视角码；run 收口 failed。修复前全文件无
    logging，静默吞异常只留脱敏通用语。"""
    import logging as _logging

    from app.core.config import Settings as _Settings

    _save_enabled(session)

    def _boom(session: Any, run_id: int, deps: Any, dept_id: int) -> None:
        raise RuntimeError("模拟引擎故障")

    with caplog.at_level(_logging.ERROR, logger="app.services.eval_auto_run"):
        created = run_weekly_eval(
            session, _CLOCK, _Settings(jwt_secret="t", test_mode=True), deps=None, executor=_boom
        )
    assert len(created) == 3
    error_records = [r for r in caplog.records if r.levelno >= _logging.ERROR]
    assert error_records, "异常分支必须有 ERROR 级日志（修复前静默）"
    assert any("run_id=" in r.getMessage() or r.exc_info for r in error_records)
    failed = [r for r in _runs(session) if r.name == "[自动]2026-W43-hr"]
    assert len(failed) == 1
    assert failed[0].status == "failed"
