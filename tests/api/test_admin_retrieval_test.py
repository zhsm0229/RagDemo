r"""管理端检索测试 API 测试（Task 10，F-503/F-703，RET-03/04）。

brief ①-⑤ 全覆盖：
① 种子 qa+chunks → 命中结构与两套分数字段分正确（qa.similarity=embedding 余弦、
  docs[].rerankScore=rerank sigmoid、docs[].recallCosine=召回余弦，standards §4.4
  两套分数各占字段不互比）、qa.thresholdPassed=true、跨部门视角隔离（hr 视角空）；
② 阈值边界（API 级）：StubReranker 权重注入使 rerank 分落到 0.55 拒答阈值两侧——
  logit 0.21 → sigmoid≈0.552 → thresholdPassed=true，logit 0.08 → sigmoid≈0.52 →
  thresholdPassed=false（brief 钉的 0.52 边界样本：rerank_and_fold 的 weights 形参
  是 logit，sigmoid 后才是对照 refuse_threshold 的分数）；
③ 折叠块 folded=true 仍在 docs 展示（进 top5 但近重复被折叠丢弃），top_n 之外的
  候选不出现（docs=top5 全量含 folded，brief 钉死）；
④ 不写 hit_counter（调参流量不污染 F-1009 零召回榜）：chunk.hit_count 读回不变 +
  进程内聚合为空；
⑤ member → 403/2004。
另固化：deptCode 枚举外 422。种子一律 ORM 直插（StubEmbedder 同输入必同输出，与
lifespan 装配的 Stub 同指纹空间）；权重按召回顺序对位注入（与端点同参复跑召回拿
hits 顺序）。
"""

import hashlib
import math
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID
from app.core.llm import StubEmbedder, StubReranker, sigmoid
from app.db.models import Chunk, Department, Document, QaPair, User
from app.services.retrieval import normalize_question, search_chunks
from tests.conftest import login_as

# ---------- 构造与登录辅助（口径同 tests/api/test_admin_qa_manage.py） ----------


