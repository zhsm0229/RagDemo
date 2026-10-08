r"""评测跑分引擎集成测试（Task 6，F-602 核心）：真 pg + Stub 三件套，brief ①-⑦ 逐条。

合成 4 题库：客服事实题×2（密码重置/差旅报销，检索必命中→被回答→pending）、
`[拒答]` 拒答题、cross_reject 串库题（无命中内容→被拒→correct）；题面挂 service
部门、perspective=hr 触发——检索边界按视角部门 [hr, all]，语料块播种在 hr 部门下
（裁决：视角部门决定检索过滤，题面 dept_id 只进 byDept 报告口径，不参与检索）。

替身：_MarkerReranker 按 marker 在 query 与 doc 双出现注分（高分 sigmoid(3.0)≥0.55
放行、低分 sigmoid(-8)<0.55 必拒），与召回排序解耦、确定性可复现；_BreakOneLLM 仅
对指定题注入流中断裂（产出一段后 UpstreamError(broken=True)，Task 4 交接的断流形态）。

跑分执行路径两路都测：直接调 run_eval_run（session_factory 指向测试事务连接，对齐
tests/api/conftest chat_client 的 SessionLocal 替换先例）；API 接线（POST 202/审计/
GET 列表详情/后台任务同步执行）走 eval_client。认证辅助 make_user/login/admin_login
与 tests/api 两份重复——跨目录 import 依赖收集顺序不可靠，暂复制最小份；第三份出现
时上收（rider③ 备注）。
"""

import os
from datetime import date
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.assembly import load_prompt_pack
from app.core.clock import FakeClock
from app.core.config import Settings, get_settings
from app.core.errors import ERR_NOT_FOUND, ApiError
from app.core.llm import StubEmbedder, StubLLMProvider, UpstreamError, sigmoid
from app.db.models import (
    AuditLog,
    Chunk,
    Conversation,
    Department,
    Document,
    EvalQuestion,
    EvalRun,
    Message,
    QaPair,
    User,
)
from app.rag.prompt import REFUSAL_TEXT
from app.services import eval_engine, eval_service
from app.services.chat import ChatDeps
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.retrieval import PgVectorStore
from tests.conftest import login_as

# ---------------------------------------------------------------------------
# 种子与装配助手（ORM 直插，先例 tests/integration/test_chat_flow.py）
# ---------------------------------------------------------------------------

_FACTUAL_1 = "员工如何重置门户登录密码？"
_FACTUAL_2 = "差旅费报销标准是多少？"
_REJECT_1 = "人力部门的薪酬体系是怎样的？"
_REJECT_2 = "其他部门全员大会时间是几点？"


def _dept_ids(session: Session) -> dict[str, int]:
    """种子三部门 code → id（seed.py：service/hr/all）。"""
    return dict(session.execute(select(Department.code, Department.id)).all())


def _admin_id(session: Session) -> int:
    return session.execute(select(User.id).where(User.username == "admin")).scalar_one()


def _add_document(session: Session, dept_id: int, admin_id: int, title: str) -> Document:
    doc = Document(
        title=title,
        dept_id=dept_id,
        original_filename=f"{title}.docx",
        file_path=f"seed/{title}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=f"eval-{title}",
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        is_active=True,
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    return doc


def _add_chunk(session: Session, doc: Document, content: str, embedder: StubEmbedder) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=0,
        page_no=None,
        embedding=embedder.embed([content])[0],
    )
    session.add(chunk)
    session.flush()
    return chunk


def _add_question(
    session: Session,
    dept_id: int,
    question: str,
    expected: str,
    qtype: str,
    source_doc_id: int | None = None,
) -> EvalQuestion:
    """按导入归一后的落库形态直插评测题（expected=[拒答] ⇔ qtype=cross_reject）。"""
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
    return eq


def _add_qa(
    session: Session, dept_id: int, admin_id: int, question: str, answer: str, embedder
) -> QaPair:
    """直插问答对（形态同 tests/integration/test_chat_flow.py 的 _add_qa）。"""
    qa = QaPair(
        dept_id=dept_id,
        question=question,
        answer=answer,
        embedding=embedder.embed([question])[0],
        status="enabled",
        created_by=admin_id,
    )
    session.add(qa)
    session.flush()
    return qa


def _seed_bank(session: Session, admin_id: int, embedder: StubEmbedder) -> dict[str, int]:
    """hr 视角可见语料：两块必命中文本（marker 与事实题问题双出现），返回块 id 表。"""
    doc = _add_document(session, _dept_ids(session)["hr"], admin_id, "HR_共享客服语料")
    chunk_a = _add_chunk(
        session, doc, "登录密码重置：登录页点击忘记密码，验证手机号后重置。", embedder
    )
    chunk_b = _add_chunk(session, doc, "差旅报销：交通费与住宿费凭票据实报销。", embedder)
    return {"chunk_a": chunk_a.id, "chunk_b": chunk_b.id}


