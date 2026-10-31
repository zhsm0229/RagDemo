r"""检索服务集成测试（Task 5，真 pg + StubEmbedder 种子）：断言清单 ①-⑤ 逐条落地。

① 强制 JOIN documents（W1 移交最高优先级项①）：归档（is_active=false）与未就绪
  （status='parsing'）文档的 chunk 一律不命中——防泄漏红线（DOC-13 检索方向）。
② 部门隔离（F-502/RET-01/02）：dept_id IN (用户部门, 全员)，且 dept_ids 必经绑定
  参数占位（编译 SQL 无内联字面量）。
③ QA 匹配（QA-05）：SQL 带 status='enabled' 过滤；跨部门不命中；低于阈值不命中。
④ 重排折叠（RET-05/F-1003）：近重复（embedding 余弦 ≥0.98）低分项被折叠；top_n 截断。
⑤ SET LOCAL hnsw.ef_search：值来自参数、经 psycopg sql.Literal 执行、事务内同连接生效。

种子一律 ORM 直插：真 embedding 向量用 StubEmbedder 生成后写入，读回断言。
fixtures 见 tests/conftest.py：session（每测试独立事务，结束整体回滚，用例互不污染）。
"""

import hashlib
import math
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.llm import StubEmbedder, StubReranker, sigmoid
from app.db.models import Chunk, Department, Document, QaPair, User
from app.rag.doc_profile import resolve_top_n
from app.services.retrieval import (
    _chunk_search_statement,
    match_qa,
    rerank_and_fold,
    search_chunks,
)

# ---------------------------------------------------------------------------
# 种子助手：ORM 直插，content_hash 以标题摘要保证唯一（部分唯一索引，软删不占）
# ---------------------------------------------------------------------------


def _dept_ids(session: Session) -> dict[str, int]:
    """种子三部门 code → id（seed.py：service/hr/all）。"""
    return dict(session.execute(select(Department.code, Department.id)).all())


def _admin_id(session: Session) -> int:
    return session.execute(select(User.id).where(User.username == "admin")).scalar_one()


def _add_document(
    session: Session,
    dept_id: int,
    admin_id: int,
    title: str,
    *,
    status: str = "ready",
    is_active: bool = True,
    doc_type: str = "general",
    effective_date: date | None = None,
) -> Document:
    doc = Document(
        title=title,
        dept_id=dept_id,
        original_filename=f"{title}.docx",
        file_path=f"seed/{title}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=hashlib.sha256(title.encode("utf-8")).hexdigest(),
        effective_date=effective_date or date(2026, 9, 1),
        owner_name="制度责任人",
        status=status,
        is_active=is_active,
        doc_type=doc_type,
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
    *,
    status: str = "enabled",
) -> QaPair:
    qa = QaPair(
        dept_id=dept_id,
        question=question,
        answer=answer,
        embedding=embedder.embed([question])[0],
        status=status,
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
    embedding: list[float] | None = None,
) -> Chunk:
    if embedding is None:
        embedding = embedder.embed([content])[0]
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=position,
        page_no=page_no,
        embedding=embedding,
    )
    session.add(chunk)
    session.flush()
    return chunk


# ---------------------------------------------------------------------------
# ① 强制 JOIN documents：归档/未就绪不泄漏（W2 移交项① + DOC-13 检索方向）
# ---------------------------------------------------------------------------


