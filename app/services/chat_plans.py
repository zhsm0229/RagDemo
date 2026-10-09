r"""问答计划装配（自 chat.py 转出，行数红线 §1.4，conversations_store 先例；
retrieval-policy §6 预案命中）：qa/rag 计划构建与评测 trace 装配缝。

三个纯装配函数，零 IO 零 DB：qa_plan/rag_plan 把折叠命中转成 TurnPlan（citations
为 SSE DTO 形态 camelCase，standards §3.6，亦即 messages.citations 的 JSONB 存储形
态）；with_trace 是评测模式收集缝（metrics-balance §4.2，主路径不传 ranked_out 零
改动）。分数口径（standards §4.4）：citation score 只取 rerank_and_fold 产物（降级
轮为召回余弦，scoreScale 由 trace 标注），两套分数绝不互比。char_budget 接
policy.char_budget（retrieval-policy §7 机制就位默认关：缺省 None 不截，逐字现状，
工单 06 rider 接线）。
"""

from app.rag.prompt import ContextSource, build_chat_messages, build_context_block
from app.services.retrieval import DocHit, QaHit
from app.services.turn_plan import TurnPlan


def qa_plan(
    qa_hit: QaHit, rewritten: str, cache_key: str, chat_system: str, char_budget: int | None
) -> TurnPlan:
    """qa 计划：恰 1 条 qa 引用（score=余弦相似度），答案经 ContextSource 进 <data>；
    char_budget 接 policy.char_budget（§7 机制就位默认关：缺省 None 不截，逐字现状）。"""
    source = ContextSource(
        n=1, ref_type="qa", title=qa_hit.question, title_path="", page=None, content=qa_hit.answer
    )
    citation = {
        "n": 1,
        "refType": "qa",
        "refId": qa_hit.qa_id,
        "title": qa_hit.question,
        "page": None,
        "titlePath": "",
        "score": qa_hit.similarity,
    }
    messages = build_chat_messages(
        chat_system, build_context_block([source], char_budget), rewritten
    )
    return TurnPlan("qa", [citation], rewritten, messages, None, cache_key)


def rag_plan(
    folded: list[DocHit],
    rewritten: str,
    cache_key: str | None,
    chat_system: str,
    degraded: bool,
    char_budget: int | None,
) -> TurnPlan:
    """rag 计划：citations 按 fold 序编号 1..N。降级轮（degraded=True）cache_key 恒
    None（spec §11 裁决 2），citation score 口径为召回余弦（scoreScale 由 trace 标注）；
    char_budget 接 policy.char_budget（§7 缺省 None 不截，逐字现状）。"""
    sources = [
        ContextSource(
            n=n,
            ref_type="doc",
            title=hit.title,
            title_path=hit.title_path,
            page=hit.page_no,
            content=hit.content,
        )
        for n, hit in enumerate(folded, start=1)
    ]
    citations = [
        {
            "n": n,
            "refType": "doc",
            "refId": hit.chunk_id,
            # docId：文档主键——前端引用弹窗取原文件/图片/预览按文档寻址；refId 是
            # chunk id，仅供 /api/citations/content 取切片正文，两者不可混用
            # （2026-10-05 缺陷修复：此前弹窗误用 refId 拼 /documents/{id}/file|images）。
            "docId": hit.document_id,
            "title": hit.title,
            "page": hit.page_no,
            "titlePath": hit.title_path,
            "score": hit.similarity,
        }
        for n, hit in enumerate(folded, start=1)
    ]
    messages = build_chat_messages(
        chat_system, build_context_block(sources, char_budget), rewritten
    )
    return TurnPlan("rag", citations, rewritten, messages, None, cache_key, degraded=degraded)


def with_trace(plan: TurnPlan, ranked_out: list[tuple[DocHit, float]] | None) -> TurnPlan:
    """评测缝（metrics-balance §4.2）：ranked_out 非 None 时装配 retrieval_trace。

    contextChunkIds=citations 中 refType=doc 的 refId（qa 引用非 chunk 不入集，缓存
    命中题自缓存条目 citations 还原）；topKCandidates=全量 ranked 候选（含 top_n
    折叠丢弃者，无检索则空）。主路径 ranked_out=None 原样返回，行为零改动。
    降级轮（retrieval-policy §3/§9）trace 增 degraded=True 与 scoreScale="cosine"
    标注候选分口径=召回余弦（非降级 trace 不加键，既有形状逐字不变）。"""
    if ranked_out is None:
        return plan
    plan.retrieval_trace = {
        "contextChunkIds": [c["refId"] for c in plan.citations if c["refType"] == "doc"],
        "topKCandidates": [
            {"chunkId": hit.chunk_id, "docType": hit.doc_type, "rerankScore": score}
            for hit, score in ranked_out
        ],
    }
    if plan.degraded:
        plan.retrieval_trace["degraded"] = True
        plan.retrieval_trace["scoreScale"] = "cosine"
    return plan
