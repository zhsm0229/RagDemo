r"""问答/Agent 链路依赖装配（工单 26 自 main.py 拆分达 ≤300 行红线）。

StateBundle 状态五件+可选桶、chat/agent deps 装配与 test_mode 脚本化通道——实现类只在
装配层选择（六接口红线）；语义零变化（test_state_assembly / test_token_bucket /
api 全量绿即回归钉）。导入路径经 main 门面 re-export 保持不变（scheduler_jobs/
tests 的 `from app.main import _build_chat_deps` 等零改动）。
"""

import logging
from dataclasses import dataclass
from typing import Any

from app.core.agent_model import ChannelTurn, OpenAICompatToolChannel
from app.core.assembly import (
    build_embedder,
    build_llm,
    build_reranker,
    build_storage,
    load_prompt_pack,
)
from app.core.circuit_breaker import CircuitBreakerReranker, get_rerank_breaker
from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.llm import (
    Embedder,
    LLMProvider,
    Reranker,
    StubEmbedder,
    StubLLMProvider,
    StubReranker,
)
from app.core.security import JtiBlacklistProto, default_blacklist
from app.core.tools import ToolRegistry
from app.services.agent_runs import AgentDeps
from app.services.agent_tools import (
    KnowledgeBaseSearchTool,
    KnowledgeSearchDeps,
    SaveDraftDeps,
    SaveDraftTool,
)
from app.services.auth import (
    LOGIN_RATE_MAX_EVENTS,
    LOGIN_RATE_WINDOW_S,
    RateLimiterProto,
    default_limiter,
)
from app.services.chat import ChatDeps
from app.services.chat_cache import (
    ChatCacheProto,
    DailyCounter,
    DailyCounterProto,
    ExactCache,
    HitCounter,
    HitCounterProto,
    RateBucketProto,
    TokenBucketLimiter,
)
from app.services.retrieval import PgVectorStore
from app.services.vector_store import VectorStore


@dataclass
class StateBundle:
    """进程内状态五件套（工单 19 装配载体）：_build_state 的产物，lifespan 挂
    app.state.state_bundle——问答链路（cache/counter/hit_counter）与鉴权路由
    （limiter/blacklist）显式取用，消除缺省解析的隐式全局。bucket（工单 21）为
    可选第六件：memory 分支装配、pg 分支留 TODO（PG 桶未实施），缺省 None 兜底。"""

    cache: ChatCacheProto
    counter: DailyCounterProto
    hit_counter: HitCounterProto
    limiter: RateLimiterProto
    blacklist: JtiBlacklistProto
    bucket: RateBucketProto | None = None


def _build_state(settings: Settings) -> StateBundle:
    """状态五件装配（工单 19）：test_mode 恒 memory（conftest reset() 测试基建建立在
    进程内语义上）；state_backend=pg 外置三张 state_* 表（跨实例共享，工单 20 多副本
    前置）。clock 用 SystemClock 直连：状态五件是进程级单例，不随测试拨动全局可注入
    时钟而换实例（TTL 与限额口径在进程生命周期内稳定，原 _build_chat_deps 口径上收）。
    limiter/blacklist 的 memory 分支复用模块级单例：conftest 的 reset() 契约建立在
    同一对象上（api 测试隔离零改动），生产路由经 bundle 显式传参后不再触达缺省解析。"""
    backend = "memory" if settings.test_mode else settings.state_backend
    clock = SystemClock()
    if backend == "pg":
        from app.services.state_pg import PgDailyCounter, PgExactCache, PgHitCounter
        from app.services.state_pg_auth import PgJtiBlacklist, PgSlidingWindowLimiter

        # TODO(工单 21 预留)：PG 桶未实施——state_counters scope='bucket'（迁移 0020
        # 已含 refill_at 列）。scale 拓扑 + 限流同时启用需 PG 桶；缺省
        # rate_limiter_enabled=false 不阻塞（design/21 §3）。两开关同时开时令牌桶
        # 静默失效——至少告警，避免运维误以为第二层在生效（spec 评审 C3）。
        if settings.rate_limiter_enabled:
            logging.getLogger(__name__).warning(
                "STATE_BACKEND=pg 且 RATE_LIMITER_ENABLED=true：令牌桶 PG 实现未落地，"
                "分级限流第二层不生效（登录滑窗与日配额不受影响）；scale 拓扑建议先关总闸，"
                "待 PG 桶（工单 21 预留）落地后开启"
            )
        return StateBundle(
            cache=PgExactCache(clock, settings.cache_ttl_s),
            counter=PgDailyCounter(clock),
            hit_counter=PgHitCounter(clock),
            limiter=PgSlidingWindowLimiter(LOGIN_RATE_WINDOW_S, LOGIN_RATE_MAX_EVENTS, clock),
            blacklist=PgJtiBlacklist(clock),
        )
    return StateBundle(
        cache=ExactCache(clock, settings.cache_ttl_s),
        counter=DailyCounter(clock),
        hit_counter=HitCounter(clock),
        limiter=default_limiter,
        blacklist=default_blacklist,
        bucket=TokenBucketLimiter(clock),  # 常驻惰性对象：总闸关时零消费（守卫短路）
    )


