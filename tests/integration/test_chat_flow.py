r"""问答编排集成测试（Task 8，真 pg + Stub 三件套 + FakeClock）：brief ①-⑧ 逐条落地。

① 改写：多轮经 llm 产出独立问题并按改写后向量检索；改写上游故障降级原问题继续
  （CHAT-08）；改写只携带最近 3 轮历史。
② 缓存（CHAT-05）：prepare 不写缓存（put 归 Task 9 finalize 后）；二次同输入命中
  cache 计划（条目与首次写入一致、不再触发生成）；不同 dept 同问题不命中（键含部门）。
③ QA 优先（CHAT-04）：qa 命中不走文档链路（rerank_calls==0、hit_counter 空），
  llm_messages 的问答对答案在 <data> 内。
④ RAG：citations 按 rerank 分降序编号 1..N，title/page/titlePath/score 逐字段断言，
  score 为 rerank sigmoid 分。
⑤ 空检索拒答（CHAT-03）：citations==[]、llm_messages is None、不调 LLM 不重排。
⑥ 阈值拒答（CHAT-16/强制交接①）：召回余弦≈1.0 的同文 chunk 注入 rerank 分 0.52
  （<0.55）→ refuse——若误用召回余弦则永不拒答，本用例双向锁定"判定取 fold 后值"；
  阈值放宽 0.5 → 同输入变 rag（RET-04 方向），citation score==0.52（rerank 分）。
⑦ 折叠一致：citations 与检索 fold 结果一致，近重复折叠后 5→4（<top_n）连续编号。
⑧ 拒答不写缓存：prepare 后 cache.get 为空。
另有 finalize_turn / mark_interrupted 的真 pg 读回断言（flush 不 commit，commit 归
Task 9）与 TurnPlan/ChatDeps 字段契约锁定。种子一律 ORM 直插（先例 test_retrieval.py）。
"""

import hashlib
import math
from dataclasses import fields
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.assembly import load_prompt_pack
from app.core.clock import FakeClock
from app.core.config import Settings
from app.core.llm import StubEmbedder, StubLLMProvider, StubReranker, sigmoid
from app.db.models import Chunk, Conversation, Department, Document, Message, QaPair, User
from app.rag.prompt import REFUSAL_TEXT
from app.services.chat import ChatDeps, TurnPlan, finalize_turn, mark_interrupted, prepare_turn
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.retrieval import normalize_question, rerank_and_fold, search_chunks

# ---------------------------------------------------------------------------
# 种子与装配助手（ORM 直插，先例 tests/integration/test_retrieval.py）
# ---------------------------------------------------------------------------


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
        content_hash=hashlib.sha256(title.encode("utf-8")).hexdigest(),
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        is_active=True,
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    return doc


def _add_qa(
    session: Session,
    dept_id: int,
    admin_id: int,
    question: str,
    answer: str,
    embedder: StubEmbedder,
) -> QaPair:
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


def _add_chunk(
    session: Session,
    doc: Document,
    content: str,
    embedder: StubEmbedder,
    *,
    position: int = 0,
    page_no: int | None = None,
    title_path: str | None = None,
    embedding: list[float] | None = None,
) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=title_path if title_path is not None else doc.title,
        content=content,
        content_chars=len(content),
        position=position,
        page_no=page_no,
        embedding=embedding if embedding is not None else embedder.embed([content])[0],
    )
    session.add(chunk)
    session.flush()
    return chunk


def _make_deps(
    embedder: StubEmbedder,
    reranker: StubReranker,
    llm: StubLLMProvider,
    *,
    refuse_threshold: float = 0.55,
) -> ChatDeps:
    settings = Settings(
        jwt_secret="chat-flow-test", test_mode=True, refuse_threshold=refuse_threshold
    )
    clock = FakeClock("2026-10-15T09:30:00+00:00")
    return ChatDeps(
        embedder=embedder,
        reranker=reranker,
        llm=llm,
        cache=ExactCache(clock, settings.cache_ttl_s),
        counter=DailyCounter(clock),
        hit_counter=HitCounter(clock),
        settings=settings,
        prompts=load_prompt_pack(),
    )


