r"""问答编排（Task 8，W2 链路枢纽）：一轮问答的检索决策计划与落库收尾。

prepare_turn 时序（brief ①-⑧）：多轮先改写（仅携带最近 3 轮，上游故障降级原问题）
→ normalize_question → 精确缓存查（键 = 部门 + 规范化改写问题，§17.5 多轮用改写后
问题作键）→ 命中返 cache 计划 → 未命中 embed 一次计算复用 → match_qa 命中返 qa
计划（score=embedding 余弦相似度）→ 未命中 search_chunks + rerank_and_fold → fold
空或 top1.similarity < refuse_threshold → refuse 计划（不写缓存）→ 否则 rag 计划
（score=rerank sigmoid 分）。分数口径（强制交接①，standards §4.4）：拒答判定与
citation score 只取 rerank_and_fold 产物——其之前的 DocHit.similarity 是召回余弦
（仅供检索内部排序），两套分数绝不互比。
缓存 put 不发生在 prepare：生成完成 finalize 后由 Task 9 以 plan.cache_key 执行
（refuse 不可缓存、cache 计划已在缓存中，两者 cache_key 恒为 None）；缓存条目契约
（Task 9 写、prepare 读）：{"answer": str, "citations": list[dict], "answer_type":
str, "model": str}。本模块只 flush 不 commit、只 HitCounter.add 不 flush，事务边界
归调用方（对齐 services 层惯例，record_audit 先例）。
"""

from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.assembly import PromptPack
from app.core.config import Settings
from app.core.errors import ERR_NOT_FOUND, ApiError
from app.core.llm import Embedder, LLMProvider, Reranker
from app.db.models import Conversation, Message, User
from app.rag.prompt import ContextSource, build_chat_messages, build_context_block
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.retrieval import (
    DocHit,
    QaHit,
    match_qa,
    normalize_question,
    rerank_and_fold,
    search_chunks,
)
from app.services.rewrite import rewrite_question

_REWRITE_ROUNDS: int = 3  # 改写历史取最近 3 轮（prompt 层不截条数，截断归调用方约定）
MSG_CONVERSATION_NOT_FOUND: str = "会话不存在或无权访问"  # 404/3003 对外文案（归属/软删共用）


@dataclass
class TurnPlan:
    """一轮问答的执行计划（Task 9 SSE 端点直接消费，字段名即接口契约，不得删改）。

    kind: 'cache' | 'qa' | 'rag' | 'refuse'；citations 为 SSE DTO 形态（camelCase
    键，standards §3.6）亦即 messages.citations 的 JSONB 存储形态；llm_messages 仅
    qa/rag 非空；cache_entry 仅 cache 计划非空（命中条目，Task 9 取其中 answer 直接
    改流下发）；cache_key 仅 qa/rag 非空——finalize 后由 Task 9 以其 put。
    """

    kind: str
    citations: list[dict]
    rewritten_question: str
    llm_messages: list[dict] | None
    cache_entry: dict | None
    cache_key: str | None


@dataclass
class ChatDeps:
    """问答链路依赖包：Task 8 消费检索/提示词/缓存，Task 9 消费 counter（每日限额）
    与 settings.chat_max_tokens（生成 clamp）。"""

    embedder: Embedder
    reranker: Reranker
    llm: LLMProvider
    cache: ExactCache
    counter: DailyCounter
    hit_counter: HitCounter
    settings: Settings
    prompts: PromptPack