def _seed_questions(session: Session) -> list[EvalQuestion]:
    """合成 4 题库（service 部门）：事实×2 + 拒答×2，插入序即跑分序。"""
    dept_id = _dept_ids(session)["service"]
    return [
        _add_question(session, dept_id, _FACTUAL_1, "登录页点击忘记密码后重置。", "factual"),
        _add_question(session, dept_id, _FACTUAL_2, "交通费凭票据实报销。", "factual"),
        _add_question(session, dept_id, _REJECT_1, "[拒答]", "cross_reject"),
        _add_question(session, dept_id, _REJECT_2, "[拒答]", "cross_reject"),
    ]


def _make_deps(embedder: StubEmbedder, reranker, llm) -> ChatDeps:
    settings = Settings(jwt_secret="eval-engine-test", test_mode=True)
    clock = FakeClock("2026-10-15T09:30:00+00:00")
    return ChatDeps(
        embedder=embedder,
        reranker=reranker,
        llm=llm,
        cache=ExactCache(clock, settings.cache_ttl_s),  # 主缓存引用：⑤ 断言其不被污染
        counter=DailyCounter(clock),
        hit_counter=HitCounter(clock),
        settings=settings,
        prompts=load_prompt_pack(),
        store=PgVectorStore(),
    )


def _new_run(session: Session, admin_id: int, name: str = "W4 回归") -> EvalRun:
    run = EvalRun(
        name=name,
        config=None,
        status="running",
        total=0,
        correct=0,
        accuracy=0,
        detail={"items": []},
        triggered_by=admin_id,
    )
    session.add(run)
    session.flush()
    return run


def _engine_factory(session: Session) -> sessionmaker:
    """引擎自管短会话指向测试事务连接（对齐 chat_client 的 SessionLocal 替换先例）。"""
    return sessionmaker(bind=session.connection(), expire_on_commit=False)


class _MarkerReranker:
    """确定性替身：marker 在 query 与 doc 双出现 → 高分（放行回答），否则低分（必拒答）。"""

    def __init__(self, markers: list[str]) -> None:
        self.markers = markers

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        return [
            sigmoid(3.0 if any(m in query and m in doc for m in self.markers) else -8.0)
            for doc in docs
        ]


class _BreakOneLLM:
    """仅对含 marker 的问题注入流中断裂（先产出一段再 UpstreamError broken=True），其余透传。"""

    def __init__(self, inner: StubLLMProvider, marker: str) -> None:
        self.inner = inner
        self.marker = marker

    def chat_stream(self, messages, max_tokens):  # type: ignore[no-untyped-def]
        if self.marker in messages[-1]["content"]:
            yield "部分"
            raise UpstreamError("stub stream broken", broken=True)
        yield from self.inner.chat_stream(messages, max_tokens)


class _ProgressProbeLLM:
    """逐题进度观测替身（W6 Task1 detail-stall 回归）：每次生成入口读回已提交的
    detail.items——单线程同步执行下恰落在"上一题 commit 之后、本题 commit 之前"，
    与前端 GET /{id} 轮询（请求级会话对已提交行的读回）视角等价且无采样竞态。"""

    def __init__(self, inner: StubLLMProvider, session: Session, run_id: int) -> None:
        self.inner = inner
        self.session = session
        self.run_id = run_id
        self.observed: list[list[int]] = []  # 每次观测的已落库 questionId 序列

    def chat_stream(self, messages, max_tokens):  # type: ignore[no-untyped-def]
        self.session.expire_all()  # 过期缓存后 get → 对已提交行的真 SELECT（非身份映射旧值）
        detail = self.session.get(EvalRun, self.run_id).detail
        self.observed.append([item["questionId"] for item in detail["items"]])
        yield from self.inner.chat_stream(messages, max_tokens)


def _run_engine(session: Session, deps: ChatDeps, run_id: int) -> None:
    """以 hr 视角部门直接执行跑分任务体（session_factory 注入测试事务）。"""
    eval_engine.run_eval_run(
        run_id, deps, _dept_ids(session)["hr"], session_factory=_engine_factory(session)
    )


def _item_of(run: EvalRun, question_id: int) -> dict:
    """detail.items 中按题目 id 取逐题条目。"""
    return next(item for item in run.detail["items"] if item["questionId"] == question_id)


# ---------------------------------------------------------------------------
# ① 收口：status/total/correct/accuracy 与 config 快照逐字段
# ---------------------------------------------------------------------------


