r"""评测指标纯函数测试（metrics-balance §2 口径 + §8 裁决 4/10 + §6 unit seams）。

断言基线即 spec 冻结口径：recall——拒答类与 source_doc_id 为空的存量旧题记 null
（不计入集均分），命中 1 / 未命中 0（空 context 天然未命中）；citationsOk——无角标
=不可核对=机检必须红（裁决 4），任一角标越界即 false，拒答题记 null；
contextPrecision——排名加权 Σ rel_i/i ÷ Σ 1/i（i 从 1 起，全相关=1），空块无 context
可评记 null（工单 07 增补）。
"""

import pytest

from app.services.eval_metrics import (
    check_citations,
    evaluate_context_precision,
    evaluate_recall,
)

# ---------- evaluate_recall：命中/未命中/拒答 null/来源空 null/空 context ----------


def test_recall_context含来源文档得1() -> None:
    assert evaluate_recall([11, 22, 33], 22, is_reject=False) == 1


def test_recall_context不含来源文档得0() -> None:
    assert evaluate_recall([11, 22, 33], 99, is_reject=False) == 0


def test_recall_拒答类记null() -> None:
    # 入参取"否则会得 1"的形态，钉死 is_reject 优先于命中判定
    assert evaluate_recall([11], 11, is_reject=True) is None


def test_recall_source_doc_id为空的存量旧题记null() -> None:
    assert evaluate_recall([11, 22], None, is_reject=False) is None


def test_recall_空context未命中得0() -> None:
    assert evaluate_recall([], 11, is_reject=False) == 0


# ---------- check_citations：合法 true / 越界 false / 无角标 false / 拒答 null ----------


def test_citations_ok_全部角标落在context范围内记true() -> None:
    assert check_citations("年假为 5 天 [1]，报销周期 30 天 [2]。", 2, is_reject=False) is True


def test_citations_ok_角标越界记false() -> None:
    assert check_citations("年假为 5 天 [3]。", 2, is_reject=False) is False


def test_citations_ok_回答完全不含角标记false() -> None:
    # 裁决 4：无角标=不可核对，机检必须红
    assert check_citations("年假为 5 天。", 2, is_reject=False) is False


def test_citations_ok_拒答题记null() -> None:
    assert check_citations("知识库中未找到相关内容", 0, is_reject=True) is None


def test_citations_ok_多角标混合越界记false() -> None:
    assert check_citations("前半 [1] 合法，后半 [5] 越界。", 2, is_reject=False) is False


# ---------- evaluate_context_precision：排名加权公式（工单 07，§2）----------


def test_precision_全相关得1() -> None:
    # Σ rel_i/i = Σ 1/i，分子分母同式恒等 1（spec §2：全相关=1）
    assert evaluate_context_precision([True, True, True]) == pytest.approx(1.0)


def test_precision_手算全等() -> None:
    # [T, F, T]：(1/1 + 1/3) ÷ (1/1 + 1/2 + 1/3) = (4/3) ÷ (11/6) = 8/11
    assert evaluate_context_precision([True, False, True]) == pytest.approx(8 / 11)


def test_precision_首位不相关按名次惩罚() -> None:
    # [F, T]：(1/2) ÷ (1 + 1/2) = 1/3——相关块名次越靠后惩罚越重
    assert evaluate_context_precision([False, True]) == pytest.approx(1 / 3)


def test_precision_全不相关得0() -> None:
    assert evaluate_context_precision([False, False]) == pytest.approx(0.0)


def test_precision_空块无context可评记null() -> None:
    # qa 直答等无 chunk context 的题：0/0 无定义，记 null 由报告自然排除
    assert evaluate_context_precision([]) is None
