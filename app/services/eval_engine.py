r"""评测跑分引擎（Task 6，F-602 核心）：复用问答管道对题库自动跑分并自动判分。

职责边界：本模块只含后台任务体；run 行创建与审计在 eval_service.create_eval_run。
评测专用 ChatDeps 副本（旁路缓存，EVAL-02）：随跑弃置 ExactCache 绝不污染线上问答
缓存、counter=None 不占日限。逐题：user 消息（is_eval=true 不入运营统计）→
prepare_turn 单轮无改写 → refuse 直出话术不调 LLM；qa/rag 流收集（生成器首次
next() 才抛，迭代器与迭代同一 UpstreamError 边界）；单题失败记 error 继续下一题
（整体不失败）、assistant 行一次 INSERT 完整。判分（工单 07 接线，
judge_item_with_metrics 承担）：拒答类规则自动判、metrics=null；非拒答类 judge 三判
预填 autoJudgment（失败 pending+judgeError 不炸 run、人工判优先口径不变）+ 落
detail.metrics 四指标，judge 用 build_llm(settings, 0.0) 第二实例（temperature=0
不影响主链路）。检索 trace（§4.2）= detail.retrieval；逐题 commit 的 flag_modified
缘由见循环内注释；整体异常 → failed。
事务：任务体自管短会话逐题独立 commit，绝不横跨整个跑分（连接占用红线）；测试经
session_factory 实参指向测试事务连接。检索边界恒 [视角部门, 全员]（F-502）。
"""

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.core.assembly import build_llm
from app.core.clock import get_clock
from app.core.llm import LLMProvider, UpstreamError
from app.db.base import SessionLocal
from app.db.models import Conversation, Department, EvalQuestion, EvalRun, Message, User
from app.rag.prompt import REFUSAL_TEXT
from app.services.chat import ChatDeps, TurnPlan, prepare_turn
from app.services.chat_cache import ExactCache
from app.services.eval_judge import (
    _CORRECT,
    _PENDING,
    _STUB_MODEL,
    _config_snapshot,
    judge_item_with_metrics,
    load_context_rows,
)
from app.services.retrieval import DocHit

logger = logging.getLogger(__name__)

_ALL_DEPT_CODE = "all"  # 全员部门 code（F-502：检索范围恒为 [视角部门, 全员]）
_ERROR_MAX_CHARS = 500  # detail.error 截断：留根因不吞日志，防异常串撑爆 detail


@dataclass
class _TurnAnswer:
    """单题产出：全文、引用快照、答案类型与 usage（None 记双 0，对齐 finalize_turn）。"""

    content: str
    citations: list[dict]
    answer_type: str
    usage: tuple[int, int] | None


def eval_chat_deps(deps: ChatDeps) -> ChatDeps:
    """评测专用副本：旁路缓存（随跑弃置实例）+ counter=None（不占每日限额）。"""
    return replace(deps, cache=ExactCache(get_clock(), deps.settings.cache_ttl_s), counter=None)


def run_eval_run(
    run_id: int,
    deps: ChatDeps,
    perspective_dept_id: int,
    *,
    session_factory: sessionmaker | None = None,
    judge_llm: LLMProvider | None = None,
) -> None:
    """后台任务体：逐题跑分、逐题 commit，收口 done；整体异常置 failed 不外泄。
    deps/视角部门经 add_task 实参注入（后台任务拿不到 request/app.state）；幂等闸：
    run 不存在或已收口直接返回不重跑。judge_llm 为测试缝，缺省按 spec §2 以
    build_llm(settings, 0.0) 构造评测专用第二实例（temperature=0，不影响主链路）。
    """
    factory = session_factory if session_factory is not None else SessionLocal
    eval_deps = eval_chat_deps(deps)
    judge = judge_llm if judge_llm is not None else build_llm(eval_deps.settings, 0.0)
    snapshot = _config_snapshot(eval_deps.settings, eval_deps.prompts)
    with factory() as db:
        run = db.get(EvalRun, run_id)
        if run is None or run.status != "running":
            return
        try:
            # 全员部门种子幂等存在；缺失按整体失败处理（不留 running 幽灵行）
            all_dept_id = db.scalar(select(Department.id).where(Department.code == _ALL_DEPT_CODE))
            if all_dept_id is None:
                raise RuntimeError("全员部门未播种，无法执行评测跑分")
            owner_id = run.triggered_by
            if owner_id is None:
                # 系统自动跑分（0014）无触发人：会话归属挂种子 admin（审计同款先例）
                owner_id = db.scalar(select(User.id).where(User.username == "admin"))
                if owner_id is None:
                    raise RuntimeError("系统跑分缺少会话归属用户（admin 未播种）")
            conversation = Conversation(
                user_id=owner_id,
                dept_id=perspective_dept_id,
                title=f"[评测]{run.name}",
            )
            db.add(conversation)
            # 全部 is_active 题，按 id 升序（插入序即跑分序，与 detail 逐题顺序一致）
            questions = list(
                db.scalars(
                    select(EvalQuestion)
                    .where(EvalQuestion.is_active.is_(True))
                    .order_by(EvalQuestion.id)
                ).all()
            )
            db.commit()  # 会话先落库：逐题消息随之可见
            correct, items = _run_questions(
                db, eval_deps, judge, run, conversation, all_dept_id, questions
            )
        except Exception as exc:
            logger.exception("评测跑分整体失败 run_id=%s", run_id)
            _mark_failed(db, run_id, exc)
            return
        _finish(db, run_id, snapshot, correct, len(questions), items)
        # 07 票：收口后同口径环比退化判定（config.perspective 在册才对比；
        # 阈值读 settings 实时值，写 detail.regression 供健康卡消费）
        from app.services.eval_report import mark_regression

        mark_regression(
            db,
            db.get(EvalRun, run_id),
            threshold_pp=eval_deps.settings.eval_auto_regression_pp,
        )


