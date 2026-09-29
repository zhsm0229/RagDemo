r"""管理端检索测试服务（Task 10，F-503/F-703，RET-03/04）：检索管道的 admin 调参重放。

与 chat.prepare_turn 同一管道（normalize_question → embed → match_qa → search_chunks
→ rerank_and_fold）但 qa 与 docs 两路结果都展示、互不短路——qa 命中不遮蔽文档召回
（调参需同时看到两道闸的原始表现）。与问答链路的三点差异（brief 钉死）：
- 不调 LLM（无改写无生成）、不写 hit_counter（调参流量不污染 F-1009 零召回榜）；
- fold 前后信息都展示：rerank_and_fold(return_folded=True) 取回进 top_n 但被折叠
  丢弃的块，与保留块合并按 rerank 分降序，folded 标记供前端打标；
- 两套分数字段分明（standards §4.4）：qa.similarity=embedding 余弦（qa_threshold
  口径）、docs[].rerankScore=rerank sigmoid（refuse_threshold 口径）、
  docs[].recallCosine=召回余弦（rerank 覆写前的 search_chunks 产物，仅展示）。
thresholdPassed 逐块对照当前 Settings（qa 用 qa_threshold、doc 用 refuse_threshold）；
admin 视角 deptCode 任意（service|hr|all），检索 SQL 恒带 dept_id IN 部门过滤（F-502）。
只读不写库（无 commit 无审计，同 services.admin_views 只读服务惯例）。
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.core.llm import Embedder, Reranker
from app.db.models import Department
from app.services.retrieval import (
    DocHit,
    QaHit,
    match_qa,
    normalize_question,
    rerank_and_fold,
    search_chunks,
)

_DEPT_CODES = ("service", "hr", "all")
_MSG_BAD_DEPT = "部门编码无效：{code}（可选 service/hr/all）"  # 与 qa_pairs._resolve_dept 同文案


@dataclass(frozen=True)
class DocTestView:
    """文档块调参视图：rerankScore 与 recallCosine 两套分数各占字段，folded 标记
    进 top_n 但被折叠丢弃的块（brief：调参需看到被折叠块）。"""

    chunk_id: int
    document_title: str
    page_no: int | None
    title_path: str
    rerank_score: float
    recall_cosine: float
    threshold_passed: bool
    folded: bool


@dataclass(frozen=True)
class RetrievalTestReport:
    """检索测试报告：qa 与 docs 独立成路（空库时 qa=None 且 docs=[]），阈值回显
    当前 Settings 供前端对照（RET-04 调参依据）。"""

    qa: QaHit | None
    docs: list[DocTestView]
    qa_threshold: float
    refuse_threshold: float


def run_retrieval_test(
    db: Session,
    embedder: Embedder,
    reranker: Reranker,
    settings: Settings,
    question: str,
    dept_code: str,
) -> RetrievalTestReport:
    """重放检索管道：归一化 → embed（问答链路同一 embedder 实例，指纹空间一致）→
    qa 匹配 + 文档召回重排折叠。embedder/reranker 经 app.state.chat_deps 注入
    （装配层唯一选择实现，standards §1.2）；召回余弦在 fold 覆写前按 chunk_id
    留存供展示。"""
    normalized = normalize_question(question)
    query_vec = embedder.embed([normalized])[0]  # 一次计算，qa 与文档链路复用（chat.py 同款）
    dept_ids = _view_dept_ids(db, dept_code)
    qa_hit = match_qa(db, query_vec, dept_ids, settings.qa_threshold)
    hits = search_chunks(db, query_vec, dept_ids, settings.retriever_top_k, settings.hnsw_ef_search)
    recall_cosine = {hit.chunk_id: hit.similarity for hit in hits}
    kept, folded_out = rerank_and_fold(
        reranker,
        normalized,
        hits,
        settings.rerank_top_n,
        settings.chunk_dedup_sim,
        return_folded=True,
    )
    docs = [
        _doc_view(hit, recall_cosine[hit.chunk_id], settings.refuse_threshold, folded)
        for hit, folded in [(item, False) for item in kept] + [(item, True) for item in folded_out]
    ]
    docs.sort(key=lambda view: view.rerank_score, reverse=True)
    return RetrievalTestReport(
        qa=qa_hit,
        docs=docs,
        qa_threshold=settings.qa_threshold,
        refuse_threshold=settings.refuse_threshold,
    )


def _doc_view(
    hit: DocHit, recall_cosine: float, refuse_threshold: float, folded: bool
) -> DocTestView:
    """DocHit（fold 后 similarity=rerank sigmoid 分）→ 调参视图；阈值标记对照当前
    Settings，与 folded 互相独立（被折叠块同样标记是否过拒答阈值）。"""
    return DocTestView(
        chunk_id=hit.chunk_id,
        document_title=hit.title,
        page_no=hit.page_no,
        title_path=hit.title_path,
        rerank_score=hit.similarity,
        recall_cosine=recall_cosine,
        threshold_passed=hit.similarity >= refuse_threshold,
        folded=folded,
    )


def _view_dept_ids(db: Session, dept_code: str) -> list[int]:
    """deptCode → F-502 检索视角：service/hr → [该部门, 全员]，all → [全员]。

    枚举外（api 层 Literal 兜底后的服务级防线）或未播种 → 422（qa_pairs._resolve_dept
    同文案口径）；admin 调参允许任意视角，但过滤语义与真实用户完全一致。
    """
    if dept_code not in _DEPT_CODES:
        raise ApiError(ERR_PARAM_INVALID, _MSG_BAD_DEPT.format(code=dept_code), 422)
    dept = db.scalars(select(Department).where(Department.code == dept_code)).first()
    if dept is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_BAD_DEPT.format(code=dept_code), 422)
    if dept_code == "all":
        return [dept.id]
    all_dept = db.scalars(select(Department).where(Department.code == "all")).first()
    if all_dept is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_BAD_DEPT.format(code="all"), 422)
    return [dept.id, all_dept.id]
