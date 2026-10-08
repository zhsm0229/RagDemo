r"""eval_report 四指标与分型分组单元测试（metrics-balance 工单 07，spec §4.4）。

合成 detail 逐题条目（新键 metrics/sourceDocId/judgeError + 旧跑分缺键形态），
db 缝（_dept_codes/_doc_types 两次 in_ 查询）以 monkeypatch 替身固定返回——聚合
逻辑纯读可独立验证；端到端透出读回见 tests/api/test_admin_eval_report.py。
冻结口径：fourMetrics 每项 {value, evaluatedCount}、null 排除集均分；answerCorrectness
走 final_judgment 合并口径（judge 预填 autoJudgment、人工判优先，pending 不计）；
拒答类不计入四指标；unassigned（sourceDocId 缺失/None）组只显 questionCount、
四指标 null，但成员值仍计入全局；既有 totals/byDept/byQtype 口径零变化。
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import eval_report

# 常用 metrics 形态缩写（五键与引擎落库一致，citationsOk 不入 fourMetrics）
_M = lambda recall, precision, relevancy, correctness: {
    "recall": recall,
    "citationsOk": True,
    "contextPrecision": precision,
    "relevancy": relevancy,
    "correctness": correctness,
}


def _item(
    question_id: int,
    *,
    qtype: str = "factual",
    expected: str = "标准答案",
    metrics: dict | None = None,
    source_doc_id: int | None = 11,
    auto: str = "pending",
    judgment: str | None = None,
) -> dict:
    """合成逐题条目：source_doc_id=None 时不落 sourceDocId 键（旧跑分容忍形态同理）。"""
    item: dict = {
        "questionId": question_id,
        "deptId": 1,
        "qtype": qtype,
        "expected": expected,
        "answerType": "rag",
        "answer": "答案 [1]",
        "citations": [],
        "retrieval": None,
        "metrics": metrics,
        "autoJudgment": auto,
        "judgeError": None,
        "error": None,
    }
    if source_doc_id is not None:
        item["sourceDocId"] = source_doc_id
    if judgment is not None:
        item["judgment"] = judgment
    return item


def _build(items: list[dict]) -> dict:
    """build_report 纯聚合路径：run 以 SimpleNamespace 合成，db 缝 monkeypatch 固定。"""
    run = SimpleNamespace(detail={"items": items}, config=None, status="done")
    with (
        patch.object(eval_report, "_dept_codes", return_value={1: "service"}),
        patch.object(eval_report, "_doc_types", return_value={11: "general", 12: "notice"}),
    ):
        return eval_report.build_report(None, run)  # type: ignore[arg-type]


def test_四指标_null排除集均分与参算题数() -> None:
    """两题：recall 1/0 → 0.5（count 2）；precision 一题 null → 只均 0.8（count 1）；
    relevancy 1/0.5 → 0.75；correctness 取 autoJudgment，pending 排除 → 1.0（count 1）。"""
    report = _build(
        [
            _item(1, metrics=_M(1, 0.8, 1.0, 1.0), auto="correct"),
            _item(2, metrics=_M(0, None, 0.5, None), auto="pending"),
        ]
    )

    assert report["fourMetrics"] == {
        "contextRecall": {"value": 0.5, "evaluatedCount": 2},
        "contextPrecision": {"value": pytest.approx(0.8), "evaluatedCount": 1},
        "answerRelevancy": {"value": 0.75, "evaluatedCount": 2},
        "answerCorrectness": {"value": 1.0, "evaluatedCount": 1},
    }


def test_answerCorrectness走final_judgment人工优先且pending不计() -> None:
    """合并口径钉死：auto correct 被人工判 wrong 计 0；judge 失败题（metrics 全 null）
    人工补判 wrong 计 0——人工仲裁补位 judge 缺席；pending 题排除。"""
    report = _build(
        [
            _item(1, metrics=_M(1, 0.8, 1.0, 1.0), auto="correct", judgment="wrong"),
            _item(2, metrics=_M(1, 0.6, 0.5, 0.5), auto="partial"),
            _item(3, metrics=None, auto="pending", judgment="wrong"),
            _item(4, metrics=_M(1, 0.4, 1.0, None), auto="pending"),
        ]
    )

    correctness = report["fourMetrics"]["answerCorrectness"]
    assert correctness["value"] == pytest.approx((0 + 0.5 + 0) / 3)
    assert correctness["evaluatedCount"] == 3
    # relevancy 与 correctness 分母独立：judge 失败题无 relevancy（null 排除）
    relevancy = report["fourMetrics"]["answerRelevancy"]
    assert relevancy["value"] == pytest.approx((1.0 + 0.5 + 1.0) / 3)
    assert relevancy["evaluatedCount"] == 3


def test_拒答类不计入四指标_既有字段口径零变化() -> None:
    """拒答题（auto correct）不计入任何四指标；totals/accuracy 仍按既有合并口径
    全量计入——四指标是新增维度，不动既有字段分母。"""
    report = _build(
        [
            _item(
                1,
                qtype="cross_reject",
                expected="[拒答]",
                metrics=None,
                source_doc_id=None,
                auto="correct",
            ),
            _item(2, metrics=_M(1, 0.8, 1.0, 1.0), auto="correct"),
        ]
    )

    assert report["fourMetrics"] == {
        "contextRecall": {"value": 1.0, "evaluatedCount": 1},
        "contextPrecision": {"value": pytest.approx(0.8), "evaluatedCount": 1},
        "answerRelevancy": {"value": 1.0, "evaluatedCount": 1},
        "answerCorrectness": {"value": 1.0, "evaluatedCount": 1},
    }
    assert report["totals"] == {"total": 2, "correct": 2.0, "accuracy": 1.0, "pendingCount": 0}
    assert report["byQtype"] == [
        {"qtype": "cross_reject", "total": 1, "accuracy": 1.0},
        {"qtype": "factual", "total": 1, "accuracy": 1.0},
    ]


def test_byDocType分组与unassigned组_null成员仍计入全局() -> None:
    """分组键=sourceDocId 指向文档的 doc_type（字典序）；unassigned 组只显 questionCount、
    四指标 null，但成员 relevancy/precision/correctness 仍计入全局四指标（recall null
    自然排除）——§4.4 双口径。"""
    report = _build(
        [
            # general 两题
            _item(1, source_doc_id=11, metrics=_M(1, 0.8, 1.0, 1.0), auto="correct"),
            _item(2, source_doc_id=11, metrics=_M(0, None, 0.0, None), auto="pending"),
            # notice 一题
            _item(3, source_doc_id=12, metrics=_M(1, 0.6, 0.5, 0.0), auto="wrong"),
            # 存量旧题（无来源，metrics 齐、recall null）
            _item(4, source_doc_id=None, metrics=_M(None, 0.5, 1.0, 1.0), auto="correct"),
            # 拒答题（无来源）
            _item(
                5,
                qtype="cross_reject",
                expected="[拒答]",
                metrics=None,
                source_doc_id=None,
                auto="correct",
            ),
        ]
    )

    assert report["byDocType"] == [
        {
            "docType": "general",
            "questionCount": 2,
            "contextRecall": 0.5,
            "contextPrecision": pytest.approx(0.8),
            "answerRelevancy": 0.5,
            "answerCorrectness": 1.0,
        },
        {
            "docType": "notice",
            "questionCount": 1,
            "contextRecall": 1.0,
            "contextPrecision": pytest.approx(0.6),
            "answerRelevancy": 0.5,
            "answerCorrectness": 0.0,
        },
        {
            "docType": "unassigned",
            "questionCount": 2,
            "contextRecall": None,
            "contextPrecision": None,
            "answerRelevancy": None,
            "answerCorrectness": None,
        },
    ]
    # 全局：recall 1+0+1 → 2/3（存量旧题 null 排除、拒答排除）；precision 0.8+0.6+0.5；
    # relevancy 四题；correctness 1+0+1 → 2/3（pending 与拒答排除）
    assert report["fourMetrics"] == {
        "contextRecall": {"value": pytest.approx(2 / 3), "evaluatedCount": 3},
        "contextPrecision": {"value": pytest.approx(1.9 / 3), "evaluatedCount": 3},
        "answerRelevancy": {"value": pytest.approx(2.5 / 4), "evaluatedCount": 4},
        "answerCorrectness": {"value": pytest.approx(2 / 3), "evaluatedCount": 3},
    }


def test_旧跑分detail缺新键_四指标全null且unassigned只显题数() -> None:
    """工单 07 之前的 detail（无 metrics/sourceDocId 键）容忍：四指标 null/0 分母，
    byDocType 全部落 unassigned 组；既有字段照常聚合不炸。"""
    legacy = {
        "questionId": 1,
        "deptId": 1,
        "qtype": "factual",
        "expected": "标准答案",
        "answerType": "rag",
        "answer": "答案",
        "citations": [],
        "autoJudgment": "pending",
        "error": None,
    }
    report = _build(
        [
            dict(legacy),
            {
                **legacy,
                "questionId": 2,
                "qtype": "cross_reject",
                "expected": "[拒答]",
                "autoJudgment": "correct",
            },
        ]
    )

    assert report["fourMetrics"] == {
        "contextRecall": {"value": None, "evaluatedCount": 0},
        "contextPrecision": {"value": None, "evaluatedCount": 0},
        "answerRelevancy": {"value": None, "evaluatedCount": 0},
        "answerCorrectness": {"value": None, "evaluatedCount": 0},
    }
    assert report["byDocType"] == [
        {
            "docType": "unassigned",
            "questionCount": 2,
            "contextRecall": None,
            "contextPrecision": None,
            "answerRelevancy": None,
            "answerCorrectness": None,
        }
    ]
    assert report["totals"]["total"] == 2 and report["totals"]["correct"] == 1.0


def test_空明细_四指标零分母且byDocType空数组() -> None:
    report = _build([])

    assert report["fourMetrics"] == {
        "contextRecall": {"value": None, "evaluatedCount": 0},
        "contextPrecision": {"value": None, "evaluatedCount": 0},
        "answerRelevancy": {"value": None, "evaluatedCount": 0},
        "answerCorrectness": {"value": None, "evaluatedCount": 0},
    }
    assert report["byDocType"] == []
    assert report["totals"]["total"] == 0