def test_archived_and_unready_documents_never_leak(session: Session) -> None:
    """chunk 均为 is_active=true：命中与否完全由 JOIN documents 的两道条件决定。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    admin = _admin_id(session)
    archived = _add_chunk(
        session,
        _add_document(session, ids["service"], admin, "客服_归档文档", is_active=False),
        "归档文档的考勤制度正文",
        embedder,
    )
    parsing = _add_chunk(
        session,
        _add_document(session, ids["service"], admin, "客服_解析中文档", status="parsing"),
        "解析中文档的考勤制度正文",
        embedder,
    )
    ready = _add_chunk(
        session,
        _add_document(session, ids["service"], admin, "客服_就绪文档"),
        "就绪文档的考勤制度正文",
        embedder,
    )
    assert archived.is_active and parsing.is_active  # 前提：卡点全在 documents 侧

    hits = search_chunks(
        session,
        embedder.embed(["考勤制度正文"])[0],
        [ids["service"], ids["all"]],
        top_k=10,
        ef_search=40,
    )

    assert [hit.chunk_id for hit in hits] == [ready.id]  # 仅 ready+active 文档可见


# ---------------------------------------------------------------------------
# ② 部门隔离：dept_id IN (用户部门, 全员)，且必经参数绑定
# ---------------------------------------------------------------------------


def test_department_isolation_with_bound_parameters(session: Session) -> None:
    """HR 视角（dept_ids=[hr, all]）：hr+all 命中、service 不命中；SQL 无字面量插值。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    admin = _admin_id(session)
    service_chunk = _add_chunk(
        session,
        _add_document(session, ids["service"], admin, "客服_专属文档"),
        "客服专属退货流程",
        embedder,
    )
    hr_chunk = _add_chunk(
        session,
        _add_document(session, ids["hr"], admin, "人事_专属文档"),
        "人事专属考勤流程",
        embedder,
    )
    all_chunk = _add_chunk(
        session,
        _add_document(session, ids["all"], admin, "全员_通用文档"),
        "全员通用差旅流程",
        embedder,
    )

    hr_view = [ids["hr"], ids["all"]]
    hits = search_chunks(
        session, embedder.embed(["人事专属考勤流程"])[0], hr_view, top_k=10, ef_search=40
    )
    hit_ids = {hit.chunk_id for hit in hits}
    assert hr_chunk.id in hit_ids
    assert all_chunk.id in hit_ids  # all 部门双向命中（RET-02）
    assert service_chunk.id not in hit_ids  # service 部门文档不命中（F-502）

    # 反向：service 视角查不 hr 专属内容，但 all 通用内容仍可见
    service_view = [ids["service"], ids["all"]]
    reverse = {
        hit.chunk_id: hit
        for hit in search_chunks(
            session, embedder.embed(["客服专属退货流程"])[0], service_view, top_k=10, ef_search=40
        )
    }
    assert service_chunk.id in reverse
    assert all_chunk.id in reverse
    assert hr_chunk.id not in reverse

    # 参数绑定占位：编译后 SQL 含 dept_id 占位（SQLA 2.0 渲染 __[POSTCOMPILE_dept_id_N]）
    compiled = str(
        _chunk_search_statement(embedder.embed(["任意查询"])[0], hr_view, 5).compile(
            compile_kwargs={"literal_binds": False}
        )
    )
    assert "dept_id_1" in compiled
    assert f"'{ids['hr']}'" not in compiled and f"'{ids['all']}'" not in compiled


# ---------------------------------------------------------------------------
# ③ QA 匹配：最高分且 ≥ threshold；status='enabled' / 部门 / 阈值三重过滤
# ---------------------------------------------------------------------------


def test_match_qa_returns_best_enabled_pair(session: Session) -> None:
    """同文本 query：similarity≈1 ≥0.85 命中且返回该条（QA-05 命中方向）。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    question = "退货时限是多少"
    qa = _add_qa(session, ids["service"], _admin_id(session), question, "七天内可退货", embedder)

    hit = match_qa(session, embedder.embed([question])[0], [ids["service"], ids["all"]], 0.85)

    assert hit is not None
    assert (hit.qa_id, hit.question, hit.answer) == (qa.id, question, "七天内可退货")
    assert hit.similarity >= 0.85
    assert hit.similarity == pytest.approx(1.0, abs=1e-6)  # 同向量余弦相似度为 1


def test_match_qa_ignores_disabled_cross_dept_and_below_threshold(session: Session) -> None:
    """disabled（QA-05）/跨部门/低于阈值三种情况一律不命中。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    admin = _admin_id(session)
    _add_qa(session, ids["service"], admin, "停用问题文本", "停用答案", embedder, status="disabled")
    _add_qa(session, ids["hr"], admin, "外部门问题文本", "外部门答案", embedder)
    _add_qa(session, ids["service"], admin, "在库问题文本", "在库答案", embedder)

    service_view = [ids["service"], ids["all"]]
    assert match_qa(session, embedder.embed(["停用问题文本"])[0], service_view, 0.85) is None
    assert match_qa(session, embedder.embed(["外部门问题文本"])[0], service_view, 0.85) is None
    # 阈值：可见池只有"在库问题文本"，不相关问题与其余弦≈0 < 0.85 → 不命中
    assert (
        match_qa(session, embedder.embed(["完全不相干的其它提问"])[0], service_view, 0.85) is None
    )


