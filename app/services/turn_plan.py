r"""问答计划与依赖包契约（自 chat.py 转出，行数红线 §1.4；retrieval-policy 波）。

TurnPlan 是 Task 9 SSE 端点直接消费的接口契约（字段名即契约不得删改），ChatDeps 是
装配层（main._build_chat_deps）与编排/流式/评测共用的依赖包——两者均为纯数据契约
零行为，独立成模块防契约被编排逻辑稀释；chat.py 转出再导出保持既有导入路径
（from app.services.chat import TurnPlan, ChatDeps 不变，conversations_store 先例）。
"""

from dataclasses import dataclass

from app.core.assembly import PromptPack
from app.core.config import Settings
from app.core.llm import Embedder, LLMProvider, Reranker
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter
from app.services.vector_store import VectorStore


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
    degraded（retrieval-policy §2 追加，可选）：检索上游降级轮（embed 失败拒答 /
    rerank 故障向量序兜底）为 True；降级轮 rag 计划 cache_key 恒 None、拒答闸走
    fallback_refuse_threshold、citation score 为召回余弦口径。
    """

    kind: str
    citations: list[dict]
    rewritten_question: str
    llm_messages: list[dict] | None
    cache_entry: dict | None
    cache_key: str | None
    retrieval_trace: dict | None = None
    degraded: bool = False


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