def _run_questions(
    db: Session,
    deps: ChatDeps,
    judge: LLMProvider,
    run: EvalRun,
    conversation: Conversation,
    all_dept_id: int,
    questions: list[EvalQuestion],
) -> tuple[int, list[dict]]:
    """逐题执行：detail.items 重赋值 + flag_modified 后即 commit（进度可查、崩溃保留
    已完成题）——仅重赋值不够，见循环内注释。"""
    correct = 0
    items: list[dict] = list(run.detail["items"]) if run.detail else []
    for question in questions:
        item = _run_one(db, deps, judge, conversation, run, all_dept_id, question)
        correct += item["autoJudgment"] == _CORRECT
        items.append(item)
        run.detail = {"items": items}
        # 重赋值的新 dict 与 ORM 记录的旧值共享同一 items 列表（expire_on_commit=False
        # 不失效重查），flush 的相等比较判"无变更"而抑制 UPDATE（首题旧值来自 DB 反
        # 序列化的独立对象故幸免）——必须 flag_modified 强制标记 dirty 才真发逐题 UPDATE
        flag_modified(run, "detail")
        db.commit()
    return correct, items


def _user_message(conversation: Conversation, user_id: int, dept_id: int, question: str) -> Message:
    """评测 user 消息：is_eval=true（F-5 提问记录/运营统计过滤位）。"""
    return Message(
        conversation_id=conversation.id,
        user_id=user_id,
        dept_id=dept_id,
        role="user",
        content=question,
        is_eval=True,
    )


def _assistant_message(
    conversation: Conversation,
    user_id: int,
    dept_id: int,
    turn: _TurnAnswer,
    model: str,
    latency_ms: int,
) -> Message:
    """评测 assistant 消息：不走占位+finalize（无断流恢复语义），一次 INSERT 完整行。"""
    prompt_tokens, completion_tokens = turn.usage if turn.usage is not None else (0, 0)
    return Message(
        conversation_id=conversation.id,
        user_id=user_id,
        dept_id=dept_id,
        role="assistant",
        content=turn.content,
        citations=turn.citations,
        answer_type=turn.answer_type,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        cache_hit=False,
        is_eval=True,
    )


