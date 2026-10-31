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
- 折叠算法（F-1003，已抽 rag 域纯函数 fold_top_n，app/rag/fold.py——主链路与 agent
  检索工具共用，retrieval-policy §6/§11 裁决 10）：rerank 打分 → 按分降序取 top_n →
  top_n 内从高到低遍历，与每个已保留项算 embedding 余弦相似度（向量已 L2 归一化，
  点积即余弦）≥ dedup_sim 则丢弃当前项（留高分弃低分），返回保留列表（仍按 rerank
  分降序）。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from psycopg import sql
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.llm import Reranker, UpstreamError, cosine_to_similarity
from app.db.models import Chunk, Document, QaPair
from app.rag.fold import fold_top_n

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
    doc_type：所属文档分型 code（documents.doc_type，metrics-balance §3.2 查询期
    参数化与评测 trace 的地基）；测试等手工构造处缺省 general。
    """

    chunk_id: int
    document_id: int
    title: str
    page_no: int | None
    title_path: str
    content: str
    similarity: float
    embedding: list[float]
    doc_type: str = "general"


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
    DocHit.similarity 暂存 embedding 余弦相似度，重排后由 rerank sigmoid 分覆写；
    doc_type 自 documents 透出（评测 trace 记录候选分型，metrics-balance §4.2）。
    """
    _set_ef_search(db, ef_search)
    rows = db.execute(_chunk_search_statement(query_vec, dept_ids, top_k)).all()
    return [
        DocHit(
            chunk_id=chunk_id,
            document_id=document_id,
            title=title,
            doc_type=doc_type,
            page_no=page_no,
            title_path=title_path,
            content=content,
            similarity=cosine_to_similarity(float(distance)),
            embedding=_parse_embedding(embedding),
        )
        for chunk_id, document_id, title, doc_type, page_no, title_path, content, distance, embedding in rows
    ]


def merge_multi_query_hits(paths: list[list[DocHit]]) -> list[DocHit]:
    """multi_query 多路召回合并（retrieval-policy §4）：按 chunk_id 去重、余弦取各路最高、
    整体按召回余弦降序；合并候选集交 rerank_and_fold **单次**精排（rerank 恒 1 次 ≤3 预算，
    跨路同尺可比）。分数口径=召回余弦（standards §4.4，不与 rerank 分混），纯函数不改写。"""
    best: dict[int, DocHit] = {}
    for hit in (candidate for path in paths for candidate in path):
        if hit.chunk_id not in best or hit.similarity > best[hit.chunk_id].similarity:
            best[hit.chunk_id] = hit
    return sorted(best.values(), key=lambda merged: merged.similarity, reverse=True)