def test_跑分收口_done与total_correct_accuracy及config快照逐字段(session: Session) -> None:
    """① 4 题收口 done：拒答两题 correct、事实两题 pending 不计；accuracy=0.5；
    config 快照 11 字段与当前 Settings/prompts 一致。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    _seed_bank(session, admin, embedder)
    questions = _seed_questions(session)
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅"]), StubLLMProvider())

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert run.status == "done"
    assert run.total == 4
    assert run.correct == 2  # 拒答两题被拒 correct；事实两题被答 pending 不计
    assert float(run.accuracy) == pytest.approx(0.5)  # 2/4，pending 计 0 口径
    assert run.detail["pendingCount"] == 2
    judgment = {q.id: _item_of(run, q.id)["autoJudgment"] for q in questions}
    assert [
        judgment[questions[0].id],
        judgment[questions[1].id],
        judgment[questions[2].id],
        judgment[questions[3].id],
    ] == ["pending", "pending", "correct", "correct"]
    assert run.config == {
        "model": deps.settings.llm_model or "stub",  # W2 口径：llm_model 空回退 stub
        "promptVersion": deps.settings.prompt_version,
        "qaThreshold": deps.settings.qa_threshold,
        "refuseThreshold": deps.settings.refuse_threshold,
        "efSearch": deps.settings.hnsw_ef_search,
        "topK": deps.settings.retriever_top_k,
        "topN": deps.settings.rerank_top_n,
        "topNMode": "by_doc_type",  # 工单 08：top_n 按 top1 命中型取档（spec §3.2/§5）
        "temperature": deps.prompts.temperature,
        "manifestVersion": deps.prompts.version,
        "judgePromptVersion": "v1",  # metrics-balance 工单 07 计划内演进（spec §6）
    }


# ---------------------------------------------------------------------------
# ② 拒答题自动判 wrong 反例：检索必命中的拒答题被回答
# ---------------------------------------------------------------------------


def test_拒答题检索命中被回答_自动判wrong(session: Session) -> None:
    """② 反例：拒答题语料在库且 rerank 高分 → 被回答 → autoJudgment=wrong 不计 correct。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    _seed_bank(session, admin, embedder)
    doc = _add_document(session, _dept_ids(session)["hr"], admin, "HR_薪酬语料")
    _add_chunk(session, doc, "薪酬体系：基本工资、绩效奖金与津贴构成。", embedder)  # 命中薪酬题
    questions = _seed_questions(session)
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅", "薪酬"]), StubLLMProvider())

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert run.status == "done"
    assert run.correct == 1  # 仅未命中的串库题 correct
    assert float(run.accuracy) == pytest.approx(0.25)
    wrong_item = _item_of(run, questions[2].id)
    assert wrong_item["autoJudgment"] == "wrong"
    assert wrong_item["answerType"] == "rag"
    assert wrong_item["answer"] != REFUSAL_TEXT  # 被回答（非拒答话术）→ wrong


# ---------------------------------------------------------------------------
# ③ messages 读回：is_eval=true 且专用会话单一
# ---------------------------------------------------------------------------


def test_评测消息落专用会话且is_eval为真(session: Session) -> None:
    """③ 每 run 恰一个专用会话（[评测] 前缀、user=触发者、dept=视角部门），8 条消息
    全部 is_eval=true 且 conversation 单一，user 消息按题序落题面。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    _seed_bank(session, admin, embedder)
    questions = _seed_questions(session)
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅"]), StubLLMProvider())

    _run_engine(session, deps, run.id)
    session.expire_all()

    conversations = session.scalars(select(Conversation)).all()
    assert len(conversations) == 1  # 专用会话单一
    conv = conversations[0]
    assert conv.user_id == run.triggered_by
    assert conv.dept_id == _dept_ids(session)["hr"]
    assert conv.title == f"[评测]{run.name}"
    messages = session.scalars(select(Message).order_by(Message.id)).all()
    assert len(messages) == 8  # 4 题 × (user + assistant)
    assert all(msg.conversation_id == conv.id for msg in messages)
    assert all(msg.is_eval is True for msg in messages)
    user_msgs = [msg for msg in messages if msg.role == "user"]
    assert [msg.content for msg in user_msgs] == [q.question for q in questions]


# ---------------------------------------------------------------------------
# ④ detail 逐题结构：citations 快照与回答一致
# ---------------------------------------------------------------------------


def test_detail逐题结构_citations快照与消息一致(session: Session) -> None:
    """④ 逐题条目键固定；rag 题引用按 rerank 分降序且与 assistant 消息 JSONB 全等；
    拒答题 citations 空回话术、answerType=refuse。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    chunk_ids = _seed_bank(session, admin, embedder)
    questions = _seed_questions(session)
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅"]), StubLLMProvider())

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert set(run.detail.keys()) == {"items", "pendingCount"}
    item1 = _item_of(run, questions[0].id)
    assert set(item1.keys()) == {
        "questionId",
        "deptId",
        "qtype",
        "expected",
        "sourceDocId",  # metrics-balance 工单 07：分型分组键来源（报告/评测追溯）
        "answerType",
        "answer",
        "citations",
        "retrieval",  # metrics-balance §4.2 计划内演进：检索中间产物 trace
        "metrics",  # 工单 07：四指标逐题落库（null 排除集均分的原料）
        "autoJudgment",
        "judgeError",  # 工单 07：judge 失败原因（整题 pending，绝不部分采纳）
        "error",
    }
    assert item1["answerType"] == "rag"
    assert item1["error"] is None
    assert [c["refId"] for c in item1["citations"]] == [
        chunk_ids["chunk_a"],
        chunk_ids["chunk_b"],
    ]  # 高分在前（rerank 降序）
    assert item1["citations"][0]["score"] == pytest.approx(sigmoid(3.0))
    assert item1["retrieval"]["contextChunkIds"] == [c["refId"] for c in item1["citations"]]
    assistant = session.scalar(
        select(Message).where(Message.role == "assistant", Message.content == item1["answer"])
    )
    assert assistant.citations == item1["citations"]  # citations 快照与落库消息一致
    assert assistant.answer_type == item1["answerType"]
    expected_model = deps.settings.llm_model or "stub"  # W2 口径：llm_model 空回退 stub
    assert (assistant.model, assistant.cache_hit, assistant.is_eval) == (
        expected_model,
        False,
        True,
    )

    item3 = _item_of(run, questions[2].id)
    assert (item3["answerType"], item3["answer"], item3["citations"]) == (
        "refuse",
        REFUSAL_TEXT,
        [],
    )
    # 拒答题 trace：contextChunkIds 恒空（无 context 拼装）；候选照记实际检索产物
    # （两块语料均被召回、均低于拒答阈值 → 2 条候选）
    assert item3["retrieval"]["contextChunkIds"] == []
    candidates3 = item3["retrieval"]["topKCandidates"]
    assert {c["chunkId"] for c in candidates3} == {chunk_ids["chunk_a"], chunk_ids["chunk_b"]}
    assert all(c["rerankScore"] == pytest.approx(sigmoid(-8.0)) for c in candidates3)


