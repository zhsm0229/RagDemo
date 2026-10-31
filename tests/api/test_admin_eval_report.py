r"""管理端评测评判提交与分组报告（Task 7，F-602 录分 + F-603 报告）API 测试。

brief ①-⑤：批量录分后 accuracy 重算（0.5 权重：(2+0.5)/4=0.625）、judgment 持久
读回、byDept/byQtype 分组与手工计算一致（客服/HR 分开——验收硬指标口径）、running
状态录分 422、审计读回。另钉死：合并口径（最终判定=人工 judgment 优先否则
autoJudgment——auto correct 无人工判计 1、auto correct + 人工 wrong 计 0）、
pendingCount=人工与 auto 均未判的题数、分组键字典序、member 403、未知 questionId
422 指名、空 items 与非法枚举 422/1003。

造数：直调 create_eval_run + run_eval_run（TestClient 外、session_factory 指向测试
事务连接——先例 tests/integration/test_eval_engine.py；无语料 → 全部题被拒答，
factual/multi_hop 题 autoJudgment=pending、cross_reject 题 autoJudgment=correct，
正好组合出报告所需的判定分布）。认证辅助复用 test_admin_eval_import（同目录
import，不造第四份）。
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_admin_eval_import import admin_login, make_user

from app.core.assembly import load_prompt_pack
from app.core.clock import FakeClock
from app.core.config import Settings
from app.core.errors import ERR_EVAL_STATE, ERR_FORBIDDEN, ERR_NOT_FOUND, ERR_PARAM_INVALID
from app.core.llm import StubEmbedder, StubLLMProvider
from app.db.models import AuditLog, Department, Document, EvalQuestion, EvalRun, User
from app.services import eval_engine, eval_service
from app.services.chat import ChatDeps
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.retrieval import PgVectorStore
from tests.conftest import login_as

# ---------------------------------------------------------------------------
# 造数与装配助手（直调引擎先例 tests/integration/test_eval_engine.py）
# ---------------------------------------------------------------------------


def _dept_ids(session: Session) -> dict[str, int]:
    """种子三部门 code → id（seed.py：service/hr/all）。"""
    return dict(session.execute(select(Department.code, Department.id)).all())


def _admin_id(session: Session) -> int:
    return session.scalars(select(User.id).where(User.username == "evaladmin")).one()


def _add_question(
    session: Session,
    dept_id: int,
    question: str,
    expected: str,
    qtype: str,
    source_doc_id: int | None = None,
) -> int:
    eq = EvalQuestion(
        dept_id=dept_id,
        question=question,
        expected_answer=expected,
        source_ref=None,
        source_doc_id=source_doc_id,
        qtype=qtype,
    )
    session.add(eq)
    session.flush()
    return eq.id


class _NoopReranker:
    """空语料替身：检索为空 rerank 收空列表，兜底返回等长零分（防意外触网）。"""

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        return [0.0 for _ in docs]


def _make_deps() -> ChatDeps:
    """Stub 三件套 deps（无语料场景：全部题走 refuse 分支，不调 LLM）。"""
    settings = Settings(jwt_secret="eval-report-test", test_mode=True)
    clock = FakeClock("2026-10-15T09:30:00+00:00")
    return ChatDeps(
        embedder=StubEmbedder(),
        reranker=_NoopReranker(),
        llm=StubLLMProvider(),
        cache=ExactCache(clock, settings.cache_ttl_s),
        counter=DailyCounter(clock),
        hit_counter=HitCounter(clock),
        settings=settings,
        prompts=load_prompt_pack(),
        store=PgVectorStore(),
    )


def _done_run(
    session: Session, admin_id: int, specs: list[tuple[int, str, str, str]]
) -> tuple[int, list[int]]:
    """造 done 跑分：建题（specs 顺序即落库 id 序）+ create_eval_run + 直调引擎任务体
    （TestClient 外，session_factory 指向测试事务连接）；返回 (run_id, 题 id 表)。"""
    perspective = _dept_ids(session)["hr"]
    run_id = eval_service.create_eval_run(session, admin_id, f"W3 报告-{len(specs)}题", perspective)
    qids = [
        _add_question(session, dept_id, question, expected, qtype)
        for dept_id, question, expected, qtype in specs
    ]
    factory = sessionmaker(bind=session.connection(), expire_on_commit=False)
    eval_engine.run_eval_run(run_id, _make_deps(), perspective, session_factory=factory)
    session.expire_all()
    return run_id, qids


def _items_of(session: Session, run_id: int) -> dict[int, dict]:
    """detail.items 按题目 id 索引（读回断言用）。"""
    run = session.get(EvalRun, run_id)
    return {item["questionId"]: item for item in run.detail["items"]}


def _judge(client: TestClient, run_id: int, entries: list[tuple[int, str]]):  # type: ignore[no-untyped-def]
    """批量录分请求：entries 为 (questionId, judgment) 对。"""
    return client.post(
        f"/api/admin/eval-runs/{run_id}/judgments",
        json={"items": [{"questionId": q, "judgment": j} for q, j in entries]},
    )


# ---------------------------------------------------------------------------
# ① 批量录分后 accuracy 重算（0.5 权重）+ 正确数/明细读回
# ---------------------------------------------------------------------------


def test_批量录分后accuracy按半分权重重算并读回(client, session):
    """① 2 正确 1 部分 1 错 → correct=2.5、accuracy=(2+0.5)/4=0.625（判定协议第 1 条
    0.5 权重；correct 列 NUMERIC 容半分），judgment 与 pendingCount 落库读回。"""
    admin_login(client, session)
    dept = _dept_ids(session)["service"]
    specs = [(dept, f"客服事实题{i}", "标准答案", "factual") for i in range(4)]
    run_id, qids = _done_run(session, _admin_id(session), specs)
    assert all(
        _items_of(session, run_id)[q]["autoJudgment"] == "pending" for q in qids
    )  # 前置自证：无语料全拒答，非拒答题进人工队列

    resp = _judge(
        client,
        run_id,
        [(qids[0], "correct"), (qids[1], "correct"), (qids[2], "partial"), (qids[3], "wrong")],
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "done" and data["total"] == 4
    assert data["correct"] == 2.5
    assert data["accuracy"] == pytest.approx(0.625)
    assert data["pendingCount"] == 0

    session.expire_all()
    run = session.get(EvalRun, run_id)
    assert run.correct == Decimal("2.5")  # 半分精确落库（INT 列会静默舍入的回归钉）
    assert float(run.accuracy) == pytest.approx(0.625)
    saved = _items_of(session, run_id)
    assert [saved[q]["judgment"] for q in qids] == ["correct", "correct", "partial", "wrong"]
    assert run.detail["pendingCount"] == 0  # 人工全判后 pending 队列清零


# ---------------------------------------------------------------------------
# ② judgment 持久读回 + pendingCount 刷新（部分录分）
# ---------------------------------------------------------------------------


def test_部分录分judgment持久读回且pendingCount刷新(client, session):
    """② 只录 1 题正确：GET 详情读回该题 judgment、未判题无 judgment 键；
    detail.pendingCount 2→1，report totals.pendingCount 与之一致（人工已判不算）。"""
    admin_login(client, session)
    dept = _dept_ids(session)["service"]
    specs = [
        (dept, "客服事实题一", "标准答案", "factual"),
        (dept, "客服事实题二", "标准答案", "factual"),
    ]
    run_id, qids = _done_run(session, _admin_id(session), specs)

    assert _judge(client, run_id, [(qids[0], "correct")]).status_code == 200

    session.expire_all()
    detail = client.get(f"/api/admin/eval-runs/{run_id}").json()["data"]["detail"]
    items = {item["questionId"]: item for item in detail["items"]}
    assert items[qids[0]]["judgment"] == "correct"
    assert "judgment" not in items[qids[1]]  # 未录分题不落 judgment 键
    assert detail["pendingCount"] == 1

    report = client.get(f"/api/admin/eval-runs/{run_id}/report").json()["data"]
    assert report["totals"] == {"total": 2, "correct": 1.0, "accuracy": 0.5, "pendingCount": 1}


# ---------------------------------------------------------------------------
# ③ byDept/byQtype 分组手工核对 + 合并口径（人工判优先）
# ---------------------------------------------------------------------------


def _grouped_run(client: TestClient, session: Session) -> tuple[int, list[int]]:
    """③ 场景造数：客服 2 事实 + 1 拒答、HR 1 多跳 + 1 拒答，录 4 条人工评判
    （拒答 s3 留 auto）——返回 (run_id, 题id表)，含 autoJudgment 分布前置自证。"""
    admin_login(client, session)
    depts = _dept_ids(session)
    specs = [
        (depts["service"], "客服事实题A", "答案A", "factual"),
        (depts["service"], "客服事实题B", "答案B", "factual"),
        (depts["service"], "人力部门的薪酬体系是怎样的", "[拒答]", "cross_reject"),
        (depts["hr"], "人力多跳题A", "答案D", "multi_hop"),
        (depts["hr"], "其他部门全员大会时间是几点", "[拒答]", "cross_reject"),
    ]
    run_id, qids = _done_run(session, _admin_id(session), specs)
    # 前置自证：无语料全拒答 → 拒答题 auto correct、其余 pending（合并口径两侧都有）
    items = _items_of(session, run_id)
    assert [items[q]["autoJudgment"] for q in qids] == [
        "pending",
        "pending",
        "correct",
        "pending",
        "correct",
    ]
    assert (
        _judge(
            client,
            run_id,
            [(qids[0], "correct"), (qids[1], "partial"), (qids[3], "wrong"), (qids[4], "wrong")],
        ).status_code
        == 200
    )
    return run_id, qids


def test_report分组与手工计算一致_人工判优先于auto(client, session):
    """③ 客服/HR 分开分组（验收硬指标口径）：service=1+0.5+1(auto)=2.5、
    hr=0+0=0；byQtype 三组手工核对；合并口径钉死：无人工判用 autoJudgment
    （拒答题 auto correct 计 1），人工 judgment 覆写 auto（auto correct + 人工
    wrong → 计 0）。"""
    run_id, qids = _grouped_run(client, session)

    data = client.get(f"/api/admin/eval-runs/{run_id}/report").json()["data"]
    assert set(data.keys()) == {
        "config",
        "status",
        "totals",
        "byDept",
        "byQtype",
        "fourMetrics",  # metrics-balance 工单 07 计划内演进（spec §4.4/§6）
        "byDocType",
        "byTier",  # 评测集自动更新 07 票：正式/扩充分层
        "vsPrevious",  # 07 票：同口径相邻轮环比
    }
    assert data["status"] == "done"
    assert data["totals"] == {"total": 5, "correct": 2.5, "accuracy": 0.5, "pendingCount": 0}
    assert data["byDept"] == [
        {"deptCode": "hr", "total": 2, "accuracy": 0.0},
        {"deptCode": "service", "total": 3, "accuracy": pytest.approx(2.5 / 3)},
    ]
    assert data["byQtype"] == [
        {"qtype": "cross_reject", "total": 2, "accuracy": 0.5},  # s3 auto 1 + h2 人工 wrong 0
        {"qtype": "factual", "total": 2, "accuracy": 0.75},  # 1 + 0.5
        {"qtype": "multi_hop", "total": 1, "accuracy": 0.0},
    ]

    session.expire_all()
    run = session.get(EvalRun, run_id)
    assert float(run.correct) == data["totals"]["correct"]  # 报告与收口列同口径互验
    assert float(run.accuracy) == data["totals"]["accuracy"]
    assert data["config"] == run.config  # config 快照原样透传
    judged = _items_of(session, run_id)
    assert judged[qids[4]]["autoJudgment"] == "correct"  # auto 值不被改写
    assert judged[qids[4]]["judgment"] == "wrong"  # 人工判并列落 detail，报告合并时优先


# ---------------------------------------------------------------------------
# ③′ 报告端点透出 fourMetrics/byDocType（metrics-balance 工单 07，spec §4.4）
# ---------------------------------------------------------------------------

_JUDGE_NO_CTX = '{"context_relevant": [], "relevancy": 1, "correctness": "correct"}'


def _add_document(
    session: Session, dept_id: int, admin_id: int, title: str, doc_type: str
) -> Document:
    """直插就绪文档（形态同 tests/integration/test_eval_engine._add_document），分型可指定。"""
    doc = Document(
        title=title,
        dept_id=dept_id,
        original_filename=f"{title}.docx",
        file_path=f"seed/{title}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=f"report-{title}",
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        is_active=True,
        doc_type=doc_type,
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    return doc


def test_report透出fourMetrics与byDocType读回(client, session):
    """③′ 四指标（null 排除集均分）与分型分组端到端读回：judge 三判合法（全题拒答、
    0 资料块）→ relevancy/correctness 预填计分；recall 语境空转记 0（source 已标）；
    unassigned（拒答题无来源）只显题数、四指标 null。"""
    admin_login(client, session)
    admin_id = _admin_id(session)
    depts = _dept_ids(session)
    doc1 = _add_document(session, depts["hr"], admin_id, "HR_制度语料", "general")
    doc2 = _add_document(session, depts["hr"], admin_id, "HR_通知语料", "notice")
    run_id = eval_service.create_eval_run(session, admin_id, "四指标报告", depts["hr"])
    _add_question(session, depts["service"], "客服事实题一", "答案一", "factual", doc1.id)
    _add_question(session, depts["service"], "客服事实题二", "答案二", "factual", doc2.id)
    _add_question(session, depts["service"], "人力薪酬题", "[拒答]", "cross_reject")
    factory = sessionmaker(bind=session.connection(), expire_on_commit=False)
    eval_engine.run_eval_run(
        run_id,
        _make_deps(),
        depts["hr"],
        session_factory=factory,
        judge_llm=StubLLMProvider(chunks=(_JUDGE_NO_CTX,)),
    )
    session.expire_all()

    data = client.get(f"/api/admin/eval-runs/{run_id}/report").json()["data"]

    assert data["fourMetrics"] == {
        "contextRecall": {"value": 0.0, "evaluatedCount": 2},  # 拒答空转：出处不在空 context
        "contextPrecision": {"value": None, "evaluatedCount": 0},  # 无 chunk context 可评
        "answerRelevancy": {"value": 1.0, "evaluatedCount": 2},
        "answerCorrectness": {"value": 1.0, "evaluatedCount": 2},  # judge 预填，拒答类排除
    }
    assert data["byDocType"] == [
        {
            "docType": "general",
            "questionCount": 1,
            "contextRecall": 0.0,
            "contextPrecision": None,
            "answerRelevancy": 1.0,
            "answerCorrectness": 1.0,
        },
        {
            "docType": "notice",
            "questionCount": 1,
            "contextRecall": 0.0,
            "contextPrecision": None,
            "answerRelevancy": 1.0,
            "answerCorrectness": 1.0,
        },
        {
            "docType": "unassigned",
            "questionCount": 1,
            "contextRecall": None,
            "contextPrecision": None,
            "answerRelevancy": None,
            "answerCorrectness": None,
        },
    ]
    # 既有字段零变化：judge 预填 correct 并入既有 accuracy 口径（3 题全对）
    assert data["totals"] == {"total": 3, "correct": 3.0, "accuracy": 1.0, "pendingCount": 0}
    assert data["byQtype"] == [
        {"qtype": "cross_reject", "total": 1, "accuracy": 1.0},
        {"qtype": "factual", "total": 2, "accuracy": 1.0},
    ]


# ---------------------------------------------------------------------------
# ④ running 状态录分 422 + 未知题目指名 + 寻址 404
# ---------------------------------------------------------------------------


def test_running状态录分422_未知题目指名与寻址404(client, session):
    """④ running 录分 422/3004（后台任务正逐题写 detail，并发读改写互覆）且零审计；
    questionId 不在明细 422/1003 指名题号；录分与报告寻址不存在的 run 404/3003。"""
    admin_login(client, session)
    admin_id = _admin_id(session)
    dept = _dept_ids(session)["service"]
    qid = _add_question(session, dept, "客服事实题", "标准答案", "factual")
    running_id = eval_service.create_eval_run(session, admin_id, "未收口任务", dept)

    resp = _judge(client, running_id, [(qid, "correct")])

    assert resp.status_code == 422
    assert resp.json() == {
        "code": ERR_EVAL_STATE,
        "message": "评测任务未完成，暂不能录分",
        "data": None,
    }

    done_id, _ = _done_run(session, admin_id, [(dept, "客服事实题二", "标准答案", "factual")])
    unknown = _judge(client, done_id, [(999999, "correct")])
    assert unknown.status_code == 422
    assert unknown.json()["code"] == ERR_PARAM_INVALID
    assert "999999" in unknown.json()["message"]

    missing_judge = _judge(client, 999999, [(1, "correct")])
    assert missing_judge.status_code == 404
    assert missing_judge.json()["code"] == ERR_NOT_FOUND
    missing_report = client.get("/api/admin/eval-runs/999999/report")
    assert missing_report.status_code == 404
    assert missing_report.json()["code"] == ERR_NOT_FOUND

    assert session.scalars(select(AuditLog).where(AuditLog.action == "eval.judge")).all() == []


# ---------------------------------------------------------------------------
# ⑤ 录分审计读回
# ---------------------------------------------------------------------------


def test_录分审计读回(client, session):
    """⑤ 提交 3 条评判：审计 eval.judge {runId, judged}，object_type=eval_run、
    object_id=run id、actor=评判 admin，与业务写入同事务落库。"""
    admin_login(client, session)
    admin_id = _admin_id(session)
    dept = _dept_ids(session)["service"]
    specs = [(dept, f"客服事实题{i}", "标准答案", "factual") for i in range(3)]
    run_id, qids = _done_run(session, admin_id, specs)

    assert (
        _judge(
            client, run_id, [(qids[0], "correct"), (qids[1], "partial"), (qids[2], "wrong")]
        ).status_code
        == 200
    )

    session.expire_all()
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "eval.judge"))
    assert audit is not None
    assert audit.object_type == "eval_run"
    assert audit.object_id == run_id
    assert audit.detail == {"runId": run_id, "judged": 3}
    assert audit.actor_user_id == admin_id


# ---------------------------------------------------------------------------
# ⑥ 权限门：member 403；⑦ 空 items / 非法枚举 422
# ---------------------------------------------------------------------------


def test_成员角色录分与报告_403(client, session):
    """member：录分与报告均 403/2004（AdminUser 门，路由参数校验前先挡）。"""
    make_user(session, "evalmember", "right-one", role="member")
    assert login_as(client, "evalmember", "right-one").status_code == 200

    resp = _judge(client, 1, [(1, "correct")])
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
    assert client.get("/api/admin/eval-runs/1/report").status_code == 403


def test_空录分列表与非法枚举_422参数校验(client, session):
    """空 items（无意义且会写审计）与 judgment 枚举外取值：Pydantic 422/1003，
    不触达 service。"""
    admin_login(client, session)

    empty = client.post("/api/admin/eval-runs/999999/judgments", json={"items": []})
    assert empty.status_code == 422
    assert empty.json()["code"] == ERR_PARAM_INVALID

    bad_enum = client.post(
        "/api/admin/eval-runs/999999/judgments",
        json={"items": [{"questionId": 1, "judgment": "excellent"}]},
    )
    assert bad_enum.status_code == 422
    assert bad_enum.json()["code"] == ERR_PARAM_INVALID
