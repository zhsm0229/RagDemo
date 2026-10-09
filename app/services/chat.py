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
会话/消息存取、落库收尾与归属解析拆至 conversations_store.py；TurnPlan/ChatDeps
契约拆至 turn_plan.py；qa/rag 计划装配与评测 trace 缝拆至 chat_plans.py（行数红线
§1.4，本模块转出再导出，既有导入路径不变）。
"""

from sqlalchemy.orm import Session

from app.core.circuit_breaker import get_rerank_breaker
from app.core.llm import UpstreamError
from app.rag.doc_profile import resolve_top_n
from app.rag.prompt import build_user_memory_block
from app.rag.retrieval_policy import RetrievalPolicy, resolve_policy
from app.services.chat_plans import qa_plan, rag_plan, with_trace
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
    merge_multi_query_hits,
    normalize_question,
    rerank_and_fold,
)
from app.services.rewrite import rewrite_question, rewrite_question_multi
from app.services.turn_plan import ChatDeps, TurnPlan

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
    sub_queries: list[str] = []
    if history:
        truncated = history[-2 * _REWRITE_ROUNDS :]
        if resolve_policy(question_raw, compute_tier=deps.settings.compute_tier).multi_query:
            # multi_query 门控命中（L0 multi_hop 信号且 gpu 档，spec §4）：改写升级为一次
            # 三产物调用（改写+意图+子查询）；解析失败/上游故障已在 rewrite 层降级单路
            outcome = rewrite_question_multi(
                deps.llm,
                truncated,
                question_raw,
                system=deps.prompts.rewrite_system,
                multi_system=deps.prompts.rewrite_multi_system,
            )
            rewritten, sub_queries = outcome.rewritten, outcome.sub_queries
        else:  # 门控关闭（本机 cpu 档）：既有单产物改写路径逐字不变
            rewritten = rewrite_question(
                deps.llm, truncated, question_raw, system=deps.prompts.rewrite_system
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
        return with_trace(plan, ranked_out)
    try:
        # multi_query 分支同享一次 embed 调用：[改写问题, *子查询] 一次多向量（spec §4）；
        # 门控关闭时 sub_queries 恒空，调用形态与现状 embed([rewritten]) 逐字等价
        query_vecs = deps.embedder.embed([rewritten, *sub_queries])
    except UpstreamError:
        # embed 上游故障（spec retrieval-policy §3 捕获层裁决）：拒答话术计划收口不外泄，
        # answer_type 恒 refuse——主链路/评测/检索测试三调用方同享，500 不再漏出
        return with_trace(
            TurnPlan("refuse", [], rewritten, None, None, None, degraded=True), ranked_out
        )
    query_vec = query_vecs[0]  # qa 与文档链路复用改写问题向量（多路仅用于文档召回合并）
    policy = resolve_policy(
        rewritten,
        compute_tier=deps.settings.compute_tier,
        circuit_state=get_rerank_breaker().state,
    )
    dept_ids = [user_dept_id, all_dept_id]
    qa_hit = deps.store.match_qa(db, query_vec, dept_ids, deps.settings.qa_threshold)
    if qa_hit is not None:
        return with_trace(
            qa_plan(qa_hit, rewritten, cache_key, deps.prompts.chat_system, policy.char_budget),
            ranked_out,
        )
    degraded_marks: list[bool] = []
    folded = _retrieve_folded(
        db,
        deps,
        policy,
        query_vecs,
        dept_ids,
        rewritten,
        ranked_out=ranked_out,
        degraded_out=degraded_marks,
    )
    degraded = any(degraded_marks)  # 收集缝恰一元素（True=兜底轮）；bool(列表) 会对 [False] 误判
    # 拒答闸两套阈值分轨（spec §3，§4.4 分数口径分轨同理）：降级态分数是召回余弦，
    # 用 fallback_refuse_threshold；正常态是 rerank sigmoid 分，用 refuse_threshold
    threshold = (
        deps.settings.fallback_refuse_threshold if degraded else deps.settings.refuse_threshold
    )
    if not folded or folded[0].similarity < threshold:
        plan = TurnPlan(
            "refuse", [], rewritten, None, None, None, degraded=degraded
        )  # 拒答不写缓存
        return with_trace(plan, ranked_out)
    deps.hit_counter.add([hit.chunk_id for hit in folded])  # 仅 rag 路径计数（F-1011）
    chat_system = deps.prompts.chat_system
    if memory_block:
        chat_system = chat_system.rstrip() + "\n" + memory_block  # 仅 rag 分支注入（§3.3）
    # 降级态 rag 计划 cache_key=None 不写缓存（spec §11 裁决 2）：混尺分数不入缓存、
    # 标注不丢失，代价仅故障窗口重算
    return with_trace(
        rag_plan(
            folded,
            rewritten,
            None if degraded else cache_key,
            chat_system,
            degraded,
            policy.char_budget,
        ),
        ranked_out,
    )


def _retrieve_folded(
    db: Session,
    deps: ChatDeps,
    policy: RetrievalPolicy,
    query_vecs: list[list[float]],
    dept_ids: list[int],
    rewritten: str,
    *,
    ranked_out: list[tuple[DocHit, float]] | None = None,
    degraded_out: list[bool] | None = None,
) -> list[DocHit]:
    """向量召回（经 VectorStore 接口）+ 重排折叠：产物 DocHit.similarity 已是
    rerank sigmoid 分（阈值口径）；ranked_out/degraded_out 原样透传收集缝（评测
    trace 与降级标记，retrieval-policy §3）。降级轮产物分数=召回余弦（口径见
    rerank_and_fold docstring）。top_n 传 resolve_top_n（metrics-balance §5，
    §17.3 管的检索行为变更）：精排按 top1 命中型截取条数，general 档与旧固定
    rerank_top_n=5 同值（缺省行为不变）。

    召回参数读 RetrievalPolicy（retrieval-policy §2，工单 02，由 prepare_turn 解析
    传入）：top_k/ef_search 取 resolve_policy 缺省 20/40（与 settings 旧缺省同值，
    缺省输入零变化，等价证明由既有用例承担）；rerank_mode（熔断 open→vector_only）
    的执行由装配层熔断装饰器承载（工单 01，调用方零分支）。multi_query 召回分支
    （工单 06，spec §4）：多向量（改写问题+子查询 ≤3）逐路 search_chunks 后合并
    去重（merge_multi_query_hits），合并候选集交 rerank_and_fold **单次**精排
    （rerank 恒 1 次 ≤3 次预算）；单向量（门控关闭/三产物降级）与现状逐字等价。
    doc_type 恒缺省 general（§11 裁决 9，agent TASK_PROFILES 覆盖归工单 07）。"""
    recall_paths = [
        deps.store.search_chunks(db, query_vec, dept_ids, policy.top_k, policy.ef_search)
        for query_vec in query_vecs
    ]
    hits = merge_multi_query_hits(recall_paths) if len(recall_paths) > 1 else recall_paths[0]
    return rerank_and_fold(
        deps.reranker,
        rewritten,
        hits,
        resolve_top_n,
        deps.settings.chunk_dedup_sim,
        ranked_out=ranked_out,
        degraded_out=degraded_out,
    )
