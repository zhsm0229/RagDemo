r"""agent 任务档案单测（工单 07）：TASK_PROFILES 携带策略覆盖的结构扩展与行为不变等价。

档案扩展 profile_policy（RetrievalPolicy | None，工单 07）：非 None 时按 rag 域纯函数
merge_policy 覆盖 resolver 产出（覆盖优先，见 retrieval_policy.merge_policy docstring
的"非缺省字段"合并语义）；本期两档案均 None=行为不变等价。消费点在 agent_tools
（工单 03 接线），本票只做档案结构与合并消费形状。白名单即评审点：键集/提示/工具集
逐字冻结断言，新增任务或放开工具须评审。全 Stub 零网络零 LLM 零 DB。
"""

import dataclasses
from typing import get_args

import pytest

from app.rag.retrieval_policy import RetrievalPolicy, merge_policy, resolve_policy
from app.services.agent_tasks import TASK_PROFILES, TaskProfile, TaskType, get_profile

# multi_hop 边界样例（恰 2 类：compare"差异"+multi_entity"以及"，与
# test_retrieval_policy.py 同源口径；本文件内联以防跨文件耦合样例常量）
_Q_MULTI_HOP = "年假病假差异以及如何申请"


# ---------------------------------------------------------------------------
# 档案结构（行为不变等价证明：扩展字段缺省 None，既有键集/提示/工具集逐字不变）
# ---------------------------------------------------------------------------


def test_task_profiles_键集提示与工具白名单逐字冻结() -> None:
    assert list(TASK_PROFILES) == ["kb_qa", "template_draft"]
    assert TASK_PROFILES["kb_qa"].system_prompt == "你是知识库助手"
    assert TASK_PROFILES["kb_qa"].tool_names == ("knowledge_base_search",)
    assert TASK_PROFILES["template_draft"].tool_names == (
        "knowledge_base_search",
        "save_draft",
    )


def test_task_type白名单从档案动态派生_与键集同源() -> None:
    assert list(get_args(TaskType)) == ["kb_qa", "template_draft"]


def test_task_profiles_两档案策略覆盖均为None_行为不变等价() -> None:
    """工单 07 等价证明：本期两档案 profile_policy 均 None——merge_policy(base, None)
    原样返回 resolver 产出，agent 检索参数与工单 02 逐字一致。"""
    assert TASK_PROFILES["kb_qa"].profile_policy is None
    assert TASK_PROFILES["template_draft"].profile_policy is None


def test_task_profile_可携带策略覆盖构造且构造后不可变() -> None:
    profile = TaskProfile(
        system_prompt="p",
        tool_names=("knowledge_base_search",),
        profile_policy=RetrievalPolicy(top_k=30, ef_search=64),
    )
    assert profile.profile_policy == RetrievalPolicy(top_k=30, ef_search=64)
    with pytest.raises(dataclasses.FrozenInstanceError):  # frozen：档案构造后不可篡改
        profile.profile_policy = None  # type: ignore[misc]


def test_get_profile_未知类型KeyError_fail_fast() -> None:
    with pytest.raises(KeyError):
        get_profile("unknown_task")


# ---------------------------------------------------------------------------
# 消费形状预演（工单 03 接线照此）：resolver 产出为 base、档案 profile_policy 为 override
# ---------------------------------------------------------------------------


def test_档案覆盖经merge_policy接线_覆盖优先于resolver产出() -> None:
    base = resolve_policy(_Q_MULTI_HOP)  # multi_hop 信号；门控缺省关 → 缺省 20/40
    merged = merge_policy(base, RetrievalPolicy(top_k=30))
    assert merged.top_k == 30  # 档案覆盖优先
    assert merged.ef_search == 40  # 未覆盖字段沿用 resolver 产出
    assert merged.rerank_mode == "full"
    assert merged.intent == "multi_hop"


def test_档案覆盖为None时与无覆盖等价() -> None:
    base = resolve_policy(_Q_MULTI_HOP)
    assert merge_policy(base, None) == base  # 本期两档案=None 的等价语义