class _RecordingLLM:
    """集成用记录替身：捕获 chat_stream 消息入参（供改写历史截断断言）。"""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def chat_stream(self, messages, max_tokens):  # type: ignore[no-untyped-def]
        self.calls.append(([dict(message) for message in messages], max_tokens))
        yield "改写后的问题"


# ---------------------------------------------------------------------------
# 接口契约锁定（Task 9 依赖的 dataclass 形状）
# ---------------------------------------------------------------------------


def test_turn_plan_and_chat_deps_field_contracts() -> None:
    """TurnPlan 六字段（含 cache_key，refuse/cache 计划为 None）与 ChatDeps 八字段即接口。"""
    assert [f.name for f in fields(TurnPlan)] == [
        "kind",
        "citations",
        "rewritten_question",
        "llm_messages",
        "cache_entry",
        "cache_key",
    ]
    assert [f.name for f in fields(ChatDeps)] == [
        "embedder",
        "reranker",
        "llm",
        "cache",
        "counter",
        "hit_counter",
        "settings",
        "prompts",
    ]


# ---------------------------------------------------------------------------
# ① 多轮改写：经 llm 产出独立问题；上游故障降级（CHAT-08）；仅携带最近 3 轮
# ---------------------------------------------------------------------------


def test_prepare_rewrites_multi_turn_question_via_llm(session: Session) -> None:
    """history 非空 → llm 被调用一次，改写产物成为 rewritten_question 并作检索向量。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_假勤制度")
    chunk = _add_chunk(session, doc, "改写后的问题", embedder)  # 与改写产物同文：余弦≈1
    embed_calls_after_seed = embedder.embed_calls  # 种子向量化不计入编排的一次计算
    deps = _make_deps(
        embedder, StubReranker(weights=[2.0]), StubLLMProvider(chunks=("改写后的问题",))
    )
    history = [("user", "公司的假勤制度有哪些"), ("assistant", "年假按工龄五到十天 [1]")]

    plan = prepare_turn(session, deps, ids["service"], ids["all"], "那病假怎么请", history)

    assert deps.llm.stream_calls == 1  # 改写恰消耗一次 LLM 流
    assert plan.rewritten_question == "改写后的问题"
    assert plan.kind == "rag"  # 按改写后问题检索命中（若误用原问题向量则不会命中同文块）
    assert [citation["refId"] for citation in plan.citations] == [chunk.id]
    assert embedder.embed_calls == embed_calls_after_seed + 1  # 改写后向量只算一次


def test_rewrite_upstream_failure_degrades_to_original_and_continues(session: Session) -> None:
    """改写 Stub fail_times=1：降级原问题（CHAT-08），编排不受影响正常返回 rag 计划。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_假勤制度")
    _add_chunk(session, doc, "改写后的问题", embedder)
    deps = _make_deps(
        embedder,
        StubReranker(weights=[2.0]),
        StubLLMProvider(chunks=("改写后的问题",), fail_times=1),
    )
    history = [("user", "公司的假勤制度有哪些"), ("assistant", "年假按工龄五到十天 [1]")]

    plan = prepare_turn(session, deps, ids["service"], ids["all"], "那病假怎么请", history)

    assert plan.rewritten_question == "那病假怎么请"  # 原问题继续
    assert plan.kind == "rag" and plan.citations  # 编排正常走完文档链路
    assert deps.llm.stream_calls == 1  # 失败的调用也被计数，异常未外泄


def test_rewrite_carries_only_last_three_rounds_of_history(session: Session) -> None:
    """改写消息只含最近 3 轮（6 条）历史：第 4 轮之前的轮次不进 prompt（brief 约定）。"""
    ids = _dept_ids(session)
    llm = _RecordingLLM()
    deps = _make_deps(StubEmbedder(), StubReranker(), llm)  # 空库：改写后走拒答
    history = [
        (role, f"{role}{round_}") for round_ in (1, 2, 3, 4) for role in ("user", "assistant")
    ]

    plan = prepare_turn(session, deps, ids["service"], ids["all"], "那病假呢", history)

    assert plan.kind == "refuse"  # 改写先行、检索后拒答，时序不受影响
    ((messages, _),) = llm.calls
    assert messages[0] == {"role": "system", "content": deps.prompts.rewrite_system}
    assert messages[1]["content"].splitlines() == [
        "[对话]",
        "用户：user2",
        "助手：assistant2",
        "用户：user3",
        "助手：assistant3",
        "用户：user4",
        "助手：assistant4",
        "[当前问题]",
        "那病假呢",
    ]


