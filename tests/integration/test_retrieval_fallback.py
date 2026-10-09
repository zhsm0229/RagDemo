r"""检索上游故障保护全链测试（retrieval-policy 工单 01，spec §3/§8）：兜底与降级分轨。

覆盖三段：① rerank_and_fold 兜底缝（纯函数级，Stub 抛 UpstreamError → 向量序 +
degraded_out 收集缝，主路径不传零改动）；② prepare_turn 降级全链（真 pg：降级拒答
阈值分轨、降级态 rag 计划不写缓存、embed 失败分支、降级态部门隔离同尺、trace 口径
标注）；③ finalize/读回 messages.degraded 落库（迁移 0012）。测试全 Stub——CPU TEI
基线在跑，严禁真容器故障注入；降级态分数口径=召回余弦（standards §4.4 与 rerank
sigmoid 分绝不互比，trace 以 scoreScale="cosine" 标注）。
"""

import hashlib
import math
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.assembly import load_prompt_pack
from app.core.clock import FakeClock
from app.core.config import Settings
from app.core.llm import StubEmbedder, StubLLMProvider, StubReranker
from app.db.models import Chunk, Conversation, Department, Document, Message, User
from app.rag.prompt import REFUSAL_TEXT
from app.services.chat import ChatDeps, TurnPlan, finalize_turn, prepare_turn
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.retrieval import DocHit, PgVectorStore, rerank_and_fold

# ---------------------------------------------------------------------------
# ① 兜底缝（纯函数级）种子：手工 DocHit（单位轴向量两两余弦 0，折叠互不干扰）
# ---------------------------------------------------------------------------


def _hit(chunk_id: int, similarity: float, *, x: float, y: float) -> DocHit:
    """similarity 即召回余弦口径（search_chunks 产物形态），折叠不重算该值。"""
    return DocHit(
        chunk_id=chunk_id,
        document_id=1,
        title="标题",
        page_no=None,
        title_path="路径",
        content="正文",
        similarity=similarity,
        embedding=[x, y],
        doc_type="general",
    )


def test_rerank_and_fold_catches_upstream_error_and_falls_back_to_vector_order() -> None:
    """rerank 抛 UpstreamError：向量序兜底（保持召回余弦降序入参序），similarity 保持
    召回余弦不冒充 rerank 分；去重照常在兜底序上进行（同向低分项被折叠）。"""
    hits = [_hit(1, 0.9, x=1.0, y=0.0), _hit(2, 0.8, x=0.0, y=1.0), _hit(3, 0.7, x=0.6, y=0.8)]

    kept = rerank_and_fold(StubReranker(fail_times=1), "查询", hits, top_n=3, dedup_sim=0.98)

    assert [hit.chunk_id for hit in kept] == [1, 2, 3]  # 向量序原样（两两正交不折叠）
    assert [hit.similarity for hit in kept] == [0.9, 0.8, 0.7]  # 召回余弦原样（非 rerank 分）

    near_dup = [_hit(1, 0.9, x=1.0, y=0.0), _hit(2, 0.8, x=0.0, y=1.0), _hit(3, 0.7, x=1.0, y=0.0)]
    deduped = rerank_and_fold(StubReranker(fail_times=1), "查询", near_dup, top_n=3, dedup_sim=0.5)
    assert [hit.chunk_id for hit in deduped] == [
        1,
        2,
    ]  # 3 与高分保留项 1 同向（余弦 1.0 ≥ 0.5）折叠
    assert deduped[1].similarity == 0.8  # 保留项分数仍为召回余弦