def make_user(session: Session, username: str, password: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内。"""
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
    make_user(session, "rtadmin", "right-one", role="admin")
    assert login_as(client, "rtadmin", "right-one").status_code == 200


def _dept_ids(session: Session) -> dict[str, int]:
    """种子三部门 code → id（seed.py：service/hr/all）。"""
    return dict(session.execute(select(Department.code, Department.id)).all())


def _admin_id(session: Session) -> int:
    return session.execute(select(User.id).where(User.username == "admin")).scalar_one()


def _add_document(session: Session, dept_id: int, admin_id: int, title: str) -> Document:
    """就绪且活跃的种子文档（JOIN documents 双闸的闸内样本；闸外样本归集成测试）。"""
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


def _dot(left: list[float], right: list[float]) -> float:
    """余弦相似度期望值：Stub 向量已 L2 归一化，点积即余弦（pg 侧 float4 存储有
    ~1e-6 绝对误差，断言用 abs 容差）。"""
    return sum(a * b for a, b in zip(left, right, strict=True))


def _recall_weights(
    session: Session,
    embedder: StubEmbedder,
    question: str,
    dept_view: list[int],
    weight_by_chunk: dict[int, float],
) -> list[float]:
    """与端点同参复跑召回（同事务同索引，顺序确定），按 hits 顺序对位取权重——
    StubReranker.weights 须与 rerank 的 docs 等长且按位对齐。"""
    settings = get_settings()
    hits = search_chunks(
        session,
        embedder.embed([normalize_question(question)])[0],
        dept_view,
        settings.retriever_top_k,
        settings.hnsw_ef_search,
    )
    assert {hit.chunk_id for hit in hits} == set(weight_by_chunk)  # 种子块必须全部被召回
    return [weight_by_chunk[hit.chunk_id] for hit in hits]


def _install_weighted_reranker(
    rt_client,
    session: Session,
    embedder: StubEmbedder,
    question: str,
    dept_view: list[int],
    weight_by_chunk: dict[int, float],
) -> None:
    """换装带权重 StubReranker（ChatDeps 可变 dataclass，client 每用例新 lifespan，
    换装不跨用例泄漏），权重经 _recall_weights 与召回顺序对位。"""
    rt_client.app.state.chat_deps.reranker = StubReranker(
        weights=_recall_weights(session, embedder, question, dept_view, weight_by_chunk)
    )


def _post_test(rt_client, question: str, dept_code: str):
    """端点统一入口：POST /api/admin/retrieval-test。"""
    return rt_client.post(
        "/api/admin/retrieval-test", json={"question": question, "deptCode": dept_code}
    )


def _seed_hit_fixture(session: Session, embedder: StubEmbedder) -> dict:
    """① 系列共享种子：service 部门 1 个问答对 + 1 篇文档 2 块（页码 3/4）。"""
    question = "客服差旅报销标准说明"
    ids = _dept_ids(session)
    qa = _add_qa(
        session, ids["service"], _admin_id(session), question, "差旅报销标准答案", embedder
    )
    doc = _add_document(session, ids["service"], _admin_id(session), "客服_差旅制度")
    first = _add_chunk(session, doc, "差旅报销正文甲", embedder, position=1, page_no=3)
    second = _add_chunk(session, doc, "差旅报销正文乙", embedder, position=2, page_no=4)
    return {
        "question": question,
        "ids": ids,
        "qa": qa,
        "doc_title": doc.title,
        "chunks": [(first, "差旅报销正文甲", 3), (second, "差旅报销正文乙", 4)],
    }


def _near_dup_vector(embedder: StubEmbedder, base_text: str, noise_text: str) -> list[float]:
    """近重复向量：base*100 + 噪声后 L2 归一化，与 base 余弦 ≥0.98（折叠触发样本，
    口径同 tests/integration/test_retrieval.py）。"""
    base = embedder.embed([base_text])[0]
    mixed = [b * 100.0 + n for b, n in zip(base, embedder.embed([noise_text])[0], strict=True)]
    norm = math.sqrt(sum(component * component for component in mixed))
    return [component / norm for component in mixed]


@pytest.fixture()
def rt_client(test_mode, client):
    """test_mode 先于 lifespan（fixture 参数序即装配序）：chat_deps 以 Stub 三件套
    装配挂 app.state，embedder 与检索侧同指纹空间；用例按需换装带权重 StubReranker
    构造阈值边界。"""
    return client


# ---------- ① 命中结构 / 两套分数字段 / qa 阈值 / deptCode 视角 ----------


def test_命中结构_qa命中与阈值回显(rt_client, session):
    """① 响应外壳 {code,message,data{qa,docs,thresholds}}；qa 键集恰为
    {id,question,similarity,thresholdPassed}，similarity=embedding 余弦≈1.0、
    thresholdPassed=true（≥qa_threshold）；thresholds 回显当前 Settings。"""
    embedder = StubEmbedder()
    admin_login(rt_client, session)
    seed = _seed_hit_fixture(session, embedder)
    first, second = (chunk for chunk, _, _ in seed["chunks"])
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        seed["question"],
        [seed["ids"]["service"], seed["ids"]["all"]],
        {first.id: 4.0, second.id: 3.0},
    )

    resp = _post_test(rt_client, seed["question"], "service")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["message"] == "ok"
    data = body["data"]
    assert set(data) == {"qa", "docs", "thresholds", "params", "degraded"}  # 工单 30 契约扩展
    settings = get_settings()
    assert data["thresholds"] == {"qa": settings.qa_threshold, "refuse": settings.refuse_threshold}

    qvec = embedder.embed([seed["question"]])[0]
    assert set(data["qa"]) == {"id", "question", "similarity", "thresholdPassed"}
    assert data["qa"]["id"] == seed["qa"].id
    assert data["qa"]["question"] == seed["question"]
    assert data["qa"]["similarity"] == pytest.approx(_dot(qvec, qvec), abs=1e-4)
    assert data["qa"]["thresholdPassed"] is True  # ≈1.0 ≥ qa_threshold


def test_docs两套分数字段分且逐块标记(rt_client, session):
    """① docs 键集恰为 10 键（W5 F-1011 补 hitCount/lastHitAt 历史命中两键，种子未
    计数故两键为 0/null，值断言见 tests/api/test_retrieval_test.py）；rerankScore=
    rerank sigmoid 降序（权重 4.0/3.0 对位）、recallCosine=召回余弦（rerank 覆写前的
    另一路分数，两套分数不互比）、folded=false、thresholdPassed 对照 refuse_threshold；
    documentTitle/titlePath/page 逐块读回。"""
    embedder = StubEmbedder()
    admin_login(rt_client, session)
    seed = _seed_hit_fixture(session, embedder)
    first, second = (chunk for chunk, _, _ in seed["chunks"])
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        seed["question"],
        [seed["ids"]["service"], seed["ids"]["all"]],
        {first.id: 4.0, second.id: 3.0},
    )

    data = _post_test(rt_client, seed["question"], "service").json()["data"]
    assert len(data["docs"]) == 2
    expected_doc_keys = {
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
    assert [entry["chunkId"] for entry in data["docs"]] == [first.id, second.id]  # rerank 降序
    qvec = embedder.embed([seed["question"]])[0]
    expected = [  # (chunk, 注入 logit, 块文本, 页码)：与 seed 对位但补 logit 维度
        (first, 4.0, "差旅报销正文甲", 3),
        (second, 3.0, "差旅报销正文乙", 4),
    ]
    for entry, (chunk, logit, content, page) in zip(data["docs"], expected, strict=True):
        assert set(entry) == expected_doc_keys
        assert entry["rerankScore"] == pytest.approx(sigmoid(logit), rel=1e-9)
        assert entry["recallCosine"] == pytest.approx(
            _dot(qvec, embedder.embed([content])[0]), abs=1e-4
        )
        assert entry["documentTitle"] == seed["doc_title"]
        assert entry["titlePath"] == seed["doc_title"]
        assert entry["page"] == page
        assert entry["folded"] is False
        assert entry["thresholdPassed"] is True  # sigmoid(4.0)、sigmoid(3.0) 均 ≥ 0.55


def test_跨部门视角隔离_hr视角空结果(rt_client, session):
    """① hr 视角查 service 语料：qa=null 且 docs=[]（F-502 部门隔离，调参视角与
    真实用户过滤语义一致；brief：检索结果为空时 docs=[] 且 qa=null）。"""
    embedder = StubEmbedder()
    admin_login(rt_client, session)
    seed = _seed_hit_fixture(session, embedder)
    resp = _post_test(rt_client, seed["question"], "hr")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["qa"] is None
    assert data["docs"] == []
    settings = get_settings()
    assert data["thresholds"] == {"qa": settings.qa_threshold, "refuse": settings.refuse_threshold}


# ---------- ② 阈值边界：rerank 分落到 refuse_threshold 两侧分别标记 ----------


def test_阈值边界rerank分两侧分别标记(rt_client, session):
    """② logit 0.21 → sigmoid≈0.552 ≥ 0.55 → true；logit 0.08 → sigmoid≈0.52（brief
    边界样本）< 0.55 → false；无 qa 种子 → qa=null。阈值标记逐块独立。"""
    embedder = StubEmbedder()
    admin_login(rt_client, session)
    ids = _dept_ids(session)
    doc = _add_document(session, ids["service"], _admin_id(session), "客服_阈值样本")
    above = _add_chunk(session, doc, "阈值边界文本甲", embedder, position=1)
    below = _add_chunk(session, doc, "阈值边界文本乙", embedder, position=2)
    question = "阈值边界查询"
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        question,
        [ids["service"], ids["all"]],
        {above.id: 0.21, below.id: 0.08},
    )

    data = _post_test(rt_client, question, "service").json()["data"]
    assert data["qa"] is None
    by_id = {entry["chunkId"]: entry for entry in data["docs"]}
    assert by_id[above.id]["rerankScore"] == pytest.approx(sigmoid(0.21), rel=1e-9)
    assert by_id[above.id]["thresholdPassed"] is True
    assert by_id[below.id]["rerankScore"] == pytest.approx(sigmoid(0.08), rel=1e-9)
    assert abs(by_id[below.id]["rerankScore"] - 0.52) < 0.005  # brief 钉的 0.52 边界样本
    assert by_id[below.id]["thresholdPassed"] is False


# ---------- ③ 折叠块 folded=true 出现在结果；top_n 之外不展示 ----------


def test_折叠块标记folded且topn外候选不展示(rt_client, session):
    """③ 6 块召回、top_n=5：近重复低分块（与最高分块余弦 ≥0.98）进 top5 被折叠 →
    docs 末位 folded=true 仍在展示；第 6 名（低于 top_n 截断线）不出现。"""
    embedder = StubEmbedder()
    admin_login(rt_client, session)
    ids = _dept_ids(session)
    doc = _add_document(session, ids["service"], _admin_id(session), "客服_折叠样本")
    near = _near_dup_vector(embedder, "折叠基准文本", "折叠扰动文本")
    kept1 = _add_chunk(session, doc, "折叠基准文本", embedder, position=1)
    kept2 = _add_chunk(session, doc, "折叠独立文本乙", embedder, position=2)
    kept3 = _add_chunk(session, doc, "折叠独立文本丙", embedder, position=3)
    kept4 = _add_chunk(session, doc, "折叠独立文本丁", embedder, position=4)
    folded_out = _add_chunk(session, doc, "近重复文本戊", embedder, position=5, embedding=near)
    beyond = _add_chunk(session, doc, "超量文本己", embedder, position=6)
    question = "折叠查询"
    weight_by_chunk = {
        kept1.id: 4.0,
        kept2.id: 3.0,
        kept3.id: 2.0,
        kept4.id: 1.0,
        folded_out.id: 0.5,
        beyond.id: -4.0,
    }
    _install_weighted_reranker(
        rt_client, session, embedder, question, [ids["service"], ids["all"]], weight_by_chunk
    )

    data = _post_test(rt_client, question, "service").json()["data"]
    assert len(data["docs"]) == 5  # top_n=5 全量（含 folded）；第 6 名不进 docs
    assert beyond.id not in {entry["chunkId"] for entry in data["docs"]}
    # rerank 降序展示，被折叠块分数最低殿后
    assert [entry["chunkId"] for entry in data["docs"]] == [
        kept1.id,
        kept2.id,
        kept3.id,
        kept4.id,
        folded_out.id,
    ]
    assert [entry["folded"] for entry in data["docs"]] == [False, False, False, False, True]
    last = data["docs"][-1]
    assert last["chunkId"] == folded_out.id
    assert last["rerankScore"] == pytest.approx(sigmoid(0.5), rel=1e-9)
    assert last["thresholdPassed"] is True  # sigmoid(0.5)≈0.62 ≥ 0.55：折叠与阈值标记独立
    assert data["docs"][0]["rerankScore"] == pytest.approx(sigmoid(4.0), rel=1e-9)
    assert data["docs"][0]["folded"] is False


# ---------- ④ 不写 hit_counter（读回断言） ----------


def test_检索测试不写命中计数(rt_client, session):
    """④ 命中 1 块的调参请求后：chunk.hit_count 读回仍为 0（F-1009 零召回榜不被
    调参流量污染），进程内 HitCounter 聚合同样为空。"""
    embedder = StubEmbedder()
    admin_login(rt_client, session)
    ids = _dept_ids(session)
    doc = _add_document(session, ids["service"], _admin_id(session), "客服_计数样本")
    chunk = _add_chunk(session, doc, "命中计数正文", embedder)
    question = "命中计数查询"
    _install_weighted_reranker(
        rt_client, session, embedder, question, [ids["service"], ids["all"]], {chunk.id: 4.0}
    )

    resp = _post_test(rt_client, question, "service")
    assert resp.status_code == 200
    assert len(resp.json()["data"]["docs"]) == 1  # 确认确实走通了 rag 命中路径
    session.flush()  # 落实种子 INSERT 后读回 DB 真值
    hit_count = session.scalars(select(Chunk.hit_count).where(Chunk.id == chunk.id)).one()
    assert hit_count == 0
    assert rt_client.app.state.chat_deps.hit_counter._counts == {}


# ---------- ⑤ 权限门与参数校验 ----------


def test_成员角色访问检索测试_403(rt_client, session):
    """⑤ member 角色：403/2004（admin-only 调参工具）。"""
    make_user(session, "rtmember", "right-one", role="member")
    assert login_as(rt_client, "rtmember", "right-one").status_code == 200
    resp = _post_test(rt_client, "任意问题", "service")
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN


def test_部门编码枚举外_422(rt_client, session):
    """deptCode ∉ service|hr|all → 422/1003 字段级（admin 任意视角仅限三枚举）。"""
    admin_login(rt_client, session)
    resp = _post_test(rt_client, "任意问题", "finance")
    assert resp.status_code == 422
    assert resp.json()["code"] == ERR_PARAM_INVALID


# ---------- ⑥ 工单 30：参数对比（可选参数面 / 回显 / 熔断降级标注） ----------


def _post_test_params(rt_client, question: str, dept_code: str, **params):
    """⑥ 统一入口扩展：新参数逐键透传（None 不传，等价缺省形态）。"""
    payload = {"question": question, "deptCode": dept_code}
    payload.update({k: v for k, v in params.items() if v is not None})
    return rt_client.post("/api/admin/retrieval-test", json=payload)


def test_参数对比_缺省None回显装配缺省且行为同现状(rt_client, session):
    """⑥ 缺省（全 None 不传）：params 回显装配缺省读数、degraded=false，
    qa/docs 与既有断言同形态——回归红线"缺省=现状逐字节一致"。"""
    admin_login(rt_client, session)
    embedder = StubEmbedder()
    fx = _seed_hit_fixture(session, embedder)
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        fx["question"],
        [fx["ids"]["service"], fx["ids"]["all"]],
        {c.id: 4.0 for c, _, _ in fx["chunks"]},
    )
    resp = _post_test_params(rt_client, fx["question"], "service")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data) == {"qa", "docs", "thresholds", "params", "degraded"}
    settings = get_settings()
    assert data["thresholds"] == {"qa": settings.qa_threshold, "refuse": settings.refuse_threshold}
    assert data["params"] == {
        "topK": settings.retriever_top_k,
        "topN": settings.rerank_top_n,
        "efSearch": settings.hnsw_ef_search,
        "chunkDedupSim": settings.chunk_dedup_sim,
    }
    assert data["degraded"] is False


def test_参数对比_qa阈值调档直答翻转(rt_client, session):
    """⑥ qaThreshold 档位生效（R6；stub 指纹空间边界注记：同文余弦恰 1.0、变体余弦
    ≈0——"命中/翻走"两端在 stub 下不可双测，联动行为语义由 refuse 档用例承载，本用例
    钉档位传递与 qa/docs 互不短路）：档 0.99 时 qa 命中保持（1.0 ≥ 0.99）且 thresholds.qa
    回显生效值，docs 检索路径照常。"""
    admin_login(rt_client, session)
    embedder = StubEmbedder()
    fx = _seed_hit_fixture(session, embedder)
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        fx["question"],
        [fx["ids"]["service"], fx["ids"]["all"]],
        {c.id: 4.0 for c, _, _ in fx["chunks"]},
    )
    high = _post_test_params(rt_client, fx["question"], "service", qaThreshold=0.99)
    data = high.json()["data"]
    assert data["qa"] is not None  # 同文余弦 1.0 ≥ 0.99
    assert data["qa"]["thresholdPassed"] is True
    assert data["thresholds"]["qa"] == 0.99  # 回显生效值（装配值 0.85 被覆盖）
    assert len(data["docs"]) == 2  # 互不短路：qa 命中不截断 docs 路径（①回归语义）


def test_参数对比_refuse阈值调高拒答标记翻转(rt_client, session):
    """⑥ refuseThreshold 0.99（高于全部 rerank 分）→ 命中块 thresholdPassed 全 false
    （线上会拒答的形态）；低档 0.01 → 全 true。"""
    admin_login(rt_client, session)
    embedder = StubEmbedder()
    fx = _seed_hit_fixture(session, embedder)
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        fx["question"],
        [fx["ids"]["service"], fx["ids"]["all"]],
        {c.id: 4.0 for c, _, _ in fx["chunks"]},
    )
    high = _post_test_params(rt_client, fx["question"], "service", refuseThreshold=0.99)
    assert [d["thresholdPassed"] for d in high.json()["data"]["docs"]] == [False, False]
    low = _post_test_params(rt_client, fx["question"], "service", refuseThreshold=0.01)
    assert [d["thresholdPassed"] for d in low.json()["data"]["docs"]] == [True, True]
    assert low.json()["data"]["thresholds"]["refuse"] == 0.01


def test_参数对比_topK截断候选(rt_client, session):
    """⑥ topK=1 → 召回候选截到 1，rerank/fold 后 docs 至多 1 块（候选数联动）；
    不装权重 reranker（StubReranker weights 与召回数等长校验，截断档不适用对位注入）。"""
    admin_login(rt_client, session)
    embedder = StubEmbedder()
    fx = _seed_hit_fixture(session, embedder)
    resp = _post_test_params(rt_client, fx["question"], "service", topK=1)
    assert len(resp.json()["data"]["docs"]) == 1
    assert resp.json()["data"]["params"]["topK"] == 1


def test_参数对比_topN边界与efSearch回显(rt_client, session):
    """⑥ topN=1 → docs 至多 1 块（fold 上限联动）；efSearch 仅回显（Stub 向量空间
    下 ef 行为差异属真 HNSW，回显断言+设计注记口径）。"""
    admin_login(rt_client, session)
    embedder = StubEmbedder()
    fx = _seed_hit_fixture(session, embedder)
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        fx["question"],
        [fx["ids"]["service"], fx["ids"]["all"]],
        {c.id: 4.0 for c, _, _ in fx["chunks"]},
    )
    resp = _post_test_params(rt_client, fx["question"], "service", topN=1, efSearch=64)
    data = resp.json()["data"]
    assert len(data["docs"]) == 1
    assert data["params"]["efSearch"] == 64
    assert data["params"]["topN"] == 1


def test_参数对比_越界字段级422(rt_client, session):
    """⑥ 五参数越界 → 422/1003 字段级（model_copy 跳过校验，范围全在请求层 validators）。"""
    admin_login(rt_client, session)
    for payload in (
        {"qaThreshold": 1.5},
        {"qaThreshold": -0.1},
        {"refuseThreshold": 2},
        {"topK": 0},
        {"topK": 51},
        {"topN": 21},
        {"efSearch": 0},
        {"efSearch": 201},
    ):
        resp = rt_client.post(
            "/api/admin/retrieval-test",
            json={"question": "任意问题", "deptCode": "service", **payload},
        )
        assert resp.status_code == 422, payload
        assert resp.json()["code"] == ERR_PARAM_INVALID


def test_参数对比_熔断开路degraded标注(rt_client, session):
    """⑥ 熔断 open 态：degraded=true、docs 仍出（向量序兜底）——对比结论不可信的
    标注口径（R6）。"""
    from app.core.circuit_breaker import CircuitBreaker, set_rerank_breaker
    from app.core.clock import FakeClock

    admin_login(rt_client, session)
    embedder = StubEmbedder()
    fx = _seed_hit_fixture(session, embedder)
    _install_weighted_reranker(
        rt_client,
        session,
        embedder,
        fx["question"],
        [fx["ids"]["service"], fx["ids"]["all"]],
        {c.id: 4.0 for c, _, _ in fx["chunks"]},
    )
    breaker = CircuitBreaker(FakeClock())
    for _ in range(3):
        breaker.record_failure()  # 连续 3 次失败 → open（chat 链路同款阈值）
    set_rerank_breaker(breaker)
    try:
        resp = _post_test_params(rt_client, fx["question"], "service")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["degraded"] is True
        assert len(data["docs"]) >= 1  # 向量序兜底照出
    finally:
        set_rerank_breaker(None)