def _run_one(
    db: Session,
    deps: ChatDeps,
    judge: LLMProvider,
    conversation: Conversation,
    run: EvalRun,
    all_dept_id: int,
    question: EvalQuestion,
) -> dict:
    """单题：user 消息 → prepare → 生成/拒答 → assistant 完整行一次 INSERT。
    UpstreamError：user 消息留痕、不落 assistant 行、detail 记 error（其余异常外泄走
    整体 failed）；ranked_out 收集缝见 prepare_turn（§4.2）。判分（工单 07）：
    judge_item_with_metrics 预填 autoJudgment + 落 metrics/judgeError（拒答类旁路、
    失败题恒 pending）。
    """
    # 消息行归属取会话 owner（引擎建会话时已对系统 run 做种子 admin 回落）：自动 run
    # 的 run.triggered_by=None 直写会撞 messages.user_id NOT NULL（2026-10-31 评审 P0-1）
    user_id, dept_id = conversation.user_id, conversation.dept_id
    item: dict = {
        "questionId": question.id,
        "deptId": question.dept_id,
        "qtype": question.qtype,
        "expected": question.expected_answer,
        "sourceDocId": question.source_doc_id,
        "answerType": "",
        "answer": "",
        "citations": [],
        "retrieval": None,
        "metrics": None,
        "autoJudgment": _PENDING,
        "judgeError": None,
        "error": None,
    }
    start = perf_counter()
    db.add(_user_message(conversation, user_id, dept_id, question.question))
    db.flush()
    ranked: list[tuple[DocHit, float]] = []  # 全量候选收集（spec metrics-balance §4.2）
    try:
        plan = prepare_turn(
            db, deps, dept_id, all_dept_id, question.question, [], ranked_out=ranked
        )
        item["retrieval"] = plan.retrieval_trace
        turn = _collect_answer(deps, plan)
    except UpstreamError as exc:
        db.commit()  # user 消息随失败题提交；异常已捕获，继续下一题
        item["error"] = str(exc)
        return item
    model = deps.settings.llm_model or _STUB_MODEL
    latency_ms = int((perf_counter() - start) * 1000)
    db.add(_assistant_message(conversation, user_id, dept_id, turn, model, latency_ms))
    _bypass_cache_put(deps, plan, turn, model)
    item["answerType"] = turn.answer_type
    item["answer"] = turn.content
    item["citations"] = turn.citations
    trace_ids = item["retrieval"]["contextChunkIds"] if item["retrieval"] else []
    item["autoJudgment"], item["judgeError"], item["metrics"] = judge_item_with_metrics(
        judge, question, turn.content, len(turn.citations), load_context_rows(db, trace_ids)
    )
    return item


def _bypass_cache_put(deps: ChatDeps, plan: TurnPlan, turn: _TurnAnswer, model: str) -> None:
    """qa/rag 生成结果旁路写入随跑弃置缓存（EVAL-02）：refuse/cache 不回填（无可缓存
    产物/条目本取自缓存），主缓存零污染。"""
    if plan.kind in ("qa", "rag") and plan.cache_key is not None:
        deps.cache.put(
            plan.cache_key,
            {
                "answer": turn.content,
                "citations": turn.citations,
                "answer_type": turn.answer_type,
                "model": model,
            },
        )


def _collect_answer(deps: ChatDeps, plan: TurnPlan) -> _TurnAnswer:
    """refuse 直出话术；cache 取条目；qa/rag 流收集（异常边界归调用方）。"""
    if plan.kind == "refuse":
        return _TurnAnswer(REFUSAL_TEXT, [], "refuse", None)
    if plan.kind == "cache":
        entry = plan.cache_entry or {}
        return _TurnAnswer(
            entry.get("answer", ""),
            entry.get("citations", []),
            entry.get("answer_type", "rag"),
            None,
        )
    answer = "".join(deps.llm.chat_stream(plan.llm_messages, deps.settings.chat_max_tokens))
    return _TurnAnswer(answer, plan.citations, plan.kind, getattr(deps.llm, "last_usage", None))


def _mark_failed(db: Session, run_id: int, exc: Exception) -> None:
    """整体异常收口：rollback 后重取（已 commit 的逐题 detail 保留），置 failed+error。"""
    db.rollback()
    run = db.get(EvalRun, run_id)
    detail = dict(run.detail) if run.detail else {"items": []}
    detail["error"] = str(exc)[:_ERROR_MAX_CHARS]
    run.status = "failed"
    run.detail = detail
    db.commit()


def _finish(
    db: Session,
    run_id: int,
    snapshot: dict,
    correct: int,
    total: int,
    items: list[dict],
) -> None:
    """收口置 done：total/correct/accuracy（pending 计 0、total=0 记 0）+ 快照 + pendingCount。"""
    run = db.get(EvalRun, run_id)
    pending = sum(1 for item in items if item["autoJudgment"] == _PENDING)
    run.total = total
    run.correct = correct
    run.accuracy = Decimal(correct) / Decimal(total) if total else Decimal(0)
    run.detail = {"items": items, "pendingCount": pending}
    # 合并而非覆写：预写键（自动 run 的 perspective、人工 run 的 perspective）是
    # mark_regression/vsPrevious 的取数键，被快照整体覆写即永久失效（评审 P0-1②）
    run.config = {**(run.config or {}), **snapshot}
    run.status = "done"
    db.commit()
