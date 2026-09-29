r"""阈值扫描向导（W4 Task 5，F-1012 简化版）：对 done 跑分的 detail 快照离线重放
不同阈值下的命中/拒答，出曲线与推荐值——不调 LLM、不触检索、不改线上阈值，纯内存
毫秒级，端点同步返回。为何独立成文件：eval_service.py 288 行余量不足，且重放是
无 IO 纯计算域（与导入/录分的事务业务关注点不同，eval_judge 迁出同理）。

重放口径（Task 5 钉死）：参与重放的题 = detail.items 中非 error 且最终判定非
pending 的题（final_judgment 合并口径，人工 judgment 优先否则 autoJudgment，与
report 同源）。按 answerType 分三类：
1. refuse：citations 为空、无 doc 分可对照，钉死为每档都判"拒答"（其原始判定即
   按已拒答给出）；参与拒答曲线。
2. rag：拒答判据分 = citations 中 refType='doc' 的最大 score（rerank sigmoid
   分），score < 档位阈值 → 该档判"拒答"——与线上 refuse 分支 `top1 分 <
   refuse_threshold 即拒`（services/chat.prepare_turn）同序；参与拒答曲线。
3. qa：citations 是 qa 引用（无 doc 分）→ 不参与拒答曲线（scope.qaTotal 计数
   注明）；仅参与 qa 曲线：refType='qa' 的 score（embedding 余弦）≥ 档位阈值
   即命中（与 match_qa `≥ threshold` 同序），统计各档仍命中的比例。

逐档判定权重（accuracy 与 report 同折算 JUDGMENT_SCORES，正确 1/部分 0.5/错 0）：
- 拒答期望题（expected=='[拒答]' 或 qtype=='cross_reject'，取或防御手工造数，
  _auto_judge 同口径）：该档判"拒答"= 1（判对）、判"回答"= 0（判错）——重放问
  的是"该阈值下是否给出期望的拒答行为"，不采人工覆写（否则串库漏判档档 0 分，
  抬阈值永无翻正档，曲线失义）。
- 非拒答题：判"拒答"= 0（该答的题拒了，反向必错）、判"回答"= final_judgment
  折算——其原始判定是对实际给出的回答打的，作为重放回答的得分估计。

qa 曲线口径局限（scope.note 注明）：只统计 qa 题各档位仍命中的比例（档位敏感
度），不含 rag 题反证数据——若降档会把多少本应走 rag 的问题误引入 qa 通道，需
rag 题曾见的 qa 匹配分，而 detail 快照只存最终 citations，rag 题的该分不在其中
（快照天然边界）。需反证数据须在跑分引擎侧另存。

推荐规则（钉死）：refuseThreshold = accuracy 最高的档，并列取更低阈值（同等
准确度下更倾向回答、少拒）；qaThreshold 同规则作用于 hitRate（同等命中面下更
宽容）。结果存 detail.thresholdScan 可追溯（重复扫描覆盖），审计
eval.threshold_scan 与业务写入同事务（detail 读改写单 commit，先例
submit_judgments）。
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.clock import get_clock
from app.core.errors import ERR_EVAL_STATE, ERR_NOT_FOUND, ApiError
from app.db.models import EvalRun
from app.services.audit import record_audit
from app.services.eval_service import (
    JUDGMENT_SCORES,
    REJECT_MARK,
    RUN_NOT_FOUND_MSG,
    final_judgment,
)

REFUSE_LEVELS: list[float] = [i / 100 for i in range(40, 71, 5)]  # 0.40-0.70 步长 0.05（7 档）
QA_LEVELS: list[float] = [i / 100 for i in range(75, 96, 5)]  # 0.75-0.95 步长 0.05（5 档）

_MSG_NOT_DONE: str = "评测任务未完成，暂不能扫描阈值"  # 仅 done 收口可扫（与录分同闸）
_MSG_NO_REPLAY: str = "无可重放题目：明细全部待人工评判或执行失败"
_QA_LIMIT_NOTE: str = (
    "qa 曲线仅统计 qa 题各档位仍命中的比例（档位敏感度），不含 rag 题反证数据："
    "detail 快照未存 rag 题曾见的 qa 匹配分，无法重放'降档会误入 qa 通道'的风险。"
)


def run_threshold_scan(db: Session, run_id: int, actor_user_id: int) -> dict:
    """扫描入口：状态闸（404/422）→ 纯内存重放出曲线与推荐 → detail 读改写单
    commit（审计 eval.threshold_scan 同事务），返回存库的扫描结果。

    仅 status='done' 可扫（running 时后台任务正逐题写 detail，读改写互相覆盖，
    与录分同闸）；明细无可重放题（全部待判/失败）422 指名。曲线为空的一侧推荐
    值为 None（该侧无参与者，不造数据）。
    """
    run = db.get(EvalRun, run_id)
    if run is None:
        raise ApiError(ERR_NOT_FOUND, RUN_NOT_FOUND_MSG, 404)
    if run.status != "done":
        raise ApiError(ERR_EVAL_STATE, _MSG_NOT_DONE, 422)
    items: list[dict] = (run.detail or {}).get("items", [])
    refuse_items, qa_items, pending_n, error_n = _partition(items)
    if not refuse_items and not qa_items:
        raise ApiError(ERR_EVAL_STATE, _MSG_NO_REPLAY, 422)
    refuse_curve = _refuse_curve(refuse_items)
    qa_curve = _qa_curve(qa_items)
    scan = {
        "refuseCurve": refuse_curve,
        "qaCurve": qa_curve,
        "recommended": {
            "refuseThreshold": _recommend(refuse_curve, "accuracy"),
            "qaThreshold": _recommend(qa_curve, "hitRate"),
        },
        "scannedAt": get_clock().now().isoformat(),
        "scope": {
            "refuseTotal": len(refuse_items),
            "qaTotal": len(qa_items),
            "pendingSkipped": pending_n,
            "errorSkipped": error_n,
            "note": _QA_LIMIT_NOTE,
        },
    }
    detail = dict(run.detail) if run.detail else {"items": []}
    detail["thresholdScan"] = scan
    run.detail = detail  # 重赋值：JSONB 原地改动不被变更跟踪（submit_judgments 同注）
    record_audit(db, actor_user_id, "eval.threshold_scan", "eval_run", run.id, {"runId": run.id})
    db.commit()
    return scan


def _partition(items: list[dict]) -> tuple[list[dict], list[dict], int, int]:
    """明细分区：refuse/rag → 拒答曲线参与者、qa → qa 曲线参与者；error 先判
    （error 题最终判定必 pending，计 error 不重复计 pending）、最终判定 pending
    次之；其余 answerType（正常不达）防御性跳过不入任何计数。"""
    refuse_items: list[dict] = []
    qa_items: list[dict] = []
    pending_n = error_n = 0
    for item in items:
        if item.get("error"):
            error_n += 1
        elif final_judgment(item) == "pending":
            pending_n += 1
        elif item["answerType"] in ("refuse", "rag"):
            refuse_items.append(item)
        elif item["answerType"] == "qa":
            qa_items.append(item)
    return refuse_items, qa_items, pending_n, error_n


def _is_reject(item: dict) -> bool:
    """拒答期望题：期望答案 `[拒答]` 或题型 cross_reject（取或，_auto_judge 同口径）。"""
    return item["expected"] == REJECT_MARK or item["qtype"] == "cross_reject"


def _doc_score(item: dict) -> float | None:
    """拒答判据分：citations 中 doc 块最大 rerank 分；无 doc 引用（refuse 题或
    异常空引用）返回 None，按"每档都拒"处理。"""
    scores = [c["score"] for c in item["citations"] if c.get("refType") == "doc"]
    return max(scores) if scores else None


def _would_refuse(item: dict, threshold: float) -> bool:
    """线上 refuse 分支同序：判据分 < 档位阈值 → 拒；无分（None）视作拒。"""
    score = _doc_score(item)
    return score is None or score < threshold


def _replay_weight(item: dict, threshold: float) -> Decimal:
    """单题单档重放权重（规则见模块 docstring）：拒答题拒=1/答=0；非拒答题
    拒=0/答=final_judgment 折算。Decimal 精确累加 0.5 不漂（report 同注）。"""
    if _would_refuse(item, threshold):
        return Decimal(1) if _is_reject(item) else Decimal(0)
    return Decimal(0) if _is_reject(item) else JUDGMENT_SCORES[final_judgment(item)]


def _refuse_curve(items: list[dict]) -> list[dict]:
    """拒答曲线 7 档：accuracy=重放权重均值（分母=参与者数）、refuseRate=判拒
    比例；无参与者返回空表（调用方按空曲线处理，不造 0 值档）。"""
    total = len(items)
    if not total:
        return []
    return [
        {
            "threshold": threshold,
            "accuracy": float(sum(_replay_weight(item, threshold) for item in items) / total),
            "refuseRate": sum(1 for item in items if _would_refuse(item, threshold)) / total,
        }
        for threshold in REFUSE_LEVELS
    ]


def _qa_score(item: dict) -> float:
    """qa 命中分：citations 中 qa 引用 score（qa 题恰 1 条，max 防御异常多引；
    无 qa 引用按 0 分处理即档档不命中）。"""
    scores = [c["score"] for c in item["citations"] if c.get("refType") == "qa"]
    return max(scores) if scores else 0.0


def _qa_curve(items: list[dict]) -> list[dict]:
    """qa 曲线 5 档：hitRate = score ≥ 档位阈值（match_qa 同序）的比例——仅
    档位敏感度，口径局限见模块 docstring 与 scope.note；无参与者返回空表。"""
    total = len(items)
    if not total:
        return []
    return [
        {
            "threshold": threshold,
            "hitRate": sum(1 for item in items if _qa_score(item) >= threshold) / total,
        }
        for threshold in QA_LEVELS
    ]


def _recommend(curve: list[dict], metric: str) -> float | None:
    """推荐档：metric 最高的档，并列取更低阈值（同等得分更倾向回答/更宽容）；
    空曲线（该侧无参与者）→ None。档位升序遍历保证并列时首个即最低档。"""
    if not curve:
        return None
    best = max(entry[metric] for entry in curve)
    return min(entry["threshold"] for entry in curve if entry[metric] == best)
