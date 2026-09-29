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
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Department, EvalRun
from app.services.eval_service import JUDGMENT_SCORES, final_judgment


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


def build_report(db: Session, run: EvalRun) -> dict:
    """F-603 报告 DTO：{config, status, totals, byDept, byQtype}（§3.4 camelCase）。

    明细从 detail 聚合（合并口径见模块 docstring）；config/status 原样透传
    （running/failed 跑分也可出报告——部分明细 + 真实状态，不做状态闸）。
    """
    items = (run.detail or {}).get("items", [])
    codes = _dept_codes(db, {item["deptId"] for item in items})
    overall = _Group()
    by_dept: dict[str, _Group] = {}
    by_qtype: dict[str, _Group] = {}
    for item in items:
        overall.add(item)
        by_dept.setdefault(codes[item["deptId"]], _Group()).add(item)
        by_qtype.setdefault(item["qtype"], _Group()).add(item)
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
    }


def _dept_codes(db: Session, dept_ids: set[int]) -> dict[int, str]:
    """题面 dept_id → departments.code：FK 保证行存在，detail 键存 id 报告出 code。"""
    rows = db.execute(
        select(Department.id, Department.code).where(Department.id.in_(dept_ids))
    ).all()
    return dict(rows)
