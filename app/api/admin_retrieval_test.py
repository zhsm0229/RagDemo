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
from app.services.retrieval import QaHit
from app.services.retrieval_test_views import DocTestView, RetrievalTestReport, run_retrieval_test

router = APIRouter(prefix="/admin", tags=["admin"])

_QUESTION_MAX_CHARS = 2000  # 与 ChatStreamRequest 同口径：问题 strip 后 1-2000 字


class RetrievalTestRequest(BaseModel):
    """POST /api/admin/retrieval-test 请求体：question + deptCode 检索视角。"""

    question: str
    deptCode: Literal["service", "hr", "all"]

    @field_validator("question")
    @classmethod
    def _strip_and_check_length(cls, value: str) -> str:
        """strip 后 1-2000 字（§21.5 字段级 422）：全空白等价空问题一并拒绝。"""
        stripped = value.strip()
        if not 1 <= len(stripped) <= _QUESTION_MAX_CHARS:
            raise ValueError("问题长度须在 1-2000 字之间")
        return stripped


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
    当前 Settings 阈值逐块标记；不调 LLM、不写 hit_counter（不污染 F-1009 零召回榜），
    只读不落审计。embedder/reranker 取问答链路同一实例（app.state.chat_deps），
    问题向量与检索侧同指纹空间（documents.py._request_embedder 同款约束）。"""
    deps = request.app.state.chat_deps
    report: RetrievalTestReport = run_retrieval_test(
        db, deps.embedder, deps.reranker, deps.settings, body.question, body.deptCode, deps.store
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "qa": _qa_payload(report.qa, report.qa_threshold),
            "docs": [_doc_payload(view) for view in report.docs],
            "thresholds": {"qa": report.qa_threshold, "refuse": report.refuse_threshold},
        },
    }