# ---------------------------------------------------------------------------
# ④″ 检索 trace 落库（metrics-balance §4.2）：rag 候选 / 拒答形态 / top_k 上限 /
#    缓存命中还原 / qa 直答形态
# ---------------------------------------------------------------------------


def test_评测rag题trace读回_候选三键与docType及context一致(session: Session) -> None:
    """rag 题 trace：topKCandidates 恰含全部召回候选（chunkId/docType/rerankScore 恰三键、
    docType=documents 默认 general、rerank 分降序），contextChunkIds 与 citations refId
    逐项一致。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    chunk_ids = _seed_bank(session, admin, embedder)
    questions = _seed_questions(session)
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅"]), StubLLMProvider())

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    retrieval = _item_of(run, questions[0].id)["retrieval"]
    assert set(retrieval.keys()) == {"contextChunkIds", "topKCandidates"}
    assert retrieval["contextChunkIds"] == [chunk_ids["chunk_a"], chunk_ids["chunk_b"]]
    candidates = retrieval["topKCandidates"]
    assert len(candidates) == 2  # 全量召回候选（2 块语料全部进候选），非 top_n 截取
    assert {c["chunkId"] for c in candidates} == {chunk_ids["chunk_a"], chunk_ids["chunk_b"]}
    assert all(set(c.keys()) == {"chunkId", "docType", "rerankScore"} for c in candidates)
    assert all(c["docType"] == "general" for c in candidates)  # documents.doc_type 默认档
    # 密码题命中 chunk_a（marker 双出现→高分）；chunk_b 无 marker 命中→低分——候选按
    # rerank 分降序（高分 chunk_a 在前）
    assert [c["chunkId"] for c in candidates] == [chunk_ids["chunk_a"], chunk_ids["chunk_b"]]
    assert [c["rerankScore"] for c in candidates] == [
        pytest.approx(sigmoid(3.0)),
        pytest.approx(sigmoid(-8.0)),
    ]


def test_拒答题trace形态_contextChunkIds空且候选照记实际检索产物(session: Session) -> None:
    """阈值拒答题（§2 裁决）：contextChunkIds=[]（无 context 拼装），topKCandidates 照记
    实际检索产物——低于拒答阈值的候选分保留观察价值。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    chunk_ids = _seed_bank(session, admin, embedder)
    salary_doc = _add_document(session, _dept_ids(session)["hr"], admin, "HR_薪酬语料")
    salary_chunk = _add_chunk(
        session, salary_doc, "薪酬体系：基本工资、绩效奖金与津贴构成。", embedder
    )
    questions = _seed_questions(session)
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅"]), StubLLMProvider())
    # 薪酬不在 marker 表：薪酬题三块语料全被召回、全低分 → 拒答

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert run.status == "done"
    item = _item_of(run, questions[2].id)
    assert (item["answerType"], item["answer"], item["citations"]) == (
        "refuse",
        REFUSAL_TEXT,
        [],
    )
    assert item["retrieval"]["contextChunkIds"] == []
    candidates = item["retrieval"]["topKCandidates"]
    assert {c["chunkId"] for c in candidates} == {
        chunk_ids["chunk_a"],
        chunk_ids["chunk_b"],
        salary_chunk.id,
    }
    assert all(c["docType"] == "general" for c in candidates)
    assert all(c["rerankScore"] == pytest.approx(sigmoid(-8.0)) for c in candidates)


