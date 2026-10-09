r"""RetrievalPolicy 策略骨架单测（retrieval-policy §2/§8 冻结清单，工单 02/07）。

六段覆盖：① 七字段缺省逐字等于现状（等价证明断言基础，spec §2 冻结表）；
② L0 词表边界（恰 2 类判 / 恰 1 类不判 / 同类累计恰 3 处判 / 2 处不判；词表常量
逐字冻结可评审）；③ 熔断 open 仅覆写 rerank_mode 其余维持缺省（half_open 非 open
不覆写）；④ multi_query 双门控（L0 multi_hop 且 compute_tier=gpu 才 True，
cpu_constrained 恒 False=现状）；⑤ top_k/ef_search 联动档门控（工单 07：门控缺省
False=行为不变，True 且 multi_hop→30/64，单跳/熔断正交）；⑥ merge_policy 档案
覆盖合并（覆盖非缺省字段优先，None=无覆盖，熔断决策不被档案缺省值冲掉）。模块
结构断言看护纯函数域红线（禁 import services/db，doc_profile 结构断言先例）。
全 Stub 零网络零 LLM。
"""

from dataclasses import fields
from pathlib import Path

import pytest

from app.rag import retrieval_policy
from app.rag.retrieval_policy import (
    L0_INTENT_LEXICON,
    RetrievalPolicy,
    detect_intent,
    merge_policy,
    resolve_policy,
)

# L0 边界样例（逐词核对过词表：样例不得意外命中计划外的词，否则边界断言失真）
_Q_TWO_CATEGORIES = "年假病假差异以及如何申请"  # compare(差异) + multi_entity(以及) = 恰 2 类
_Q_ONE_CATEGORY = "年假和病假有什么区别"  # 仅 compare(区别)；"和"不在词表（裁决 11）
_Q_THREE_SAME_CATEGORY = "为什么试用期会导致社保断缴，又如何影响公积金"  # causal 恰 3 词
_Q_TWO_SAME_CATEGORY = "为什么调薪会导致奖金重算"  # causal 恰 2 词
_Q_NO_SIGNAL = "今天天气怎么样"  # 零命中


# ---------------------------------------------------------------------------
# ① 七字段缺省 = 现状（spec §2 冻结表逐字）
# ---------------------------------------------------------------------------


def test_retrieval_policy_七字段缺省逐字等于现状() -> None:
    policy = RetrievalPolicy()
    assert [field.name for field in fields(RetrievalPolicy)] == [
        "top_k",
        "ef_search",
        "rerank_mode",
        "multi_query",
        "char_budget",
        "intent",
        "degraded_hint",
    ]
    assert policy.top_k == 20
    assert policy.ef_search == 40
    assert policy.rerank_mode == "full"
    assert policy.multi_query is False
    assert policy.char_budget is None
    assert policy.intent == "single_hop"
    assert policy.degraded_hint is False


def test_resolve_policy_缺省输入产出与现状参数逐字相等() -> None:
    """缺省输入（单跳问题 + general/chat/cpu_constrained/closed）产出与
    RetrievalPolicy() 全字段相等——等价证明断言基础（spec §2，接线零变化）。"""
    assert resolve_policy(_Q_NO_SIGNAL) == RetrievalPolicy()


# ---------------------------------------------------------------------------
# ② L0 词表边界：恰 2 类判 / 恰 1 类不判 / 同类累计恰 3 处判 / 2 处不判
# ---------------------------------------------------------------------------


def test_l0_恰两类各一词判multi_hop() -> None:
    assert detect_intent(_Q_TWO_CATEGORIES) == "multi_hop"


def test_l0_恰一类一词不判multi_hop() -> None:
    assert detect_intent(_Q_ONE_CATEGORY) == "single_hop"


def test_l0_同类累计恰三词判multi_hop() -> None:
    assert detect_intent(_Q_THREE_SAME_CATEGORY) == "multi_hop"


def test_l0_同类累计恰两词不判multi_hop() -> None:
    assert detect_intent(_Q_TWO_SAME_CATEGORY) == "single_hop"


def test_l0_零命中判single_hop() -> None:
    assert detect_intent(_Q_NO_SIGNAL) == "single_hop"


