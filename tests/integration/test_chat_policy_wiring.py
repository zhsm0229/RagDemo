r"""问答链路策略接线集成测试（retrieval-policy §2，工单 02；真 pg + Stub 三件套）。

两段覆盖：① 接线零变化见证——_retrieve_folded 改读 resolve_policy 产物后，同输入
计划与既有断言逐字一致（spec §2 缺省等价，等价证明由本用例与 test_chat_flow.py
既有用例共同承担）；② 接线辨识——召回候选数读 policy 缺省 20 而非
settings.retriever_top_k（settings 调成 1 仍召回全部候选），钉死检索参数唯一来源。
独立成文件因 test_chat_flow.py 已触 §1.4 测试文件 800 行上限（同被测模块 chat.py，
拆分非另起炉灶）；种子助手按本套件惯例本地最小复制（test_chat_flow.py 同款语义）。

工单 06 追加（retrieval-policy §4）：multi_query 双门控集成行为——cpu 档恒关（改写
与召回逐字现状）、gpu 档三产物成功走多向量一次 embed + 多路召回合并去重 + 合并候选
单次精排（rerank ≤3 次预算）、三产物解析失败降级单路绝不炸轮、无历史不走改写升级。
"""

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.assembly import load_prompt_pack
from app.core.clock import FakeClock
from app.core.config import Settings
from app.core.llm import StubEmbedder, StubLLMProvider, StubReranker, sigmoid
from app.db.models import Chunk, Department, Document, User
from app.services.chat import ChatDeps, prepare_turn
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.retrieval import (
    PgVectorStore,
    merge_multi_query_hits,
    normalize_question,
    rerank_and_fold,
    search_chunks,
)
from app.services.rewrite import REWRITE_MAX_TOKENS


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
        doc_type="general",
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    return doc


def _add_chunk(
    session: Session, doc: Document, content: str, embedder: StubEmbedder, *, position: int = 0
) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=position,
        page_no=None,
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
    settings: Settings | None = None,
) -> ChatDeps:
    if settings is None:  # 缺省口径与 test_chat_flow._make_deps 逐字一致
        settings = Settings(jwt_secret="chat-flow-test", test_mode=True)
    return ChatDeps(
        embedder=embedder,
        reranker=reranker,
        llm=llm,
        cache=ExactCache(FakeClock("2026-10-15T09:30:00+00:00"), settings.cache_ttl_s),
        counter=DailyCounter(FakeClock("2026-10-15T09:30:00+00:00")),
        hit_counter=HitCounter(FakeClock("2026-10-15T09:30:00+00:00")),
        settings=settings,
        prompts=load_prompt_pack(),
        store=PgVectorStore(),
    )


def test_prepare_turn_plan_verbatim_after_policy_wiring(session: Session) -> None:
    """接线零变化见证：_retrieve_folded 改读 resolve_policy 产物后，同输入计划与既有
    断言逐字一致——citations 七字段、llm_messages、cache_key、degraded/trace 全部
    保持接线前口径（spec §2 缺省等价：top_k/ef_search 走 policy 缺省 20/40，与
    settings 旧缺省同值）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_policy接线样本")
    chunk = _add_chunk(session, doc, "考勤政策接线正文", embedder)
    deps = _make_deps(embedder, StubReranker(weights=[2.0]), StubLLMProvider())
    question = "考勤政策接线正文"

    plan = prepare_turn(session, deps, ids["service"], ids["all"], question, [])

    assert plan.kind == "rag"
    assert plan.citations == [
        {
            "n": 1,
            "refType": "doc",
            "refId": chunk.id,
            "docId": doc.id,
            "title": doc.title,
            "page": None,
            "titlePath": doc.title,
            "score": pytest.approx(sigmoid(2.0)),
        }
    ]
    assert plan.llm_messages == [
        {"role": "system", "content": deps.prompts.chat_system},
        {
            "role": "user",
            "content": f"<data>\n[1] {doc.title}｜{doc.title}\n考勤政策接线正文\n</data>\n\n{question}",
        },
    ]
    assert plan.cache_key == deps.cache.make_key(ids["service"], normalize_question(question))
    assert plan.degraded is False and plan.retrieval_trace is None


def test_retrieve_folded_reads_top_k_from_policy_not_settings(session: Session) -> None:
    """接线辨识：召回候选数读 policy 缺省 20 而非 settings.retriever_top_k——settings
    调成 1 时同输入仍召回全部 3 块。若无本辨识，读 settings 的旧实现同样能通过零变化
    断言（接线方向钉死：检索参数唯一来源=RetrievalPolicy，spec §2）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_policy接线辨识")
    c1 = _add_chunk(session, doc, "接线辨识候选一", embedder, position=1)
    c2 = _add_chunk(session, doc, "接线辨识候选二", embedder, position=2)
    c3 = _add_chunk(session, doc, "接线辨识候选三", embedder, position=3)
    question = "接线辨识查询"
    recall = search_chunks(
        session, embedder.embed([question])[0], [ids["service"], ids["all"]], top_k=20, ef_search=40
    )
    assert len(recall) == 3  # 前提：3 块候选在 top_k=20 下全部可召回
    logit_of = {c1.id: 3.0, c2.id: 2.0, c3.id: 1.0}
    deps = _make_deps(
        embedder,
        StubReranker(weights=[logit_of[hit.chunk_id] for hit in recall]),
        StubLLMProvider(),
        settings=Settings(jwt_secret="chat-flow-test", test_mode=True, retriever_top_k=1),
    )

    plan = prepare_turn(session, deps, ids["service"], ids["all"], question, [])

    assert plan.kind == "rag"
    # settings.retriever_top_k=1 未生效（否则召回仅 1 块、citations ≤1）：
    assert [citation["refId"] for citation in plan.citations] == [c1.id, c2.id, c3.id]


