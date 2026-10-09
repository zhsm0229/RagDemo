r"""检索策略纯函数域（retrieval-policy §2，工单 02）：RetrievalPolicy + resolve_policy。

分层红线：本模块禁止 import services/db（纯函数域，doc_profile 同款约束，结构断言
看护）。设计要点（spec §2 冻结表）：每字段缺省值=现状行为——缺省输入下 resolve_policy
产出与既有硬编码参数逐字相等，等价证明由既有用例承担（§8"不新增测试文件"条款之外
的骨架单测钉住字面值）。本工单只做参数收拢与路由决策显式化，不新增行为：
- multi_query 双门控（L0 multi_hop 信号 且 compute_tier=gpu）在本机 cpu_constrained
  档恒 False；激活属 A 类资源门槛（§1，Mac Mini gpu 档解锁）。
- 动态 top_k/ef_search（P1 联动档 30/64）属 B 类证据门槛（§17.3 四指标对比+评审）：
  工单 07 预埋门控常量 DYNAMIC_TOPK_ENABLED（缺省 False=行为不变，唯一翻转点）；
  字符预算开闸同属 B 类，本阶段恒缺省（字段为开闸预留位）。
- L1 意图标签为 P1 预埋（§4 三产物合并调用），激活前 L0 词表是唯一意图源。
"""

from dataclasses import dataclass, fields, replace

# 参数缺省=现状（settings.retriever_top_k / hnsw_ef_search 旧缺省同值，spec §2 冻结表）
DEFAULT_TOP_K: int = 20
DEFAULT_EF_SEARCH: int = 40
RERANK_FULL: str = "full"
RERANK_VECTOR_ONLY: str = "vector_only"  # 降级/熔断态（spec §2）；执行由熔断装饰器承载
INTENT_SINGLE_HOP: str = "single_hop"
INTENT_MULTI_HOP: str = "multi_hop"
COMPUTE_TIER_CPU: str = "cpu_constrained"  # 本机 CPU TEI 缺省档（config.compute_tier 同值）
COMPUTE_TIER_GPU: str = "gpu"  # Mac Mini 部署档：A 类重手段的解锁闸

# L0 启发式词表（spec §2 冻结，改动须评审）：比较/多实体/因果三类，词包含匹配。
# 禁单字歧义词（"和""与"不入表，防"海南"式误报，裁决 11）；"各""还"为 spec 原文
# 既列的冻结例外（单测钉死，新增单字词即红）。
L0_INTENT_LEXICON: dict[str, tuple[str, ...]] = {
    "compare": ("相比", "对比", "区别", "哪个更", "差异"),
    "multi_entity": ("以及", "并且", "分别", "各", "还"),
    "causal": ("为什么", "如何影响", "会导致"),
}
_MULTI_HOP_MIN_CATEGORIES: int = 2  # 命中 ≥2 类 → multi_hop
_MULTI_HOP_MIN_WORDS: int = 3  # 或累计命中 ≥3 个词 → multi_hop

# 熔断态字面量与 app.core.circuit_breaker.OPEN 同值（spec §3 冻结）；不 import 以保持
# 零依赖纯函数域，值漂移由 test_circuit_breaker 与本模块单测双向对账。
_CIRCUIT_OPEN: str = "open"

# ---------------------------------------------------------------------------
# P1 top_k/ef_search 联动档与开闸门控（工单 07，spec §2 冻结表 P1 行）
# ---------------------------------------------------------------------------

# multi_hop 意图下的召回扩档值（spec §2：P1 联动档）。激活=B 类门槛（standards
# §17.3 四指标对比），当前缺省不启用——缺省输入恒产出 20/40（现状）。
MULTI_HOP_TOP_K: int = 30
MULTI_HOP_EF_SEARCH: int = 64

# 动态 top_k/ef_search 门控（B 类证据门槛，spec §1）：False=multi_hop 也产出缺省
# 20/40（行为不变）；True 且 intent=multi_hop 时产出联动档 30/64。本常量是未来
# 开闸的唯一翻转点——开闸须附 §17.3 四指标对比与评审记录，禁止在调用点旁路改参。
DYNAMIC_TOPK_ENABLED: bool = False


@dataclass(frozen=True)
class RetrievalPolicy:
    """一轮检索的参数包（spec §2 冻结表）：每字段缺省值=现状行为。

    degraded_hint 由调用方在得知上游降级后置位（检索前 resolver 无法预知，实际降级
    标志走工单 01 的 degraded_out 收集缝 → TurnPlan.degraded，本字段为其策略侧镜像）。
    """

    top_k: int = DEFAULT_TOP_K  # P1 联动档 30（B 类开闸前恒缺省）
    ef_search: int = DEFAULT_EF_SEARCH  # P1 联动 64（同上）
    rerank_mode: str = RERANK_FULL  # "vector_only"=降级/熔断态
    multi_query: bool = False  # 双门控：L0 multi_hop 且 compute_tier=gpu
    char_budget: int | None = None  # 字符预算（§7 机制就位默认关；0 与负数按缺省处理）
    intent: str = INTENT_SINGLE_HOP  # L0: "multi_hop"；L1: LLM 标签（P1 预埋）
    degraded_hint: bool = False  # 上游降级时置 True（SSE 透出，§5）


