r"""agent 检索对齐主链路集成测试（retrieval-policy 工单 03，spec §6/§8）：真 pg + Stub 三件套。

覆盖工单 03 测试清单：① agent 与主链路同库同输入命中集与顺序对照一致（对照命中集与
折叠，不对照 rerank 分——agent 无精排）；② match_qa 直答通道命中跳过文档检索、引用
形态与主链路 qa 计划等价；③ 0.85 阈值边界两向（0.84 不直答回落文档 / 0.87 直答）；
④ 跨部门问答对不进直答通道（F-502 部门隔离在 agent 侧同尺）；⑤ qa 答案 excerpt 500
字截断；⑥ 召回参数读 RetrievalPolicy 与主链路同源。近重复向量按 base×100+noise 重
归一化构造（余弦 ≈0.99995 确超 0.98 折叠阈值），精确余弦向量按正交补空间线性组合
构造（0.84/0.87 边界分数来源）。

测试全 Stub——严禁真 TEI 调用（CPU 上有基线跑分）；种子 ORM 直插，读回断言。
fixtures 见 tests/conftest.py：session（每测试独立事务，结束整体回滚，用例互不污染）。
"""

import hashlib
import math
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.llm import StubEmbedder, StubReranker
from app.core.tools import RunContext
from app.db.models import Chunk, Department, Document, QaPair, User
from app.rag.retrieval_policy import resolve_policy
from app.services.agent_tools import KnowledgeBaseSearchTool, KnowledgeSearchDeps
from app.services.retrieval import PgVectorStore, rerank_and_fold, search_chunks

# ---------------------------------------------------------------------------
# 种子助手：ORM 直插（test_retrieval.py 同款口径），content_hash 以标题摘要保证唯一
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
    embedding: list[float] | None = None,
) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=position,
        embedding=embedding if embedding is not None else embedder.embed([content])[0],
    )
    session.add(chunk)
    session.flush()
    return chunk


def _near(base: list[float], noise: list[float]) -> list[float]:
    """与 base 余弦≈0.99995 的近重复向量：100×base+noise 后重归一化。"""
    mixed = [b * 100.0 + n for b, n in zip(base, noise, strict=True)]
    norm = math.sqrt(sum(component * component for component in mixed))
    return [component / norm for component in mixed]


def _vector_with_cosine(
    embedder: StubEmbedder, target_text: str, target: float, salt: str
) -> list[float]:
    """构造与 target_text 向量余弦恰为 target 的单位向量（正交补空间线性组合）——
    qa 阈值边界用例的精确分数来源（test_retrieval_fallback 同款）。"""
    base = embedder.embed([target_text])[0]
    other = embedder.embed([salt])[0]
    dot = sum(a * b for a, b in zip(base, other, strict=True))
    ortho = [b - dot * a for a, b in zip(base, other, strict=True)]
    norm = math.sqrt(sum(component * component for component in ortho))
    ortho = [component / norm for component in ortho]
    rest = math.sqrt(1.0 - target * target)
    return [target * a + rest * b for a, b in zip(base, ortho, strict=True)]


class _OverrideEmbedder:
    """指定文本回预制向量、其余走 StubEmbedder：边界用例把查询向量钉到精确余弦。"""

    def __init__(self, overrides: dict[str, list[float]]) -> None:
        self._overrides = overrides
        self._stub = StubEmbedder()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            self._overrides[text] if text in self._overrides else self._stub.embed([text])[0]
            for text in texts
        ]


class _RecordingPgStore(PgVectorStore):
    """真 pg 实现 + search_chunks 计数：直答通道"跳过文档检索"断言的证据点。"""

    def __init__(self) -> None:
        self.search_calls = 0

    def search_chunks(self, db, query_vec, dept_ids, top_k, ef_search):
        self.search_calls += 1
        return super().search_chunks(db, query_vec, dept_ids, top_k, ef_search)


# ---------------------------------------------------------------------------
# 工具装配助手
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        jwt_secret="agent-align-test",
        test_mode=True,
        retriever_top_k=20,
        rerank_top_n=5,
        chunk_dedup_sim=0.98,
        hnsw_ef_search=40,
    )


def _make_tool(
    session: Session, embedder: StubEmbedder | _OverrideEmbedder | None = None
) -> tuple[KnowledgeBaseSearchTool, _RecordingPgStore]:
    """agent 工具 + 真 pg store（Stub 三件套：embedder/reranker 全 Stub，零真 TEI）。"""
    store = _RecordingPgStore()
    deps = KnowledgeSearchDeps(
        embedder=embedder if embedder is not None else StubEmbedder(),
        store=store,
        reranker=StubReranker(),  # agent 纯向量序：reranker 仅为协议件，不被调用
        settings=_settings(),
    )
    return KnowledgeBaseSearchTool(deps), store


