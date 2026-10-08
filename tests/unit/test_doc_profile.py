r"""文档分型纯函数测试（metrics-balance §3.1/§3.2 + §6 测试 seams）：五档判定、优先级
并存、边界（恰好 5 处条款 / 深度恰 2 / 1500 字）、档位常量冻结、两个适配器。

断言基线即 spec 表格：先命中先判（条款 > 表格 > 短文浅层 > 有子标题 > 兜底）；
`<1500 字` 为严格小于；深度 = 标题路径段数（" > " 分隔）。适配器两条口径红线：
ingest 期只取非空节（空白节不落 chunks），回填期剥 `title_path｜` 前缀重组——
两适配器对同一文档必须产出逐字相同的检测输入（否则 <1500 字信号漂移）。
"""

from app.rag.chunker import Block, ParsedDoc, split
from app.rag.doc_profile import (
    PROFILE_PARAMS,
    AggregateParams,
    ClauseParams,
    DocProfile,
    detect_from_text,
)
from app.services.doc_type import chunks_detect_inputs
from app.services.ingest_pipeline import detect_doc_profile


def _clauses(count: int) -> str:
    """count 处条款编号行（含正文，行文风格贴近制度文档），每行约 20 字。"""
    return "\n".join(f"第{i}条 员工应当遵守本条规定并妥善执行。" for i in range(1, count + 1))