# ---------------------------------------------------------------------------
# ② 精确缓存：prepare 不写缓存；命中短路；键含 dept（CHAT-05）
# ---------------------------------------------------------------------------


def test_cache_hit_short_circuits_and_cross_dept_misses(session: Session) -> None:
    """首问 rag + Task 9 写缓存后，二问命中 cache 计划且不再触发生成；跨部门不命中。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["all"], admin, "全员_通用制度")
    chunk = _add_chunk(session, doc, "通用考勤制度正文", embedder)
    llm = StubLLMProvider(chunks=("首次生成的完整回答",))
    deps = _make_deps(embedder, StubReranker(weights=[2.0]), llm)
    question = "通用考勤制度正文"

    plan1 = prepare_turn(session, deps, ids["service"], ids["all"], question, [])
    assert plan1.kind == "rag"
    assert deps.cache.get(plan1.cache_key) is None  # prepare 全程不写缓存
    assert plan1.cache_key == deps.cache.make_key(ids["service"], normalize_question(question))
    # 模拟 Task 9：生成 → finalize 后以 plan.cache_key put
    answer = "".join(llm.chat_stream(plan1.llm_messages, 512))
    assert llm.stream_calls == 1
    entry = {"answer": answer, "citations": plan1.citations, "answer_type": "rag", "model": "stub"}
    deps.cache.put(plan1.cache_key, entry)

    plan2 = prepare_turn(session, deps, ids["service"], ids["all"], question, [])
    assert plan2.kind == "cache"
    assert plan2.cache_entry == entry  # 条目与首次写入一致
    assert plan2.citations == entry["citations"]
    assert plan2.llm_messages is None and plan2.cache_key is None  # cache 计划不再 put/不再生成
    assert plan2.rewritten_question == question
    assert llm.stream_calls == 1  # 生成端零新增消费（Task 9 对 cache 计划不调 LLM）

    plan3 = prepare_turn(session, deps, ids["hr"], ids["all"], question, [])
    assert plan3.kind == "rag"  # 键含 dept：hr 视角同问题不命中 service 的缓存
    assert [citation["refId"] for citation in plan3.citations] == [chunk.id]


# ---------------------------------------------------------------------------
# ③ QA 优先（CHAT-04）：命中即短路，不走文档链路
# ---------------------------------------------------------------------------


def test_qa_hit_skips_document_chain(session: Session) -> None:
    """qa 相似命中：kind=='qa'、恰 1 条 qa 引用、rerank 未被调用、答案在 <data> 内。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    qa = _add_qa(session, ids["service"], admin, "试用期多久", "试用期为六个月", embedder)
    doc = _add_document(session, ids["service"], admin, "客服_试用期制度")
    _add_chunk(session, doc, "试用期有关规定正文", embedder)  # 在库也不得触发文档链路
    reranker = StubReranker()
    deps = _make_deps(embedder, reranker, StubLLMProvider())

    plan = prepare_turn(session, deps, ids["service"], ids["all"], "试用期多久", [])

    assert plan.kind == "qa"
    assert plan.citations == [
        {
            "n": 1,
            "refType": "qa",
            "refId": qa.id,
            "title": "试用期多久",
            "page": None,
            "titlePath": "",
            "score": pytest.approx(1.0, abs=1e-6),  # qa score = embedding 余弦相似度
        }
    ]
    assert reranker.rerank_calls == 0  # 不走文档链路（mock 断言，CHAT-04）
    assert deps.hit_counter._counts == {}  # qa 路径不计召回命中
    assert plan.llm_messages == [
        {"role": "system", "content": deps.prompts.chat_system},
        {
            "role": "user",
            "content": "<data>\n[1] 试用期多久｜问答对\n试用期为六个月\n</data>\n\n试用期多久",
        },
    ]
    assert plan.cache_entry is None
    assert plan.cache_key == deps.cache.make_key(ids["service"], "试用期多久")


# ---------------------------------------------------------------------------
# ④ RAG citations：rerank 分降序编号，字段逐项断言
# ---------------------------------------------------------------------------


