r"""分块纯函数测试：brief 参数化表 5 行全覆盖 + 边界用例，断言精确到逐字相等。

断言表来源 task-7-brief：标题层级（title_path=`" > ".join(heading_path)`）、
超长切分（每块正文 ≤800 且按句子边界切）、同级过短合并（≥300）、
content 前缀 `f"{title_path}｜"`、position 从 0 递增。补边界：空 blocks→空列表、
单块 <300 字原样保留、切分边界必须落在句号之后（残句即失败）、
尾块 <min_len 并入前块、无句号超长单句硬切兜底、空白正文块不产空块。
min_len/max_len 均按正文计，不含 `title_path｜` 前缀。
"""

import pytest

from app.rag.chunker import Block, OutChunk, ParsedDoc, split


def _sentences(count: int, per: int = 100) -> str:
    """构造 count 个句子：每句 per 字、以句号收尾、句内无其他终止标点。"""
    return "".join("句" * (per - 1) + "。" for _ in range(count))


def test_heading_hierarchy_single_chunk() -> None:
    """标题层级：`第3章>3.2` 下正文 300 字 → 1 块，title_path 为 ` > ` 连接路径。"""
    text = "正" * 300
    block = Block(level=2, heading_path=["第3章", "3.2"], text=text)
    doc = ParsedDoc(title="员工手册", blocks=[block])
    assert split(doc) == [
        OutChunk(content=f"第3章 > 3.2｜{text}", title_path="第3章 > 3.2", position=0)
    ]


def test_oversized_section_split_by_sentence() -> None:
    """超长切分：单节 1500 字（15 句×100）→ 2 块，每块正文 ≤800，同 title_path。"""
    text = _sentences(15)
    block = Block(level=2, heading_path=["第3章", "3.2"], text=text)
    chunks = split(ParsedDoc(title="员工手册", blocks=[block]))
    assert len(chunks) == 2
    bodies = [c.content.removeprefix("第3章 > 3.2｜") for c in chunks]
    assert [len(body) for body in bodies] == [800, 700]
    assert "".join(bodies) == text  # 按句重装后与原文逐字相等：内容零丢失
    assert bodies[0] == _sentences(8)  # 首块恰为前 8 句：切分边界在句号后
    assert all(body.endswith("。") for body in bodies)
    assert {c.title_path for c in chunks} == {"第3章 > 3.2"}
    assert [c.position for c in chunks] == [0, 1]


def test_short_sibling_sections_merged() -> None:
    """过短合并：同级两节各 150 字 → 合并 1 块（正文 ≥300），title_path 取首节。"""
    t1, t2 = "甲" * 150, "乙" * 150
    blocks = [
        Block(level=2, heading_path=["第3章", "3.1"], text=t1),
        Block(level=2, heading_path=["第3章", "3.2"], text=t2),
    ]
    chunks = split(ParsedDoc(title="员工手册", blocks=blocks))
    assert chunks == [
        OutChunk(content=f"第3章 > 3.1｜{t1}\n{t2}", title_path="第3章 > 3.1", position=0)
    ]
    assert len(chunks[0].content.removeprefix("第3章 > 3.1｜")) >= 300


@pytest.mark.parametrize(
    "blocks",
    [
        pytest.param(
            [Block(level=2, heading_path=["第3章", "3.2"], text="正文")],
            id="single-section",
        ),
        pytest.param(
            [
                Block(level=1, heading_path=["第1章"], text="甲" * 350),
                Block(level=1, heading_path=["第2章"], text="乙" * 350),
            ],
            id="multi-section",
        ),
        pytest.param(
            [
                Block(level=2, heading_path=["第3章", "3.1"], text="丙" * 150),
                Block(level=2, heading_path=["第3章", "3.2"], text="丁" * 150),
            ],
            id="merged-section",
        ),
    ],
)
def test_content_startswith_title_path_prefix(blocks: list[Block]) -> None:
    """块前缀：任意输入下每个 content 都以 `f"{title_path}｜"` 开头。"""
    for chunk in split(ParsedDoc(title="员工手册", blocks=blocks)):
        assert chunk.content.startswith(f"{chunk.title_path}｜")


def test_position_increments_from_zero() -> None:
    """顺序：多节分块 → position 从 0 连续递增且各节 title_path 独立。"""
    blocks = [Block(level=1, heading_path=[f"第{i}章"], text="正" * 350) for i in range(1, 4)]
    chunks = split(ParsedDoc(title="员工手册", blocks=blocks))
    assert [c.position for c in chunks] == [0, 1, 2]
    assert [c.title_path for c in chunks] == ["第1章", "第2章", "第3章"]


def test_empty_blocks_yield_empty_list() -> None:
    """边界：空 blocks → 空列表。"""
    assert split(ParsedDoc(title="空文档", blocks=[])) == []


def test_single_short_block_preserved_verbatim() -> None:
    """边界：单块 <300 字原样保留——不丢内容、无同级后继时不强行拼凑。"""
    text = "短" * 50
    block = Block(level=1, heading_path=["第1章"], text=text)
    assert split(ParsedDoc(title="员工手册", blocks=[block])) == [
        OutChunk(content=f"第1章｜{text}", title_path="第1章", position=0)
    ]


def test_tail_below_min_len_merged_into_previous() -> None:
    """边界：按句切后尾块 200 字 <min_len → 并入前块（1000 字 → 1 块，内容零丢失）。"""
    text = _sentences(10)
    block = Block(level=1, heading_path=["附录"], text=text)
    chunks = split(ParsedDoc(title="员工手册", blocks=[block]))
    assert chunks == [OutChunk(content=f"附录｜{text}", title_path="附录", position=0)]


def test_overlong_single_sentence_hard_split_fallback() -> None:
    """边界：无句号的超长单句按 max_len 硬切兜底，切后尾块 <min_len 并回，内容零丢失。"""
    text = "长" * 900 + "。"
    block = Block(level=1, heading_path=["免责声明"], text=text)
    chunks = split(ParsedDoc(title="员工手册", blocks=[block]))
    assert chunks == [OutChunk(content=f"免责声明｜{text}", title_path="免责声明", position=0)]


def test_trailing_text_without_ender_becomes_own_piece() -> None:
    """边界：超长节末尾无终止标点的余文单独成块（不被截进前句、不丢字符）。"""
    text = "句" * 100 + "。" + "尾" * 700
    block = Block(level=1, heading_path=["附则"], text=text)
    chunks = split(ParsedDoc(title="员工手册", blocks=[block]))
    bodies = [c.content.removeprefix("附则｜") for c in chunks]
    assert bodies == ["句" * 100 + "。", "尾" * 700]
    assert [c.position for c in chunks] == [0, 1]


def test_blank_text_block_dropped() -> None:
    """边界：纯空白正文块不产出只有前缀的空块。"""
    blocks = [
        Block(level=1, heading_path=["第1章"], text="   \n　 "),
        Block(level=1, heading_path=["第2章"], text="正" * 300),
    ]
    chunks = split(ParsedDoc(title="员工手册", blocks=blocks))
    assert chunks == [OutChunk(content=f"第2章｜{'正' * 300}", title_path="第2章", position=0)]