def _build_chat_deps(settings: Settings, state: StateBundle | None = None) -> ChatDeps:
    """问答链路依赖包装配（Task 9）：test_mode → Stub 三件套，否则 TEI / OpenAI 兼容
    真实现（build_llm 非 test_mode 且 llm_base_url 空即 RuntimeError 拒启，§6.4）。
    提示词包先装载：温度随模板同版本化（§17.4），pack.temperature 传入 LLM 装配——
    manifest 改温度在装配期生效，不允许与 provider 缺省值静默脱钩。
    state 缺省自建：scheduler 周评测与启动补偿调用点零改动（工单 19）；lifespan
    传入 _build_state 产物，问答与鉴权路由共享同一份状态。"""
    state = state or _build_state(settings)
    prompts = load_prompt_pack()
    if settings.test_mode:
        embedder: Embedder = StubEmbedder()
        reranker: Reranker = StubReranker()
        llm: LLMProvider = StubLLMProvider()
    else:
        embedder = build_embedder(settings)
        reranker = build_reranker(settings)
        llm = build_llm(settings, prompts.temperature)
    # rerank 熔断装饰器在装配层包装 build_reranker 产物（retrieval-policy §3/§11 裁决 3：
    # 实现类只在装配层出现）；test_mode 同样包装——故障语义与生产一致，状态接
    # services_health 共享同一单例
    reranker = CircuitBreakerReranker(reranker, get_rerank_breaker())
    # VectorStore 第六接口在装配层选择实现（services 层协议不进 core/assembly——
    # 五层单向依赖）；pg 实现无状态不随 test_mode 变化（stub 的是向量产物，检索恒打真库）
    store: VectorStore = PgVectorStore()
    return ChatDeps(
        embedder=embedder,
        reranker=reranker,
        llm=llm,
        cache=state.cache,
        counter=state.counter,
        hit_counter=state.hit_counter,
        settings=settings,
        prompts=prompts,
        store=store,
        bucket=state.bucket,
    )


class ScriptedAgentChannel:
    """脚本化确定性通道（test_mode agent 装配用，Stub 三件套同哲学：同输入必同输出、
    零网络）。进度 = 对话里已有的工具结果条数（agent 循环每收一个观察回灌一条 tool
    消息）：0 条发首回合、N 条发第 N+1 回合，脚本耗尽恒重放末条——无实例状态，
    lifespan 级单例跨 run 安全（实例计数版会让第二个 run 起恒拿末条正文、无工具步，
    AGENT-01 时间线断言随机挂，2026-10-11 重启容器 A/B 实验实证）。定义在装配模块
    而非 core：core 只承载协议与适配层，业务剧本属装配选择（main 内定义实现类有
    _HashedAssets 先例）。"""

    def __init__(self, turns: list[ChannelTurn]) -> None:
        if not turns:
            raise ValueError("脚本化通道至少需要一个回合")
        self._turns = turns

    def complete(
        self, messages: Any, tools: Any, max_tokens: int
    ) -> ChannelTurn:  # AgentModelChannel 协议面：tools 只透传不消费
        tool_results = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")
        return self._turns[min(tool_results, len(self._turns) - 1)]


# test_mode 装配的 agent 演示剧本（ScriptedAgentChannel 注入）：
# 首轮发起一次 knowledge_base_search 工具调用、次轮给最终正文——恰好覆盖 AG-UI 的
# 工具事件组与文本事件组，事件序测试即以此剧本为准。
_TEST_AGENT_TURNS = (
    ChannelTurn(
        content="",
        tool_calls=(
            {
                "name": "knowledge_base_search",
                "args": {"query": "年假"},
                "id": "call_1",
                "type": "tool_call",
            },
        ),
    ),
    ChannelTurn(content="根据《考勤制度》，年假按工龄五到十天。"),
)


def _build_agent_deps(settings: Settings, chat_deps: ChatDeps) -> AgentDeps:
    """Agent 链路依赖装配（F-212，工单 #07/#06）：检索工具与问答共享 embedder/reranker/
    store（test_mode 同为 Stub/PgVectorStore 组合）；save_draft 包装草稿审核流数据面
    （draft_review.create_draft），storage 走装配工厂单例。工具注册表是全集——每任务
    实际绑定哪些工具由任务档案（services/agent_tasks.TASK_PROFILES）白名单决定。
    通道按档位设置装配，留空复用主问答通道（Settings.agent_model_* 注释口径），
    test_mode 换脚本化通道（零网络确定性）。counter 直取 chat_deps 同一实例：
    agent 与问答共享每日额度，换端点不绕限。"""
    registry = ToolRegistry()
    registry.register(
        KnowledgeBaseSearchTool(
            KnowledgeSearchDeps(
                embedder=chat_deps.embedder,
                store=chat_deps.store,
                reranker=chat_deps.reranker,
                settings=settings,
            )
        )
    )
    registry.register(SaveDraftTool(SaveDraftDeps(storage=build_storage(settings))))
    if settings.test_mode:
        channel = ScriptedAgentChannel(_TEST_AGENT_TURNS)
    else:
        channel = OpenAICompatToolChannel(
            base_url=settings.agent_model_base_url or settings.llm_base_url,
            api_key=settings.agent_model_api_key or settings.llm_api_key,
            model=settings.agent_model_model or settings.llm_model,
            timeout_s=settings.agent_model_timeout_s,
            effort=settings.llm_effort or None,
        )
    return AgentDeps(
        settings=settings,
        registry=registry,
        channel=channel,
        react_fallback=settings.agent_model_react_fallback,
        counter=chat_deps.counter,
        bucket=chat_deps.bucket,  # 共享同一桶实例：同用户跨入口水位一致视图（工单 21）
    )