def test_l0词表为模块常量且逐字冻结可评审() -> None:
    assert L0_INTENT_LEXICON == {
        "compare": ("相比", "对比", "区别", "哪个更", "差异"),
        "multi_entity": ("以及", "并且", "分别", "各", "还"),
        "causal": ("为什么", "如何影响", "会导致"),
    }


def test_l0词表不含新增单字歧义词() -> None:
    """spec §2/裁决 11：词表避免单字歧义词（"和""与"不入表，防"海南"式误报）；
    "各""还"为 spec 原文既列的冻结例外——出现第三个单字词即红。"""
    words = [word for group in L0_INTENT_LEXICON.values() for word in group]
    assert "和" not in words
    assert "与" not in words
    assert [word for word in words if len(word) == 1] == ["各", "还"]


# ---------------------------------------------------------------------------
# ③ 熔断态：open 仅覆写 rerank_mode，其余字段维持缺省
# ---------------------------------------------------------------------------


def test_resolve_policy_熔断open仅覆写rerank_mode其余维持缺省() -> None:
    policy = resolve_policy(_Q_NO_SIGNAL, circuit_state="open")
    assert policy.rerank_mode == "vector_only"
    assert policy.top_k == 20
    assert policy.ef_search == 40
    assert policy.multi_query is False
    assert policy.char_budget is None
    assert policy.intent == "single_hop"
    assert policy.degraded_hint is False


def test_resolve_policy_熔断half_open不覆写rerank_mode() -> None:
    """仅字面 "open" 映射 vector_only：half_open（探测期）维持 full，由熔断装饰器
    决定该请求探测成败（工单 01 调用方零分支）。"""
    assert resolve_policy(_Q_NO_SIGNAL, circuit_state="half_open").rerank_mode == "full"


# ---------------------------------------------------------------------------
# ④ multi_query 双门控：L0 multi_hop 且 compute_tier=gpu 才开
# ---------------------------------------------------------------------------


def test_resolve_policy_gpu加multi_hop才开multi_query() -> None:
    policy = resolve_policy(_Q_TWO_CATEGORIES, compute_tier="gpu")
    assert policy.multi_query is True
    assert policy.intent == "multi_hop"


def test_resolve_policy_gpu但单跳不开multi_query() -> None:
    policy = resolve_policy(_Q_ONE_CATEGORY, compute_tier="gpu")
    assert policy.multi_query is False
    assert policy.intent == "single_hop"


def test_resolve_policy_cpu_constrained_multi_hop也不开multi_query() -> None:
    """缺省算力档恒 False（=现状零变化）：multi_query 激活属 A 类资源门槛（spec §1）。"""
    policy = resolve_policy(_Q_TWO_CATEGORIES)
    assert policy.multi_query is False
    assert policy.intent == "multi_hop"  # 意图照判，仅重手段被算力档压住


# ---------------------------------------------------------------------------
# ⑤ top_k/ef_search 联动档门控（工单 07）：门控缺省 False=行为不变；
#    True 且 intent=multi_hop → 30/64；单跳与熔断决策正交
# ---------------------------------------------------------------------------


def test_联动档常量逐字冻结且门控缺省关闭_激活门槛注释在场() -> None:
    """工单 07：联动档 30/64 逐字冻结；门控缺省 False（现状锁定）。常量无法挂
    docstring，激活=B 类门槛（§17.3 四指标对比）、"当前缺省不启用"与"唯一翻转点"
    说明以源文注释在场（结构断言）。"""
    assert retrieval_policy.MULTI_HOP_TOP_K == 30
    assert retrieval_policy.MULTI_HOP_EF_SEARCH == 64
    assert retrieval_policy.DYNAMIC_TOPK_ENABLED is False
    source = Path(retrieval_policy.__file__).read_text(encoding="utf-8")
    assert "B 类门槛" in source
    assert "当前缺省不启用" in source
    assert "唯一翻转点" in source