# ---------------------------------------------------------------------------
# multi_query 双门控与三产物改写升级（retrieval-policy §4，工单 06）
# ---------------------------------------------------------------------------

_MULTI_HOP_QUESTION = "年假和病假分别有什么区别"  # L0 恰 2 类：区别（比较）+ 分别（多实体）
_HISTORY: list[tuple[str, str]] = [
    ("user", "公司的假勤制度有哪些"),
    ("assistant", "年假按工龄五到十天 [1]"),
]
_MULTI_JSON = (
    '{"rewritten": "年假和病假分别有什么规定", "intent": "multi_hop", '
    '"subQueries": ["年假天数怎么规定", "病假证明要求是什么"]}'
)


class _RecordingEmbedder:
    """包装 StubEmbedder：记录每次 embed 调用携带的文本条数（一次多向量断言用）。"""

    def __init__(self, inner: StubEmbedder) -> None:
        self._inner = inner
        self.text_counts: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.text_counts.append(len(texts))
        return self._inner.embed(texts)


class _ScriptedLLM:
    """按调用次序弹出产出文本的替身：记录 (messages, max_tokens) 供提示词与钳制断言。"""

    def __init__(self, *outputs: str) -> None:
        self._outputs = list(outputs)
        self.calls: list[tuple[list[dict[str, str]], int]] = []

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        self.calls.append(([dict(message) for message in messages], max_tokens))
        text = self._outputs.pop(0) if self._outputs else ""
        yield text