def test_rag_citations_ordered_by_rerank_score_with_full_fields(session: Session) -> None:
    """citations 按 rerank sigmoid 分降序编号 1..N；title/page/titlePath/score 逐字段断言。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_考勤管理办法")
    c1 = _add_chunk(
        session,
        doc,
        "年假按工龄五到十天",
        embedder,
        position=1,
        page_no=3,
        title_path="假勤管理/年假",
    )
    c2 = _add_chunk(
        session,
        doc,
        "病假需要医院证明",
        embedder,
        position=2,
        page_no=7,
        title_path="假勤管理/病假",
    )
    c3 = _add_chunk(
        session, doc, "调休三日内申请", embedder, position=3, page_no=9, title_path="假勤管理/调休"
    )
    question = "假勤制度有哪些规定"
    recall = search_chunks(
        session, embedder.embed([question])[0], [ids["service"], ids["all"]], top_k=20, ef_search=40
    )
    logit_of = {c1.id: 2.0, c2.id: 1.0, c3.id: 0.5}
    deps = _make_deps(
        embedder,
        StubReranker(weights=[logit_of[hit.chunk_id] for hit in recall]),
        StubLLMProvider(),
    )

    plan = prepare_turn(session, deps, ids["service"], ids["all"], question, [])

    assert plan.kind == "rag"
    assert plan.cache_key == deps.cache.make_key(ids["service"], normalize_question(question))
    assert plan.citations == [
        {
            "n": 1,
            "refType": "doc",
            "refId": c1.id,
            "title": doc.title,
            "page": 3,
            "titlePath": "假勤管理/年假",
            "score": pytest.approx(sigmoid(2.0)),
        },
        {
            "n": 2,
            "refType": "doc",
            "refId": c2.id,
            "title": doc.title,
            "page": 7,
            "titlePath": "假勤管理/病假",
            "score": pytest.approx(sigmoid(1.0)),
        },
        {
            "n": 3,
            "refType": "doc",
            "refId": c3.id,
            "title": doc.title,
            "page": 9,
            "titlePath": "假勤管理/调休",
            "score": pytest.approx(sigmoid(0.5)),
        },
    ]
    expected_block = (
        f"[1] {doc.title}｜假勤管理/年假｜第3页\n年假按工龄五到十天\n"
        f"[2] {doc.title}｜假勤管理/病假｜第7页\n病假需要医院证明\n"
        f"[3] {doc.title}｜假勤管理/调休｜第9页\n调休三日内申请"
    )
    assert plan.llm_messages == [
        {"role": "system", "content": deps.prompts.chat_system},
        {"role": "user", "content": f"<data>\n{expected_block}\n</data>\n\n{question}"},
    ]
    assert deps.hit_counter._counts == {c1.id: 1, c2.id: 1, c3.id: 1}  # 仅 rag 路径计数


# ---------------------------------------------------------------------------
# ⑤ 空检索拒答（CHAT-03） / ⑧ 拒答不写缓存
# ---------------------------------------------------------------------------


def test_empty_retrieval_refuses_without_llm_or_rerank(session: Session) -> None:
    """无 qa 无 chunk：refuse 计划、citations 空、llm_messages 为 None、不调 LLM 不重排。"""
    ids = _dept_ids(session)
    embedder = StubEmbedder()
    reranker = StubReranker()
    llm = StubLLMProvider()
    deps = _make_deps(embedder, reranker, llm)

    plan = prepare_turn(session, deps, ids["service"], ids["all"], "完全无关的天书问题", [])

    assert plan.kind == "refuse"
    assert plan.citations == [] and plan.llm_messages is None
    assert plan.rewritten_question == "完全无关的天书问题"
    assert llm.stream_calls == 0  # 拒答不调 LLM（Task 9 直接输出 REFUSAL_TEXT）
    assert reranker.rerank_calls == 0  # 空召回短路，不发起 rerank
    assert embedder.embed_calls == 1  # 向量仍算一次（qa 与文档链路共用）
    assert plan.cache_key is None and plan.cache_entry is None


def test_refuse_plan_never_writes_cache(session: Session) -> None:
    """拒答不写缓存：prepare 后按规范化问题取缓存为空，且计划不带 cache_key。"""
    ids = _dept_ids(session)
    deps = _make_deps(StubEmbedder(), StubReranker(), StubLLMProvider())
    question = "知识库必然没有的问题"

    plan = prepare_turn(session, deps, ids["service"], ids["all"], question, [])

    assert plan.kind == "refuse"
    assert plan.cache_key is None
    key = deps.cache.make_key(ids["service"], normalize_question(question))
    assert deps.cache.get(key) is None  # ⑧：拒答计划不写缓存


# ---------------------------------------------------------------------------
# ⑥ 阈值拒答（CHAT-16/强制交接①/RET-04 方向）：判定只取 fold 后 rerank 分
# ---------------------------------------------------------------------------


def test_refuse_threshold_uses_folded_rerank_score_not_recall_cosine(session: Session) -> None:
    """同文 chunk（召回余弦≈1.0）注入 rerank 分 0.52：0.55 阈值拒答、0.5 阈值放行。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_考勤制度")
    chunk = _add_chunk(session, doc, "考勤制度怎么规定", embedder)  # 与问题同文：召回余弦≈1.0
    logit_052 = math.log(0.52 / 0.48)  # sigmoid(logit_052) == 0.52 < 0.55
    refuse_reranker = StubReranker(weights=[logit_052])
    refuse_deps = _make_deps(embedder, refuse_reranker, StubLLMProvider())

    plan = prepare_turn(session, refuse_deps, ids["service"], ids["all"], "考勤制度怎么规定", [])

    assert plan.kind == "refuse"  # 若误用召回余弦（≈1.0 ≥ 0.55）此处必不拒答——强制交接①
    assert plan.citations == [] and plan.llm_messages is None
    assert refuse_reranker.rerank_calls == 1  # 有召回且已重排：拒答出自 fold 后分数判定
    assert refuse_deps.hit_counter._counts == {}  # refuse 路径不计召回命中

    rag_deps = _make_deps(
        embedder, StubReranker(weights=[logit_052]), StubLLMProvider(), refuse_threshold=0.5
    )
    plan2 = prepare_turn(session, rag_deps, ids["service"], ids["all"], "考勤制度怎么规定", [])

    assert plan2.kind == "rag"  # RET-04 方向：阈值调低 → 同输入由拒答变回答
    assert plan2.citations[0]["refId"] == chunk.id
    assert plan2.citations[0]["score"] == pytest.approx(0.52)  # score 亦为 fold 后 rerank 分
    assert rag_deps.hit_counter._counts == {chunk.id: 1}


