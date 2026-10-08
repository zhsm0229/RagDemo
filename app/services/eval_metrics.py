r"""评测指标纯函数域（metrics-balance 工单 06/07，spec §2 口径 + §8 裁决 4/10）：Context
Recall 判定、引用覆盖率（citationsOk）检查与 Context Precision 排名加权。

分工边界：本模块只做**纯函数判定，无 IO 无 DB**。chunk→document_id 的解析
（contextChunkIds 回查 chunks 表取 document_id/正文）与 judge LLM 调用是工单 07
引擎接线的事——evaluate_recall 只收**已解析的 document_id 序列**；落库归 detail.metrics。

口径冻结：拒答类题与 source_doc_id 为空的存量旧题 recall 记 null（不计入集均分，
spec §2）；citationsOk 无角标 = false——无角标即不可核对，机检必须红（裁决 4），
拒答题记 null（拒答话术红线不动，拒答分支不产生 citations）；contextPrecision 空
块（qa 直答无 chunk context）0/0 无定义记 null。
"""

import re
from collections.abc import Sequence

# 角标唯一形态：半角 [n]（中文语境下全角［］/【】不产、不识别——docstring 声明的口径）
_CITATION_RE = re.compile(r"\[\d+\]")


def evaluate_recall(
    context_chunk_doc_ids: Sequence[int], source_doc_id: int | None, *, is_reject: bool
) -> int | None:
    """Context Recall 单题判定（spec §2，文档级口径 v1）：期望出处文档是否进入 context。

    is_reject=True → None（拒答类旁路，优先于一切）；source_doc_id 为 None
    （拒答类/存量旧题未标来源）→ None；出处文档 id 出现在 context 各 chunk 的
    document_id 中 → 1，否则 0（含 context 为空的空转情形）。
    """
    if is_reject or source_doc_id is None:
        return None
    return 1 if source_doc_id in context_chunk_doc_ids else 0


def evaluate_context_precision(relevant: Sequence[bool]) -> float | None:
    """Context Precision 排名加权（spec §2）：`Σ rel_i/i ÷ Σ 1/i`（i 从 1 起）。

    逐块布尔来自 judge 三判的 context_relevant（与 contextChunkIds 同序，名次即
    rerank 序）；全相关 = 1，相关块名次越靠后惩罚越重；空块（qa 直答无 chunk
    context）0/0 无定义 → None，由报告按 null 排除口径处理。
    """
    if not relevant:
        return None
    weighted = sum(1 / i for i, flag in enumerate(relevant, 1) if flag)
    normalizer = sum(1 / i for i in range(1, len(relevant) + 1))
    return weighted / normalizer


def check_citations(answer: str, context_size: int, *, is_reject: bool) -> bool | None:
    """引用覆盖率机检（W6 硬指标 #2）：回答中每个 [n] 可解析且 1 ≤ n ≤ context_size。

    全部合法 → True；完全无角标 → False（裁决 4，不可核对必须红）；任一角标越界
    → False；拒答题 → None。context_size=0 时任何角标均越界，自然落 False。
    """
    if is_reject:
        return None
    marks = _CITATION_RE.findall(answer)
    if not marks:
        return False
    return all(1 <= int(mark[1:-1]) <= context_size for mark in marks)