def detect_intent(question: str) -> str:
    """L0 意图启发式（零 LLM 零延迟）：词包含匹配，命中 ≥2 类或累计 ≥3 个词目 →
    multi_hop，否则 single_hop。

    "累计处数"按命中的词目数计（每词至多计 1，不按出现次数）——同词重复不成多实体
    信号（"为什么"×3 仍是单跳语义），边界样例见 test_retrieval_policy.py。
    """
    matched_categories = 0
    matched_words = 0
    for words in L0_INTENT_LEXICON.values():
        hits = sum(1 for word in words if word in question)
        if hits:
            matched_categories += 1
            matched_words += hits
    if matched_categories >= _MULTI_HOP_MIN_CATEGORIES or matched_words >= _MULTI_HOP_MIN_WORDS:
        return INTENT_MULTI_HOP
    return INTENT_SINGLE_HOP


def resolve_policy(
    question: str,
    doc_type: str = "general",
    task: str = "chat",
    compute_tier: str = COMPUTE_TIER_CPU,
    circuit_state: str = "closed",
) -> RetrievalPolicy:
    """纯规则零 LLM 零延迟地把一轮检索路由到参数包（spec §2）。

    - circuit_state="open"（熔断开路）→ rerank_mode=vector_only；该决策的执行由装配层
      熔断装饰器承载（工单 01，调用方零分支），本字段是同一决策的策略侧显式化。
    - multi_query 双门控：L0 multi_hop 且 compute_tier=gpu 才开（本机 cpu_constrained
      恒 False=现状零变化）。
    - doc_type 恒缺省 "general"（§11 裁决 9：主链路查询期检索前无 doc_type 可得），
      参数为 agent TASK_PROFILES 目标型别覆盖预留（工单 07）；task 同为预留位。
    - top_k/ef_search：门控 DYNAMIC_TOPK_ENABLED=False（缺省）时恒 20/40（=工单 02
      行为，multi_hop 也不例外）；True 且 intent=multi_hop 时产出联动档 30/64——门控
      常量是未来开闸的唯一翻转点（B 类门槛 §1）。char_budget 恒缺省：字符预算开闸同
      属 B 类，激活值等基线数据（§7）。
    """
    intent = detect_intent(question)
    # 联动档门控（工单 07）：缺省关闸时 multi_hop 也走 20/40；开闸且 multi_hop 才
    # 扩档。rerank_mode/multi_query 决策与联动档正交（降级路径不受门控影响）。
    top_k = DEFAULT_TOP_K
    ef_search = DEFAULT_EF_SEARCH
    if DYNAMIC_TOPK_ENABLED and intent == INTENT_MULTI_HOP:
        top_k = MULTI_HOP_TOP_K
        ef_search = MULTI_HOP_EF_SEARCH
    return RetrievalPolicy(
        top_k=top_k,
        ef_search=ef_search,
        rerank_mode=RERANK_VECTOR_ONLY if circuit_state == _CIRCUIT_OPEN else RERANK_FULL,
        multi_query=intent == INTENT_MULTI_HOP and compute_tier == COMPUTE_TIER_GPU,
        char_budget=None,
        intent=intent,
        degraded_hint=False,
    )


def merge_policy(base: RetrievalPolicy, override: RetrievalPolicy | None) -> RetrievalPolicy:
    """档案策略覆盖与 resolver 产出的合并（工单 07）：override 中非缺省的字段优先，
    其余沿用 base；override=None 原样返回 base（等价于无覆盖）。

    为什么按"非缺省字段"合并而非整包替换：熔断 open 态 resolver 产出 rerank_mode=
    vector_only，整包替换会被档案对象里的缺省 full 冲掉、破坏降级路径——按字段合并
    让档案只声明它真正想改的字段（如 top_k/ef_search），决策性字段（rerank_mode/
    degraded_hint）仍归 resolver/调用方。纯函数：不读全局状态，测试可全量枚举。
    """
    if override is None:
        return base
    defaults = RetrievalPolicy()
    picked = {
        field.name: getattr(override, field.name)
        for field in fields(RetrievalPolicy)
        if getattr(override, field.name) != getattr(defaults, field.name)
    }
    return replace(base, **picked)
