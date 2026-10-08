r"""条款边界切分测试（metrics-balance §3.2 policy_clause 档）：整条成块、超长句切、
碎条并入、尾块归并、节归属，断言精确到逐字相等（条款 content 完整性是红线）。

细节裁决口径：条款块 title_path 取该条所在节 heading_path；跨节条款按条款起始位置
归属（并接时同块相邻子串直连保逐字、跨块补换行，与聚合路径组内换行同口径）；
碎条（<merge_below=120）并入下条、并入后允许超 max_clause_len=600（同"min 优先于
max"先例）；句切尾片过碎同样归并前片；孤立碎条无并入对象时原样保留。
注意：段边界取"下一处条号起点"，段文本携带原文中的换行等分隔符（零丢字口径）。
"""

import pytest

from app.rag.chunker import Block, OutChunk, ParsedDoc, split_by_profile, split_clauses
from app.rag.doc_profile import PROFILE_PARAMS, ClauseParams, DocProfile

PARAMS = PROFILE_PARAMS[DocProfile.POLICY_CLAUSE]
assert isinstance(PARAMS, ClauseParams)  # 钉死常量形态：防档位表漂移让本文件断言失义


def _clause(no: int, sentences: int, per: int = 36) -> str:
    """单条制度条款：条号开头、句号收尾；len = 4 + sentences × per（4 句 = 148 字 ≥120）。"""
    return f"第{no}条 " + "".join("款" * (per - 1) + "。" for _ in range(sentences))


def _doc(text: str, path: tuple[str, ...] = ("总则",), page: int | None = None) -> ParsedDoc:
    """单节条款文档：整节正文置于一个块（docx/解析器的真实粒度）。"""
    return ParsedDoc(
        title="考勤制度", blocks=[Block(level=1, heading_path=list(path), text=text, page=page)]
    )


def _bodies(chunks: list[OutChunk], path: str = "总则") -> list[str]:
    return [chunk.content.removeprefix(f"{path}｜") for chunk in chunks]


# ---------- 整条成块（工单目标：分块从句切截断变为整条成块） ----------


def test_整条成块_完整条款不被拦腰截断() -> None:
    c1, c2, c3 = (_clause(i, 4) for i in (1, 2, 3))  # 各 148 字 ≥120：各自成块
    chunks = split_clauses(_doc(c1 + c2 + c3), PARAMS)
    assert [chunk.content for chunk in chunks] == [
        f"总则｜{c1}",
        f"总则｜{c2}",
        f"总则｜{c3}",
    ]
    assert [(chunk.title_path, chunk.position) for chunk in chunks] == [
        ("总则", 0),
        ("总则", 1),
        ("总则", 2),
    ]


def test_单条超六百字按句切且零丢字() -> None:
    text = _clause(1, 20)  # 724 字 >600：16 句/包在句号后封包
    chunks = split_clauses(_doc(text, path=("罚则",)), PARAMS)
    bodies = _bodies(chunks, "罚则")
    assert [len(body) for body in bodies] == [
        580,
        144,
    ]  # 首句含条号前缀（40 字）：16 句/包句号后封包
    assert all(len(body) <= PARAMS.max_clause_len for body in bodies)
    assert bodies[0].endswith("。")  # 切分边界落在句号后
    assert "".join(bodies) == text  # 逐字相等：内容零丢失
    assert [chunk.position for chunk in chunks] == [0, 1]


def test_句切尾片过碎归并前片且允许超max() -> None:
    text = _clause(1, 18)  # 652 字 → 句切 [576, 36]，尾片 36 <120 归并前片
    chunks = split_clauses(_doc(text), PARAMS)
    assert len(text) == 652 > PARAMS.max_clause_len
    assert chunks == [
        OutChunk(content=f"总则｜{text}", title_path="总则", position=0)
    ]  # 归并后超 max


# ---------- 碎条并入（<merge_below=120） ----------


def test_碎条并入下条_并入后允许超max且逐字无损() -> None:
    c1, c2 = _clause(1, 2, 48), _clause(2, 15)  # 100 字 + 544 字
    chunks = split_clauses(_doc(c1 + c2), PARAMS)
    assert chunks == [OutChunk(content=f"总则｜{c1}{c2}", title_path="总则", position=0)]
    assert len(c1 + c2) == 644 > PARAMS.max_clause_len  # 不二次句切：并入后允许超 max


def test_连续碎条链式并入直至达标() -> None:
    c1, c2, c3 = _clause(1, 2, 38), _clause(2, 2, 38), _clause(3, 8, 37)  # 80+80+300
    chunks = split_clauses(_doc(c1 + c2 + c3), PARAMS)
    assert _bodies(chunks) == [c1 + c2, c3]  # 前两条合并 160 ≥120 即封块，不吞第三条
    assert [chunk.position for chunk in chunks] == [0, 1]


def test_尾块碎条并入前块() -> None:
    c1, c2 = _clause(1, 15), _clause(2, 2, 48)  # 544 + 100：尾条无下条可并 → 归并前块
    chunks = split_clauses(_doc(c1 + c2), PARAMS)
    assert chunks == [OutChunk(content=f"总则｜{c1}{c2}", title_path="总则", position=0)]