def test_候选分恒为全量召回_不随top_n截断(session: Session) -> None:
    """21 块语料：topKCandidates 恰为 top_k 封顶的全量召回（21 块中 top_k 进候选），而
    citations/context 仍按 top_n 折叠——收集缝是 top_n 之外候选分的唯一获取点。
    断言以 Settings 实际生效值为准（本地 .env 的 RETRIEVER_TOP_K 覆盖不影响不变量）。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, _dept_ids(session)["hr"], admin, "HR_密码大全")
    for no in range(21):
        _add_chunk(session, doc, f"密码重置说明第{no}条：渠道{no}验证后重置。", embedder)
    dept_id = _dept_ids(session)["service"]
    question = _add_question(session, dept_id, _FACTUAL_1, "登录页点击忘记密码后重置。", "factual")
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码"]), StubLLMProvider())

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert run.status == "done"
    item = _item_of(run, question.id)
    assert item["answerType"] == "rag"
    retrieval = item["retrieval"]
    candidates = retrieval["topKCandidates"]
    # 不变量断言（不依赖本地 .env 的 RETRIEVER_TOP_K 覆盖）：候选数=top_k 封顶而非
    # top_n 截取——前提 top_n < top_k < 21，否则本测试前提失效先红
    assert deps.settings.rerank_top_n < deps.settings.retriever_top_k < 21
    assert len(candidates) == deps.settings.retriever_top_k  # 全量召回，top_k 封顶
    assert len({c["chunkId"] for c in candidates}) == deps.settings.retriever_top_k  # 无重复
    assert all(c["docType"] == "general" for c in candidates)
    assert all(c["rerankScore"] == pytest.approx(sigmoid(3.0)) for c in candidates)
    assert len(retrieval["contextChunkIds"]) == deps.settings.rerank_top_n  # context 按 top_n 折叠
    assert retrieval["contextChunkIds"] == [
        c["chunkId"] for c in candidates[: deps.settings.rerank_top_n]
    ]
    assert [c["refId"] for c in item["citations"]] == retrieval["contextChunkIds"]


def test_缓存命中题trace_contextChunkIds自citations还原且候选空(session: Session) -> None:
    """同题重复两问（§2 裁决）：第二问命中随跑旁路缓存——contextChunkIds 自缓存条目
    citations 还原（与首问一致），topKCandidates=[]（无新检索）。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    chunk_ids = _seed_bank(session, admin, embedder)
    dept_id = _dept_ids(session)["service"]
    q1 = _add_question(session, dept_id, _FACTUAL_1, "登录页点击忘记密码后重置。", "factual")
    q_dup = _add_question(session, dept_id, _FACTUAL_1, "登录页点击忘记密码后重置。", "factual")
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅"]), StubLLMProvider())

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    first = _item_of(run, q1.id)["retrieval"]
    assert [c["chunkId"] for c in first["topKCandidates"]] == [
        chunk_ids["chunk_a"],
        chunk_ids["chunk_b"],
    ]
    second = _item_of(run, q_dup.id)
    assert second["answerType"] == "rag"  # 缓存条目内的原答案类型
    assert second["retrieval"] == {
        "contextChunkIds": first["contextChunkIds"],
        "topKCandidates": [],  # 无新检索
    }