def prepare_turn(
    db: Session,
    deps: ChatDeps,
    user_dept_id: int,
    all_dept_id: int,
    question_raw: str,
    history: list[tuple[str, str]],
) -> TurnPlan:
    """按 brief 时序产出一轮计划：改写 → 归一化 → 缓存 → QA → 检索折叠 → 计划。

    dept_ids 恒为 [user_dept_id, all_dept_id]（F-502 部门隔离，全员部门参与过滤
    语义）；refuse 计划 citations 恒空、llm_messages 恒 None；缓存 put 不发生在
    prepare（Task 9 finalize 后执行）。
    """
    rewritten = question_raw
    if history:
        rewritten = rewrite_question(
            deps.llm,
            history[-2 * _REWRITE_ROUNDS :],
            question_raw,
            system=deps.prompts.rewrite_system,
        )
    cache_key = deps.cache.make_key(user_dept_id, normalize_question(rewritten))
    cached = deps.cache.get(cache_key)
    if cached is not None:
        return TurnPlan("cache", cached.get("citations", []), rewritten, None, cached, None)
    query_vec = deps.embedder.embed([rewritten])[0]  # 一次计算，qa 与文档链路复用
    dept_ids = [user_dept_id, all_dept_id]
    qa_hit = match_qa(db, query_vec, dept_ids, deps.settings.qa_threshold)
    if qa_hit is not None:
        return _qa_plan(qa_hit, rewritten, cache_key, deps.prompts.chat_system)
    folded = _retrieve_folded(db, deps, query_vec, dept_ids, rewritten)
    if not folded or folded[0].similarity < deps.settings.refuse_threshold:
        return TurnPlan("refuse", [], rewritten, None, None, None)  # 拒答不写缓存
    deps.hit_counter.add([hit.chunk_id for hit in folded])  # 仅 rag 路径计数（F-1011）
    return _rag_plan(folded, rewritten, cache_key, deps.prompts.chat_system)


def _retrieve_folded(
    db: Session, deps: ChatDeps, query_vec: list[float], dept_ids: list[int], rewritten: str
) -> list[DocHit]:
    """向量召回 + 重排折叠：产物 DocHit.similarity 已是 rerank sigmoid 分（阈值口径）。"""
    hits = search_chunks(
        db, query_vec, dept_ids, deps.settings.retriever_top_k, deps.settings.hnsw_ef_search
    )
    return rerank_and_fold(
        deps.reranker, rewritten, hits, deps.settings.rerank_top_n, deps.settings.chunk_dedup_sim
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
            "title": hit.title,
            "page": hit.page_no,
            "titlePath": hit.title_path,
            "score": hit.similarity,
        }
        for n, hit in enumerate(folded, start=1)
    ]
    messages = build_chat_messages(chat_system, build_context_block(sources), rewritten)
    return TurnPlan("rag", citations, rewritten, messages, None, cache_key)


def finalize_turn(
    db: Session,
    assistant_msg_id: int,
    *,
    content: str,
    citations: list[dict],
    answer_type: str,
    model: str,
    usage: tuple[int, int] | None,
    latency_ms: int,
    cache_hit: bool,
) -> None:
    """收尾占位 assistant 消息：纯 UPDATE + flush，commit 归调用方（Task 9）。

    usage 即 provider.last_usage，None（拒答未调 LLM 等）写 0；answer_type 取值
    'rag'/'qa'/'refuse'（CHECK 约束）——cache 计划由 Task 9 取缓存条目内的原
    answer_type，不得传 'cache'。缓存 put 同归 Task 9 在 finalize 后执行。
    """
    prompt_tokens, completion_tokens = usage if usage is not None else (0, 0)
    db.execute(
        update(Message)
        .where(Message.id == assistant_msg_id)
        .values(
            content=content,
            citations=citations,
            answer_type=answer_type,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
        )
    )
    db.flush()


def mark_interrupted(db: Session, assistant_msg_id: int) -> None:
    """中断收尾（CHAT-12）：只置 is_active=false 标失败，不重复落库；flush 不 commit。"""
    db.execute(update(Message).where(Message.id == assistant_msg_id).values(is_active=False))
    db.flush()


def resolve_owned_conversation(db: Session, user: User, conversation_id: int) -> Conversation:
    """按 id 解析本人且 is_active 的会话，否则 404/3003（问答端点与会话管理端点共享）。

    不区分"不存在"与"无权"，不给探测空间（IDOR 防线 §21.8）；部门过滤豁免说明：
    个人会话仅属一人，user_id 归属校验严格强于 dept 过滤（api/chat.py 同注先例）。
    Task 10 自 api/chat.py._resolve_conversation 提取共享，两处行为必须一致。
    """
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id or not conversation.is_active:
        raise ApiError(ERR_NOT_FOUND, MSG_CONVERSATION_NOT_FOUND, 404)
    return conversation