# ---------------------------------------------------------------------------
# ⑦ citations 与检索折叠结果一致：近重复折叠后 5→4（< top_n）
# ---------------------------------------------------------------------------


def test_citations_match_fold_result_and_renumber_after_dedup(session: Session) -> None:
    """近重复低分块被折叠：citations 逐项等于 rerank_and_fold 产物，编号折叠后连续。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_折叠样本")
    base = embedder.embed(["折叠基准文本"])[0]
    perturb = embedder.embed(["折叠扰动文本"])[0]
    mixed = [b * 100.0 + p for b, p in zip(base, perturb, strict=True)]
    norm = math.sqrt(sum(component * component for component in mixed))
    near_vec = [component / norm for component in mixed]
    chunk_base = _add_chunk(session, doc, "折叠基准文本", embedder, position=1)
    _add_chunk(session, doc, "折叠独立文本乙", embedder, position=2)
    _add_chunk(session, doc, "折叠独立文本丙", embedder, position=3)
    _add_chunk(session, doc, "折叠独立文本丁", embedder, position=4)
    chunk_near = _add_chunk(session, doc, "近重复文本戊", embedder, position=5, embedding=near_vec)
    question = "折叠查询文本"
    recall = search_chunks(
        session, embedder.embed([question])[0], [ids["service"], ids["all"]], top_k=20, ef_search=40
    )
    assert len(recall) == 5
    logit_of = {chunk_base.id: 3.0, chunk_near.id: 0.5}
    deps = _make_deps(
        embedder,
        StubReranker(weights=[logit_of.get(hit.chunk_id, 2.0) for hit in recall]),
        StubLLMProvider(),
    )

    plan = prepare_turn(session, deps, ids["service"], ids["all"], question, [])

    expected = rerank_and_fold(
        StubReranker(weights=[logit_of.get(hit.chunk_id, 2.0) for hit in recall]),
        question,
        recall,
        deps.settings.rerank_top_n,
        deps.settings.chunk_dedup_sim,
    )
    assert plan.kind == "rag"
    ref_ids = [citation["refId"] for citation in plan.citations]
    assert ref_ids == [hit.chunk_id for hit in expected]  # 与 fold 结果逐项一致
    assert chunk_near.id not in ref_ids  # 近重复低分块被折叠
    assert len(plan.citations) == 4 < deps.settings.rerank_top_n  # 折叠后可少于 top_n
    assert [citation["n"] for citation in plan.citations] == [1, 2, 3, 4]  # 折叠后连续编号
    assert [citation["score"] for citation in plan.citations] == [
        pytest.approx(hit.similarity) for hit in expected
    ]


# ---------------------------------------------------------------------------
# finalize_turn / mark_interrupted：纯 UPDATE + flush（commit 归 Task 9），真 pg 读回
# ---------------------------------------------------------------------------


def _add_conversation(session: Session, dept_id: int, admin_id: int) -> Conversation:
    conv = Conversation(user_id=admin_id, dept_id=dept_id, title="编排测试会话")
    session.add(conv)
    session.flush()
    return conv


def _add_assistant_placeholder(
    session: Session, conv: Conversation, admin_id: int, dept_id: int
) -> Message:
    """占位 assistant 消息（Task 9 语义：请求即落占位，生成完成/中断后二选一收尾）。"""
    msg = Message(
        conversation_id=conv.id, user_id=admin_id, dept_id=dept_id, role="assistant", content=""
    )
    session.add(msg)
    session.flush()
    return msg


def test_finalize_turn_updates_assistant_message_fields(session: Session) -> None:
    """finalize 八字段逐项 UPDATE 并 flush：同事务读回断言（flush 不 commit）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    conv = _add_conversation(session, ids["service"], admin)
    msg = _add_assistant_placeholder(session, conv, admin, ids["service"])
    citations = [
        {
            "n": 1,
            "refType": "doc",
            "refId": 9,
            "title": "假勤制度",
            "page": 3,
            "titlePath": "假勤/年假",
            "score": 0.9,
        }
    ]

    finalize_turn(
        session,
        msg.id,
        content="年假按工龄五到十天 [1]",
        citations=citations,
        answer_type="rag",
        model="glm-4",
        usage=(120, 30),
        latency_ms=850,
        cache_hit=False,
    )

    row = session.execute(select(Message).where(Message.id == msg.id)).scalar_one()
    assert (row.content, row.answer_type, row.model) == ("年假按工龄五到十天 [1]", "rag", "glm-4")
    assert row.citations == citations
    assert (row.prompt_tokens, row.completion_tokens) == (120, 30)
    assert (row.latency_ms, row.cache_hit, row.is_active) == (850, False, True)