def test_孤立碎条无并入对象时原样保留() -> None:
    c1 = _clause(1, 2, 38)  # 80 字：全文唯一段，不强行拼凑
    assert split_clauses(_doc(c1), PARAMS) == [
        OutChunk(content=f"总则｜{c1}", title_path="总则", position=0)
    ]


def test_条款前导文并入首个条款且逐字无损() -> None:
    pre = "为了规范考勤管理，特制定本制度。"  # 首个条号前的导语
    c1, c2 = _clause(1, 4), _clause(2, 4)
    chunks = split_clauses(_doc(pre + c1 + c2), PARAMS)
    assert _bodies(chunks) == [pre + c1, c2]  # 同块相邻子串直连：逐字无损


# ---------- 节归属（架构师裁决） ----------


def test_条款块title_path取所在节heading_path() -> None:
    c1, c2 = _clause(1, 4), _clause(2, 4)
    doc = ParsedDoc(
        title="考勤制度",
        blocks=[
            Block(level=2, heading_path=["第一章", "第一节"], text=c1),
            Block(level=2, heading_path=["第二章", "第二节"], text=c2),
        ],
    )
    chunks = split_clauses(doc, PARAMS)
    assert [chunk.title_path for chunk in chunks] == ["第一章 > 第一节", "第二章 > 第二节"]
    assert [chunk.content for chunk in chunks] == [
        f"第一章 > 第一节｜{c1}",
        f"第二章 > 第二节｜{c2}",
    ]


def test_跨节碎条并入下节条款时归属起始节并补换行() -> None:
    c5, c6 = _clause(5, 2, 48), _clause(6, 4)  # 100 字碎条（第一章末） + 148 字（第二章）
    doc = ParsedDoc(
        title="考勤制度",
        blocks=[
            Block(level=1, heading_path=["第一章"], text=c5),
            Block(level=1, heading_path=["第二章"], text=c6),
        ],
    )
    chunks = split_clauses(doc, PARAMS)
    assert len(chunks) == 1
    assert chunks[0].title_path == "第一章"  # 按条款起始位置归属所在节
    assert chunks[0].content == f"第一章｜{c5}\n{c6}"  # 跨块并接补换行：同聚合路径组内口径


def test_无条款导语节按碎条规则并入下节首个条款() -> None:
    doc = ParsedDoc(
        title="考勤制度",
        blocks=[
            Block(level=1, heading_path=["前言"], text="本制度经职工代表大会审议通过。"),
            Block(level=2, heading_path=["第一章", "总则"], text=_clause(1, 4)),
        ],
    )
    chunks = split_clauses(doc, PARAMS)
    assert len(chunks) == 1
    assert chunks[0].title_path == "前言"  # 归属起始节：前导文所在节
    assert chunks[0].content == f"前言｜本制度经职工代表大会审议通过。\n{_clause(1, 4)}"


def test_空白节丢弃_条款从非空节切分() -> None:
    doc = ParsedDoc(
        title="考勤制度",
        blocks=[
            Block(level=1, heading_path=["第一章"], text="  \n　 "),
            Block(level=1, heading_path=["第二章"], text=_clause(1, 4)),
        ],
    )
    assert [chunk.content for chunk in split_clauses(doc, PARAMS)] == [f"第二章｜{_clause(1, 4)}"]


def test_条款块页码取起始节page() -> None:
    c1, c2 = _clause(1, 4), _clause(2, 4)
    doc = ParsedDoc(
        title="扫描制度",
        blocks=[
            Block(level=1, heading_path=["p"], text=c1, page=3),
            Block(level=1, heading_path=["p"], text=c2, page=4),
        ],
    )
    assert [chunk.page for chunk in split_clauses(doc, PARAMS)] == [3, 4]


# ---------- 分派入口与不变式 ----------


def test_split_by_profile_条款档与split_clauses等价() -> None:
    c1, c2 = _clause(1, 4), _clause(2, 4)
    doc = _doc(c1 + c2)
    assert split_by_profile(doc, PARAMS) == split_clauses(doc, PARAMS)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_clause(1, 4) + _clause(2, 4), id="两条各自成块"),
        pytest.param(_clause(1, 20), id="超长句切"),
        pytest.param("为了规范管理。\n" + _clause(1, 4), id="跨块前导文"),
    ],
)
def test_条款块content前缀与title_path成对出现(text: str) -> None:
    """不变式：任意输入下每个 content 都以 `f"{title_path}｜"` 开头且 title_path 非空。"""
    doc = ParsedDoc(
        title="考勤制度",
        blocks=[
            Block(level=1, heading_path=["前言"], text=text),
            Block(level=1, heading_path=["附则"], text=_clause(9, 4)),
        ],
    )
    for chunk in split_clauses(doc, PARAMS):
        assert chunk.title_path != ""
        assert chunk.content.startswith(f"{chunk.title_path}｜")