def test_rerank_and_fold_degraded_out_seam_marks_fallback_and_success() -> None:
    """degraded_out 收集缝（ranked_out 同款）：降级轮 append True、成功轮 append False、
    空候选短路不触碰收集列表；主路径不传该缝行为零改动。"""
    hits = [_hit(1, 0.9, x=1.0, y=0.0), _hit(2, 0.8, x=0.0, y=1.0)]
    degraded_marks: list[bool] = []

    kept = rerank_and_fold(
        StubReranker(fail_times=1),
        "查询",
        hits,
        top_n=2,
        dedup_sim=0.98,
        degraded_out=degraded_marks,
    )
    assert degraded_marks == [True]
    assert [hit.chunk_id for hit in kept] == [1, 2]

    ok_marks: list[bool] = []
    rerank_and_fold(
        StubReranker(weights=[3.0, 1.0]),
        "查询",
        hits,
        top_n=2,
        dedup_sim=0.98,
        degraded_out=ok_marks,
    )
    assert ok_marks == [False]

    empty_marks: list[bool] = []
    rerank_and_fold(
        StubReranker(fail_times=1), "查询", [], top_n=2, dedup_sim=0.98, degraded_out=empty_marks
    )
    assert empty_marks == []  # 空候选短路发生在 rerank 之前：无降级语义

    plain = rerank_and_fold(StubReranker(fail_times=1), "查询", hits, top_n=2, dedup_sim=0.98)
    assert [hit.chunk_id for hit in plain] == [1, 2]  # 缺省（主问答路径）不传缝：零改动


def test_rerank_and_fold_degraded_top_n_callable_and_ranked_out_use_recall_cosine() -> None:
    """降级态 top_n callable 在向量序上求值（docstring 口径：top1 为向量序 top1）；
    ranked_out 写 (hit, 召回余弦)——similarity 与 score 均为召回余弦，禁止冒充 rerank 分。"""
    hits = [_hit(1, 0.9, x=1.0, y=0.0), _hit(2, 0.8, x=0.0, y=1.0), _hit(3, 0.7, x=0.6, y=0.8)]
    received_orders: list[list[int]] = []

    def top_n_by_vector_order(seq: list[DocHit]) -> int:
        received_orders.append([hit.chunk_id for hit in seq])
        return 2

    ranked_out: list[tuple[DocHit, float]] = []
    degraded_marks: list[bool] = []

    kept = rerank_and_fold(
        StubReranker(fail_times=1),
        "查询",
        hits,
        top_n_by_vector_order,
        0.98,
        ranked_out=ranked_out,
        degraded_out=degraded_marks,
    )

    assert received_orders == [[1, 2, 3]]  # callable 拿到的是向量序（非 rerank 序）
    assert [hit.chunk_id for hit in kept] == [1, 2]
    assert [(hit.chunk_id, hit.similarity) for hit, _ in ranked_out] == [
        (1, 0.9),
        (2, 0.8),
        (3, 0.7),  # top_n 之外候选也入收集缝，分=召回余弦
    ]
    assert all(hit.similarity == pytest.approx(score) for hit, score in ranked_out)
    assert degraded_marks == [True]


# ---------------------------------------------------------------------------
# ②/③ prepare_turn 降级全链与落库（真 pg + Stub；种子助手同 test_chat_flow 口径）
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


def _add_chunk(session: Session, doc: Document, content: str, embedder: StubEmbedder) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=0,
        embedding=embedder.embed([content])[0],
    )
    session.add(chunk)
    session.flush()
    return chunk


def _make_deps(
    embedder: StubEmbedder,
    reranker: StubReranker,
    llm: StubLLMProvider,
    *,
    fallback_refuse_threshold: float = 0.60,
) -> ChatDeps:
    """缺省把降级拒答阈值覆写为 0.60（spec §8 分轨用例 0.58 拒/0.61 过的给定口径；
    生产缺省 0.63 由 tests/unit/test_config.py 锁定）。"""
    settings = Settings(
        jwt_secret="retrieval-fallback-test",
        test_mode=True,
        fallback_refuse_threshold=fallback_refuse_threshold,
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
        store=PgVectorStore(),
    )


def _vector_with_cosine(
    embedder: StubEmbedder, query: str, target: float, salt: str
) -> list[float]:
    """构造与 query 向量余弦恰为 target 的单位向量：salt 向量正交化到 query 的正交补
    空间后按 (target, √(1-target²)) 线性组合（降级阈值分轨的精确分数来源）。"""
    q = embedder.embed([query])[0]
    other = embedder.embed([salt])[0]
    dot = sum(a * b for a, b in zip(q, other, strict=True))
    ortho = [b - dot * a for a, b in zip(q, other, strict=True)]
    norm = math.sqrt(sum(component * component for component in ortho))
    ortho = [component / norm for component in ortho]
    s = math.sqrt(1.0 - target * target)
    return [target * a + s * b for a, b in zip(q, ortho, strict=True)]