def test_cpu档多跳问题门控关闭_改写与召回与现状逐字不变(session: Session) -> None:
    """门控关闭（缺省 cpu_constrained 档）：多跳信号问题仍走既有单产物改写路径——
    LLM 流恰 1 次（提示词/钳制同现状）、embed 恰 1 向量、计划逐字一致（spec §4 双门控：
    本机行为零变化）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_multiquery门控关")
    chunk = _add_chunk(session, doc, "改写后的问题", embedder)
    recording = _RecordingEmbedder(embedder)
    llm = _ScriptedLLM("改写后的问题")
    reranker = StubReranker(weights=[2.0])
    deps = _make_deps(recording, reranker, llm)  # settings 缺省：cpu_constrained 档

    plan = prepare_turn(session, deps, ids["service"], ids["all"], _MULTI_HOP_QUESTION, _HISTORY)

    assert plan.kind == "rag"
    assert plan.rewritten_question == "改写后的问题"
    assert [citation["refId"] for citation in plan.citations] == [chunk.id]
    ((messages, max_tokens),) = llm.calls
    assert llm.calls and len(llm.calls) == 1  # LLM 流次数与现状相同
    assert messages[0] == {"role": "system", "content": deps.prompts.rewrite_system}
    assert max_tokens == REWRITE_MAX_TOKENS  # 256：单产物路径钳制，非三产物的 150
    assert recording.text_counts == [1]  # 单向量 embed（多路未启用）
    assert reranker.rerank_calls == 1
    # char_budget rider 接线（缺省 None）：上下文块逐字现状（user 末尾=改写后问题）
    assert plan.llm_messages == [
        {"role": "system", "content": deps.prompts.chat_system},
        {
            "role": "user",
            "content": (
                f"<data>\n[1] {doc.title}｜{doc.title}\n改写后的问题\n</data>\n\n改写后的问题"
            ),
        },
    ]


def test_gpu档门控开启_三产物成功_多向量一次embed_合并去重_单次精排(session: Session) -> None:
    """gpu 档 + L0 multi_hop 信号 + 多轮历史：改写升级三产物——embed 一次调 3 向量、
    多路召回按 chunk_id 合并去重（6 路-候选坍缩为 2 块）、合并候选集 rerank 恰 1 次
    （≤3 次预算，spec §4）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_multiquery门控开")
    c1 = _add_chunk(session, doc, "年假天数按工龄计算", embedder, position=1)
    c2 = _add_chunk(session, doc, "病假需要医院证明", embedder, position=2)
    recording = _RecordingEmbedder(embedder)
    gpu_settings = Settings(jwt_secret="chat-flow-test", test_mode=True, compute_tier="gpu")
    # 期望合并集先行复算（与实现同规则），权重按 chunk 对位注入保证排序确定
    rewritten = "年假和病假分别有什么规定"
    vecs = embedder.embed([rewritten, "年假天数怎么规定", "病假证明要求是什么"])
    paths = [
        search_chunks(session, vec, [ids["service"], ids["all"]], top_k=20, ef_search=40)
        for vec in vecs
    ]
    merged = merge_multi_query_hits(paths)
    assert [hit.chunk_id for hit in merged] == [c1.id, c2.id]  # 每路 top20 召回全部 2 块：去重后 2
    weights = [3.0, 2.0]  # merged 序：c1 召回余弦高（与改写问题同主题词）
    reranker = StubReranker(weights=weights)
    deps = _make_deps(recording, reranker, _ScriptedLLM(_MULTI_JSON), settings=gpu_settings)

    plan = prepare_turn(session, deps, ids["service"], ids["all"], _MULTI_HOP_QUESTION, _HISTORY)

    assert plan.kind == "rag"
    assert plan.rewritten_question == rewritten  # 三产物之改写问题
    assert recording.text_counts == [3]  # 原问题+2 子查询：一次 embed 调用多向量
    assert reranker.rerank_calls == 1  # 合并候选集单次精排（≤3 次预算）
    ((messages, max_tokens),) = deps.llm.calls  # type: ignore[attr-defined]
    assert messages[0] == {"role": "system", "content": deps.prompts.rewrite_multi_system}
    assert max_tokens == 150  # 三产物整包输出钳制
    expected = rerank_and_fold(
        StubReranker(weights=weights),
        rewritten,
        merged,
        deps.settings.rerank_top_n,
        deps.settings.chunk_dedup_sim,
    )
    assert [citation["refId"] for citation in plan.citations] == [hit.chunk_id for hit in expected]
    assert plan.degraded is False


def test_gpu档三产物解析失败_降级单路不炸轮(session: Session) -> None:
    """gpu 档门控开但 LLM 产出非法 JSON：降级单路（原问题继续、单向量 embed），编排
    正常返回 rag 计划——与改写既有降级模式同款（失败静默回退）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_multiquery降级")
    chunk = _add_chunk(session, doc, _MULTI_HOP_QUESTION, embedder)  # 与原问题同文：余弦≈1
    recording = _RecordingEmbedder(embedder)
    gpu_settings = Settings(jwt_secret="chat-flow-test", test_mode=True, compute_tier="gpu")
    reranker = StubReranker(weights=[2.0])
    deps = _make_deps(recording, reranker, _ScriptedLLM("完全不是 JSON"), settings=gpu_settings)

    plan = prepare_turn(session, deps, ids["service"], ids["all"], _MULTI_HOP_QUESTION, _HISTORY)

    assert plan.kind == "rag"
    assert plan.rewritten_question == _MULTI_HOP_QUESTION  # 原问题继续
    assert [citation["refId"] for citation in plan.citations] == [chunk.id]
    assert recording.text_counts == [1]  # 降级单路：恰一向量
    assert reranker.rerank_calls == 1
    assert plan.degraded is False  # 降级单路≠上游降级：degraded 语义不混用


def test_gpu档无历史不走改写升级_单路现状(session: Session) -> None:
    """三产物是「改写升级」（retrieval-policy §4：multi_query 门控命中时改写升级）：
    无多轮历史则无改写调用，即使 gpu 档门控为真也单路检索、零 LLM 消费（每题 LLM
    ≤2 次预算不因预埋松动）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_multiquery无历史")
    chunk = _add_chunk(session, doc, _MULTI_HOP_QUESTION, embedder)
    recording = _RecordingEmbedder(embedder)
    gpu_settings = Settings(jwt_secret="chat-flow-test", test_mode=True, compute_tier="gpu")
    llm = _ScriptedLLM()
    deps = _make_deps(recording, StubReranker(weights=[2.0]), llm, settings=gpu_settings)

    plan = prepare_turn(session, deps, ids["service"], ids["all"], _MULTI_HOP_QUESTION, [])

    assert plan.kind == "rag"
    assert [citation["refId"] for citation in plan.citations] == [chunk.id]
    assert llm.calls == []  # 无历史：不发起改写（更不发起三产物）
    assert recording.text_counts == [1]
