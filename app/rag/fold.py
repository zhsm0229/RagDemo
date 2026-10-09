r"""折叠去重纯函数域（retrieval-policy §6/§11 裁决 10）：fold_top_n 主/agent 共用一把尺。

分层红线：本模块禁止 import services/db（纯函数域，doc_profile/retrieval_policy 同款
约束，结构断言看护）。F-1003 折叠算法自 services/retrieval 抽出为 rag 域纯函数：主
链路 rerank_and_fold（rerank 序与降级向量序兜底）与 agent 检索工具（纯向量序）共用
同一实现——"不另造"落在 fold 纯函数上（spec §6 冻结裁决），两侧口径漂移由单测对照
用例钉死（tests/unit/test_agent_tools.py）。

算法（F-1003，与抽出前逐字等价）：入参已按相关性分数降序（序与分数口径由调用方保证：
主链路 rerank sigmoid 分 / 降级兜底与 agent 召回余弦），先截前 top_n，再从高到低遍历，
与任一已保留项的 embedding 余弦相似度 ≥ dedup_sim 则丢弃当前项（留高分弃低分）。
本函数只做选择、不改写分数（standards §4.4：分数口径归调用方，两套分数绝不互比）。
"""

from collections.abc import Sequence
from typing import Protocol, TypeVar


class FoldableHit(Protocol):
    """折叠输入的最小结构接口：携带 L2 归一化 embedding 向量的命中即满足（点积即余弦）。

    retrieval.DocHit 结构化满足本协议——rag 域不得反向 import services（模块头红线），
    故以 Protocol 表达依赖而非导入具体类型（doc_profile.ProfiledHit 同款先例）。
    """

    embedding: list[float]


_FoldableT = TypeVar("_FoldableT", bound=FoldableHit)


def fold_top_n(
    hits: Sequence[_FoldableT],
    top_n: int,
    dedup_sim: float,
    *,
    return_folded: bool = False,
) -> list[_FoldableT] | tuple[list[_FoldableT], list[_FoldableT]]:
    """top_n 截断 + 近重复折叠（F-1003）：返回保留列表（入参序=分数降序），分数原样。

    return_folded=True（管理端检索测试 RET-03/04 缝，rerank_and_fold 透传）：返回
    (保留列表, 折叠丢弃列表)；缺省只返回保留列表（agent 工具形态）。
    """
    kept: list[_FoldableT] = []
    folded_out: list[_FoldableT] = []
    for hit in hits[:top_n]:
        if any(_cosine(hit.embedding, kept_hit.embedding) >= dedup_sim for kept_hit in kept):
            folded_out.append(hit)  # 与高分已保留项近重复：折叠丢弃（留高分弃低分）
            continue
        kept.append(hit)
    if return_folded:
        return kept, folded_out
    return kept


def _cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度：两向量均已 L2 归一化（入库侧保证），点积即余弦。"""
    return sum(a * b for a, b in zip(left, right, strict=True))
