r"""评测分组报告聚合（Task 7，F-603）：从 eval_runs.detail 逐题明细纯读聚合成
F-603 报告 DTO，不 commit（事务边界归调用方，路由只读）。

合并口径（单一来源 eval_service.final_judgment，报告不自定口径）：每题最终判定 =
人工 judgment（若有）否则 autoJudgment；correct 按 判定协议第 1 条折算（correct=1
/ partial=0.5 / wrong=0，pending 计 0）——拒答题未被拒（autoJudgment='wrong'）无
人工判计 0，auto correct 被人工判 wrong 计 0；pendingCount=人工与 auto 均未判的
题数（录分时 detail.pendingCount 同口径刷新）。byDept 按题面 dept_id →
departments.code（题面部门只进报告口径、不参与检索，F-502 同源）；byQtype 按
qtype；分组键均按字典序输出（钉死顺序可精确断言），组内 accuracy=组内加权
correct/组内 total（total=0 记 0）。totals 从 detail 聚合而非直读 run 列——录分
重算走同一 final_judgment，两处数字一致、报告自洽。

四指标与分型分组（metrics-balance 工单 07，spec §2/§4.4）：fourMetrics 每项
{value, evaluatedCount}，null 排除集均分——contextRecall/contextPrecision/
answerRelevancy 直读 detail.metrics（机检与 judge 折算在引擎落库）；answerCorrectness
走 final_judgment 合并口径（judge 预填 autoJudgment、人工仲裁优先，pending 与拒答类
不计）。byDocType 分组键=题目 sourceDocId 指向文档的 doc_type（documents 一次 in_
回查）；sourceDocId 为 NULL 的题入 unassigned（未标注）组——只显 questionCount、
四指标 null，但其 relevancy/precision/correctness 仍计入全局（recall null 自然排除），
§4.4 双口径。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Department, Document, EvalRun
from app.services.eval_service import JUDGMENT_SCORES, REJECT_MARK, final_judgment

# 四指标键与输出顺序（spec §4.4 冻结：Recall/Precision/Relevancy/Correctness）
_FOUR_METRICS: tuple[str, ...] = (
    "contextRecall",
    "contextPrecision",
    "answerRelevancy",
    "answerCorrectness",
)
_DOC_TYPE_UNASSIGNED = "unassigned"  # source_doc_id 为 NULL 的未标注组（§4.4）


@dataclass
class _Group:
    """分组累加器：correct 加权总分（Decimal 精确累加 0.5 不漂）、total 题数。"""

    correct: Decimal = Decimal(0)
    total: int = 0

    def add(self, item: dict) -> None:
        self.correct += JUDGMENT_SCORES[final_judgment(item)]
        self.total += 1

    def to_dto(self, key: str, value: str) -> dict:
        """分组项 DTO：accuracy=correct/total（total=0 记 0——防御分支，正常不达）。"""
        correct = float(self.correct)
        return {
            key: value,
            "total": self.total,
            "accuracy": correct / self.total if self.total else 0.0,
        }


@dataclass
class _MetricStat:
    """四指标累加器（§4.4）：null 排除后集均分，evaluatedCount=参算题数。"""

    total: float = 0.0
    count: int = 0

    def add(self, value: float | None) -> None:
        if value is not None:
            self.total += value
            self.count += 1

    def to_dto(self) -> dict:
        return {
            "value": self.total / self.count if self.count else None,
            "evaluatedCount": self.count,
        }


def build_report(db: Session, run: EvalRun) -> dict:
    """F-603 报告 DTO：{config, status, totals, byDept, byQtype, fourMetrics, byDocType}
    （§3.4 camelCase）。

    明细从 detail 聚合（合并口径见模块 docstring）；config/status 原样透传
    （running/failed 跑分也可出报告——部分明细 + 真实状态，不做状态闸）。
    """
    items = (run.detail or {}).get("items", [])
    codes = _dept_codes(db, {item["deptId"] for item in items})
    doc_types = _doc_types(db, _source_doc_ids(items))
    overall = _Group()
    by_dept: dict[str, _Group] = {}
    by_qtype: dict[str, _Group] = {}
    overall_metrics: dict[str, _MetricStat] = {name: _MetricStat() for name in _FOUR_METRICS}
    dt_counts: dict[str, int] = {}
    dt_metrics: dict[str, dict[str, _MetricStat]] = {}
    for item in items:
        overall.add(item)
        by_dept.setdefault(codes[item["deptId"]], _Group()).add(item)
        by_qtype.setdefault(item["qtype"], _Group()).add(item)
        values = _metric_values(item)
        for name, value in values.items():
            overall_metrics[name].add(value)
        doc_type = (
            _DOC_TYPE_UNASSIGNED
            if item.get("sourceDocId") is None
            else doc_types[item["sourceDocId"]]
        )
        dt_counts[doc_type] = dt_counts.get(doc_type, 0) + 1
        if doc_type != _DOC_TYPE_UNASSIGNED:  # unassigned 组只显题数，四指标恒 null（§4.4）
            stats = dt_metrics.setdefault(doc_type, {name: _MetricStat() for name in _FOUR_METRICS})
            for name, value in values.items():
                stats[name].add(value)
    return {
        "config": run.config,
        "status": run.status,
        "totals": {
            "total": overall.total,
            "correct": float(overall.correct),
            "accuracy": float(overall.correct) / overall.total if overall.total else 0.0,
            "pendingCount": sum(1 for item in items if final_judgment(item) == "pending"),
        },
        "byDept": [by_dept[code].to_dto("deptCode", code) for code in sorted(by_dept)],
        "byQtype": [by_qtype[qtype].to_dto("qtype", qtype) for qtype in sorted(by_qtype)],
        "fourMetrics": {name: overall_metrics[name].to_dto() for name in _FOUR_METRICS},
        "byDocType": [
            _doc_type_dto(doc_type, dt_counts, dt_metrics) for doc_type in sorted(dt_counts)
        ],
    }


def _metric_values(item: dict) -> dict[str, float | None]:
    """单题四指标取值：机检与 judge 值直读 detail.metrics（工单 07 前的旧明细缺键
    记 null）；answerCorrectness 走 final_judgment 合并口径（人工优先），拒答类与
    pending 不计（None → null 排除集均分）。"""
    metrics: dict[str, Any] = item.get("metrics") or {}
    correctness = None
    if item["expected"] != REJECT_MARK and item["qtype"] != "cross_reject":
        judgment = final_judgment(item)
        if judgment != "pending":
            correctness = float(JUDGMENT_SCORES[judgment])
    return {
        "contextRecall": metrics.get("recall"),
        "contextPrecision": metrics.get("contextPrecision"),
        "answerRelevancy": metrics.get("relevancy"),
        "answerCorrectness": correctness,
    }


def _source_doc_ids(items: list[dict]) -> set[int]:
    """detail 里出现过的 sourceDocId 全集（旧明细缺键容忍）；FK 保证文档行存在。"""
    return {item["sourceDocId"] for item in items if item.get("sourceDocId") is not None}


def _doc_type_dto(
    doc_type: str, counts: dict[str, int], metrics: dict[str, dict[str, _MetricStat]]
) -> dict:
    """分型分组项：unassigned 只显 questionCount（四指标 null），其余组内同口径集均分。"""
    entry: dict = {"docType": doc_type, "questionCount": counts[doc_type]}
    if doc_type == _DOC_TYPE_UNASSIGNED:
        entry.update({name: None for name in _FOUR_METRICS})
    else:
        entry.update({name: metrics[doc_type][name].to_dto()["value"] for name in _FOUR_METRICS})
    return entry


def _dept_codes(db: Session, dept_ids: set[int]) -> dict[int, str]:
    """题面 dept_id → departments.code：FK 保证行存在，detail 键存 id 报告出 code。"""
    rows = db.execute(
        select(Department.id, Department.code).where(Department.id.in_(dept_ids))
    ).all()
    return dict(rows)


def _doc_types(db: Session, doc_ids: set[int]) -> dict[int, str]:
    """source_doc_id → documents.doc_type（分型分组键，§4.4）：一次 in_ 批量回查。"""
    if not doc_ids:
        return {}
    rows = db.execute(select(Document.id, Document.doc_type).where(Document.id.in_(doc_ids))).all()
    return dict(rows)