def _ctx(session: Session, dept_ids: tuple[int, ...]) -> RunContext:
    return RunContext(user_id=_admin_id(session), username="admin", dept_ids=dept_ids, db=session)


# ---------------------------------------------------------------------------
# ① 同库同输入：agent 与主链路命中集与顺序对照一致（不对照 rerank 分）
# ---------------------------------------------------------------------------


def test_agent与主链路同库同输入_命中集与顺序对照一致(session: Session) -> None:
    """同库同输入：主链路检索核心（search_chunks + rerank_and_fold，权重按召回序对位
    注入使 rerank 序=向量序）与 agent 工具产物逐项相等（excerpt 序=折叠序）；近重复块
    两侧同折；分数不对照——agent 是召回余弦、主链路是 rerank sigmoid 分（§4.4 绝不互比）。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_对照样本")
    base = embedder.embed(["差旅报销标准正文"])[0]
    chunk1 = _add_chunk(session, doc, "差旅报销标准正文", embedder, position=1)
    near = _near(base, embedder.embed(["扰动噪声"])[0])
    _add_chunk(session, doc, "差旅报销流程说明", embedder, position=2, embedding=near)
    _add_chunk(session, doc, "住宿费报销标准说明", embedder, position=3)
    _add_chunk(session, doc, "交通费报销标准说明", embedder, position=4)
    _add_chunk(session, doc, "审批流转节点说明", embedder, position=5)
    query = "差旅报销"

    # 主链路检索核心（chat._retrieve_folded 同函数）：rerank 序对位召回序，折叠同尺
    query_vec = embedder.embed([query])[0]
    recall = search_chunks(session, query_vec, [ids["service"], ids["all"]], 20, 40)
    assert len(recall) == 5  # 前提：5 候选全召回，折叠发生在同窗之内
    weights = [5.0, 4.0, 3.0, 2.0, 1.0]  # 召回序即 rerank 序：两侧折叠行为完全同构
    main = rerank_and_fold(
        StubReranker(weights=weights), query, recall, _settings().rerank_top_n, 0.98
    )
    assert len(main) == 4  # 前提：近重复对确有一侧被折叠（主链路折叠真实发生）

    tool, store = _make_tool(session)
    result = tool.invoke({"query": query}, _ctx(session, (ids["service"], ids["all"])))

    assert result.ok is True
    assert store.search_calls == 1
    items = result.value["items"]
    assert [item["excerpt"] for item in items] == [hit.content for hit in main]  # 命中集与顺序一致
    # 分数不对照（agent 无精排）：agent 分=该块召回余弦（search_chunks 口径，工具侧
    # round 4 位 → 容差 5e-5），非 rerank sigmoid 分（两套分数绝不互比，§4.4）
    recall_by_content = {hit.content: hit for hit in recall}
    for item in items:
        hit = recall_by_content[item["excerpt"]]
        assert item["score"] == pytest.approx(hit.similarity, abs=5e-5)
    assert chunk1.title_path == doc.title  # 读回锚点：种子与召回同源


# ---------------------------------------------------------------------------
# ②/③/⑤ match_qa 直答通道：命中跳过文档检索、阈值边界、excerpt 截断
# ---------------------------------------------------------------------------


def test_agent_qa直答通道命中_真pg_跳过文档检索(session: Session) -> None:
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_直答样本")
    _add_chunk(session, doc, "退货时限正文块", embedder)
    qa = _add_qa(session, ids["service"], admin, "退货时限是多少", "七天内可退货", embedder)

    tool, store = _make_tool(session)
    result = tool.invoke({"query": "退货时限是多少"}, _ctx(session, (ids["service"], ids["all"])))

    assert result.ok is True
    assert store.search_calls == 0  # 直答命中：文档检索未发起
    item = result.value["items"][0]
    assert item["refType"] == "qa"
    assert item["refId"] == qa.id  # 读回：引用指向真实问答对主键
    assert item["title"] == "退货时限是多少"
    assert item["excerpt"] == "七天内可退货"
    assert item["score"] == pytest.approx(1.0, abs=1e-6)  # 同文余弦相似度=1 ≥ 0.85


def test_agent_qa阈值边界_0点84不直答回落文档_0点87直答(session: Session) -> None:
    """低于 0.85 边界反向：余弦 0.84 不进直答通道、回落文档检索；0.87 进直答。
    （match_qa 语义为 similarity < threshold 即不命中，边界方向由本用例钉死。）"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    question = "试用期社保缴纳比例是多少"
    _add_qa(session, ids["service"], admin, question, "试用期社保按转正基数缴纳", embedder)
    doc = _add_document(session, ids["service"], admin, "客服_社保文档")
    chunk = _add_chunk(session, doc, "社保缴纳正文块", embedder)

    below = _OverrideEmbedder({question: _vector_with_cosine(embedder, question, 0.84, "盐")})
    tool_below, store_below = _make_tool(session, below)
    result = tool_below.invoke({"query": question}, _ctx(session, (ids["service"], ids["all"])))

    assert store_below.search_calls == 1  # 未直答：回落文档检索
    assert result.value["items"][0]["excerpt"] == chunk.content
    assert "refType" not in result.value["items"][0]
    assert "试用期社保按转正基数缴纳" not in str(result.value)  # qa 答案未走直答通道

    above = _OverrideEmbedder({question: _vector_with_cosine(embedder, question, 0.87, "盐")})
    tool_above, store_above = _make_tool(session, above)
    result = tool_above.invoke({"query": question}, _ctx(session, (ids["service"], ids["all"])))

    assert store_above.search_calls == 0  # ≥0.85：直答命中
    assert result.value["items"][0]["refType"] == "qa"
    assert result.value["items"][0]["score"] == pytest.approx(0.87, abs=1e-4)


