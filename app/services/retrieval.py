r"""检索服务（Task 5，问答链路地基）：QA 匹配、向量召回、重排折叠。

红线与口径（本模块正确性的全部锚点）：
- 部门隔离（F-502）：所有检索 SQL 强制 ``dept_id IN (用户部门, 全员)``；dept_ids
  一律经绑定参数传入（见 _chunk_search_statement，编译产物含 dept_id_N 占位，
  无字面量插值），QA 侧同。
- 强制 JOIN documents（W1 移交最高优先级项①）：召回必须 JOIN documents 且同时满足
  ``Document.is_active`` 与 ``Document.status == 'ready'``——归档与解析中/失败文档的
  chunk 一律不可见（DOC-13 检索方向）。``Chunk.is_active`` 一并过滤：既排除停用块，
  也是命中部分 HNSW 索引（postgresql_where=is_active）的谓词条件。
- 分数口径（standards §4.4，两套分数绝不互比）：``DocHit.similarity`` 在
  rerank_and_fold 之后存 **rerank sigmoid 分**（引用展示与拒答阈值口径）；
  ``QaHit.similarity`` 存 **embedding 余弦相似度 = 1 − 余弦距离**（问答对阈值口径）。
  search_chunks 召回阶段 DocHit.similarity 暂存召回余弦相似度，仅供候选排序参考，
  不供阈值判定；消费方（Task 8 编排）必须以 rerank_and_fold 产物为准。
- SET LOCAL hnsw.ef_search：值经 psycopg sql.Literal 客户端安全转义（§4.3 禁
  f-string 拼 SQL），且 SET LOCAL 只在事务内生效——与检索查询同连接同事务执行，
  事务结束自动还原。
- 折叠算法（F-1003）：rerank 打分 → 按分降序取 top_n → top_n 内从高到低遍历，与每个
  已保留项算 embedding 余弦相似度（向量已 L2 归一化，点积即余弦）≥ dedup_sim 则
  丢弃当前项（留高分弃低分），返回保留列表（仍按 rerank 分降序）。
"""

import re
from dataclasses import dataclass, replace
from typing import Any

from psycopg import sql
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.llm import Reranker, cosine_to_similarity
from app.db.models import Chunk, Document, QaPair

_WHITESPACE = re.compile(r"\s")
_PUNCTUATION = "，。？！；：、,.?!;:"


def normalize_question(raw: str) -> str:
    """问题归一化（brief 规格）：strip → 去全部空白 → 去中英文标点集 → lower。"""
    without_space = _WHITESPACE.sub("", raw.strip())
    return "".join(char for char in without_space if char not in _PUNCTUATION).lower()


@dataclass(frozen=True)
class QaHit:
    """问答对命中。similarity=embedding 余弦相似度（qa 阈值口径，standards §4.4）。"""

    qa_id: int
    question: str
    answer: str
    similarity: float


@dataclass(frozen=True)
class DocHit:
    """文档块命中。

    similarity 口径：rerank_and_fold 后为 **rerank sigmoid 分**（引用展示与拒答阈值
    口径）；search_chunks 召回阶段暂存召回余弦相似度（仅候选排序，不供阈值判定）。
    embedding 供折叠两两比较（psycopg 无 pgvector 适配时列读回为 vector 文本，
    经 _parse_embedding 还原为 float 列表）。
    """

    chunk_id: int
    document_id: int
    title: str
    page_no: int | None
    title_path: str
    content: str
    similarity: float
    embedding: list[float]


def match_qa(
    db: Session, query_vec: list[float], dept_ids: list[int], threshold: float
) -> QaHit | None:
    """最高分问答对且相似度 ≥ threshold 才命中；否则 None。

    SQL 带 status='enabled'（QA-05）与 is_active（QA-07 软删双闸：删除端点置
    is_active=false 即退出检索）过滤，及 dept_id IN 绑定参数（F-502），按余弦
    距离升序取第一名；similarity = 1 − 余弦距离（embedding 余弦相似度，qa 阈值口径）。
    """
    distance = QaPair.embedding.cosine_distance(query_vec)
    row = db.execute(
        select(QaPair.id, QaPair.question, QaPair.answer, distance)
        .where(
            QaPair.status == "enabled",
            QaPair.is_active.is_(True),
            QaPair.dept_id.in_(dept_ids),
        )
        .order_by(distance)
        .limit(1)
    ).first()
    if row is None:
        return None
    similarity = cosine_to_similarity(float(row[3]))
    if similarity < threshold:
        return None
    return QaHit(qa_id=row[0], question=row[1], answer=row[2], similarity=similarity)


def search_chunks(
    db: Session, query_vec: list[float], dept_ids: list[int], top_k: int, ef_search: int
) -> list[DocHit]:
    """向量召回：SET LOCAL hnsw.ef_search 后按余弦距离升序取 top_k。

    语句见 _chunk_search_statement（部门隔离 + 强制 JOIN documents）。召回阶段
    DocHit.similarity 暂存 embedding 余弦相似度，重排后由 rerank sigmoid 分覆写。
    """
    _set_ef_search(db, ef_search)
    rows = db.execute(_chunk_search_statement(query_vec, dept_ids, top_k)).all()
    return [
        DocHit(
            chunk_id=chunk_id,
            document_id=document_id,
            title=title,
            page_no=page_no,
            title_path=title_path,
            content=content,
            similarity=cosine_to_similarity(float(distance)),
            embedding=_parse_embedding(embedding),
        )
        for chunk_id, document_id, title, page_no, title_path, content, distance, embedding in rows
    ]


