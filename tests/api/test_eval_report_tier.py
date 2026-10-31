r"""报告分层（byTier）与退化环比（vsPrevious / regression）测试（07 票）。

口径：
- byTier：按题目 tier（official/extended）分组 total/accuracy，键字典序；
- vsPrevious：同 config.perspective、status=done、id 更小的最近一条 done run 的
  accuracy 环比（无前轮或旧 run 无 perspective → null）；
- regression：delta ≤ -threshold_pp 时 mark_regression 写 run.detail.regression
  （口径=run 级全量 accuracy；official-only 分层口径由报告 byTier 呈现）。
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.db.models import Department, EvalQuestion, EvalRun
from app.services.eval_report import build_report, mark_regression

_T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
_CLOCK = FakeClock("2026-10-20T05:00:00+00:00")


def _dept_id(session: Session, code: str = "service") -> int:
    return session.scalar(select(Department.id).where(Department.code == code))


def _add_question(session: Session, question: str, *, tier: str = "official") -> EvalQuestion:
    item = EvalQuestion(
        dept_id=_dept_id(session),
        question=question,
        expected_answer="答案",
        tier=tier,
    )
    session.add(item)
    session.flush()
    return item


def _item(question: EvalQuestion, judgment: str) -> dict:
    return {
        "questionId": question.id,
        "deptId": question.dept_id,
        "qtype": "factual",
        "expected": "答案",
        "answerType": "rag",
        "answer": "回答",
        "citations": [],
        "autoJudgment": judgment,
        "metrics": None,
    }


def _make_run(
    session: Session,
    name: str,
    *,
    perspective: str | None,
    accuracy: str,
    items: list[dict],
) -> EvalRun:
    run = EvalRun(
        name=name,
        status="done",
        total=len(items),
        correct=Decimal(accuracy) * len(items),
        accuracy=Decimal(accuracy),
        detail={"items": items, "pendingCount": 0},
        triggered_by=None,
        config={"perspective": perspective} if perspective else {},
        created_at=_CLOCK.now(),
    )
    session.add(run)
    session.flush()
    return run


def test_byTier分层与vsPrevious环比(session: Session) -> None:
    """① official 两题对一错（0.5）、extended 一题全对（1.0）；第二轮同视角报告带
    vsPrevious（前轮 id/accuracy/delta）。"""
    q1 = _add_question(session, "题一", tier="official")
    q2 = _add_question(session, "题二", tier="official")
    q3 = _add_question(session, "题三", tier="extended")
    first = _make_run(
        session,
        "首轮",
        perspective="service",
        accuracy="0.5",
        items=[_item(q1, "correct"), _item(q2, "wrong"), _item(q3, "correct")],
    )

    report = build_report(session, first)
    tiers = {entry["tier"]: entry for entry in report["byTier"]}
    assert tiers["official"]["total"] == 2
    assert tiers["official"]["accuracy"] == 0.5
    assert tiers["extended"]["total"] == 1
    assert tiers["extended"]["accuracy"] == 1.0
    assert report["vsPrevious"] is None  # 无前轮

    second_items = [
        _item(q1, "correct"),
        _item(q2, "correct"),
        _item(q3, "wrong"),
    ]
    second = _make_run(
        session,
        "次轮",
        perspective="service",
        accuracy="0.6667",
        items=second_items,
    )
    report2 = build_report(session, second)
    vs = report2["vsPrevious"]
    assert vs is not None
    assert vs["previousRunId"] == first.id
    assert abs(vs["previousAccuracy"] - 0.5) < 1e-4
    assert abs(vs["delta"] - (2 / 3 - 0.5)) < 1e-2


def test_旧run无perspective不参与环比(session: Session) -> None:
    """② config 无 perspective（存量人工 run）：不作为前轮，vsPrevious=null。"""
    q1 = _add_question(session, "环比题一")
    _make_run(session, "旧人工轮", perspective=None, accuracy="0.9", items=[_item(q1, "wrong")])
    current = _make_run(
        session, "新自动轮", perspective="service", accuracy="0.8", items=[_item(q1, "correct")]
    )

    report = build_report(session, current)

    assert report["vsPrevious"] is None


def test_mark_regression超阈值写detail(session: Session) -> None:
    """③ 降幅 ≥ 阈值：detail.regression 落 {previousRunId, previousAccuracy, delta}。"""
    q1 = _add_question(session, "退化题")
    prev = _make_run(
        session, "前轮", perspective="hr", accuracy="0.8", items=[_item(q1, "correct")]
    )
    current = _make_run(
        session, "本轮", perspective="hr", accuracy="0.7", items=[_item(q1, "wrong")]
    )

    mark = mark_regression(session, current, threshold_pp=5.0)

    assert mark is not None
    assert mark["previousRunId"] == prev.id
    assert abs(mark["previousAccuracy"] - 0.8) < 1e-4
    assert abs(mark["delta"] - (-0.1)) < 1e-4
    session.expire_all()
    session.refresh(current)
    assert current.detail["regression"]["delta"] == mark["delta"]


def test_mark_regression阈值内不写(session: Session) -> None:
    """④ 降幅小于阈值（或上升）：返回 None 且不写 detail。"""
    q1 = _add_question(session, "平稳题")
    _make_run(session, "前轮平稳", perspective="all", accuracy="0.8", items=[_item(q1, "wrong")])
    current = _make_run(
        session, "本轮平稳", perspective="all", accuracy="0.9", items=[_item(q1, "correct")]
    )

    assert mark_regression(session, current, threshold_pp=5.0) is None
    session.expire_all()
    session.refresh(current)
    assert (current.detail or {}).get("regression") is None