def test_match_qa_ignores_soft_deleted_pair(session: Session) -> None:
    """软删（is_active=false，QA-07）：status 仍为 enabled 也不命中——status+is_active
    双闸，删除端点置软删位后问答对即刻退出检索（行保留库内可追溯）。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    qa = _add_qa(session, ids["service"], _admin_id(session), "软删问题文本", "软删答案", embedder)
    qa.is_active = False  # 模拟 DELETE 端点软删效果：仅动 is_active，status 不变
    session.flush()
    assert qa.status == "enabled"  # 前提：本用例只验证 is_active 一道闸

    service_view = [ids["service"], ids["all"]]
    assert match_qa(session, embedder.embed(["软删问题文本"])[0], service_view, 0.85) is None


# ---------------------------------------------------------------------------
# ④ 重排折叠：近重复折叠（RET-05/F-1003）+ top_n 截断 + 字段读回
# ---------------------------------------------------------------------------


def test_rerank_fold_drops_near_duplicate_and_truncates(session: Session) -> None:
    """chunk5 与 chunk1 embedding 余弦 ≥0.98：折叠只留高分 chunk1；top_n 截断生效。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    doc = _add_document(session, ids["service"], _admin_id(session), "客服_折叠样本")
    base = embedder.embed(["折叠基准文本"])[0]
    mixed = [b * 100.0 + n for b, n in zip(base, embedder.embed(["折叠扰动文本"])[0], strict=True)]
    norm = math.sqrt(sum(component * component for component in mixed))
    near = [component / norm for component in mixed]
    chunk1 = _add_chunk(session, doc, "折叠基准文本", embedder, position=1)
    chunk2 = _add_chunk(session, doc, "折叠独立文本乙", embedder, position=2)
    chunk3 = _add_chunk(session, doc, "折叠独立文本丙", embedder, position=3)
    chunk4 = _add_chunk(session, doc, "折叠独立文本丁", embedder, position=4)
    chunk5 = _add_chunk(session, doc, "近重复文本戊", embedder, position=5, embedding=near)

    hits = search_chunks(
        session,
        embedder.embed(["折叠查询"])[0],
        [ids["service"], ids["all"]],
        top_k=5,
        ef_search=40,
    )
    assert {hit.chunk_id for hit in hits} == {chunk1.id, chunk2.id, chunk3.id, chunk4.id, chunk5.id}

    score_of = {chunk1.id: 5.0, chunk2.id: 4.0, chunk3.id: 3.0, chunk4.id: 2.0, chunk5.id: 1.0}
    weights = [score_of[hit.chunk_id] for hit in hits]  # 权重按召回顺序对位注入
    folded = rerank_and_fold(
        StubReranker(weights=weights), "改写后问题", hits, top_n=5, dedup_sim=0.98
    )

    assert [hit.chunk_id for hit in folded] == [chunk1.id, chunk2.id, chunk3.id, chunk4.id]
    # chunk5 被折叠（与已保留的 chunk1 近重复且分最低）；分数为 rerank sigmoid 分且保序
    assert [hit.similarity for hit in folded] == [
        sigmoid(5.0),
        sigmoid(4.0),
        sigmoid(3.0),
        sigmoid(2.0),
    ]
    truncated = rerank_and_fold(
        StubReranker(weights=weights), "改写后问题", hits, top_n=3, dedup_sim=0.98
    )
    assert [hit.chunk_id for hit in truncated] == [chunk1.id, chunk2.id, chunk3.id]  # 5→3 截断