def test_resolve_policy_门控开启且multi_hop产出联动档30_64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_policy, "DYNAMIC_TOPK_ENABLED", True)
    policy = resolve_policy(_Q_TWO_CATEGORIES)
    assert policy.top_k == 30
    assert policy.ef_search == 64
    assert policy.intent == "multi_hop"
    assert policy.rerank_mode == "full"  # 联动档只扩召回档位，不动熔断决策
    assert policy.multi_query is False  # cpu_constrained 档 multi_query 仍关（A 类门独立）


def test_resolve_policy_门控开启单跳仍产出缺省20_40逐字(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_policy, "DYNAMIC_TOPK_ENABLED", True)
    policy = resolve_policy(_Q_NO_SIGNAL)
    assert policy.top_k == 20
    assert policy.ef_search == 40
    assert policy == RetrievalPolicy()  # 逐字：与缺省参数包全字段相等


def test_resolve_policy_门控关闭multi_hop仍产出缺省20_40(monkeypatch: pytest.MonkeyPatch) -> None:
    """现行为锁定：门控 False 时 multi_hop 的召回档也走 20/40——与工单 02 逐字等价
    （意图照判 multi_hop，仅联动档不生效）。"""
    monkeypatch.setattr(retrieval_policy, "DYNAMIC_TOPK_ENABLED", False)
    policy = resolve_policy(_Q_TWO_CATEGORIES)
    assert policy.top_k == 20
    assert policy.ef_search == 40
    assert policy == RetrievalPolicy(intent="multi_hop")  # 其余六字段逐字=缺省


def test_resolve_policy_门控开启联动档与熔断降级正交(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_policy, "DYNAMIC_TOPK_ENABLED", True)
    policy = resolve_policy(_Q_TWO_CATEGORIES, circuit_state="open")
    assert policy.top_k == 30
    assert policy.ef_search == 64
    assert policy.rerank_mode == "vector_only"  # 熔断决策独立于联动档


# ---------------------------------------------------------------------------
# ⑥ merge_policy（工单 07）：档案覆盖优先于 resolver 产出，按"非缺省字段"合并
# ---------------------------------------------------------------------------


def test_merge_policy_覆盖非缺省字段优先_其余沿用base() -> None:
    """档案想改的字段（非缺省值）优先于 resolver 产出；熔断决策不被档案缺省值冲掉
    （open→vector_only 必须存活，降级路径红线）。"""
    base = resolve_policy(_Q_NO_SIGNAL, circuit_state="open")
    merged = merge_policy(base, RetrievalPolicy(top_k=30, ef_search=64))
    assert merged.top_k == 30
    assert merged.ef_search == 64
    assert merged.rerank_mode == "vector_only"
    assert merged.multi_query is False
    assert merged.char_budget is None
    assert merged.intent == "single_hop"
    assert merged.degraded_hint is False


def test_merge_policy_覆盖为None原样返回base() -> None:
    """本期两档案 profile_policy=None 的等价语义：无覆盖=resolver 产出原样。"""
    base = resolve_policy(_Q_TWO_CATEGORIES, compute_tier="gpu")
    assert merge_policy(base, None) == base


def test_merge_policy_全缺省覆盖不改变base() -> None:
    """缺省字段不算覆盖意图：RetrievalPolicy() 整包传入不冲掉 base 的非缺省值。"""
    base = resolve_policy(_Q_NO_SIGNAL, circuit_state="open")
    assert merge_policy(base, RetrievalPolicy()) == base


def test_merge_policy_仅改单字段其余非缺省值沿用base() -> None:
    base = RetrievalPolicy(top_k=30, ef_search=64, char_budget=8000, degraded_hint=True)
    merged = merge_policy(base, RetrievalPolicy(top_k=25))
    assert merged.top_k == 25
    assert merged.ef_search == 64
    assert merged.char_budget == 8000
    assert merged.degraded_hint is True


# ---------------------------------------------------------------------------
# 模块结构断言（纯函数域红线，test_topn_by_profile 先例）
# ---------------------------------------------------------------------------


def test_retrieval_policy_纯函数域禁止import_services_结构断言() -> None:
    """retrieval_policy 是纯函数域（spec §2 模块头红线）：源码不得出现 services/db
    依赖字样。"""
    source = Path(retrieval_policy.__file__).read_text(encoding="utf-8")
    assert "app.services" not in source
    assert "app.db" not in source