def test_qa直答题trace_contextChunkIds与候选均空(session: Session) -> None:
    """qa 直答题：引用为问答对（非 chunk），trace 两键同构但均空——refType=qa 的 refId
    不混入 contextChunkIds。qa 对种在视角部门 hr（检索边界 [hr, all]，题面 dept 只进
    报告口径）。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    _seed_bank(session, admin, embedder)
    qa = _add_qa(session, _dept_ids(session)["hr"], admin, "试用期多久", "试用期为六个月", embedder)
    question = _add_question(
        session, _dept_ids(session)["service"], "试用期多久", "六个月", "factual"
    )
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅"]), StubLLMProvider())

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    item = _item_of(run, question.id)
    assert item["answerType"] == "qa"
    assert item["citations"][0]["refType"] == "qa"
    assert item["citations"][0]["refId"] == qa.id
    assert item["retrieval"] == {"contextChunkIds": [], "topKCandidates": []}


# ---------------------------------------------------------------------------
# ④′ 逐题 detail 进度落库（W6 Task1 回归：部署验收 detail-stall 缺陷）
# ---------------------------------------------------------------------------


def test_跑分中途detail逐题可见_读回进度随题数增长(session: Session) -> None:
    """④′ 逐题 commit 生效：生成入口读回的 detail.items 逐题增长（[→Q1→Q1Q2→Q1Q2Q3]）。
    缺陷形态（重赋值共享 list 被 flush 相等比较判无变更、UPDATE 受抑制）：中途恒停
    首题快照、_finish 收口才跳全量——"进度可查/崩溃保留"意图失效。4 题均必命中被
    回答，保证每题都经 LLM 观测点。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, _dept_ids(session)["hr"], admin, "HR_进度语料")
    _add_chunk(session, doc, "密码重置：登录页点击忘记密码，验证手机号后重置。", embedder)
    _add_chunk(session, doc, "差旅报销：交通费与住宿费凭票据实报销。", embedder)
    _add_chunk(session, doc, "考勤补卡：迟到需在当月内补卡。", embedder)
    _add_chunk(session, doc, "年假申请：需提前三天在 OA 提交。", embedder)
    dept_id = _dept_ids(session)["service"]
    questions = [
        _add_question(
            session, dept_id, "员工如何重置门户登录密码？", "忘记密码后重置。", "factual"
        ),
        _add_question(session, dept_id, "差旅费报销标准是多少？", "凭票据实报销。", "factual"),
        _add_question(session, dept_id, "考勤补卡怎么办理？", "当月内补卡。", "factual"),
        _add_question(session, dept_id, "年假如何申请？", "提前三天 OA 提交。", "factual"),
    ]
    run = _new_run(session, admin)
    probe = _ProgressProbeLLM(StubLLMProvider(), session, run.id)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅", "考勤", "年假"]), probe)

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert run.status == "done"  # 回归不破坏收口：终态完整
    assert run.total == 4
    assert probe.observed == [
        [],  # Q1 生成前：尚无已完成题
        [questions[0].id],  # Q2 生成前：Q1 已落库
        [questions[0].id, questions[1].id],  # Q3 生成前：Q1Q2 已落库
        [questions[0].id, questions[1].id, questions[2].id],  # Q4 生成前：Q1Q2Q3 已落库
    ]


# ---------------------------------------------------------------------------
# ⑤ 评测不写主缓存（旁路：put 进随跑弃置实例）
# ---------------------------------------------------------------------------


def test_评测不写主缓存(session: Session) -> None:
    """⑤ 跑完后 deps.cache（主缓存引用）_entries 恒空——评测 put 只进旁路弃置实例。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    _seed_bank(session, admin, embedder)
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码", "差旅"]), StubLLMProvider())

    _run_engine(session, deps, run.id)

    assert deps.cache._entries == {}  # 主缓存零污染（EVAL-02 旁路精确缓存）


# ---------------------------------------------------------------------------
# ⑥ 单题 LLM 中断：不落 assistant 行、detail 记 error、整体 done
# ---------------------------------------------------------------------------


def test_单题LLM中断不拖垮整体(session: Session) -> None:
    """⑥ 仅差旅题流中断裂：该题不落 assistant 行、detail 记 error、autoJudgment pending，
    其余题正常判分，run 收口 done。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    _seed_bank(session, admin, embedder)
    questions = _seed_questions(session)
    run = _new_run(session, admin)
    deps = _make_deps(
        embedder,
        _MarkerReranker(["密码", "差旅"]),
        # marker 取题面全短语：Q1 的 <data> 资料段含另一块的"差旅报销"文本，短词会误伤
        _BreakOneLLM(StubLLMProvider(), "差旅费报销标准"),
    )

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert run.status == "done"  # 单题失败不拖垮整体
    assert run.total == 4 and run.correct == 2
    conv_id = session.scalar(select(Conversation.id))
    roles = session.scalars(
        select(Message.role).where(Message.conversation_id == conv_id).order_by(Message.id)
    ).all()
    assert roles.count("user") == 4  # 断裂题 user 消息保留
    assert roles.count("assistant") == 3  # 断裂题不落 assistant 行
    broken_item = _item_of(run, questions[1].id)
    assert broken_item["error"] == "stub stream broken"
    assert broken_item["autoJudgment"] == "pending"  # 失败题进人工复核队列
    assert run.detail["pendingCount"] == 2  # 事实题一答一断，均 pending


# ---------------------------------------------------------------------------
# ⑦ 创建审计读回 + 端点接线（POST 202 / GET 列表 / GET 详情 / 404 / 未知部门 422）
# ---------------------------------------------------------------------------


