r"""检索测试响应历史命中字段测试（W5 Task 4，F-1011）：docs[] 补 hitCount/lastHitAt。

口径：检索测试只**读现列**展示真实问答流量沉淀的召回统计（hit_count 由 chat 链路
HitCounter 刷库、last_hit_at 同事务统一置），调参重放本身不写计数（不写 hit_counter
红线沿用 tests/api/test_admin_retrieval_test.py ④，本文件另以 DB 读回固化"读了也不写"）。
种子 hit_count=0/2/5 与 last_hit_at 混合（从未召回/近期召回），响应逐块读回；被折叠
块（folded=true）同样携带两键——调参视角需完整看到每块的历史表现。种子一律 ORM 直插
（StubEmbedder 同输入必同输出，与 lifespan 装配的 Stub 同指纹空间），权重按召回顺序
对位注入（同 test_admin_retrieval_test 口径）。
"""

import hashlib
import math
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.llm import StubEmbedder, StubReranker
from app.db.models import Chunk, Department, Document, User
from app.services.retrieval import normalize_question, search_chunks


def make_user(session: Session, username: str, password: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内（同 test_admin_retrieval_test）。"""
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


def admin_login(client: TestClient, session: Session) -> None:
    """创建并登录 admin（cookie 由 client 保持）。"""
    make_user(session, "f1011admin", "right-one", role="admin")
    assert (
        client.post(
            "/api/auth/login", json={"username": "f1011admin", "password": "right-one"}
        ).status_code
        == 200
    )


def _dept_ids(session: Session) -> dict[str, int]:
    """种子三部门 code → id（seed.py：service/hr/all）。"""
    return dict(session.execute(select(Department.code, Department.id)).all())


def _admin_id(session: Session) -> int:
    return session.execute(select(User.id).where(User.username == "admin")).scalar_one()


def _add_document(session: Session, dept_id: int, admin_id: int, title: str) -> Document:
    """就绪且活跃的种子文档（检索双闸闸内样本）。"""
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
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    return doc


def _add_chunk(
    session: Session,
    doc: Document,
    content: str,
    embedder: StubEmbedder,
    *,
    position: int,
    hit_count: int,
    last_hit_at: datetime | None,
    embedding: list[float] | None = None,
) -> Chunk:
    """直插块并钉历史命中两列（F-1011 被读现列）：hit_count/last_hit_at 由用例指定。"""
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=position,
        embedding=embedding if embedding is not None else embedder.embed([content])[0],
        hit_count=hit_count,
        last_hit_at=last_hit_at,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _near_dup_vector(embedder: StubEmbedder, base_text: str, noise_text: str) -> list[float]:
    """近重复向量：base*100 + 噪声后 L2 归一化，与 base 余弦 ≥0.98（折叠触发样本）。"""
    base = embedder.embed([base_text])[0]
    mixed = [b * 100.0 + n for b, n in zip(base, embedder.embed([noise_text])[0], strict=True)]
    norm = math.sqrt(sum(component * component for component in mixed))
    return [component / norm for component in mixed]


def _install_weighted_reranker(
    client: TestClient,
    session: Session,
    embedder: StubEmbedder,
    question: str,
    dept_view: list[int],
    weight_by_chunk: dict[int, float],
) -> None:
    """换装带权重 StubReranker，权重经同参复跑召回与 hits 顺序对位。"""
    settings = get_settings()
    hits = search_chunks(
        session,
        embedder.embed([normalize_question(question)])[0],
        dept_view,
        settings.retriever_top_k,
        settings.hnsw_ef_search,
    )
    assert {hit.chunk_id for hit in hits} == set(weight_by_chunk)  # 种子块必须全部被召回
    client.app.state.chat_deps.reranker = StubReranker(
        weights=[weight_by_chunk[hit.chunk_id] for hit in hits]
    )


@pytest.fixture()
def rt_client(test_mode, client):
    """test_mode 先于 lifespan（fixture 参数序即装配序）：chat_deps 以 Stub 三件套装配。"""
    return client


def test_docs键集恰10键_历史命中计数与时刻逐块读回(rt_client, session):
    """F-1011 主用例：docs 键集恰为 10 键（原 8 键 + hitCount/lastHitAt）；种子
    hit_count=5/2/0 与 last_hit_at=时刻/时刻/NULL 混合，按 rerank 降序逐块读回
    （lastHitAt 为 ISO8601，NULL 出 null）；被折叠块（folded=true，与最高分块近重复）
    同样携带两键——调参需完整看到每块历史表现。"""
    embedder = StubEmbedder()
    admin_login(rt_client, session)
    ids = _dept_ids(session)
    doc = _add_document(session, ids["service"], _admin_id(session), "客服_命中统计")
    hit5 = _add_chunk(
        session,
        doc,
        "命中统计正文甲",
        embedder,
        position=1,
        hit_count=5,
        last_hit_at=datetime(2026, 9, 25, 10, 30, tzinfo=UTC),
    )
    hit2 = _add_chunk(
        session,
        doc,
        "命中统计正文乙",
        embedder,
        position=2,
        hit_count=2,
        last_hit_at=datetime(2026, 9, 28, 8, 0, tzinfo=UTC),
    )
    never = _add_chunk(
        session,
        doc,
        "近重复统计文本丙",
        embedder,
        position=3,
        hit_count=0,
        last_hit_at=None,
        embedding=_near_dup_vector(embedder, "命中统计正文甲", "命中统计扰动丁"),
    )
    question = "命中统计查询"
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        question,
        [ids["service"], ids["all"]],
        {hit5.id: 4.0, hit2.id: 3.0, never.id: 0.5},
    )

    resp = rt_client.post(
        "/api/admin/retrieval-test", json={"question": question, "deptCode": "service"}
    )
    assert resp.status_code == 200
    docs = resp.json()["data"]["docs"]
    assert [entry["chunkId"] for entry in docs] == [hit5.id, hit2.id, never.id]  # rerank 降序
    expected_keys = {
        "chunkId",
        "documentTitle",
        "page",
        "titlePath",
        "rerankScore",
        "recallCosine",
        "thresholdPassed",
        "folded",
        "hitCount",
        "lastHitAt",
    }
    expected_hits = [
        (hit5, 5, "2026-09-25T10:30:00+00:00"),
        (hit2, 2, "2026-09-28T08:00:00+00:00"),
        (never, 0, None),
    ]
    assert len(docs) == 3
    for entry, (chunk, count, iso) in zip(docs, expected_hits, strict=True):
        assert set(entry) == expected_keys  # 恰 10 键，无多余键
        assert entry["hitCount"] == count
        assert entry["lastHitAt"] == iso
    assert docs[-1]["folded"] is True  # 近重复块被折叠，历史命中两键照样在列


def test_检索测试读命中列只读不写(rt_client, session):
    """读现列不写（F-1011 红线面）：命中路径走通后，DB 读回 hit_count/last_hit_at
    与种子钉值逐字相等——调参重放不产生计数（不污染 F-1009 零召回榜）。"""
    embedder = StubEmbedder()
    admin_login(rt_client, session)
    ids = _dept_ids(session)
    doc = _add_document(session, ids["service"], _admin_id(session), "客服_只读计数")
    planted_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    chunk = _add_chunk(
        session,
        doc,
        "只读计数正文",
        embedder,
        position=1,
        hit_count=7,
        last_hit_at=planted_at,
    )
    question = "只读计数查询"
    _install_weighted_reranker(
        rt_client, session, embedder, question, [ids["service"], ids["all"]], {chunk.id: 4.0}
    )

    resp = rt_client.post(
        "/api/admin/retrieval-test", json={"question": question, "deptCode": "service"}
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]["docs"]) == 1  # 确认确实走通了 rag 命中路径
    row = session.execute(
        select(Chunk.hit_count, Chunk.last_hit_at).where(Chunk.id == chunk.id)
    ).one()
    assert row.hit_count == 7
    assert row.last_hit_at == planted_at
