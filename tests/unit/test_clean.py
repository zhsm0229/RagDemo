r"""清洗纯函数测试：规则链逐条正/反参数化，输入输出逐字精确断言。

断言表来源 task-6-brief：页眉（重复 >=3 次且长度 <=30 的短行）删除、目录行
（`…\d+$` / `\.{3,}\s*\d+$` 两种形态）删除、独立页码行删除、水印标记行删除、
3+ 连续换行折叠为一个空行、首尾全角空白去除。反例均为不含该模式的文本，
经完整规则链后原样通过（逐字相等）。
task-6b 修复轮：repeated_short_lines 文档级聚合口径 + clean_text 可选
known_repeated_lines 参数（只强化计数型页眉规则，缺省行为逐字不变）。
"""

import pytest

from app.rag.clean import RAW_CHARS, clean_text, repeated_short_lines

HEADER = "客服_退货政策"
LONG_FOOTER = "长" * 31  # 31 字：超出页眉短行阈值（<=30），重复出现也不删


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 页眉/页脚：重复 >=3 次的短行整行删除，正文保留
        pytest.param(
            f"正文A\n{HEADER}\n正文B\n{HEADER}\n{HEADER}",
            "正文A\n正文B",
            id="header-pos-repeated-short-line-dropped",
        ),
        pytest.param(
            f"正文A\n{HEADER}\n正文B",
            f"正文A\n{HEADER}\n正文B",
            id="header-neg-single-occurrence-kept",
        ),
        pytest.param(
            f"{LONG_FOOTER}\n{LONG_FOOTER}\n{LONG_FOOTER}",
            f"{LONG_FOOTER}\n{LONG_FOOTER}\n{LONG_FOOTER}",
            id="header-neg-line-over-30-chars-kept",
        ),
        # 目录行：`\.{3,}\s*\d+$`（点导线）与 `…\d+$`（省略号）两种形态
        pytest.param(
            "第3章 报销 ......... 12\n正文",
            "正文",
            id="toc-pos-dotted-leader-dropped",
        ),
        pytest.param(
            "第二章 术语 …5\n正文",
            "正文",
            id="toc-pos-ellipsis-dropped",
        ),
        pytest.param(
            "第3章 报销 12 页\n正文",
            "第3章 报销 12 页\n正文",
            id="toc-neg-no-dot-leader-kept",
        ),
        # 独立页码行：整行只有数字
        pytest.param("内容\n12\n内容", "内容\n内容", id="pageno-pos-standalone-dropped"),
        pytest.param(
            "内容\n第 12 页\n内容",
            "内容\n第 12 页\n内容",
            id="pageno-neg-inline-number-kept",
        ),
        # 水印：整行精确命中标记表
        pytest.param("内部资料\n正文", "正文", id="watermark-pos-marker-dropped"),
        pytest.param(
            "内部资料汇总\n正文",
            "内部资料汇总\n正文",
            id="watermark-neg-substring-not-marker-kept",
        ),
        # 空行折叠：3+ 连续换行折叠为一个空行（两个换行）
        pytest.param(
            "a\n\n\n\nb",
            "a\n\nb",
            id="blank-pos-run-of-3-collapsed",
        ),
        pytest.param("a\n\nb", "a\n\nb", id="blank-neg-single-empty-line-kept"),
        # 首尾全角空白
        pytest.param("　正文　", "正文", id="strip-pos-fullwidth-space-stripped"),
        pytest.param("正 文", "正 文", id="strip-neg-internal-space-kept"),
    ],
)
def test_clean_text_rule_chain(raw: str, expected: str) -> None:
    assert clean_text(raw) == expected


def test_clean_text_full_chain_composition() -> None:
    """多种脏数据并存的原文，经整条规则链后只剩正文两行。"""
    raw = (
        "　\n\n\n\n内部资料\n\n\n\n正文A\n客服_退货政策\n目录 ......... 3\n12\n"
        "正文B\n客服_退货政策\n客服_退货政策\n\n\n\n"
    )
    assert clean_text(raw) == "正文A\n正文B"


def test_raw_chars_counts_raw_input_length() -> None:
    """RAW_CHARS 对原始输入计字符数（清洗前），供红线校验复用。"""
    assert RAW_CHARS("正文A\n正文B") == 7
    assert RAW_CHARS("a\n\n\n\nb") == 6


def test_repeated_short_lines_document_level_scope() -> None:
    """聚合口径（供管线跨块复用，与 _drop_repeated_short_lines 同判）：≥3 次且 ≤30 字
    的非空行入选；仅 1 次、>30 字、空行一律不入集合。"""
    text = "\n".join(["页眉", "页眉", "页眉", "独一行", "长" * 31, "长" * 31, "长" * 31, ""])
    assert repeated_short_lines(text) == frozenset({"页眉"})


def test_clean_text_known_repeated_lines_drop_below_local_threshold() -> None:
    """known_repeated_lines 只强化计数型页眉规则：集合内行块内仅 1 次也删；其余规则不受影响。"""
    raw = "正文A\n客服_退货政策\n目录 ......... 3\n12\n正文B"
    known = frozenset({"客服_退货政策"})
    assert clean_text(raw, known_repeated_lines=known) == "正文A\n正文B"  # 页眉+目录+页码全清
    assert clean_text("正文A\n正文B", known_repeated_lines=known) == "正文A\n正文B"  # 集合外不动
    assert clean_text(raw) == "正文A\n客服_退货政策\n正文B"  # 缺省不传：块内单次行为不变


def test_clean_text_known_repeated_lines_empty_default_unchanged() -> None:
    """缺省空集合与逐字等价的原规则链行为一致（既有单测兼容性锁定）。"""
    raw = "正文A\n客服_退货政策\n正文B\n客服_退货政策\n客服_退货政策"
    assert clean_text(raw) == clean_text(raw, known_repeated_lines=frozenset()) == "正文A\n正文B"
