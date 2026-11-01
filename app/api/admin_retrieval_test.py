r"""管理端检索测试路由（Task 10，F-503/F-703，RET-03/04）：admin 检索调参工具。

复用问答检索管道（services.retrieval_test_views）但不调 LLM、不写 hit_counter；
未挂 admin_views.py 汇聚 router——该文件已 242/300 行，按其头注释"超出时按域拆子
router"独立成文件、由 main.py 注册（同 admin_eval.py 先例）。权限门同管理端全家桶
（AdminUser：登录 → 首登改密 → 角色，401/2003、403/2004）；deptCode ∈ service|hr|all
（Literal 枚举外 422），admin 任意视角但检索 SQL 恒带部门过滤（F-502）。本层只做
参数接收与 DTO 组装（§1.2 分层），DTO 键 camelCase（§3.4），响应体 {code,message,data}。
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, field_validator

from app.api.deps import AdminUser, DbSession
from app.core.circuit_breaker import get_rerank_breaker
from app.services.retrieval import QaHit
from app.services.retrieval_test_views import DocTestView, RetrievalTestReport, run_retrieval_test

router = APIRouter(prefix="/admin", tags=["admin"])

_QUESTION_MAX_CHARS = 2000  # 与 ChatStreamRequest 同口径：问题 strip 后 1-2000 字


class RetrievalTestRequest(BaseModel):
    """POST /api/admin/retrieval-test 请求体：question + deptCode 检索视角。

    工单 30 参数对比（可选档位，全部缺省 None=线上装配值，行为与缺省形态逐字节
    一致）：qaThreshold/refuseThreshold（0-1）、topK（1-50）、topN（1-20）、efSearch
    （1-200）。model_copy 跳过校验——范围约束全部在本层 validators（design/30 R2）。"""

    question: str
    deptCode: Literal["service", "hr", "all"]
    qaThreshold: float | None = None
    refuseThreshold: float | None = None
    topK: int | None = None
    topN: int | None = None
    efSearch: int | None = None

    @field_validator("question")
    @classmethod
    def _strip_and_check_length(cls, value: str) -> str:
        """strip 后 1-2000 字（§21.5 字段级 422）：全空白等价空问题一并拒绝。"""
        stripped = value.strip()
        if not 1 <= len(stripped) <= _QUESTION_MAX_CHARS:
            raise ValueError("问题长度须在 1-2000 字之间")
        return stripped

    @field_validator("qaThreshold", "refuseThreshold")
    @classmethod
    def _check_threshold_range(cls, value: float | None) -> float | None:
        """阈值 0-1（工单 30）：None=线上值直通；显式值越界 422/1003 字段级。"""
        if value is not None and not 0 <= value <= 1:
            raise ValueError("阈值须在 0-1 之间")
        return value

    @field_validator("topK")
    @classmethod
    def _check_top_k(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 50:
            raise ValueError("topK 须在 1-50 之间")
        return value

    @field_validator("topN")
    @classmethod
    def _check_top_n(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 20:
            raise ValueError("topN 须在 1-20 之间")
        return value

    @field_validator("efSearch")
    @classmethod
    def _check_ef_search(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 200:
            raise ValueError("efSearch 须在 1-200 之间")
        return value


def _qa_payload(qa: QaHit | None, qa_threshold: float) -> dict | None:
    """qa 命中 DTO：similarity 为 embedding 余弦（qa_threshold 口径，standards §4.4，
    与 docs 的 rerankScore 两套分数不互比）；match_qa 低于阈值即返回 None，命中时
    thresholdPassed 恒 true——字段保留供前端统一渲染。"""
    if qa is None:
        return None
    return {
        "id": qa.qa_id,
        "question": qa.question,
        "similarity": qa.similarity,
        "thresholdPassed": qa.similarity >= qa_threshold,
    }


def _doc_payload(view: DocTestView) -> dict:
    """文档块 DTO（§3.4 camelCase）：rerankScore 与 recallCosine 两套分数分字段，
    folded 标记进 top_n 但被折叠丢弃的块（brief 钉死：调参需看到被折叠块）；
    hitCount/lastHitAt 为历史召回统计（F-1011，读现列，lastHitAt ISO8601、
    null=从未被召回），检索测试自身不写计数（不污染 F-1009 零召回榜）。"""
    return {
        "chunkId": view.chunk_id,
        "documentTitle": view.document_title,
        "page": view.page_no,
        "titlePath": view.title_path,
        "rerankScore": view.rerank_score,
        "recallCosine": view.recall_cosine,
        "thresholdPassed": view.threshold_passed,
        "folded": view.folded,
        "hitCount": view.hit_count,
        "lastHitAt": view.last_hit_at.isoformat() if view.last_hit_at else None,
    }


@router.post("/retrieval-test")
def retrieval_test(
    body: RetrievalTestRequest, request: Request, user: AdminUser, db: DbSession
) -> dict:
    """检索测试（RET-03/04）：重放 normalize → embed → match_qa → search_chunks →
    rerank_and_fold，qa 与 docs 两路结果并呈（互不短路）；每块 thresholdPassed 对照
    生效阈值逐块标记；不调 LLM、不写 hit_counter（不污染 F-1009 零召回榜），只读不落
    审计。embedder/reranker 取问答链路同一实例（app.state.chat_deps），问题向量与检索
    侧同指纹空间（documents.py._request_embedder 同款约束）。

    工单 30 参数对比：请求可选档位（None=线上值）经 settings 副本直传
    （run_retrieval_test 显式收 settings、链路全读该对象——app.state.chat_deps 零接触，
    design/30 R1/R2）；响应增 params 回显生效值与 degraded 熔断标注（open/half_open
    下 rerankScore 为余弦口径，thresholdPassed 对比结论不可信，R6）。"""
    deps = request.app.state.chat_deps
    override = {
        field: value
        for field, value in (
            ("qa_threshold", body.qaThreshold),
            ("refuse_threshold", body.refuseThreshold),
            ("retriever_top_k", body.topK),
            ("rerank_top_n", body.topN),
            ("hnsw_ef_search", body.efSearch),
        )
        if value is not None
    }
    effective = deps.settings.model_copy(update=override) if override else deps.settings
    report: RetrievalTestReport = run_retrieval_test(
        db, deps.embedder, deps.reranker, effective, body.question, body.deptCode, deps.store
    )
    breaker = get_rerank_breaker()
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "qa": _qa_payload(report.qa, report.qa_threshold),
            "docs": [_doc_payload(view) for view in report.docs],
            "thresholds": {"qa": report.qa_threshold, "refuse": report.refuse_threshold},
            "params": {
                "topK": effective.retriever_top_k,
                "topN": effective.rerank_top_n,
                "efSearch": effective.hnsw_ef_search,
                "chunkDedupSim": effective.chunk_dedup_sim,
            },
            "degraded": breaker is not None and breaker.state in ("open", "half_open"),
        },
    }