def test_finalize_turn_none_usage_writes_zero_tokens(session: Session) -> None:
    """usage=None（拒答未调 LLM 等 last_usage 缺席场景）→ prompt/completion tokens 双 0。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    conv = _add_conversation(session, ids["service"], admin)
    msg = _add_assistant_placeholder(session, conv, admin, ids["service"])

    finalize_turn(
        session,
        msg.id,
        content=REFUSAL_TEXT,
        citations=[],
        answer_type="refuse",
        model="glm-4",
        usage=None,
        latency_ms=5,
        cache_hit=False,
    )

    row = session.execute(select(Message).where(Message.id == msg.id)).scalar_one()
    assert (row.content, row.answer_type, row.citations) == (REFUSAL_TEXT, "refuse", [])
    assert (row.prompt_tokens, row.completion_tokens) == (0, 0)


def test_mark_interrupted_flags_assistant_message_inactive(session: Session) -> None:
    """CHAT-12：中断只置 is_active=false，内容与引用保持占位原样（不重复落库）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    conv = _add_conversation(session, ids["service"], admin)
    msg = _add_assistant_placeholder(session, conv, admin, ids["service"])

    mark_interrupted(session, msg.id)

    row = session.execute(select(Message).where(Message.id == msg.id)).scalar_one()
    assert row.is_active is False
    assert row.content == "" and row.citations is None  # 占位未被覆写