def test_agent_跨部门问答对不进直答通道_部门隔离(session: Session) -> None:
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    question = "外部门直答问题文本"
    qa = _add_qa(session, ids["hr"], admin, question, "外部门答案", embedder)

    tool, store = _make_tool(session)
    result = tool.invoke({"query": question}, _ctx(session, (ids["service"], ids["all"])))

    assert result.ok is True
    assert store.search_calls == 1  # 直答通道未命中：已回落文档检索
    assert result.value == {"items": [], "note": "知识库中未找到相关内容"}  # 空手拒答话术不变
    assert "外部门答案" not in str(result.value)  # 跨部门问答对不可泄漏（F-502）

    hr_tool, _hr_store = _make_tool(session)
    hr_result = hr_tool.invoke({"query": question}, _ctx(session, (ids["hr"], ids["all"])))
    assert hr_result.value["items"][0]["refId"] == qa.id  # 对照：本部门视角直答命中


def test_agent_qa答案摘录500字截断(session: Session) -> None:
    ids = _dept_ids(session)
    embedder = StubEmbedder()
    _add_qa(session, ids["service"], _admin_id(session), "长答案问题文本", "长" * 600, embedder)

    tool, _store = _make_tool(session)
    result = tool.invoke({"query": "长答案问题文本"}, _ctx(session, (ids["service"], ids["all"])))

    excerpt = result.value["items"][0]["excerpt"]
    assert len(excerpt) == 500  # qa 答案与文档摘录同闸：agent 上下文预算 500 字
    assert excerpt == "长" * 500


# ---------------------------------------------------------------------------
# ⑥ 检索参数读 policy：agent 与主链路召回参数同源（resolve_policy 缺省档）
# ---------------------------------------------------------------------------


def test_agent召回参数与主链路policy同源(session: Session) -> None:
    """agent search_chunks 收到 policy.top_k/ef_search（resolve_policy 缺省 20/40），
    与主链路 _retrieve_folded 同源——参数分叉即红。"""
    ids = _dept_ids(session)
    admin = _admin_id(session)
    embedder = StubEmbedder()
    doc = _add_document(session, ids["service"], admin, "客服_policy样本")
    _add_chunk(session, doc, "policy参数正文", embedder)
    policy = resolve_policy("policy参数正文")
    captured: list[tuple[int, int]] = []

    class _CaptureStore(_RecordingPgStore):
        def search_chunks(self, db, query_vec, dept_ids, top_k, ef_search):
            captured.append((top_k, ef_search))
            return super().search_chunks(db, query_vec, dept_ids, top_k, ef_search)

    deps = KnowledgeSearchDeps(
        embedder=embedder,
        store=_CaptureStore(),
        reranker=StubReranker(),
        settings=_settings(),
    )
    result = KnowledgeBaseSearchTool(deps).invoke(
        {"query": "policy参数正文"}, _ctx(session, (ids["service"], ids["all"]))
    )

    assert result.ok is True
    assert captured == [(policy.top_k, policy.ef_search)]  # 与主链路同读 RetrievalPolicy