def _plain(chars: int) -> str:
    """无标点终止符干扰的普通正文（无条款编号、无表格、无句切边界诉求）。"""
    return "这是一段普通说明文字，用于凑齐指定长度。" * (chars // 20 + 1)


# ---------- 五档判定 ----------


def test_五档判定_全文命中五处条款判制度条款() -> None:
    assert detect_from_text("制度", [], _clauses(5)) is DocProfile.POLICY_CLAUSE


def test_五档判定_markdown分隔行判表格报表() -> None:
    text = "月度对账如下：\n| 项目 | 金额 |\n|---|---|\n| 差旅 | 100 |\n以上。"
    assert detect_from_text("报表", [], text) is DocProfile.TABLE_REPORT


def test_五档判定_连续三行双竖线判表格报表() -> None:
    lines = "| a | b |\n| c | d |\n| e | f |"
    assert detect_from_text("报表", [], lines) is DocProfile.TABLE_REPORT


def test_五档判定_短文且无子标题判通知公告() -> None:
    assert detect_from_text("通知", ["关于放假的通知"], "关于国庆放假安排的通知。" * 10) is (
        DocProfile.NOTICE
    )


def test_五档判定_有子标题层级判操作手册() -> None:
    assert detect_from_text("手册", ["第一章 > 1.1 开机"], _plain(3000)) is (
        DocProfile.OPERATION_MANUAL
    )


def test_五档判定_长文无层级无表格判通用兜底() -> None:
    assert detect_from_text("文章", ["杂谈"], _plain(3000)) is DocProfile.GENERAL


def test_五档判定_全角与汉字数字条款编号同样计数() -> None:
    text = "第一条 甲\n第十二条 乙\n第一百条 丙\n第3条 丁\n第１２条 戊"
    assert detect_from_text("制度", [], text) is DocProfile.POLICY_CLAUSE


# ---------- 优先级并存（先命中先判） ----------


def test_优先级并存_条款与表格同时命中判制度条款() -> None:
    text = _clauses(6) + "\n| a | b |\n| c | d |\n| e | f |"
    assert detect_from_text("制度", ["第一章 > 第一节"], text) is DocProfile.POLICY_CLAUSE


def test_优先级并存_表格与短文浅层同时命中判表格报表() -> None:
    text = "| a | b |\n| c | d |\n| e | f |"
    assert detect_from_text("通知", ["附表"], text) is DocProfile.TABLE_REPORT


def test_优先级并存_深层子标题与短文同判操作手册() -> None:
    # 深度 3 且全文 <1500：信号 3（浅层）不命中，信号 4 命中 → 手册优先于兜底
    assert detect_from_text("手册", ["一 > 1.1 > 1.1.1"], _plain(100)) is (
        DocProfile.OPERATION_MANUAL
    )


# ---------- 边界：恰好 5 处条款 / 深度恰 2 / 1500 字 ----------


def test_优先级边界_恰好五处条款判制度条款四处不判() -> None:
    assert detect_from_text("制度", [], _clauses(5)) is DocProfile.POLICY_CLAUSE
    assert detect_from_text("制度", [], _clauses(4)) is DocProfile.NOTICE  # 4 处落回短文浅层


def test_深度边界_深度恰2且全文超1500判操作手册() -> None:
    assert detect_from_text("手册", ["第一章 > 1.1"], _plain(1600)) is (DocProfile.OPERATION_MANUAL)


def test_深度边界_深度恰2且全文不足1500判通知公告() -> None:
    # 深度 2 同时落在信号 3（≤2）与信号 4（≥2）交集：短文时信号 3 先命中
    assert detect_from_text("手册", ["第一章 > 1.1"], _plain(500)) is DocProfile.NOTICE


def test_长度边界_全文恰1500字且无子标题判通用() -> None:
    text = "字" * 1500
    assert len(text) == 1500  # 边界钉死：严格小于 1500 才判通知
    assert detect_from_text("文章", ["无层级"], text) is DocProfile.GENERAL


def test_长度边界_全文1499字且无子标题判通知公告() -> None:
    assert detect_from_text("文章", ["无层级"], "字" * 1499) is DocProfile.NOTICE


def test_表格信号_连续两行双竖线不构成表格() -> None:
    text = "| a | b |\n| c | d |"
    assert detect_from_text("通知", ["无层级"], text) is DocProfile.NOTICE


# ---------- 档位常量冻结（spec §3.2 档位表，随代码评审冻结） ----------


def test_档位常量_五档参数与spec档位表逐字一致() -> None:
    assert PROFILE_PARAMS[DocProfile.POLICY_CLAUSE] == ClauseParams(
        max_clause_len=600, merge_below=120, top_n=5
    )
    assert PROFILE_PARAMS[DocProfile.OPERATION_MANUAL] == AggregateParams(
        min_len=400, max_len=1000, top_n=6
    )
    assert PROFILE_PARAMS[DocProfile.TABLE_REPORT] == AggregateParams(
        min_len=300, max_len=1500, top_n=5
    )
    assert PROFILE_PARAMS[DocProfile.NOTICE] == AggregateParams(min_len=800, max_len=2000, top_n=3)
    assert PROFILE_PARAMS[DocProfile.GENERAL] == AggregateParams(min_len=300, max_len=800, top_n=5)


def test_档位常量_general档参数与现状切分默认值逐字一致() -> None:
    general = PROFILE_PARAMS[DocProfile.GENERAL]
    assert isinstance(general, AggregateParams)
    assert (general.min_len, general.max_len) == (300, 800)  # chunker.split 既有默认


# ---------- 适配器（spec §6：ParsedDoc 构造 / chunks 回填构造含剥离前缀） ----------


def _parsed_doc() -> ParsedDoc:
    """带空白节与两级标题的样例文档：空白节不落 chunks，适配器必须同步剔除。"""
    return ParsedDoc(
        title="员工手册",
        blocks=[
            Block(level=1, heading_path=["第一章"], text="总则说明。"),
            Block(level=1, heading_path=["第一章"], text="   \n  "),  # 空白节：须被剔除
            Block(level=2, heading_path=["第一章", "1.1"], text="正文" * 200),
        ],
    )


def test_适配器_ingest期ParsedDoc构造_剔除空白节且三参逐字一致() -> None:
    parsed = _parsed_doc()
    assert detect_doc_profile(parsed) == detect_from_text(
        parsed.title,
        ["第一章", "第一章 > 1.1"],
        "总则说明。\n" + "正文" * 200,
    )


def test_适配器_回填期chunks构造_剥离title_path前缀后与ingest口径一致() -> None:
    chunks = split(_parsed_doc())
    rows = [(chunk.title_path, chunk.content) for chunk in chunks]
    title_paths, full_text = chunks_detect_inputs(rows)
    assert title_paths == ["第一章", "第一章 > 1.1"]
    assert full_text == "总则说明。\n" + "正文" * 200
    # 前缀剥离确凿：重组全文不含任何 "｜" 分隔符
    assert "｜" not in full_text


def test_两适配器口径一致_同一文档检测输入逐字相同() -> None:
    parsed = _parsed_doc()
    rows = [(c.title_path, c.content) for c in split(parsed)]
    title_paths, full_text = chunks_detect_inputs(rows)
    assert detect_doc_profile(parsed) == detect_from_text(parsed.title, title_paths, full_text)
