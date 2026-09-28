r"""文档清洗纯函数：RULES 规则链顺序执行，输入 str、输出 str、无副作用。

规则表（task-6-brief）：页眉页脚重复短行（出现 >=3 次且长度 <=30）删除、
目录行（`…\d+$` / `\.{3,}\s*\d+$`）删除、独立页码行删除、水印标记行删除、
3+ 连续换行折叠为一个空行、去首尾空白（str.strip 覆盖全角 U+3000）。
RAW_CHARS 按原始输入计字符数，供红线校验复用（签名逐字采用 brief 的 lambda）。
"""

import re
from collections import Counter
from collections.abc import Callable

_TOC_LINE_RE = re.compile(r"…\d+$|\.{3,}\s*\d+$")
_PAGE_NUMBER_LINE_RE = re.compile(r"\d+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

WATERMARK_MARKS: frozenset[str] = frozenset(
    {"内部资料", "内部文件", "仅供内部使用", "禁止外传", "未经授权禁止复制"}
)


def _drop_repeated_short_lines(text: str) -> str:
    """去页眉页脚：非空行出现 >=3 次且长度 <=30 视为页眉/页脚，所有出现处整行删除。"""
    lines = text.split("\n")
    counts = Counter(lines)
    repeated = {line for line, n in counts.items() if n >= 3 and 0 < len(line) <= 30}
    return "\n".join(line for line in lines if line not in repeated)


def _drop_toc_lines(text: str) -> str:
    """去目录行：行尾为 `…数字` 或 `三个以上点+可选空白+数字` 视为目录条目，整行删除。"""
    return "\n".join(line for line in text.split("\n") if _TOC_LINE_RE.search(line) is None)


def _drop_page_number_lines(text: str) -> str:
    """去独立页码行：整行（忽略首尾空白后）只有数字视为页码，整行删除。"""
    return "\n".join(
        line for line in text.split("\n") if _PAGE_NUMBER_LINE_RE.fullmatch(line.strip()) is None
    )


def _drop_watermark_lines(text: str) -> str:
    """去水印行：整行（去首尾空白后）精确命中水印标记表才删除（子串不算，保正文）。"""
    return "\n".join(line for line in text.split("\n") if line.strip() not in WATERMARK_MARKS)


def _collapse_blank_lines(text: str) -> str:
    """折叠空行：3 个及以上连续换行折叠为一个空行（两个换行）。"""
    return _BLANK_RUN_RE.sub("\n\n", text)


def _strip_edges(text: str) -> str:
    """去首尾空白：str.strip 覆盖全角空白（U+3000 等 Unicode 空白），内部空白保留。"""
    return text.strip()


RULES: list[Callable[[str], str]] = [
    _drop_repeated_short_lines,
    _drop_toc_lines,
    _drop_page_number_lines,
    _drop_watermark_lines,
    _collapse_blank_lines,
    _strip_edges,
]


def clean_text(raw: str) -> str:
    """按 RULES 顺序执行规则链，返回清洗后的文本。"""
    for rule in RULES:
        raw = rule(raw)
    return raw


# 红线复用：按原始输入（清洗前）计字符数；lambda 形态逐字采用 brief 签名
RAW_CHARS: Callable[[str], int] = lambda raw: len(raw)
