r"""难例归一纯函数单测（01 票）：normalize_question 是采集去重与合成去重的唯一口径。

全角→半角（NFKC）+ 连续空白折叠 + 首尾修剪；中文原样保留（不做音译/分词）。
"""

from app.services.hardcase_mine import normalize_question


def test_全角转半角与空白折叠() -> None:
    """全角字母数字/空格折算半角，连续空白（含全角空格）折叠为单空格。"""
    assert normalize_question("ＢＧＥ－Ｍ３　模型的　上下文　长度") == "BGE-M3 模型的 上下文 长度"


def test_首尾修剪与内部空白规整() -> None:
    assert normalize_question("  报销标准   是什么？\t") == "报销标准 是什么?"


def test_中文原样保留与全角标点折叠() -> None:
    """NFKC 保留中文汉字原样；全角标点（？）折算半角（?）——归一只为去重，
    同义形态折叠是预期行为，不改写用户可见文本（快照列存原文）。"""
    assert normalize_question("蓝鲸值班的夜班补贴是多少？") == "蓝鲸值班的夜班补贴是多少?"