def rerank_and_fold(
    reranker: Reranker,
    query: str,
    hits: list[DocHit],
    top_n: int,
    dedup_sim: float,
    *,
    return_folded: bool = False,
) -> list[DocHit] | tuple[list[DocHit], list[DocHit]]:
    """重排 + 折叠（F-1003）：top_n 内两两去重，返回 rerank 分降序保留列表。

    rerank 文本 = ``{title_path}\n{content}``（与入库分块文本同构），query 为改写后
    问题；打分后按分降序取 top_n，从高到低遍历：与任一已保留项的 embedding 余弦
    相似度 ≥ dedup_sim 则丢弃当前项。DocHit.similarity 在此覆写为 rerank sigmoid 分
    （拒答阈值口径，standards §4.4）；空候选短路，不发起 rerank 调用。

    return_folded=False（缺省，问答编排路径）：返回保留列表，签名与语义同 Task 5；
    True（管理端检索测试 RET-03/04）：返回 (保留列表, 折叠丢弃列表)——进 top_n 但被
    近重复折叠的块带 rerank 分一并交回，供调参视角完整展示（问答编排不受影响）。
    """
    if not hits:
        return ([], []) if return_folded else []
    docs = [f"{hit.title_path}\n{hit.content}" for hit in hits]
    scores = reranker.rerank(query, docs)
    ranked = sorted(zip(hits, scores, strict=True), key=lambda pair: pair[1], reverse=True)
    kept: list[DocHit] = []
    folded_out: list[DocHit] = []
    for hit, score in ranked[:top_n]:
        if any(_cosine(hit.embedding, kept_hit.embedding) >= dedup_sim for kept_hit in kept):
            folded_out.append(replace(hit, similarity=score))  # 与高分已保留项近重复：折叠丢弃
            continue
        kept.append(replace(hit, similarity=score))
    if return_folded:
        return kept, folded_out
    return kept


def _chunk_search_statement(
    query_vec: list[float], dept_ids: list[int], top_k: int
) -> Select[tuple[Any, ...]]:
    """召回语句（独立成函数供测试编译断言：dept_ids 必经绑定参数占位，无字面量插值）。

    强制 JOIN documents（W2 移交项①）：归档（is_active=false）与未就绪
    （status != 'ready'）文档的 chunk 一律不可见；SELECT 显式取 Chunk.embedding
    供折叠阶段两两余弦比较。
    """
    distance = Chunk.embedding.cosine_distance(query_vec)
    return (
        select(
            Chunk.id,
            Chunk.document_id,
            Document.title,
            Chunk.page_no,
            Chunk.title_path,
            Chunk.content,
            distance,
            Chunk.embedding,
        )
        .join(Document, Chunk.document_id == Document.id)
        .where(
            Chunk.is_active.is_(True),
            Chunk.dept_id.in_(dept_ids),  # F-502 部门隔离：dept_id IN (用户部门, 全员)
            Document.is_active.is_(True),  # W2 移交项①：归档文档不泄漏
            Document.status == "ready",  # W2 移交项①：未就绪文档不泄漏
        )
        .order_by(distance)
        .limit(top_k)
    )


def _set_ef_search(db: Session, ef_search: int) -> None:
    """SET LOCAL hnsw.ef_search：值经 sql.Literal 客户端转义（§4.3 禁 f-string 拼 SQL）。

    SET LOCAL 只在事务内生效：db.connection() 在当前事务（无则 autobegin）上执行，
    与随后的检索查询同连接同事务，保证对本查询生效、事务结束自动还原。
    """
    connection = db.connection()
    statement = sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(ef_search))
    connection.exec_driver_sql(statement.as_string(connection.connection.dbapi_connection))


def _parse_embedding(raw: Any) -> list[float]:
    """vector 列读回适配：psycopg 未注册 pgvector 适配时读回为 "[a,b,...]" 文本。"""
    if not isinstance(raw, str):
        return [float(component) for component in raw]
    body = raw.strip().strip("[]")
    return [] if not body else [float(part) for part in body.split(",")]


def _cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度：两向量均已 L2 归一化（入库侧保证），点积即余弦。"""
    return sum(a * b for a, b in zip(left, right, strict=True))


class PgVectorStore:
    """pgvector 实现（VectorStore 协议的首个实现，服务层无状态适配）。

    检索 SQL 即本模块 match_qa/search_chunks——方法仅委托，协议存在的意义是替换缝
    （scaling-plan §1.7 B 阶段 Milvus 第二实现在装配层与本类二选一），非 Middle Man
    消极转发；db 按调用传入，本类不持连接。"""

    def match_qa(
        self, db: Session, query_vec: list[float], dept_ids: list[int], threshold: float
    ) -> QaHit | None:
        return match_qa(db, query_vec, dept_ids, threshold)

    def search_chunks(
        self, db: Session, query_vec: list[float], dept_ids: list[int], top_k: int, ef_search: int
    ) -> list[DocHit]:
        return search_chunks(db, query_vec, dept_ids, top_k, ef_search)