def test_prepare_turn_degraded_refuse_uses_fallback_threshold(session: Session) -> None:
    """rerank 降级 + 召回余弦 0.58 < 降级阈值 0.60：拒答且计划 degraded=True、不写缓存；
    若误用主阈值 0.55 则 0.58 会放行——分轨方向由本用例钉死。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_降级拒答样本")
    question = "降级拒答阈值分轨查询"
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=question,
        content_chars=len(question),
        position=0,
        embedding=_vector_with_cosine(embedder, question, 0.58, "正交扰动salt"),
    )
    session.add(chunk)
    session.flush()
    deps = _make_deps(embedder, StubReranker(fail_times=1), StubLLMProvider())

    plan = prepare_turn(session, deps, ids["service"], ids["all"], question, [])

    assert plan.kind == "refuse"
    assert plan.degraded is True
    assert plan.citations == [] and plan.llm_messages is None
    assert plan.cache_key is None  # 降级拒答同 refuse 口径不写缓存
    assert deps.hit_counter._counts == {}  # 拒答路径不计召回命中


def test_prepare_turn_degraded_rag_passes_fallback_threshold_without_cache_key(
    session: Session,
) -> None:
    """rerank 降级 + 召回余弦 0.61 ≥ 降级阈值 0.60：放行 rag 且计划 degraded=True、
    cache_key=None（降级态不写精确缓存，spec §11 裁决 2）；citation score=召回余弦。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_降级放行样本")
    question = "降级放行阈值分轨查询"
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=question,
        content_chars=len(question),
        position=0,
        embedding=_vector_with_cosine(embedder, question, 0.61, "正交扰动salt"),
    )
    session.add(chunk)
    session.flush()
    deps = _make_deps(embedder, StubReranker(fail_times=1), StubLLMProvider())

    plan = prepare_turn(session, deps, ids["service"], ids["all"], question, [])

    assert plan.kind == "rag"
    assert plan.degraded is True
    assert plan.cache_key is None  # 降级态 rag 计划不写缓存（避免混尺分数入缓存）
    assert deps.cache.get(deps.cache.make_key(ids["service"], question)) is None
    assert [citation["refId"] for citation in plan.citations] == [chunk.id]
    assert plan.citations[0]["score"] == pytest.approx(0.61, abs=1e-6)  # 召回余弦口径
    assert deps.hit_counter._counts == {chunk.id: 1}  # 放行的降级 rag 仍计召回命中


