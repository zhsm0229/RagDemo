r"""xlsx 整批校验基建单元测试（Task 3 问答对导入 / Task 5 评测题导入共用）。

裁决语义固化：read_rows 只做结构级校验（表头逐字匹配、缺/多/重复列）与取值
（全转 strip 后字符串），必填列只校验"出现在表头"，**值空否由调用方业务校验**；
行号 1 基（表头=1，数据从 2 起），文件级错误约定 row=0；空行跳过不计行数；
超 max_rows 先计数后解析，防大文件内存放大。错误聚合为 XlsxBatchError 一次性
抛出（services 层纯业务，不 import fastapi），API 层统一转 422/1003。
"""

from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook

from app.services.xlsx_io import XlsxBatchError, read_rows, write_template

HEADERS = ["问题", "标准答案", "部门"]


def _xlsx(rows: list[list[object]]) -> bytes:
    """测试内现造 xlsx 字节流：rows[0] 视为表头行，其余为数据行。"""
    wb = Workbook()
    ws = wb.worksheets[0]
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _empty_xlsx() -> bytes:
    """连表头都没有的 0 行工作表（下载了空模板或文件损坏的极端形态）。"""
    wb = Workbook()
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _data_rows(n: int) -> list[list[object]]:
    return [[f"问题{i}", f"答案{i}", "客服"] for i in range(n)]


def _expected_row(i: int) -> dict[str, str]:
    return {"问题": f"问题{i}", "标准答案": f"答案{i}", "部门": "客服"}


# ---------------------------------------------------------------------------
# read_rows：正常读取
# ---------------------------------------------------------------------------


def test_read_rows_正常三行读取_表头乱序按名映射():
    # 文件里表头顺序与传入 headers 顺序不同，必须按列名（strip 后逐字）映射取值；
    # 数据行字段随表头一并换位（[部门, 问题, 答案]），确保 fixture 本身对位正确
    shuffled = [[r[2], r[0], r[1]] for r in _data_rows(3)]
    data = _xlsx([["部门", "问题", "标准答案"], *shuffled])

    rows = read_rows(data, HEADERS, max_rows=50, required={"问题", "标准答案"})

    assert rows == [_expected_row(0), _expected_row(1), _expected_row(2)]


def test_read_rows_单元格取值_strip空白_None转空串_数字转字符串():
    data = _xlsx([HEADERS, ["  带首尾空格  ", 123, None]])

    rows = read_rows(data, HEADERS, max_rows=10, required=set())

    assert rows == [{"问题": "带首尾空格", "标准答案": "123", "部门": ""}]


def test_read_rows_空行跳过不计行数不报错():
    data = _xlsx([HEADERS, _data_rows(1)[0], [], _data_rows(1)[0]])

    # 物理上第 3 行为空行：跳过；max_rows=2 恰好等于有效数据行数，不触上限
    rows = read_rows(data, HEADERS, max_rows=2, required=set())

    assert rows == [_expected_row(0), _expected_row(0)]


# ---------------------------------------------------------------------------
# read_rows：结构级错误（row=1）
# ---------------------------------------------------------------------------


def test_read_rows_缺列_结构错误指名缺失列():
    data = _xlsx([["问题", "部门"], *_data_rows(1)])

    with pytest.raises(XlsxBatchError) as exc_info:
        read_rows(data, HEADERS, max_rows=50, required=set())

    assert exc_info.value.errors == [
        {"row": 1, "field": "标准答案", "reason": "表头缺少列「标准答案」"}
    ]


def test_read_rows_多列_结构错误指名多余列():
    data = _xlsx([[*HEADERS, "备注"], *_data_rows(1)])

    with pytest.raises(XlsxBatchError) as exc_info:
        read_rows(data, HEADERS, max_rows=50, required=set())

    assert exc_info.value.errors == [{"row": 1, "field": "备注", "reason": "表头多余列「备注」"}]


def test_read_rows_重复列_结构错误指名重复列():
    data = _xlsx([["问题", "问题", "标准答案", "部门"], *_data_rows(1)])

    with pytest.raises(XlsxBatchError) as exc_info:
        read_rows(data, HEADERS, max_rows=50, required=set())

    assert exc_info.value.errors == [{"row": 1, "field": "问题", "reason": "表头重复列「问题」"}]


def test_read_rows_必填列缺失于headers入参_结构错误指名必填列():
    # 调用方契约：required ⊆ headers；违背时结构级报"缺少必填列"而非静默放过
    data = _xlsx([["问题"], *_data_rows(1)])

    with pytest.raises(XlsxBatchError) as exc_info:
        read_rows(data, ["问题"], max_rows=50, required={"标准答案"})

    assert exc_info.value.errors == [
        {"row": 1, "field": "标准答案", "reason": "表头缺少必填列「标准答案」"}
    ]


def test_read_rows_表头存在空白单元格_结构错误不再继续匹配列():
    # Excel 清空单元格后维度残留常产生幽灵空列，指名道姓比"多余列「」"可读
    data = _xlsx([["问题", None, "标准答案", "部门"], *_data_rows(1)])

    with pytest.raises(XlsxBatchError) as exc_info:
        read_rows(data, HEADERS, max_rows=50, required=set())

    assert exc_info.value.errors == [{"row": 1, "field": None, "reason": "表头存在空白单元格"}]


# ---------------------------------------------------------------------------
# read_rows：文件级错误（row=0）
# ---------------------------------------------------------------------------


def test_read_rows_超过行上限_文件级错误且边界值恰好不报错():
    data = _xlsx([HEADERS, *_data_rows(51)])

    with pytest.raises(XlsxBatchError) as exc_info:
        read_rows(data, HEADERS, max_rows=50, required=set())

    assert exc_info.value.errors == [{"row": 0, "field": None, "reason": "文件超过 50 行上限"}]
    # 边界：恰好 50 行 = 上限，正常解析
    assert len(read_rows(_xlsx([HEADERS, *_data_rows(50)]), HEADERS, 50, required=set())) == 50


@pytest.mark.parametrize("data", ["header-only", "zero-rows"])
def test_read_rows_无数据行_报文件无数据(data):
    payload = _xlsx([HEADERS]) if data == "header-only" else _empty_xlsx()

    with pytest.raises(XlsxBatchError) as exc_info:
        read_rows(payload, HEADERS, max_rows=50, required=set())

    assert exc_info.value.errors == [{"row": 0, "field": None, "reason": "文件无数据"}]


# ---------------------------------------------------------------------------
# 裁决语义固化：required 只约束表头结构，值空否归调用方
# ---------------------------------------------------------------------------


def test_read_rows_必填列空单元格_原样返回空串不报错_业务校验归调用方():
    data = _xlsx([HEADERS, ["问题0", None, "客服"], ["问题1", "答案1", "客服"]])

    rows = read_rows(data, HEADERS, max_rows=50, required={"问题", "标准答案"})

    assert rows == [
        {"问题": "问题0", "标准答案": "", "部门": "客服"},
        _expected_row(1),
    ]


# ---------------------------------------------------------------------------
# write_template
# ---------------------------------------------------------------------------


def test_write_template_读回表头与示例行等值():
    samples = [
        ["员工如何重置密码？", "登录页点击忘记密码", "客服"],
        ["年假怎么休？", "OA 提前 3 天申请", "HR"],
    ]

    payload = write_template(HEADERS, samples)

    wb = load_workbook(BytesIO(payload), read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        all_rows = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    assert all_rows == [HEADERS, *samples]