def make_user(session: Session, username: str, password: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内（口径同 tests/api 导入测试）。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


@pytest.fixture()
def eval_client(session: Session, monkeypatch: pytest.MonkeyPatch):
    """评测跑分 API 客户端：get_db 覆写 + TEST_MODE（lifespan 装 Stub 三件套）+ 引擎
    自管短会话指向测试事务连接（对齐 tests/api/conftest chat_client 先例）。TestClient
    同步执行 BackgroundTasks——202 响应返回时跑分已收口，可直接断言终态。"""
    saved = os.environ.get("TEST_MODE")
    os.environ["TEST_MODE"] = "true"
    get_settings.cache_clear()
    monkeypatch.setattr(eval_engine, "SessionLocal", _engine_factory(session))
    from app.api.deps import get_db
    from app.main import app
    from app.services.auth import default_limiter

    default_limiter.reset()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as http:
        yield http
    app.dependency_overrides.pop(get_db, None)
    if saved is None:
        os.environ.pop("TEST_MODE", None)
    else:
        os.environ["TEST_MODE"] = saved
    get_settings.cache_clear()


def test_创建跑分_202与审计读回及GET接线(eval_client: TestClient, session: Session) -> None:
    """⑦ POST 202 {id} + 审计 eval.run {runId,name,perspective}；空题库跑完 done
    （TestClient 同步执行后台任务）；GET 列表/详情/404 接线读回。"""
    make_user(session, "evalrunadmin", "right-one", role="admin")
    assert login_as(eval_client, "evalrunadmin", "right-one").status_code == 200

    resp = eval_client.post(
        "/api/admin/eval-runs", json={"name": "上线前回归", "perspectiveDeptCode": "hr"}
    )

    assert resp.status_code == 202
    run_id = resp.json()["data"]["id"]
    assert resp.json()["code"] == 0 and isinstance(run_id, int)

    session.expire_all()
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "eval.run"))
    assert audit is not None
    assert audit.object_type == "eval_run"
    assert audit.object_id == run_id
    assert audit.detail == {"runId": run_id, "name": "上线前回归", "perspective": "hr"}
    admin_id = session.scalars(select(User.id).where(User.username == "evalrunadmin")).one()
    assert audit.actor_user_id == admin_id

    run = session.get(EvalRun, run_id)
    assert run.status == "done" and run.total == 0  # 空题库：正常收口
    assert run.detail == {"items": [], "pendingCount": 0}

    listing = eval_client.get("/api/admin/eval-runs?page=1&pageSize=20")
    assert listing.status_code == 200
    body = listing.json()["data"]
    assert body["total"] == 1 and body["page"] == 1 and body["pageSize"] == 20
    assert body["items"] == [
        {
            "id": run_id,
            "name": "上线前回归",
            "status": "done",
            "total": 0,
            "correct": 0,
            "accuracy": 0.0,
            "createdAt": run.created_at.isoformat(),
        }
    ]

    detail = eval_client.get(f"/api/admin/eval-runs/{run_id}")
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["id"] == run_id and data["status"] == "done"
    assert set(data["config"].keys()) == {
        "model",
        "promptVersion",
        "qaThreshold",
        "refuseThreshold",
        "efSearch",
        "topK",
        "topN",
        "topNMode",  # 工单 08 计划内演进（spec §6）：top_n 生效模式辨识
        "temperature",
        "manifestVersion",
        "judgePromptVersion",  # 工单 07 计划内演进（spec §6）
    }
    assert data["detail"] == {"items": [], "pendingCount": 0}

    missing = eval_client.get("/api/admin/eval-runs/999999")
    assert missing.status_code == 404
    assert missing.json()["code"] == ERR_NOT_FOUND