def test_prepare_turn_degraded_trace_marks_cosine_scale(session: Session) -> None:
    """评测缝（ranked_out 非 None）：降级轮 trace 增 degraded=True 与 scoreScale="cosine"
    （禁止冒充 rerank 分）；非降级轮 trace 不含这两键（既有形状逐字不变）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_降级trace样本")
    question = "降级trace口径查询"
    _add_chunk(session, doc, question, embedder)  # 同文：召回余弦≈1.0 ≥ 0.60 放行
    ranked_out: list[tuple[DocHit, float]] = []
    deps = _make_deps(embedder, StubReranker(fail_times=1), StubLLMProvider())

    plan = prepare_turn(
        session, deps, ids["service"], ids["all"], question, [], ranked_out=ranked_out
    )

    assert plan.kind == "rag" and plan.degraded is True
    assert plan.retrieval_trace is not None
    assert plan.retrieval_trace["degraded"] is True
    assert plan.retrieval_trace["scoreScale"] == "cosine"
    assert ranked_out  # 候选照常入收集缝（分数=召回余弦）

    ranked_out_ok: list[tuple[DocHit, float]] = []
    ok_deps = _make_deps(embedder, StubReranker(weights=[2.0]), StubLLMProvider())
    ok_plan = prepare_turn(
        session, ok_deps, ids["service"], ids["all"], question, [], ranked_out=ranked_out_ok
    )
    assert ok_plan.kind == "rag" and ok_plan.degraded is False
    assert ok_plan.retrieval_trace is not None
    assert set(ok_plan.retrieval_trace.keys()) == {"contextChunkIds", "topKCandidates"}


def test_prepare_turn_embed_failure_yields_refuse_plan_with_degraded(session: Session) -> None:
    """embed 上游故障（捕获层=prepare_turn，spec §11 裁决 6）：拒答话术计划 + degraded=True，
    answer_type 恒 refuse（kind='refuse'），异常绝不外泄，rerank/LLM 零调用。"""
    ids = _dept_ids(session)
    embedder = StubEmbedder(fail_times=1)
    reranker = StubReranker()
    llm = StubLLMProvider()
    deps = _make_deps(embedder, reranker, llm)

    plan = prepare_turn(session, deps, ids["service"], ids["all"], "完全无关的天书问题", [])

    assert plan.kind == "refuse"
    assert plan.degraded is True
    assert plan.citations == [] and plan.llm_messages is None
    assert plan.cache_key is None
    assert reranker.rerank_calls == 0 and llm.stream_calls == 0


def test_prepare_turn_degraded_fallback_keeps_department_isolation(session: Session) -> None:
    """降级兜底路径与主路径同尺（spec §8）：hr 视角看不到 service 专属块——降级轮
    （rerank Stub 抛错）照样拒答；service 视角兜底命中且引用不含外部门块。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    service_doc = _add_document(session, ids["service"], admin, "客服_专属降级文档")
    _add_chunk(session, service_doc, "客服专属退货流程正文", embedder)
    hr_doc = _add_document(session, ids["hr"], admin, "人事_专属降级文档")
    hr_chunk = _add_chunk(session, hr_doc, "人事专属考勤流程正文", embedder)

    hr_deps = _make_deps(embedder, StubReranker(fail_times=1), StubLLMProvider())
    hr_plan = prepare_turn(session, hr_deps, ids["hr"], ids["all"], "客服专属退货流程正文", [])
    assert hr_plan.kind == "refuse"  # service 块对 hr 不可见 → 空召回拒答
    assert hr_plan.degraded is True
    assert hr_plan.citations == []

    service_deps = _make_deps(embedder, StubReranker(fail_times=1), StubLLMProvider())
    service_plan = prepare_turn(
        session, service_deps, ids["service"], ids["all"], "客服专属退货流程正文", []
    )
    assert service_plan.kind == "rag"
    assert service_plan.degraded is True
    assert [citation["refId"] for citation in service_plan.citations] != [hr_chunk.id]
    assert service_plan.citations  # 兜底命中自身部门块


def test_finalize_turn_persists_degraded_flag_with_readback(session: Session) -> None:
    """messages.degraded 落库读回（迁移 0012）：degraded=True 写入并读回 true；缺省
    （正常轮）保持 false——重开会话标注不丢失的前提是列在库。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    conv = Conversation(user_id=admin, dept_id=ids["service"], title="降级标注测试会话")
    session.add(conv)
    session.flush()
    msg = Message(
        conversation_id=conv.id, user_id=admin, dept_id=ids["service"], role="assistant", content=""
    )
    session.add(msg)
    session.flush()

    finalize_turn(
        session,
        msg.id,
        content=REFUSAL_TEXT,
        citations=[],
        answer_type="refuse",
        model="stub",
        usage=None,
        latency_ms=5,
        cache_hit=False,
        degraded=True,
    )

    row = session.execute(select(Message).where(Message.id == msg.id)).scalar_one()
    assert row.degraded is True
    assert row.answer_type == "refuse"

    normal = Message(
        conversation_id=conv.id, user_id=admin, dept_id=ids["service"], role="assistant", content=""
    )
    session.add(normal)
    session.flush()
    finalize_turn(
        session,
        normal.id,
        content="正常回答",
        citations=[],
        answer_type="rag",
        model="stub",
        usage=(10, 5),
        latency_ms=20,
        cache_hit=False,
    )
    normal_row = session.execute(select(Message).where(Message.id == normal.id)).scalar_one()
    assert normal_row.degraded is False  # 正常轮缺省 false


def test_turn_plan_degraded_defaults_false_for_normal_plans() -> None:
    """TurnPlan.degraded 缺省 False（retrieval_trace 先例的计划内演进）：直构四分支
    计划不传该字段即为正常轮，下游 SSE/落库缺省口径一致。"""
    assert TurnPlan("rag", [], "问题", None, None, "key").degraded is False
    assert TurnPlan("qa", [], "问题", None, None, "key").degraded is False
    assert TurnPlan("refuse", [], "问题", None, None, None).degraded is False
    assert TurnPlan("cache", [], "问题", None, {}, None).degraded is False