def test_doc_hit_fields_roundtrip_from_seed(session: Session) -> None:
    """写库读回：DocHit 逐字段等于种子值，embedding 读回解析为 float 列表（float4 精度）。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    content = "读回断言的差旅报销正文"
    doc = _add_document(session, ids["hr"], _admin_id(session), "人事_读回样本")
    chunk = _add_chunk(session, doc, content, embedder, position=0, page_no=7)

    hits = search_chunks(
        session, embedder.embed(["差旅报销正文"])[0], [ids["hr"], ids["all"]], top_k=5, ef_search=40
    )

    assert len(hits) == 1
    hit = hits[0]
    assert (hit.chunk_id, hit.document_id) == (chunk.id, doc.id)
    assert hit.title == doc.title and hit.title_path == doc.title
    assert hit.content == content and hit.page_no == 7
    stored = session.scalars(select(Chunk.embedding).where(Chunk.id == chunk.id)).one()
    assert isinstance(stored, str)  # psycopg 未注册 pgvector 适配：vector 列读回为文本
    assert hit.embedding == pytest.approx(embedder.embed([content])[0], rel=1e-6)


# ---------------------------------------------------------------------------
# ⑥ 查询期 top_n 按型档（metrics-balance §3.2/§5）：doc_type 自 documents 透出，
#    notice 档 3 条；int 通路同输入对照（零回归背书）
# ---------------------------------------------------------------------------


def test_top_n_by_doc_type_notice_keeps_three_and_int_path_unchanged(session: Session) -> None:
    """notice 型文档 5 个候选：doc_type 从 pg 列经 search_chunks 读回 → resolve_top_n=3
    截取前 3 名；同输入传 int 5 照旧——锁 int 通路与按型通路分叉可见。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    doc = _add_document(
        session, ids["service"], _admin_id(session), "客服_通知样本", doc_type="notice"
    )
    chunks = [
        _add_chunk(session, doc, f"通知候选正文{i}", embedder, position=i) for i in range(1, 6)
    ]

    hits = search_chunks(
        session,
        embedder.embed(["通知查询"])[0],
        [ids["service"], ids["all"]],
        top_k=20,
        ef_search=40,
    )
    assert {hit.chunk_id for hit in hits} == {chunk.id for chunk in chunks}  # 5 候选全召回
    assert {hit.doc_type for hit in hits} == {"notice"}  # 分型自 documents 列透出

    weights = [5.0, 4.0, 3.0, 2.0, 1.0]  # 按召回序对位注入且互异：召回序即 ranked 序
    by_type = rerank_and_fold(
        StubReranker(weights=weights), "改写后问题", hits, top_n=resolve_top_n, dedup_sim=0.98
    )
    fixed = rerank_and_fold(
        StubReranker(weights=weights), "改写后问题", hits, top_n=5, dedup_sim=0.98
    )

    assert [hit.chunk_id for hit in by_type] == [hit.chunk_id for hit in hits[:3]]  # 通知档 3 条
    assert [hit.chunk_id for hit in fixed] == [hit.chunk_id for hit in hits]  # int 5 条照旧


# ---------------------------------------------------------------------------
# ⑤ SET LOCAL hnsw.ef_search：值来自参数，事务内同连接生效
# ---------------------------------------------------------------------------


def test_ef_search_set_local_from_parameter_within_transaction(session: Session) -> None:
    """检索后同事务 SHOW：ef_search 等于参数值，且随下次调用变化（确非固定值）。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    doc = _add_document(session, ids["service"], _admin_id(session), "客服_ef样本")
    _add_chunk(session, doc, "ef搜索样本正文", embedder)
    query_vec = embedder.embed(["ef搜索样本正文"])[0]

    search_chunks(session, query_vec, [ids["service"]], top_k=5, ef_search=400)
    assert session.execute(text("SHOW hnsw.ef_search")).scalar_one() == "400"

    search_chunks(session, query_vec, [ids["service"]], top_k=5, ef_search=123)
    assert session.execute(text("SHOW hnsw.ef_search")).scalar_one() == "123"


# ---------------------------------------------------------------------------
# 生效日期前置闸（工单 22）：未生效文档不参与检索，生效日当天起可见
# ---------------------------------------------------------------------------


def test_未生效文档不参与检索_生效日当天起可见(session: Session) -> None:
    """effective_date > 数据库今天 → 召回排除；当天起可见（闸走 CURRENT_DATE，与
    到期清扫同 UTC 日期口径）。修复前"生效日期"只是元数据承诺，上传即 ready 即可
    被检索——未来生效的制度文档提前泄漏。"""
    embedder = StubEmbedder()
    ids = _dept_ids(session)
    admin = _admin_id(session)
    today = datetime.now(UTC).date()
    future = _add_document(
        session,
        ids["service"],
        admin,
        "客服_未来生效制度",
        effective_date=today + timedelta(days=30),
    )
    active = _add_document(session, ids["service"], admin, "客服_已生效制度", effective_date=today)
    text = "客服专属退货流程全文内容"
    future_chunk = _add_chunk(session, future, text, embedder)
    active_chunk = _add_chunk(session, active, text, embedder)

    hits = search_chunks(
        session, embedder.embed([text])[0], [ids["service"], ids["all"]], top_k=10, ef_search=40
    )
    hit_ids = {hit.chunk_id for hit in hits}
    assert active_chunk.id in hit_ids
    assert future_chunk.id not in hit_ids  # 修复前：未来生效文档照样召回