def test_未知视角部门_422部门编码无效(eval_client: TestClient, session: Session) -> None:
    """rider②（跑分侧）：perspectiveDeptCode 枚举外 → 422"部门编码无效"，不建 run。
    （错误转换收敛到 services.departments.resolve_dept_id 后与上传表单同文案。）"""
    make_user(session, "evalrunadmin2", "right-one", role="admin")
    assert login_as(eval_client, "evalrunadmin2", "right-one").status_code == 200

    resp = eval_client.post(
        "/api/admin/eval-runs", json={"name": "坏视角", "perspectiveDeptCode": "finance"}
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == 1003
    assert resp.json()["message"] == "部门编码无效：finance（可选 service/hr/all）"
    assert session.scalar(select(func.count()).select_from(EvalRun)) == 0


# ---------------------------------------------------------------------------
# ⑧ judge 预填 autoJudgment + metrics 落库（metrics-balance 工单 07，spec §2/§4.3）
# ---------------------------------------------------------------------------

_JUDGE_OK = '{"context_relevant": [true], "relevancy": 1, "correctness": "correct"}'


def test_judge预填autoJudgment与metrics落库读回(session: Session) -> None:
    """⑧ judge 三判合法（专用第二实例注入）：非拒答题 autoJudgment 预填 correctness
    （correct 计入收口 correct、pendingCount 不含）；metrics 五键读回——recall 命中 1、
    citationsOk 无角标 false、contextPrecision 全相关 1、relevancy/correctness 折算；
    拒答题旁路 metrics=null 且 judgeError=null。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, _dept_ids(session)["hr"], admin, "HR_密码语料")
    _add_chunk(session, doc, "密码重置：登录页点击忘记密码，验证手机号后重置。", embedder)
    dept_id = _dept_ids(session)["service"]
    factual = _add_question(
        session, dept_id, _FACTUAL_1, "登录页点击忘记密码后重置。", "factual", source_doc_id=doc.id
    )
    reject = _add_question(session, dept_id, _REJECT_1, "[拒答]", "cross_reject")
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码"]), StubLLMProvider())
    judge_llm = StubLLMProvider(chunks=(_JUDGE_OK,))  # 评测专用第二实例替身（spec §2）

    # 幂等闸（status!=running 直接返回）决定 judge 注入必须随首次执行：直调任务体传
    # judge_llm 实参（对齐 _run_engine 的 session_factory 指向，hr 视角部门）
    eval_engine.run_eval_run(
        run.id,
        deps,
        _dept_ids(session)["hr"],
        session_factory=_engine_factory(session),
        judge_llm=judge_llm,
    )
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert run.status == "done"
    answered = _item_of(run, factual.id)
    assert answered["autoJudgment"] == "correct"  # judge 预填（原恒 pending）
    assert answered["judgeError"] is None
    assert answered["sourceDocId"] == doc.id
    assert answered["metrics"] == {
        "recall": 1,  # contextChunkIds 的 chunk 均属来源文档
        "citationsOk": False,  # stub 回答无角标：不可核对，机检必须红（裁决 4）
        "contextPrecision": pytest.approx(1.0),  # 单块全相关 = 1
        "relevancy": 1,
        "correctness": 1,
    }
    assert run.correct == 2  # judge 预填 correct 1 + 拒答 auto correct 1
    assert run.detail["pendingCount"] == 0
    refused = _item_of(run, reject.id)
    assert refused["autoJudgment"] == "correct"  # 拒答类走既有规则自动判
    assert refused["metrics"] is None and refused["judgeError"] is None
    assert judge_llm.stream_calls == 1  # 拒答类不调 judge（§2 旁路）


def test_judge失败_单题pending与judgeError不炸run(session: Session) -> None:
    """⑧ judge 输出非法（stub 默认产出非 JSON，§2 裁决 3）：该题 pending + judgeError
    落 detail，机检 recall/citationsOk 照算、三判折算三键 null；run 照常收口 done。"""
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, _dept_ids(session)["hr"], admin, "HR_密码语料")
    _add_chunk(session, doc, "密码重置：登录页点击忘记密码，验证手机号后重置。", embedder)
    dept_id = _dept_ids(session)["service"]
    factual = _add_question(
        session, dept_id, _FACTUAL_1, "登录页点击忘记密码后重置。", "factual", source_doc_id=doc.id
    )
    run = _new_run(session, admin)
    deps = _make_deps(embedder, _MarkerReranker(["密码"]), StubLLMProvider())
    # 不注入 judge_llm：缺省 build_llm(settings, 0.0) 在 test_mode 下产出 stub 固定文案

    _run_engine(session, deps, run.id)
    session.expire_all()
    run = session.get(EvalRun, run.id)

    assert run.status == "done"  # 单题 judge 失败不拖垮整体
    item = _item_of(run, factual.id)
    assert item["autoJudgment"] == "pending"  # 绝不部分采纳、绝不判 wrong
    assert item["judgeError"] is not None
    assert item["judgeError"].startswith("judge 输出不是合法 JSON")
    assert item["metrics"] == {
        "recall": 1,
        "citationsOk": False,
        "contextPrecision": None,
        "relevancy": None,
        "correctness": None,
    }
    assert run.detail["pendingCount"] == 1  # 失败题进人工复核队列


def test_部门未播种_导入422部门数据未初始化(session: Session) -> None:
    """rider②（导入侧）：_resolve_depts 播种缺失分支——拔掉部门 code 后导入 422 全量退回。
    （departments 行被 users.dept_id 外键引用不可物理删，code 唯一约束下逐行改空 code
    等效未播种。）"""
    admin = _admin_id(session)
    for dept in session.scalars(select(Department)).all():
        dept.code = f"drained_{dept.id}"
    wb = Workbook()
    ws = wb.worksheets[0]
    ws.append(["部门", "问题", "期望答案", "来源", "题型"])
    ws.append(["客服", "问题一", "答案一", "", "factual"])
    buf = BytesIO()
    wb.save(buf)

    with pytest.raises(ApiError) as exc_info:
        eval_service.import_eval_questions(session, admin, buf.getvalue())

    assert exc_info.value.http_status == 422
    assert exc_info.value.message == "部门数据未初始化：service"
    assert session.scalar(select(func.count()).select_from(EvalQuestion)) == 0