def rerank_and_fold(
    reranker: Reranker,
    query: str,
    hits: list[DocHit],
    top_n: int | Callable[[list[DocHit]], int],
    dedup_sim: float,
    *,
    return_folded: bool = False,
    ranked_out: list[tuple[DocHit, float]] | None = None,
    degraded_out: list[bool] | None = None,
) -> list[DocHit] | tuple[list[DocHit], list[DocHit]]:
    """重排 + 折叠（F-1003）：top_n 内两两去重，返回 rerank 分降序保留列表。

    rerank 文本 = ``{title_path}\n{content}``（与入库分块文本同构），query 为改写后
    问题；打分后按分降序取 top_n，从高到低遍历：与任一已保留项的 embedding 余弦
    相似度 ≥ dedup_sim 则丢弃当前项。DocHit.similarity 在此覆写为 rerank sigmoid 分
    （拒答阈值口径，standards §4.4）；空候选短路，不发起 rerank 调用。

    top_n（metrics-balance §3.2 查询期参数化）：int 为固定条数（既有行为，逐字不变）；
    可调用形态 ``Callable[[list[DocHit]], int]`` 在 **rerank 排序完成后**以求值——入参
    为分降序 ranked 命中序列（top1 即最高分命中），档位由 top1 命中型决定（生产接线
    为 doc_profile.resolve_top_n）；空候选短路发生在求值之前。

    return_folded=False（缺省，问答编排路径）：返回保留列表，签名与语义同 Task 5；
    True（管理端检索测试 RET-03/04）：返回 (保留列表, 折叠丢弃列表)——进 top_n 但被
    近重复折叠的块带 rerank 分一并交回，供调参视角完整展示（问答编排不受影响）。

    ranked_out（metrics-balance §4.2 接线缝）：非 None 时把全量 ranked
    (DocHit 已覆写 rerank 分, rerank 分) 按分降序 append 进调用方列表——top_n 折叠
    之外候选分的唯一获取点（评测 trace 用）；主问答路径不传，行为零改动。

    degraded_out（retrieval-policy §3 弹性兜底缝）：非 None 时本调用结束 append 恰一个
    布尔（True=上游故障已走向量序兜底）；空候选短路不触碰该缝。**降级态口径**：不发起
    重排，按入参序（召回余弦降序）折叠，top_n callable 在向量序上求值；similarity 与
    ranked_out 分数均为召回余弦而非 rerank sigmoid 分，消费方（chat 阈值分轨、trace
    scoreScale="cosine"）按余弦口径消费，两套分数绝不互比（standards §4.4）。
    """
    if not hits:
        return ([], []) if return_folded else []
    docs = [f"{hit.title_path}\n{hit.content}" for hit in hits]
    try:
        scores = reranker.rerank(query, docs)
    except UpstreamError:
        return _fold_degraded(hits, top_n, dedup_sim, ranked_out, degraded_out, return_folded)
    ranked = sorted(zip(hits, scores, strict=True), key=lambda pair: pair[1], reverse=True)
    if ranked_out is not None:
        ranked_out.extend((replace(hit, similarity=score), score) for hit, score in ranked)
    if degraded_out is not None:
        degraded_out.append(False)
    keep_n = top_n([hit for hit, _ in ranked]) if callable(top_n) else top_n
    # 折叠主体共用 rag 域纯函数（retrieval-policy §6/裁决 10）：先按 rerank 分覆写
    # similarity（frozen dataclass 生成新实例），fold_top_n 只选择不改写分数
    return fold_top_n(
        [replace(hit, similarity=score) for hit, score in ranked],
        keep_n,
        dedup_sim,
        return_folded=return_folded,
    )


def _fold_degraded(
    hits: list[DocHit],
    top_n: int | Callable[[list[DocHit]], int],
    dedup_sim: float,
    ranked_out: list[tuple[DocHit, float]] | None,
    degraded_out: list[bool] | None,
    return_folded: bool,
) -> list[DocHit] | tuple[list[DocHit], list[DocHit]]:
    """上游重排故障的向量序兜底（retrieval-policy §3），参数同主函数同名缝；分数口径
    （召回余弦）见主函数 docstring 降级态段。"""
    if degraded_out is not None:
        degraded_out.append(True)
    ranked = [(hit, hit.similarity) for hit in hits]  # 召回序即余弦降序（search_chunks 语义）
    if ranked_out is not None:
        ranked_out.extend(ranked)
    keep_n = top_n([hit for hit, _ in ranked]) if callable(top_n) else top_n
    return fold_top_n(
        [hit for hit, _ in ranked],  # 降级态分数=召回余弦已在 hit.similarity，无需覆写
        keep_n,
        dedup_sim,
        return_folded=return_folded,
    )


def _chunk_search_statement(
    query_vec: list[float], dept_ids: list[int], top_k: int
) -> Select[tuple[Any, ...]]:
    """召回语句（独立成函数供测试编译断言：dept_ids 必经绑定参数占位，无字面量插值）。

    强制 JOIN documents（W2 移交项①）：归档（is_active=false）与未就绪
    （status != 'ready'）文档的 chunk 一律不可见；生效日期未到同样排除（工单 22，
    CURRENT_DATE 数据库时钟）。SELECT 显式取 Chunk.embedding 供折叠阶段两两余弦
    比较，Document.doc_type 供评测 trace 记录候选分型。
    """
    distance = Chunk.embedding.cosine_distance(query_vec)
    return (
        select(
            Chunk.id,
            Chunk.document_id,
            Document.title,
            Document.doc_type,  # 分型 code（metrics-balance §3.2/§4.2）
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
            # 生效日期前置闸（工单 22）：未生效文档不可见——CURRENT_DATE 走数据库时钟，
            # 与到期清扫（expire_due_documents）同 UTC 日期口径，零签名改动
            Document.effective_date <= func.current_date(),
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
