r"""问答编排（Task 8，W2 链路枢纽）：一轮问答的检索决策计划与落库收尾。

prepare_turn 时序（brief ①-⑧）：多轮先改写（仅携带最近 3 轮，上游故障降级原问题）
→ normalize_question → 精确缓存查（键 = 部门 + 规范化改写问题，§17.5 多轮用改写后
问题作键）→ 命中返 cache 计划 → 未命中 embed 一次计算复用 → match_qa 命中返 qa
计划（score=embedding 余弦相似度）→ 未命中 search_chunks + rerank_and_fold（top_n 按
top1 命中型取档，resolve_top_n，metrics-balance §5）→ fold
空或 top1.similarity < refuse_threshold → refuse 计划（不写缓存）→ 否则 rag 计划
（score=rerank sigmoid 分）。分数口径（强制交接①，standards §4.4）：拒答判定与
citation score 只取 rerank_and_fold 产物——其之前的 DocHit.similarity 是召回余弦
（仅供检索内部排序），两套分数绝不互比。
长期记忆注入（F-210 §3.3，agent-evolution）：user_id 非 None 时先召回本人启用且
活跃的画像，非空则 rag 分支把 <user_memory> 块（不可信声明内建）拼进 chat_system，
qa/拒答/缓存分支不注入；注入非空时缓存键追加 user 维度后缀（§3.4，收敛在
ExactCache.make_key 一处），为空保持旧键。
缓存 put 不发生在 prepare：生成完成 finalize 后由 Task 9 以 plan.cache_key 执行
（refuse 不可缓存、cache 计划已在缓存中，两者 cache_key 恒为 None）；缓存条目契约
（Task 9 写、prepare 读）：{"answer": str, "citations": list[dict], "answer_type":
str, "model": str}。本模块只 flush 不 commit、只 HitCounter.add 不 flush，事务边界
归调用方（对齐 services 层惯例，record_audit 先例）。
会话/消息存取、落库收尾与归属解析拆至 conversations_store.py（行数红线 §1.4，本模块
转出再导出；metrics-balance 03 因本文件触及 300 行红线追加转出 finalize 等）。
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.assembly import PromptPack
from app.core.config import Settings
from app.core.llm import Embedder, LLMProvider, Reranker
from app.rag.doc_profile import resolve_top_n
from app.rag.prompt import (
    ContextSource,
    build_chat_messages,
    build_context_block,
    build_user_memory_block,
)
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.conversations_store import (
    MSG_CONVERSATION_NOT_FOUND,
    MSG_MESSAGE_NOT_FOUND,
    finalize_turn,
    list_conversation_messages,
    list_user_conversations,
    load_recent_history,
    mark_interrupted,
    resolve_own_answer,
    resolve_owned_conversation,
)
from app.services.memory import recall_recent
from app.services.retrieval import (
    DocHit,
    QaHit,
    normalize_question,
    rerank_and_fold,
)
from app.services.rewrite import rewrite_question
from app.services.vector_store import VectorStore

# 会话/消息存取与归属解析自本文件拆出（行数红线 §1.4）：实现在 conversations_store.py，
# 此处转出再导出保持既有导入路径（from app.services.chat import resolve_owned_conversation
# 等，api/chat、api/conversations、api/feedback 三端点消费）。
__all__ = [
    "MSG_CONVERSATION_NOT_FOUND",
    "MSG_MESSAGE_NOT_FOUND",
    "ChatDeps",
    "TurnPlan",
    "finalize_turn",
    "list_conversation_messages",
    "list_user_conversations",
    "load_recent_history",
    "mark_interrupted",
    "prepare_turn",
    "resolve_own_answer",
    "resolve_owned_conversation",
]

_REWRITE_ROUNDS: int = 3  # 改写历史取最近 3 轮（prompt 层不截条数，截断归调用方约定）


@dataclass
class TurnPlan:
    """一轮问答的执行计划（Task 9 SSE 端点直接消费，字段名即接口契约，不得删改）。

    kind: 'cache' | 'qa' | 'rag' | 'refuse'；citations 为 SSE DTO 形态（camelCase
    键，standards §3.6）亦即 messages.citations 的 JSONB 存储形态；llm_messages 仅
    qa/rag 非空；cache_entry 仅 cache 计划非空（命中条目，Task 9 取其中 answer 直接
    改流下发）；cache_key 仅 qa/rag 非空——finalize 后由 Task 9 以其 put。
    retrieval_trace（metrics-balance §4.2 追加，可选）：评测模式检索轨迹
    {contextChunkIds, topKCandidates}，仅 prepare_turn 传入 ranked_out 时装配；
    主问答路径恒 None（不落 trace，行为零改动）。
    """

    kind: str
    citations: list[dict]
    rewritten_question: str
    llm_messages: list[dict] | None
    cache_entry: dict | None
    cache_key: str | None
    retrieval_trace: dict | None = None


@dataclass
class ChatDeps:
    """问答链路依赖包：Task 8 消费检索/提示词/缓存，Task 9 消费 counter（每日限额）
    与 settings.chat_max_tokens（生成 clamp）；store 为 VectorStore 第六接口
    （规范/规格审查补齐，实现由装配层选择，见 services/vector_store）。"""

    embedder: Embedder
    reranker: Reranker
    llm: LLMProvider
    cache: ExactCache
    counter: DailyCounter
    hit_counter: HitCounter
    settings: Settings
    prompts: PromptPack
    store: "VectorStore"


def prepare_turn(
    db: Session,
    deps: ChatDeps,
    user_dept_id: int,
    all_dept_id: int,
    question_raw: str,
    history: list[tuple[str, str]],
    *,
    user_id: int | None = None,
    ranked_out: list[tuple[DocHit, float]] | None = None,
) -> TurnPlan:
    """按 brief 时序产出一轮计划：改写 → 归一化 → 缓存 → QA → 检索折叠 → 计划。

    dept_ids 恒为 [user_dept_id, all_dept_id]（F-502 部门隔离，全员部门参与过滤
    语义）；refuse 计划 citations 恒空、llm_messages 恒 None；缓存 put 不发生在
    prepare（Task 9 finalize 后执行）。ranked_out 非 None 即评测模式（metrics-balance
    §4.2）：检索候选全量分经此收集并装配 plan.retrieval_trace，主路径不传零改动。

    user_id 非 None 时启用长期记忆注入（F-210 §3.3，agent-evolution）：仅 rag 分支
    把 <user_memory> 块拼进 system（qa 整条返回/拒答无生成必要，均不注入）；注入
    非空时缓存键追加 user 维度（§3.4，带不同画像的两人不互串）。user_id=None（评测
    引擎等直调方）或缺省时跳过召回查询，行为与上线前逐字节一致。
    """
    rewritten = question_raw
    if history:
        rewritten = rewrite_question(
            deps.llm,
            history[-2 * _REWRITE_ROUNDS :],
            question_raw,
            system=deps.prompts.rewrite_system,
        )
    memory_block = ""
    if user_id is not None:
        memories = recall_recent(db, user_id)
        if memories:
            memory_block = build_user_memory_block(memories)
    cache_key = deps.cache.make_key(
        user_dept_id,
        normalize_question(rewritten),
        user_suffix=f"u{user_id}" if memory_block else None,
    )
    cached = deps.cache.get(cache_key)
    if cached is not None:
        plan = TurnPlan("cache", cached.get("citations", []), rewritten, None, cached, None)
        return _with_trace(plan, ranked_out)
    query_vec = deps.embedder.embed([rewritten])[0]  # 一次计算，qa 与文档链路复用
    dept_ids = [user_dept_id, all_dept_id]
    qa_hit = deps.store.match_qa(db, query_vec, dept_ids, deps.settings.qa_threshold)
    if qa_hit is not None:
        return _with_trace(
            _qa_plan(qa_hit, rewritten, cache_key, deps.prompts.chat_system), ranked_out
        )
    folded = _retrieve_folded(db, deps, query_vec, dept_ids, rewritten, ranked_out=ranked_out)
    if not folded or folded[0].similarity < deps.settings.refuse_threshold:
        plan = TurnPlan("refuse", [], rewritten, None, None, None)  # 拒答不写缓存
        return _with_trace(plan, ranked_out)
    deps.hit_counter.add([hit.chunk_id for hit in folded])  # 仅 rag 路径计数（F-1011）
    chat_system = deps.prompts.chat_system
    if memory_block:
        chat_system = chat_system.rstrip() + "\n" + memory_block  # 仅 rag 分支注入（§3.3）
    return _with_trace(_rag_plan(folded, rewritten, cache_key, chat_system), ranked_out)


def _retrieve_folded(
    db: Session,
    deps: ChatDeps,
    query_vec: list[float],
    dept_ids: list[int],
    rewritten: str,
    *,
    ranked_out: list[tuple[DocHit, float]] | None = None,
) -> list[DocHit]:
    """向量召回（经 VectorStore 接口）+ 重排折叠：产物 DocHit.similarity 已是
    rerank sigmoid 分（阈值口径）；ranked_out 原样透传收集缝（评测 trace）。
    top_n 传 resolve_top_n（metrics-balance §5，§17.3 管的检索行为变更）：精排按
    top1 命中型截取条数，general 档与旧固定 rerank_top_n=5 同值（缺省行为不变）。"""
    hits = deps.store.search_chunks(
        db, query_vec, dept_ids, deps.settings.retriever_top_k, deps.settings.hnsw_ef_search
    )
    return rerank_and_fold(
        deps.reranker,
        rewritten,
        hits,
        resolve_top_n,
        deps.settings.chunk_dedup_sim,
        ranked_out=ranked_out,
    )


def _qa_plan(qa_hit: QaHit, rewritten: str, cache_key: str, chat_system: str) -> TurnPlan:
    """qa 计划：恰 1 条 qa 引用（score=余弦相似度），答案经 ContextSource 进 <data>。"""
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
    messages = build_chat_messages(chat_system, build_context_block([source]), rewritten)
    return TurnPlan("qa", [citation], rewritten, messages, None, cache_key)


def _rag_plan(folded: list[DocHit], rewritten: str, cache_key: str, chat_system: str) -> TurnPlan:
    """rag 计划：citations 按 fold 序（rerank 分降序）编号 1..N，资料段与之一一对应。"""
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
    messages = build_chat_messages(chat_system, build_context_block(sources), rewritten)
    return TurnPlan("rag", citations, rewritten, messages, None, cache_key)


def _with_trace(plan: TurnPlan, ranked_out: list[tuple[DocHit, float]] | None) -> TurnPlan:
    """评测缝（metrics-balance §4.2）：ranked_out 非 None 时装配 retrieval_trace。

    contextChunkIds=citations 中 refType=doc 的 refId（qa 引用非 chunk 不入集，缓存
    命中题自缓存条目 citations 还原）；topKCandidates=全量 ranked 候选（含 top_n
    折叠丢弃者，无检索则空）。主路径 ranked_out=None 原样返回，行为零改动。"""
    if ranked_out is None:
        return plan
    plan.retrieval_trace = {
        "contextChunkIds": [c["refId"] for c in plan.citations if c["refType"] == "doc"],
        "topKCandidates": [
            {"chunkId": hit.chunk_id, "docType": hit.doc_type, "rerankScore": score}
            for hit, score in ranked_out
        ],
    }
    return plan
